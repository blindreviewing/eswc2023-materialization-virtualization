#!/usr/bin/env python3

import os
import configparser
from container import Container
from rdflib import Graph, BNode, Namespace, Literal, RDF
from timeout_decorator import timeout, TimeoutError

VERSION = '4.6.3.4'
TIMEOUT = 6 * 3600 # 6 hours
R2RML = Namespace('http://www.w3.org/ns/r2rml#')
RML = Namespace('http://semweb.mmlab.be/ns/rml#')
D2RQ = Namespace('http://www.wiwiss.fu-berlin.de/suhl/bizer/D2RQ/0.1#')

class SDMRDFizer(Container):
    def __init__(self, data_path: str, config_path: str, verbose: bool):
        self._data_path = os.path.abspath(data_path)
        self._config_path = os.path.abspath(config_path)
        os.umask(0)
        os.makedirs(os.path.join(self._data_path, 'sdmrdfizer'), exist_ok=True)
        super().__init__(f'blindreviewing/sdm-rdfizer:v{VERSION}', 'SDM-RDFizer',
                         verbose,
                         volumes=[f'{self._data_path}/sdmrdfizer:/data',
                                  f'{self._data_path}/shared:/data/shared'])

    @property
    def root_mount_directory(self) -> str:
        return __name__.lower()

    @timeout(TIMEOUT)
    def _execute_with_timeout(self, arguments) -> bool:
        cmd = f'python3 sdm-rdfizer/rdfizer/run_rdfizer.py ' + \
              f'/data/config_sdmrdfizer.ini'
        return self.run_and_wait_for_exit(cmd)

    def execute(self, arguments: list) -> bool:
        try:
            return self._execute_with_timeout(arguments)
        except TimeoutError:
            msg = f'Timeout ({TIMEOUT}s) reached for SDM-RDFizer'
            print(msg, file=sts.stderr)
            self._log.append(msg)

        return False

    def execute_mapping(self, mapping_file: str, output_file: str,
                        serialization: str, rdb_username: str = None,
                        rdb_password: str = None, rdb_host: str = None,
                        rdb_port: str = None, rdb_name: str = None,
                        rdb_type: str = None) -> bool:

        # Configuration file
        name = os.path.splitext(os.path.basename(output_file))[0]
        config = configparser.ConfigParser(delimiters=':')
        config['default'] = {
            'main_directory': '/data/shared'
        }
        config['datasets'] = {
            'number_of_datasets': 1,
            'output_folder': '/data/shared',
            'all_in_one_file': 'yes',
            'remove_duplicate': 'yes',
            'enrichment': 'yes',
            'name': name,
            'ordered': 'no',
            'large_file': 'false'
        }
        config['dataset1'] = {
            'name': name,
            'mapping': f'/data/shared/{os.path.basename(mapping_file)}'
        }

        if serialization == 'ntriples':
            config['datasets']['output_format'] = 'n-triples'
        elif serialization == 'turtle':
            config['datasets']['output_format'] = 'turtle'
        else:
            raise NotImplementedError('SDM-RDFizer does not support'
                                      f'"serialization" output format')

        if rdb_username is not None and rdb_password is not None \
            and rdb_host is not None and rdb_port is not None \
            and rdb_name is not None and rdb_type is not None:
            config['dataset1']['user'] = rdb_username
            config['dataset1']['password'] = rdb_password
            config['dataset1']['host'] = rdb_host
            config['dataset1']['port'] = rdb_port
            config['dataset1']['db'] = rdb_name
            config['dataset1']['mapping'] = '/data/mapping_converted.rml.ttl'
            if rdb_type == 'MySQL':
                config['datasets']['dbType'] = 'mysql'
                driver = 'jdbc:mysql'
            elif rdb_type == 'PostgreSQL':
                config['datasets']['dbType'] = 'postgres'
                driver = 'jdbc:postgresql'
            else:
                raise NotImplementedError('SDM-RDFizer does not support RDB '
                                          f'"{rdb_type}"')
            dsn = f'{driver}://{rdb_host}:{rdb_port}/{rdb_name}'

            # Compatibility with R2RML mapping files
            # Replace rr:logicalTable with rml:logicalSource + D2RQ description
            # and rr:column with rml:reference
            g = Graph()
            g.bind('rr', R2RML)
            g.bind('rml', RML)
            g.bind('d2rq', D2RQ)
            g.bind('rdf', RDF)
            g.parse(os.path.join(self._data_path, 'shared',
                                 os.path.basename(mapping_file)))

            # rr:logicalTable --> rml:logicalSource
            for triples_map_iri, p, o in g.triples((None, RDF.type,
                                                    R2RML.TriplesMap)):
                logical_source_iri = BNode()
                d2rq_rdb_iri = BNode()
                logical_table_iri = g.value(triples_map_iri, R2RML.logicalTable)
                table_name_literal = g.value(logical_table_iri, R2RML.tableName)
                g.add((d2rq_rdb_iri, D2RQ.jdbcDSN, Literal(dsn)))
                g.add((d2rq_rdb_iri, D2RQ.jdbcDriver, Literal(driver)))
                g.add((d2rq_rdb_iri, D2RQ.username, Literal(rdb_username)))
                g.add((d2rq_rdb_iri, D2RQ.password, Literal(rdb_password)))
                g.add((d2rq_rdb_iri, RDF.type, D2RQ.Database))
                g.add((logical_source_iri, R2RML.sqlVersion, R2RML.SQL2008))
                g.add((logical_source_iri, R2RML.tableName, table_name_literal))
                g.add((logical_source_iri, RML.source, d2rq_rdb_iri))
                g.add((logical_source_iri, RDF.type, RML.LogicalSource))
                g.add((triples_map_iri, RML.logicalSource, logical_source_iri))
                g.remove((triples_map_iri, R2RML.logicalTable,
                          logical_table_iri))
                g.remove((logical_table_iri, R2RML.tableName,
                          table_name_literal))
                g.remove((logical_table_iri, RDF.type, R2RML.LogicalTable))
                g.remove((logical_table_iri, R2RML.sqlVersion, R2RML.SQL2008))

            # rr:column --> rml:reference
            for s, p, o in g.triples((None, R2RML.column, None)):
                 g.add((s, RML.reference, o))
                 g.remove((s, p, o))

            # SDM-RDFizer cannot handle rml:referenceFormulation when using
            # RDBs, remove it for safety
            # https://github.com/SDM-TIB/SDM-RDFizer/issues/71
            for s, p, o in g.triples((None, RML.referenceFormulation, None)):
                g.remove((s, p, o))

            destination = os.path.join(self._data_path, 'sdmrdfizer',
                                       'mapping_converted.rml.ttl')
            g.serialize(destination=destination, format='turtle')

        os.umask(0)
        os.makedirs(os.path.join(self._data_path, 'sdmrdfizer'), exist_ok=True)
        path = os.path.join(self._data_path, 'sdmrdfizer',
                            'config_sdmrdfizer.ini')
        with open(path, 'w') as f:
            config.write(f, space_around_delimiters=False)

        return self.execute([])

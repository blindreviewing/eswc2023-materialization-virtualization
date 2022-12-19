#!/usr/bin/env python3

import os
import sys
import psycopg2
import tempfile
from psycopg2 import sql
from csv import reader
from time import sleep
from container import Container
from typing import List
from timeout_decorator import timeout, TimeoutError

VERSION = '14.5'
HOST = 'localhost'
USER = 'root'
PASSWORD = 'root'
DB = 'db'
PORT = '5432'
WAIT_TIME = 3
CLEAR_TABLES_TIMEOUT = 5 * 60 # 5 minutes

class PostgreSQL(Container):
    def __init__(self, data_path: str, config_path: str, verbose: bool):
        self._data_path = os.path.abspath(data_path)
        self._config_path = os.path.abspath(config_path)
        tmp_dir = os.path.join(tempfile.gettempdir(), 'postgresql')
        os.umask(0)
        os.makedirs(tmp_dir, exist_ok=True)
        os.makedirs(os.path.join(self._data_path, 'postgresql'), exist_ok=True)
        self._tables = []

        super().__init__(f'blindreviewing/postgresql:v{VERSION}', 'PostgreSQL',
                         verbose,
                         ports={PORT: PORT},
                         environment={'POSTGRES_PASSWORD': PASSWORD,
                                      'POSTGRES_USER': USER,
                                      'POSTGRES_DB': DB,
                                      'PGPASSWORD': PASSWORD,
                                      'POSTGRES_HOST_AUTH_METHOD': 'trust'},
                         volumes=[f'{self._data_path}/shared:/data/shared',
                                  f'{tmp_dir}:/var/lib/postgresql/data'])

    def initialization(self) -> bool:
        # PostgreSQL should start with a initialized database, start PostgreSQL
        # if not initialized to avoid the pre-run start during benchmark
        # execution
        success = self.wait_until_ready()
        if not success:
            return False
        success = self.stop()

        return success

    @property
    def root_mount_directory(self) -> str:
        return __name__.lower()

    def wait_until_ready(self, command: str = '') -> bool:
        success = self.run_and_wait_for_log(f'port {PORT}', command=command)
        sleep(WAIT_TIME)

        return success

    def load(self, csv_file: str, table: str) -> bool:
        return self._load_csv(csv_file, table, True)

    def load_sql_schema(self, schema_file: str, csv_files: List[str]) -> bool:
        success = True

        # Load SQL schema
        success, output = self.exec(f'psql -h {HOST} -p {PORT} -U {USER} '
                                    f'-d {DB} -f /data/shared/{schema_file}')
        # Load CSVs
        if success:
            for csv_file, table in csv_files:
                success = self._load_csv(csv_file, table, False)
                if not success:
                    break

        return success

    def load_multiple(self, csv_files: List[dict]) -> bool:
        for entry in csv_files:
            if not self._load_csv(entry['file'], entry['table'], True):
                return False
        return True

    def _load_csv(self, csv_file: str, table: str, create: bool):
        success = True
        columns = None
        table = table.lower()
        path = os.path.join(self._data_path, 'shared', csv_file)

        self._tables.append(table)

        # Analyze and move CSV for loading
        if not os.path.exists(path):
            print(f'CSV file "{path}" does not exist', file=sys.stderr)
            return False

        with open(path, 'r') as f:
            csv_reader = reader(f)
            columns = next(csv_reader)
            columns = [x.lower() for x in columns]

        # Load CSV
        connection = psycopg2.connect(host=HOST, database=DB,
                                      user=PASSWORD, password=PASSWORD)
        try:
            cursor = connection.cursor()

            if create:
                cursor.execute(f'DROP TABLE IF EXISTS {table};')
                c = ' VARCHAR , '.join(columns) + ' VARCHAR'
                cursor.execute(f'CREATE TABLE {table} (KEY SERIAL, {c}, '
                               'PRIMARY KEY(KEY))')

            c = ','.join(columns)
            cursor.execute(f'COPY {table} ({c}) FROM '
                           f'\'/data/shared/{csv_file}\' '
                           'DELIMITER \',\' NULL \'NULL\' CSV HEADER;')
            cursor.execute('COMMIT;')

            if self._verbose:
                header = '| ID | ' + ' | '.join(columns) + ' |'
                print(header)
                print('-' * len(header))

            cursor.execute(f'SELECT * FROM {table};')
            number_of_records = 0
            for record in cursor:
                number_of_records += 1
                if self._verbose:
                    print(record)
            if number_of_records == 0:
                success = False
        except Exception as e:
            print(f'Failed to load CSV: "{e}"', file=sys.stderr)
            success = False
        finally:
            connection.close()

        return success

    @timeout(CLEAR_TABLES_TIMEOUT)
    def _clear_tables(self):
        connection = psycopg2.connect(host=HOST, database=DB,
                                      user=PASSWORD, password=PASSWORD)
        cursor = connection.cursor()
        for table in self._tables:
            cursor.execute(f'DROP TABLE IF EXISTS {table};')
            cursor.execute(f'COMMIT;')
        self._tables = []
        connection.close()

    def stop(self) -> bool:
        try:
            self._clear_tables()
        except TimeoutError:
            print('Clearing PostgreSQL tables timed out after '
                  f'{CLEAR_TABLES_TIMEOUT}s!', file=sys.stderr)

        return super().stop()

if __name__ == '__main__':
    print('ℹ️  Starting up...')
    p = PostgreSQL('data', 'config', True)
    p.wait_until_ready()
    input('ℹ️  Press any key to stop')
    p.stop()
    print('ℹ️  Stopped')

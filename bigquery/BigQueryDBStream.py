import copy
import datetime
import os
import re

import dbstream
import time
import google.cloud.bigquery
import pandas as pd
from google.cloud import bigquery
from google.cloud.bigquery import QueryJobConfig, LoadJobConfig, SchemaField
from google.cloud.bigquery.dbapi import Cursor
from googleauthentication import GoogleAuthentication

from bigquery.core.Column import change_columns_type, columns_type_bool_to_str, change_column_value_to_string, \
    change_column_value_to_int, change_column_value_to_float, find_sample_value, detect_type
from bigquery.core.tools.print_colors import C
from bigquery.core.Table import create_table, create_columns
import logging


class BigQueryDBStream(dbstream.DBStream):
    def __init__(self, instance_name, client_id, google_auth: GoogleAuthentication):
        super().__init__(instance_name, client_id=client_id)
        self.instance_type_prefix = "BIGQ"
        self.google_auth = google_auth
        self.ssh_init_port = 6543

    def connection(self):
        try:
            con = google.cloud.bigquery.client.Client(
                project=os.environ["BIG_QUERY_PROJECT_ID"],
                credentials=self.google_auth.credentials()
            )
        except google.cloud.bigquery.dbapi.OperationalError:
            time.sleep(2)
            if self.ssh_tunnel:
                self.ssh_tunnel.close()
                self.create_tunnel()
            con = google.cloud.bigquery.client.Client(
                project=os.environ["BIG_QUERY_PROJECT_ID"],
                credentials=self.google_auth.credentials()
            )
        return con

    def _execute_query_custom(self, query):
        client = self.connection()
        con = google.cloud.bigquery.dbapi.connect(client=client)
        cursor = Cursor(con)
        try:
            cursor.execute(query)
        except Exception as e:
            cursor.close()
            con.close()
            raise e
        con.commit()
        try:
            result = cursor.fetchall()
        except:
            result = None
        cursor.close()
        con.close()
        query_create_table = re.search("(?i)(?<=((create table ))).*(?= as)", query)
        if result:
            return [dict(r) for r in result]
        elif query_create_table:
            return {'execute_query': query_create_table}
        else:
            empty_list = []
            return empty_list

    def _send(self, data, replace, batch_size=1000):
        print(C.WARNING + "Initiate send_to_bigquery on table " + data["table_name"] + "..." + C.ENDC)

        if replace:
            print(C.WARNING + "Table will be cleaned: " + data["table_name"] + C.ENDC)

        total_rows = len(data["rows"])

        # Construct a BigQuery client object.
        client = self.connection()
        columns_name = data["columns_name"]

        df = pd.DataFrame(data["rows"], columns=columns_name)
        file_path = "./tmp.csv"
        df.to_csv(file_path, index=False)

        params = {}
        df = df.where((pd.notnull(df)), None)
        for i in range(len(columns_name)):
            name = columns_name[i]
            example_max, example_min = find_sample_value(df, name, i)
            col = dict()
            col["example"] = example_max
            type_max = detect_type(self, name=name, example=example_max)
            if type_max == "TIMESTAMP":
                type_min = detect_type(self, name=name, example=example_min)
                if type_min == type_max:
                    col["type"] = type_max
                else:
                    col["type"] = type_min
            else:
                col["type"] = type_max
            params[name] = col

        schema = [
            SchemaField(name=c, field_type=params[c]["type"]) for c in params.keys()
        ]
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=1,
            schema=schema,
            autodetect=True,
            write_disposition="WRITE_TRUNCATE" if replace else "WRITE_APPEND"
        )
        table_id = os.environ["BIG_QUERY_PROJECT_ID"] + "." + data["table_name"]

        with open(file_path, "rb") as source_file:
            job = client.load_table_from_file(source_file, table_id, job_config=job_config)

        job.result()  # Waits for the job to complete.

        table = client.get_table(table_id)  # Make an API request.
        print(
            "Loaded {} rows and {} columns to {}".format(
                table.num_rows, len(table.schema), table_id
            )
        )

        print(C.HEADER + str(total_rows) + ' rows sent to BigQuery table ' + data["table_name"] + C.ENDC)
        print(C.OKGREEN + "[OK] Sent to bigquery" + C.ENDC)
        return 0

    def _send_data_custom(self,
                          data,
                          replace=True,
                          batch_size=1000,
                          other_table_to_update=None,
                          n=1
                          ):
        """
        data = {
            "table_name" 	: 'name_of_the_redshift_schema' + '.' + 'name_of_the_redshift_table' #Must already exist,
            "columns_name" 	: [first_column_name,second_column_name,...,last_column_name],
            "rows"		: [[first_raw_value,second_raw_value,...,last_raw_value],...]
        }
        """
        data_copy = copy.deepcopy(data)
        try:
            self._send(data, replace=replace, batch_size=batch_size)
        except Exception as e:
            error_lowercase = str(e).lower()
            logging.info(error_lowercase.split("\n")[0])
            if ("value has type float64 which cannot be inserted into" in error_lowercase
                or "value has type int64 which cannot be inserted into" in error_lowercase
                or "value has type bool which cannot be inserted into" in error_lowercase) \
                    and "string" in error_lowercase:
                column = str(e).split("column ")[1].split(",")[0]
                change_column_value_to_string(
                    data=data_copy,
                    column=column,
                )
            elif (
                    "value has type float64 which cannot be inserted into" in error_lowercase and not "string" in error_lowercase) \
                    or (
                    "value has type string which cannot be inserted into" in error_lowercase and not "bool" in error_lowercase):
                column = str(e).split("column ")[1].split(",")[0]
                if n == 2:
                    change_columns_type(
                        self,
                        data=data_copy,
                        other_table_to_update=other_table_to_update
                    )
                    n = 1
                else:
                    if "which has type int" in error_lowercase:
                        change_column_value_to_int(
                            data=data_copy,
                            column=column,
                        )
                    elif "which has type float" in error_lowercase:
                        change_column_value_to_float(
                            data=data_copy,
                            column=column,
                        )
                    n = 2

            elif "value has type string which cannot be inserted into" in error_lowercase and "bool" in error_lowercase:
                columns_type_bool_to_str(
                    self,
                    data=data_copy,
                    other_table_to_update=other_table_to_update
                )
            elif " was not found " in error_lowercase and (
                    " table " in error_lowercase or " dataset " in error_lowercase):
                print("Destination table doesn't exist! Will be created")
                create_table(
                    self,
                    data=data_copy,
                    other_table_to_update=other_table_to_update
                )
                replace = False
            elif " is not present in table " in error_lowercase and "column" in error_lowercase:
                create_columns(
                    self,
                    data=data_copy,
                    other_table_to_update=other_table_to_update
                )
            else:
                raise e

            self._send_data_custom(data_copy, replace=replace, batch_size=batch_size,
                                   other_table_to_update=other_table_to_update, n=n)

    def clean(self, selecting_id, schema_prefix, table):
        print('trying to clean table %s.%s using %s' % (schema_prefix, table, selecting_id))
        cleaning_query = """
                DELETE FROM %(schema_name)s.%(table_name)s WHERE %(id)s IN (SELECT distinct %(id)s FROM %(schema_name)s.%(table_name)s_temp);
                INSERT INTO %(schema_name)s.%(table_name)s 
                SELECT * FROM %(schema_name)s.%(table_name)s_temp;
                DELETE FROM %(schema_name)s.%(table_name)s_temp WHERE 1=1;
                """ % {"table_name": table,
                       "schema_name": schema_prefix,
                       "id": selecting_id}
        self.execute_query(cleaning_query)
        print('cleaned')

    def get_max(self, schema, table, field, filter_clause=""):
        try:
            print("SELECT max(%s) as max FROM %s.%s %s" % (field, schema, table, filter_clause))
            r = self.execute_query("SELECT max(%s) as max FROM %s.%s %s" % (field, schema, table, filter_clause))
            return r[0]["max"]
        except Exception as e:
            if "was not found" in str(e):
                return None
            raise e

    def get_data_type(self, table_name, schema_name):
        query = """ SELECT column_name, data_type FROM %s.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='%s' """ \
                % (schema_name, table_name)
        return self.execute_query(query=query)

    def create_view_from_columns(self, view_name, columns, schema_name, table_name):
        view_query = '''DROP VIEW IF EXISTS %s.%s ;CREATE VIEW %s.%s as (SELECT %s FROM %s.%s)''' \
                     % (schema_name, view_name, schema_name, view_name, columns, schema_name, table_name)
        self.execute_query(view_query)

    def create_schema(self, schema_name):
        con = self.connection()
        dataset = google.cloud.bigquery.Dataset(con.project + "." + schema_name)
        con.create_dataset(dataset)

    def drop_schema(self, schema_name):
        con = self.connection()
        con.delete_dataset(dataset=con.project + "." + schema_name, delete_contents=True, not_found_ok=True)

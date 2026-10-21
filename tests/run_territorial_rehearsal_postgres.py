"""Run compiled SQL against disposable local PostgreSQL, never a cloud DSN."""
from __future__ import annotations
import os
from pathlib import Path
import psycopg
from scripts.compile_neon_territorial_rehearsal import compile_rehearsal


def main():
    # No arbitrary DSN input: the CI service is local and contains synthetic data.
    database = 'render_rehearsal_test'
    connection = psycopg.connect(host='127.0.0.1', port=5432, dbname=database,
        user='postgres', password=os.environ['REHEARSAL_TEST_PASSWORD'],
        connect_timeout=5, autocommit=True)
    with connection:
        connection.execute("SET neon.project_id = 'project-test'")
        connection.execute("SET neon.branch_id = 'br-child'")
        connection.execute('CREATE TABLE public.tenant_profile (id integer PRIMARY KEY)')
        connection.execute('CREATE TABLE public."user" (id integer PRIMARY KEY)')
        connection.execute('CREATE TABLE public.alembic_version (version_num varchar(128) PRIMARY KEY)')
        connection.execute("INSERT INTO public.alembic_version VALUES ('20260829_global_writer_authority_v1')")
        connection.execute('INSERT INTO public.tenant_profile VALUES (1)')
        connection.execute('INSERT INTO public."user" VALUES (1)')
        report = compile_rehearsal(Path(__file__).resolve().parents[1],
            project_id='project-test', branch_id='br-child',
            source_branch_id='br-parent', database_name=database)
        results = []
        with connection.transaction():
            for sql in report['sql_statements']:
                cursor = connection.execute(sql)
                if cursor.description:
                    fields = [column.name for column in cursor.description]
                    results.extend(dict(zip(fields, row)) for row in cursor.fetchall())
        final = next(row for row in results if row.get('contract_version') == report['contract_version'])
        assert final['rollback_verified'] is True
        assert final['schema_committed'] is False and final['cutover_authorized'] is False
        assert final['tables_verified'] == 3 and final['rows_verified'] == 3
        assert final['restored_revision'] == '20260829_global_writer_authority_v1'
        assert connection.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0] == 3
        assert connection.execute("SELECT to_regclass('public.territorial_geocoding_job')").fetchone()[0] is None
        print('PostgreSQL rehearsal passed: three revisions executed; DDL and version rolled back; synthetic baseline preserved.')

if __name__ == '__main__':
    main()

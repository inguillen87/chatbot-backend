"""Create, test and remove a new synthetic loopback PostgreSQL18/TLS cluster.

Requires locally installed PostgreSQL18 binaries and openssl. It accepts no DSN,
customer database, existing data directory, provider credential or production
environment. DATABASE_URL and dotenv are disabled before application imports.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import unittest


FIXTURE_DATABASE = 'chatboc_addresses_concurrency_ci'


def command(arguments, *, timeout=20):
    completed = subprocess.run([str(value) for value in arguments], check=False,
        capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError('synthetic_cluster_command_failed:' + Path(arguments[0]).name)
    return completed.stdout


@contextmanager
def fresh_cluster(binary_directory, scratch_parent):
    import psycopg
    from sqlalchemy.engine import URL
    binary_directory = Path(binary_directory).resolve(strict=True)
    if not command([binary_directory / 'postgres', '--version']).startswith('postgres (PostgreSQL) 18.'):
        raise RuntimeError('postgres18_binaries_required')
    directory = Path(tempfile.mkdtemp(prefix='chatboc-address-concurrency-', dir=scratch_parent))
    os.chmod(directory, 0o700)
    running = False
    try:
        password = secrets.token_urlsafe(32)
        password_file = directory / 'initdb-password'
        password_file.write_text(password, encoding='ascii')
        password_file.chmod(0o600)
        command([binary_directory / 'initdb', '-D', directory / 'data', '--username=postgres',
            '--auth-local=scram-sha-256', '--auth-host=scram-sha-256', '--pwfile', password_file,
            '--encoding=UTF8', '--locale=C'])
        password_file.unlink()
        command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost',
            '-keyout', directory / 'server.key', '-out', directory / 'server.crt'])
        (directory / 'server.key').chmod(0o600)
        with socket.socket() as free_port:
            free_port.bind(('127.0.0.1', 0))
            port = free_port.getsockname()[1]
        with (directory / 'data' / 'postgresql.conf').open('a') as configuration:
            configuration.write("\nlisten_addresses='127.0.0.1'\n"
                + f"port={port}\nunix_socket_directories=''\nssl=on\n"
                + "ssl_cert_file='../server.crt'\nssl_key_file='../server.key'\nmax_connections=12\n")
        command([binary_directory / 'pg_ctl', '-D', directory / 'data', '-l', directory / 'postgres.log',
            '-w', '-t', '10', 'start'])
        running = True
        parameters = dict(host='127.0.0.1', port=port, user='postgres', password=password,
            sslmode='verify-full', sslrootcert=str(directory / 'server.crt'), connect_timeout=3)
        with psycopg.connect(**parameters, dbname='postgres', autocommit=True) as connection:
            databases = connection.execute("SELECT datname FROM pg_database WHERE NOT datistemplate").fetchall()
            if databases != [('postgres',)]:
                raise RuntimeError('new_cluster_identity_mismatch')
            connection.execute('CREATE DATABASE ' + FIXTURE_DATABASE)
        with psycopg.connect(**parameters, dbname=FIXTURE_DATABASE) as connection:
            identity = connection.execute('SELECT current_database(), current_user').fetchone()
            ssl = connection.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()[0]
            existing = connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public'").fetchone()[0]
            if identity != (FIXTURE_DATABASE, 'postgres') or ssl is not True or existing:
                raise RuntimeError('fresh_tls_fixture_required')
        yield URL.create('postgresql+psycopg', username='postgres', password=password,
            host='127.0.0.1', port=port, database=FIXTURE_DATABASE,
            query={'sslmode': 'verify-full', 'sslrootcert': str(directory / 'server.crt')})
    finally:
        if running:
            command([binary_directory / 'pg_ctl', '-D', directory / 'data', '-w', '-t', '10', '-m', 'fast', 'stop'])
        shutil.rmtree(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin', required=True)
    parser.add_argument('--scratch-parent', type=Path, default=Path(tempfile.gettempdir()))
    arguments = parser.parse_args(argv)
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    from tests import test_organization_address_concurrency as cases
    with fresh_cluster(arguments.postgres_bin, arguments.scratch_parent) as url:
        cases.DATABASE_URL = url
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(cases.OrganizationAddressConcurrencyTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary = {'successful': result.wasSuccessful() and result.testsRun == 4 and not result.skipped,
            'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'postgres': '18', 'transport': 'loopback_tls_verify_full',
            'actual_models_and_services': True, 'external_network_calls': 0,
            'cases': cases.OrganizationAddressConcurrencyTests.evidence}
    summary['synthetic_cluster_stopped_and_removed'] = True
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'successful': False, 'error_type': type(error).__name__,
            'reason_code': 'disposable_address_fixture_failed'}, sort_keys=True))
        raise SystemExit(1)

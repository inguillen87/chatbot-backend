"""Reuse the existing fixed PostgreSQL18 GitHub service; accept no caller DSN.

This CI runner uses the job's disposable loopback container (without TLS), creates
only its own empty fixture database, and removes it after each suite. Local TLS
acceptance remains in the separate fresh_cluster/verify-full runners.
"""
import json
import os
import unittest


def main():
    if not (os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('CI') == 'true'
            and os.environ.get('RUNNER_OS') == 'Linux'):
        raise RuntimeError('github_actions_fixture_required')
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import URL
    from tests import test_organization_address_concurrency as concurrency
    from tests import test_organization_address_stale_config as stale

    database = concurrency.FIXTURE_DATABASE
    assert database == stale.FIXTURE_DATABASE == 'chatboc_addresses_concurrency_ci'
    parameters = dict(host='127.0.0.1', port=5432, user='postgres', dbname='vaultcredregression',
        sslmode='disable', connect_timeout=3)
    summaries = []
    with psycopg.connect(**parameters, autocommit=True) as control:
        identity = control.execute('SELECT current_database(), current_user, current_setting(\'server_version_num\')::int').fetchone()
        if identity[:2] != ('vaultcredregression', 'postgres') or not 180000 <= identity[2] < 190000:
            raise RuntimeError('fixed_ci_service_identity_required')
        for module, case, count in ((concurrency, concurrency.OrganizationAddressConcurrencyTests, 4),
                (stale, stale.OrganizationAddressStaleConfigTests, 3)):
            # Never erase a pre-existing database, even in CI. The runner owns
            # only the exact empty database it successfully creates below.
            if control.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,)).fetchone():
                raise RuntimeError('new_fixture_database_required')
            control.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
            try:
                with psycopg.connect(**{**parameters, 'dbname': database}) as empty:
                    if empty.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='public'").fetchone()[0]:
                        raise RuntimeError('empty_fixture_database_required')
                module.CI_SERVICE_MODE = True
                module.DATABASE_URL = URL.create('postgresql+psycopg', username='postgres',
                    host='127.0.0.1', port=5432, database=database, query={'sslmode': 'disable'})
                case.evidence = []
                result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
                summaries.append({'suite': case.__name__, 'tests': result.testsRun,
                    'successful': result.wasSuccessful() and result.testsRun == count and not result.skipped,
                    'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
                    'cases': case.evidence})
            finally:
                module.DATABASE_URL = None
                module.CI_SERVICE_MODE = False
                control.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))
    summary = {'successful': len(summaries) == 2 and all(row['successful'] for row in summaries),
        'postgres': '18', 'transport': 'github_actions_loopback_service_without_tls',
        'owned_fixture_databases_removed': True, 'external_network_calls': 0, 'suites': summaries}
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'successful': False, 'error_type': type(error).__name__,
            'reason_code': 'address_ci_fixture_failed'}, sort_keys=True))
        raise SystemExit(1)

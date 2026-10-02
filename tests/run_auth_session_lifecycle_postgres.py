"""Run lifecycle tests only against an explicitly selected empty loopback fixture.

Never reads DATABASE_URL or starts, migrates, or drops a customer database.
The GitHub mode matches the isolated PostgreSQL service in auth-private-storage.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import unittest


FIXTURE_DATABASE = 'chatboc_auth_lifecycle_ci'
FIXTURE_USER = 'postgres'
LOCAL_PASSWORD_ENV = 'CHATBOC_AUTH_LIFECYCLE_TEST_PASSWORD'
REQUIRED_TESTS = frozenset({
    'test_actual_native_login_with_default_password_hash_and_postgres_retirement',
    'test_different_request_ids_serialize_one_retirement_transition',
    'test_logout_lock_blocks_refresh_child_and_is_visible_to_another_worker',
    'test_provider_event_lock_prevents_exchange_resurrection_after_unlinked_tombstone',
    'test_real_migration_and_exact_schema_postcheck_on_empty_public_schema',
    'test_refresh_committed_before_logout_child_is_revoked_too',
    'test_request_id_collision_across_families_rolls_back_losing_retirement',
    'test_simultaneous_replay_has_one_audit_and_one_receipt',
})


class FixtureRefused(RuntimeError):
    """Stable public reason code; never include connection credentials."""


def fixture_configuration(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--github-disposable-service', action='store_true')
    mode.add_argument('--fresh-loopback-fixture', action='store_true')
    parser.add_argument('--port', type=int, default=5432)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        raise FixtureRefused('fixture_port_invalid')
    if arguments.github_disposable_service:
        if environ.get('GITHUB_ACTIONS') != 'true' or environ.get('CI') != 'true':
            raise FixtureRefused('github_disposable_context_required')
        if arguments.port != 5432:
            raise FixtureRefused('github_disposable_port_mismatch')
        # This credential belongs only to the fresh local CI service, never a provider.
        password = 'local_only_test'
    else:
        password = environ.get(LOCAL_PASSWORD_ENV)
        if not isinstance(password, str) or not password:
            raise FixtureRefused('explicit_disposable_password_required')
    return {'host':'127.0.0.1', 'port':arguments.port,
            'dbname':FIXTURE_DATABASE, 'user':FIXTURE_USER, 'password':password}


def verify_empty_fixture(connection):
    """Reject a reused database or cluster before importing any application code."""
    connection.execute("SET LOCAL statement_timeout = '1500ms'")
    database, actor = connection.execute('SELECT current_database(), current_user').fetchone()
    if (database, actor) != (FIXTURE_DATABASE, FIXTURE_USER):
        raise FixtureRefused('fixture_identity_mismatch')
    version = int(connection.execute('SHOW server_version_num').fetchone()[0])
    if not 180000 <= version < 190000:
        raise FixtureRefused('fixture_postgres18_required')
    if connection.execute("SELECT count(*) FROM pg_catalog.pg_database WHERE NOT datistemplate "
            "AND datname NOT IN ('postgres', %s)", (FIXTURE_DATABASE,)).fetchone()[0]:
        raise FixtureRefused('fixture_cluster_not_dedicated')
    if connection.execute("SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname NOT LIKE 'pg_%%' "
            "AND rolname <> %s", (FIXTURE_USER,)).fetchone()[0]:
        raise FixtureRefused('fixture_roles_not_dedicated')
    if connection.execute("SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg_%' "
            "AND nspname NOT IN ('public', 'information_schema')").fetchone()[0]:
        raise FixtureRefused('fixture_application_schema_not_empty')
    if connection.execute("SELECT count(*) FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace "
            "WHERE namespace.nspname='public'").fetchone()[0]:
        raise FixtureRefused('fixture_public_schema_not_empty')


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_subtests = 0

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is None:
            self.passed_subtests += 1


def result_summary(result, expected_count):
    successful = (result.wasSuccessful() and not result.skipped
        and result.testsRun == expected_count and expected_count >= len(REQUIRED_TESTS)
        and result.passed_subtests >= 4)
    return {'tests_run':result.testsRun, 'expected_tests':expected_count,
        'passed_schema_subtests':result.passed_subtests, 'failures':len(result.failures),
        'errors':len(result.errors), 'skipped':len(result.skipped), 'successful':successful,
        'target':'explicit_fresh_loopback_postgres18', 'remote_database_access':False}


def main(argv=None):
    configuration = fixture_configuration(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    os.environ['CORS_ALLOWED_ORIGINS'] = 'https://panel.example.invalid'
    import psycopg
    with psycopg.connect(**configuration, connect_timeout=3) as connection:
        verify_empty_fixture(connection)
    from sqlalchemy.engine import URL
    from tests import auth_session_lifecycle_postgres as cases
    from tests import http_writer_authority_lease_postgres as http_cases
    present = {name for name in unittest.defaultTestLoader.getTestCaseNames(cases.AuthSessionLifecyclePostgresTests)}
    if not REQUIRED_TESTS <= present:
        raise FixtureRefused('required_lifecycle_tests_missing')
    cases.DATABASE_URL = URL.create('postgresql+psycopg', username=configuration['user'],
        password=configuration['password'], host=configuration['host'],
        port=configuration['port'], database=configuration['dbname'])
    http_cases.DATABASE_URL = cases.DATABASE_URL
    expected_http = {'test_provider_pending_past_old_idle_timeout_blocks_cas_until_request_finishes',
        'test_stream_close_callback_provider_effect_finishes_before_cas',
        'test_stream_error_rolls_back_and_releases_before_fence_completes',
        'test_handler_exception_rolls_back_control_transaction_without_lock_leak',
        'test_never_consumed_wsgi_iterable_blocks_until_explicit_close',
        'test_nested_route_lease_reuses_connection_while_cas_is_queued',
        'test_eight_concurrent_provider_requests_hold_cas_until_all_finish',
        'test_socket_background_provider_finishes_before_cas_and_queued_welcome_is_denied',
        'test_expired_sql_cookie_opens_before_http_and_socket_context_without_any_dml',
        'test_read_only_get_sql_session_commit_owns_lease_and_blocks_cas'}
    present_http = set(unittest.defaultTestLoader.getTestCaseNames(http_cases.HttpWriterAuthorityLeasePostgresTests))
    if not expected_http <= present_http:
        raise FixtureRefused('required_http_authority_tests_missing')
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(cases),
                               unittest.defaultTestLoader.loadTestsFromModule(http_cases)))
    expected_count = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    summary = result_summary(result, expected_count)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except FixtureRefused as error:
        print(json.dumps({'successful':False, 'reason_code':str(error)}, sort_keys=True))
        raise SystemExit(2)
    except Exception as error:
        print(json.dumps({'successful':False, 'reason_code':'disposable_fixture_failed',
                          'error_type':type(error).__name__}, sort_keys=True))
        raise SystemExit(1)

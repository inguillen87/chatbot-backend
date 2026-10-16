from types import SimpleNamespace

import pytest

from tests.run_auth_session_lifecycle_postgres import (
    FIXTURE_DATABASE, FixtureRefused, fixture_configuration,
    result_summary, verify_empty_fixture,
)


def test_loopback_configuration_never_accepts_ambient_customer_dsn():
    configuration = fixture_configuration(['--fresh-loopback-fixture', '--port', '15432'],
        {'DATABASE_URL':'postgresql://ignored.invalid/customer',
         'CHATBOC_AUTH_LIFECYCLE_TEST_PASSWORD':'disposable_fixture_only'})
    assert configuration['host'] == '127.0.0.1'
    assert configuration['port'] == 15432
    assert configuration['dbname'] == FIXTURE_DATABASE
    assert configuration['user'] == 'postgres'


def test_github_mode_matches_fixed_disposable_service_and_ignores_ambient_dsn():
    configuration = fixture_configuration(['--github-disposable-service'],
        {'GITHUB_ACTIONS':'true', 'CI':'true', 'DATABASE_URL':'postgresql://ignored.invalid/customer'})
    assert configuration == {'host':'127.0.0.1', 'port':5432, 'dbname':FIXTURE_DATABASE,
                             'user':'postgres', 'password':'local_only_test'}


@pytest.mark.parametrize('arguments', [[], ['--fresh-loopback-fixture', '--host', 'remote.invalid'],
    ['--fresh-loopback-fixture', '--dsn', 'postgresql://ignored.invalid/customer']])
def test_fixture_requires_explicit_mode_and_has_no_host_or_dsn_argument(arguments):
    with pytest.raises(SystemExit):
        fixture_configuration(arguments, {})


@pytest.mark.parametrize('arguments,environment,reason', [
    (['--fresh-loopback-fixture'], {}, 'explicit_disposable_password_required'),
    (['--fresh-loopback-fixture', '--port', '0'], {}, 'fixture_port_invalid'),
    (['--github-disposable-service'], {}, 'github_disposable_context_required'),
    (['--github-disposable-service', '--port', '15432'], {'GITHUB_ACTIONS':'true', 'CI':'true'},
        'github_disposable_port_mismatch'),
])
def test_fixture_configuration_refuses_unsafe_inputs(arguments, environment, reason):
    with pytest.raises(FixtureRefused, match=reason):
        fixture_configuration(arguments, environment)


class FixtureConnection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        if statement.startswith('SET LOCAL'):
            return None
        return SimpleNamespace(fetchone=lambda:next(self.rows))


@pytest.mark.parametrize('rows,reason', [
    ([('other_database', 'postgres')], 'fixture_identity_mismatch'),
    ([(FIXTURE_DATABASE, 'postgres'), ('170006',)], 'fixture_postgres18_required'),
    ([(FIXTURE_DATABASE, 'postgres'), ('180006',), (1,)], 'fixture_cluster_not_dedicated'),
    ([(FIXTURE_DATABASE, 'postgres'), ('180006',), (0,), (1,)], 'fixture_roles_not_dedicated'),
    ([(FIXTURE_DATABASE, 'postgres'), ('180006',), (0,), (0,), (1,)], 'fixture_application_schema_not_empty'),
    ([(FIXTURE_DATABASE, 'postgres'), ('180006',), (0,), (0,), (0,), (1,)], 'fixture_public_schema_not_empty'),
])
def test_preflight_refuses_reused_fixture_before_application_tables_can_be_created(rows, reason):
    connection = FixtureConnection(rows)
    with pytest.raises(FixtureRefused, match=reason):
        verify_empty_fixture(connection)
    assert not any(statement.startswith(('CREATE', 'DROP', 'ALTER')) for statement in connection.statements)


def test_skipped_or_missing_schema_probes_cannot_be_reported_successful():
    result = SimpleNamespace(wasSuccessful=lambda:True, testsRun=8, skipped=[],
        passed_subtests=4, failures=[], errors=[])
    assert result_summary(result, 8)['successful']
    result.skipped = [('case', 'fixture omitted')]
    assert not result_summary(result, 8)['successful']
    result.skipped = []; result.passed_subtests = 3
    assert not result_summary(result, 8)['successful']

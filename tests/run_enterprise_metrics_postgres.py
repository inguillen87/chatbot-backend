"""Native pytest/JUnit gate for the existing dedicated GitHub PostgreSQL 18.

No ambient DSN, customer credentials, service startup, migration, or global DROP.
The application is loaded only after the existing disposable-fixture preflight.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
import xml.etree.ElementTree as ET

from tests.run_auth_session_lifecycle_postgres import (
    FIXTURE_DATABASE,
    FixtureRefused,
    fixture_configuration,
    verify_empty_fixture,
)


OPT_IN = "CHATBOC_ENTERPRISE_METRICS_TEST_POSTGRES"
TEST_PATH = "tests/integration/test_enterprise_metrics_postgres.py"
JUNIT_RELATIVE = ".qa-artifacts/auth-session-postgres/enterprise-metrics.xml"
REQUIRED_TESTS = frozenset({
    "test_postgres_orm_api_counts_distinct_and_null_identity",
    "test_postgres_currency_normalization_and_mixed_unknown_disclosure",
    "test_postgres_missing_nonfinite_and_unsafe_amounts_are_unavailable",
    "test_postgres_exact_profile_quarantines_shared_owner_and_legacy_rows",
    "test_postgres_unique_owner_keeps_only_safe_unbound_legacy_orders",
    "test_postgres_registered_grant_and_selector_conflict_boundaries",
    "test_postgres_empty_scope_has_observed_zero_but_no_average",
    "test_postgres_real_select_failure_never_becomes_zero",
})
REQUIRED_PROPERTIES = {
    "enterprise_metrics.database_dialect": "postgresql",
    "enterprise_metrics.fixture_database": FIXTURE_DATABASE,
    "enterprise_metrics.server_major": "18",
    "enterprise_metrics.owned_schema": "true",
    "enterprise_metrics.business_candidate": "v5",
    "enterprise_metrics.fixture_cleanup_verified": "true",
}


BUSINESS_SOURCE_PATHS = (
    "app.py", "routes/metricas.py", "routes/estadisticas.py",
    "services/metricas_service.py", "tests/test_metricas.py",
    "tests/test_estadisticas_dashboard.py", "tests/test_enterprise_metrics_contract.py",
)


def business_source_hashes():
    repository = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
            for name in BUSINESS_SOURCE_PATHS}


def metrics_fixture_configuration(argv=None, environ=None):
    """Accept only the exact existing GitHub service mode, never a local DSN."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    if arguments != ["--github-disposable-service"]:
        raise FixtureRefused("metrics_github_disposable_mode_required")
    configuration = fixture_configuration(arguments, environment)
    if environment.get(OPT_IN) != "1":
        raise FixtureRefused("metrics_postgres_opt_in_required")
    return configuration


def junit_summary(path, *, cleanup_verified):
    """A green job requires every named native PostgreSQL case, without skips."""
    if not Path(path).is_file():
        raise FixtureRefused("metrics_native_junit_missing")
    document = ET.parse(path).getroot()
    cases = document.findall(".//testcase")
    names = [case.get("name") for case in cases]
    properties = {item.get("name"): item.get("value")
                  for item in document.findall(".//properties/property")}
    failures = len(document.findall(".//failure"))
    errors = len(document.findall(".//error"))
    skipped = len(document.findall(".//skipped"))
    complete = (len(cases) == len(REQUIRED_TESTS)
                and len(names) == len(set(names)) and set(names) == REQUIRED_TESTS)
    verified_properties = all(properties.get(key) == value
                              for key, value in REQUIRED_PROPERTIES.items())
    try:
        observed_version = int(properties.get("enterprise_metrics.server_version_num", ""))
        observed_source_hashes = json.loads(properties.get("enterprise_metrics.business_source_hashes", "null"))
    except (ValueError, TypeError):
        observed_version, observed_source_hashes = 0, None
    observed_postgres18 = (180000 <= observed_version < 190000
                           and properties.get("enterprise_metrics.server_major") == str(observed_version // 10000))
    source_hashes_verified = observed_source_hashes == business_source_hashes()
    verified_properties = verified_properties and observed_postgres18 and source_hashes_verified
    return {
        "server_version_num_observed": observed_version,
        "business_candidate": "v5",
        "business_source_hashes_verified": source_hashes_verified,
        "tests_run": len(cases), "expected_tests": len(REQUIRED_TESTS),
        "failures": failures, "errors": errors, "skipped": skipped,
        "required_cases_verified": complete,
        "postgres_properties_verified": verified_properties,
        "fixture_cleanup_verified": bool(cleanup_verified),
        "successful": bool(complete and verified_properties and cleanup_verified
                           and not failures and not errors and not skipped),
        "target": "explicit_fresh_loopback_postgres18",
        "remote_database_access": False,
    }


def main(argv=None):
    configuration = metrics_fixture_configuration(argv)
    repository = Path(__file__).resolve().parents[1]
    junit = repository / JUNIT_RELATIVE
    if junit.exists():
        raise FixtureRefused("metrics_native_junit_already_exists")

    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    # This flag permits collection only. Connection authority is the validated
    # runtime-only fixture plugin below, plus the actual PostgreSQL preflight.
    os.environ[OPT_IN] = "1"
    os.environ["CORS_ALLOWED_ORIGINS"] = "https://panel.example.invalid"
    import psycopg
    with psycopg.connect(**configuration, connect_timeout=3) as connection:
        verify_empty_fixture(connection)

    import pytest

    class OwnedFixturePlugin:
        @pytest.fixture(scope="session")
        def enterprise_metrics_configuration(self):
            return MappingProxyType(dict(configuration))

    junit.parent.mkdir(parents=True, exist_ok=True)
    exit_code = pytest.main([
        "--noconftest", "-q", "-p", "no:cacheprovider", "--tb=short",
        str(repository / TEST_PATH), "--junitxml=" + str(junit),
    ], plugins=[OwnedFixturePlugin()])
    # A module teardown failure or leaked schema cannot yield accepted proof.
    with psycopg.connect(**configuration, connect_timeout=3) as connection:
        verify_empty_fixture(connection)
    summary = junit_summary(junit, cleanup_verified=True)
    summary["pytest_exit_code"] = int(exit_code)
    summary["successful"] = summary["successful"] and exit_code == 0
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["successful"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FixtureRefused as error:
        print(json.dumps({"successful": False, "reason_code": str(error)}, sort_keys=True))
        raise SystemExit(2)
    except Exception as error:
        print(json.dumps({"successful": False,
                          "reason_code": "disposable_metrics_fixture_failed",
                          "error_type": type(error).__name__}, sort_keys=True))
        raise SystemExit(1)

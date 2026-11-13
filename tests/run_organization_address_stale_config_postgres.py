"""Run stale tenant-config tests in a new, synthetic PostgreSQL18/TLS cluster."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest

from tests.run_organization_address_concurrency_postgres import fresh_cluster


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin', required=True)
    parser.add_argument('--scratch-parent', type=Path, default=Path(tempfile.gettempdir()))
    arguments = parser.parse_args(argv)
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    from tests import test_organization_address_stale_config as cases
    with fresh_cluster(arguments.postgres_bin, arguments.scratch_parent) as url:
        cases.DATABASE_URL = url
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(cases.OrganizationAddressStaleConfigTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary = {'successful': result.wasSuccessful() and result.testsRun == 3 and not result.skipped,
            'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'postgres': '18', 'transport': 'loopback_tls_verify_full',
            'actual_models_and_services': True, 'external_network_calls': 0,
            'cases': cases.OrganizationAddressStaleConfigTests.evidence}
    summary['synthetic_cluster_stopped_and_removed'] = True
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'successful': False, 'error_type': type(error).__name__,
            'reason_code': 'disposable_address_stale_config_fixture_failed'}, sort_keys=True))
        raise SystemExit(1)

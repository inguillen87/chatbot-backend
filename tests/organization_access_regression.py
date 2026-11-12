"""Run authentication/provisioning regressions without developer credentials."""
from tests.profile_acceptance_runtime import prepare_process


if __name__ == '__main__':
    prepare_process()
    import pytest
    raise SystemExit(pytest.main([
        '-q', 'tests/test_admin_tenant_verification.py',
        'tests/test_organization_setup_journey.py',
        'tests/test_organization_workspace.py',
        'tests/test_channel_activation_contract.py',
    ]))

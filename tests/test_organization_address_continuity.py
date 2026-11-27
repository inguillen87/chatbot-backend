"""Address activation: explicit blocking reasons and visible deadlines.

Real Clerk JWT lineage and HTTP boundary (shared harness); provider observations are
synthetic. No provider, domain, deployment or customer database is reached.
"""
from hashlib import sha256
import unicodedata

import pytest

from models import TenantProfile, db
from services import organization_addresses as addresses
from services.organization_address_vercel import (
    AddressVercelAdapter, ProvisioningError, VERIFICATION_TTL, canonical,
)
from tests.test_meta_credential_operations import environment  # noqa: F401 (fixture)
from tests.test_organization_address_runtime import (  # noqa: F401 (fixture)
    HOST, SLUG, TENANT_ID, approved, http, no_activation, post,
)
from tests.test_organization_address_vercel import NOW, Provider, plan, probe


def _raise(reason):
    def hook(_plan):
        raise ProvisioningError(reason)
    return hook


def test_provider_without_route_table_fails_closed_with_its_own_reason():
    for missing in ('null', 'absent'):
        expected = plan()
        provider = Provider(expected)
        if missing == 'null':
            provider.deployment['routes'] = None
        else:
            provider.deployment.pop('routes')
        witness_calls = []
        adapter = AddressVercelAdapter(provider, https_probe=lambda *a: witness_calls.append(a), clock=lambda: NOW)
        with pytest.raises(ProvisioningError, match='^provider_routes_unavailable$'):
            adapter.observe(expected)
        # No digest, however it was computed, substitutes for the missing provider evidence.
        for supplied in (sha256(canonical(None)).hexdigest(), sha256(b'{"rewrites":[]}').hexdigest()):
            with pytest.raises(ProvisioningError, match='^provider_routes_unavailable$'):
                AddressVercelAdapter(provider, https_probe=probe(expected), clock=lambda: NOW).observe(
                    plan(routes_sha256=supplied))
        assert not witness_calls
        assert all(method == 'GET' for method, _, _ in provider.calls)
    provider = Provider(plan())
    provider.deployment['routes'] = [{'src': '/(.*)', 'dest': '/index.html'}]
    with pytest.raises(ProvisioningError, match='^reviewed_route_setup_required$'):
        AddressVercelAdapter(provider, https_probe=probe(plan()), clock=lambda: NOW).observe(plan())


@pytest.mark.parametrize('action', ['activation-plan', 'activate'])
def test_blocked_observation_reports_only_an_allowlisted_reason(http, action):
    value = None if action == 'activation-plan' else {**approved(http)}
    http.observe_hook = _raise('provider_routes_unavailable')
    blocked = post(http, action, value)
    assert blocked.status_code == 409, blocked.json
    assert blocked.json == {'contract_version': 'organization.addresses_error.v2',
        'reason_code': 'address_provider_verification_failed', 'retryable': False,
        'writes_performed': False, 'blocking_reason': 'provider_routes_unavailable'}
    http.observe_hook = _raise('synthetic-internal-detail-never-shown')
    hidden = post(http, action, value)
    assert hidden.status_code == 409
    assert 'blocking_reason' not in hidden.json
    assert 'synthetic-internal-detail' not in hidden.get_data(as_text=True)
    assert http.lease_enters == 0
    no_activation(http)


def test_descriptor_shows_request_deadline_and_operator_guidance(http):
    result = http.client.get(f'/api/admin/tenants/{SLUG}/addresses',
        headers={'Authorization': 'Bearer ' + http.bearer})
    assert result.status_code == 200, result.json
    payload = result.json
    deadline = http.now + addresses.REQUEST_TTL
    assert payload['platform']['status'] == 'pending_platform'
    assert payload['platform']['pending_until'] == deadline
    assert payload['custom']['pending_until'] is None
    guidance = payload['operator_guidance']
    assert set(guidance) == {'contract_version', 'labels', 'messages', 'blocking'}
    assert guidance['contract_version'] == 'organization.address_operator_guidance.v1'
    assert set(guidance['blocking']) == addresses.BLOCKING_REASONS
    assert 'provider_routes_unavailable' in guidance['blocking']
    texts = list(guidance['labels'].values()) + list(guidance['messages'].values())
    texts += [part for entry in guidance['blocking'].values() for part in (entry['message'], entry['action'])]
    for entry in guidance['blocking'].values():
        assert set(entry) == {'message', 'action'}
    for text in texts:
        assert isinstance(text, str) and 1 <= len(text) <= 1000
        assert not any(unicodedata.category(char).startswith('C') for char in text)
        for private in (SLUG, HOST, 'dpl_', 'Bearer ', 'vercel.json'):
            assert private not in text
    # The pinned operator_ui.v1 contract is untouched.
    assert set(payload['operator_ui']['messages']) == {
        'not_ready', 'descriptor_error', 'plan_error', 'activation_error', 'uncertain',
        'expired', 'input_invalid', 'descriptor_changed', 'platform_admin_required',
        'operator_runtime_unavailable', 'maintenance', 'not_pending', 'success'}
    with http.app.app_context():
        lapsed = addresses.build_addresses(db.session.get(TenantProfile, TENANT_ID), can_manage=True,
            config=http.app.config, now=deadline)
    assert lapsed['platform']['status'] == 'verification_expired'
    assert lapsed['platform']['pending_until'] == deadline
    assert lapsed['capabilities']['platform']['reason_code'] == 'address_revoke_required'


def test_current_policy_caps_active_platform_validity_at_the_request_deadline(http):
    """Diagnosis, not a goal: pins today's policy so changing it is a conscious decision.

    A platform activation is valid until min(request deadline, observation + 7 days), and
    the request deadline is 24 hours after the request. There is no renewal operation.
    """
    deadline = http.now + addresses.REQUEST_TTL
    # The real observer bounds its result by the plan deadline.
    bounded = plan(valid_until=NOW + addresses.REQUEST_TTL)
    observed = AddressVercelAdapter(Provider(bounded), https_probe=probe(bounded), clock=lambda: NOW).observe(bounded)
    assert observed.valid_until == bounded.valid_until < NOW + VERIFICATION_TTL
    # Activate 30 minutes after the request (the session token lasts one hour) with the
    # bound the real observer would return.
    http.clock['now'] = http.now + 1800
    http.observation_change = {'valid_until': min(deadline, http.clock['now'] + VERIFICATION_TTL)}
    activated = post(http, 'activate', approved(http))
    assert activated.status_code == 200, activated.json
    db.session.remove()
    tenant = db.session.get(TenantProfile, TENANT_ID)
    row = addresses.read_store(tenant)['platform']
    assert row['status'] == 'active' and row['platform_valid_until'] == row['request_expires_at'] == deadline
    resolve = lambda moment: addresses.resolve_host(db.session, TenantProfile, HOST,
        legacy_entitlement=lambda _tenant: True, config=http.app.config, now=moment)
    assert resolve(deadline - 1) is not None
    assert resolve(deadline) is None
    lapsed = addresses.build_addresses(tenant, can_manage=True, config=http.app.config, now=deadline)
    assert lapsed['platform']['status'] == 'verification_expired'
    assert lapsed['capabilities']['platform']['reason_code'] == 'address_revoke_required'
    with pytest.raises(addresses.DomainBindingError) as refused:
        addresses.prepare_plan(tenant, kind='platform', expected_revision=addresses.revision(tenant),
            frontend_deployment='dpl_1234567890', frontend_source='a' * 40, routes_sha256='c' * 64,
            backend_source='b' * 40, config=http.app.config, now=deadline - 60)
    assert refused.value.code == 'address_platform_not_ready'
    db.session.remove()


def test_missing_route_table_accepts_only_the_declared_functional_proof():
    from services.organization_domain_vercel import functional_route_evidence
    base = plan()
    declared = plan(routes_sha256=functional_route_evidence(base.frontend_deployment))
    provider = Provider(declared)
    provider.deployment['routes'] = None
    observation = AddressVercelAdapter(provider, https_probe=probe(declared), clock=lambda: NOW).observe(declared)
    assert observation.host == declared.host
    # The declaration alone activates nothing: the nonce must come back through the host.
    with pytest.raises(ProvisioningError):
        AddressVercelAdapter(provider, https_probe=_raise('public_https_unavailable'), clock=lambda: NOW).observe(declared)
    # It is bound to one deployment and never replaces a route table the provider does report.
    other = plan(routes_sha256=functional_route_evidence('dpl_otra'))
    with pytest.raises(ProvisioningError, match='^provider_routes_unavailable$'):
        AddressVercelAdapter(provider, https_probe=probe(other), clock=lambda: NOW).observe(other)
    reported = Provider(declared)
    with pytest.raises(ProvisioningError, match='^reviewed_route_setup_required$'):
        AddressVercelAdapter(reported, https_probe=probe(declared), clock=lambda: NOW).observe(declared)

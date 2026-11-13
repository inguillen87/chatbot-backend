"""Real JWT/Clerk lineage, HTTP boundary and SQL rollback for address activation.

Only the provider observation and global writer lease are synthetic. Run in the
isolated prepare_process() runtime; no external request or deployment occurs.
PostgreSQL lock exclusion is exercised separately by the dedicated PG runner.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import time
from types import SimpleNamespace

import jwt
import pytest
from sqlalchemy import event

from extensions import limiter
from models import AuditEvent, AuthProviderSession, AuthSession, TenantProfile, User, db
from routes import organization_addresses as route
from services import organization_address_runtime as runtime
from services import organization_addresses as addresses
from services.auth_session_lifecycle import issue_token, revoke_provider_session
from services.organization_address_vercel import AddressObservation, plan_digest
from services.organization_domain_vercel import canonical
from utils.auth_helpers import auth_session_version
from utils.tenant_admin_access import can_manage_tenant_control_plane
from tests.test_meta_credential_operations import environment


SLUG = 'tierra-del-fuego'
TENANT_ID = 46
HOST = 'synthetic-runtime.chatboc.ar'
SID = 'synthetic-address-operator-clerk-session'
ROUTES_HASH = sha256(canonical([{'src': '/api/(.*)', 'dest': 'https://api.chatboc.ar/api/$1'}])).hexdigest()
SYNTHETIC_TOKEN = 'synthetic-local-vercel-token-1234567890'


@pytest.fixture
def http(environment, monkeypatch):
    app = environment.app
    app.config.update(SECRET_KEY='synthetic-address-jwt-secret-only',
        AUTH_TOKEN_COOKIE_NAME='auth_token', RATELIMIT_STORAGE_URI='memory://', RATELIMIT_ENABLED=True,
        ORGANIZATION_ADDRESS_ACTIVATION_ENABLED=True,
        ORGANIZATION_ADDRESS_VERCEL_TOKEN=SYNTHETIC_TOKEN,
        ORGANIZATION_ADDRESS_VERCEL_TEAM_ID=runtime.TEAM,
        ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON='{}', BACKEND_VERSION='b' * 40)
    monkeypatch.setenv('CLERK_SUPERADMIN_EMAILS', 'operator@example.invalid')
    app.register_blueprint(route.organization_addresses_bp)
    limiter.init_app(app)
    limiter.reset()
    now = int(time.time())
    tenant = db.session.get(TenantProfile, TENANT_ID)
    tenant.plan, tenant.configuracion = 'full', {'keep': True}
    db.session.commit()
    initial = addresses.save_addresses(db.session, TenantProfile, User, AuditEvent,
        tenant_id=TENANT_ID, actor_id=1,
        data={'kind': 'platform', 'operation': 'request', 'host': HOST,
            'expected_revision': addresses.revision(tenant)},
        authorize=can_manage_tenant_control_plane, config=app.config, now=now)
    actor = db.session.get(User, 1)
    bearer = issue_token({'user_id': 1, 'session_kind': 'clerk', 'auth_provider': 'clerk',
        'clerk_sid': SID, 'sv': auth_session_version(actor), 'exp': now + 3600,
        'audience': 'tenant_owner', 'auth_intent': 'tenant_owner'})
    claims = jwt.decode(bearer, app.config['SECRET_KEY'], algorithms=['HS256'])
    assert db.session.get(AuthSession, claims['asid']).provider_session_id == SID
    assert AuthProviderSession.query.filter_by(provider='clerk', provider_session_id=SID).one().revoked_at is None
    db.session.remove()
    state = SimpleNamespace(app=app, client=app.test_client(), bearer=bearer, now=now,
        clock={'now': now}, observations=[], observe_hook=None, observation_change={},
        revision=initial['addresses']['revision'], active_transactions=set(), order=[],
        lease_active=False, lease_enters=0, lease_checks=0, lease_hook=None,
        staged_audit=False, commits=[], lineage_id=claims['asid'], token_expiry=now + 3600)

    def transaction_begin(connection):
        state.active_transactions.add(id(connection))

    def transaction_end(connection):
        state.active_transactions.discard(id(connection))

    def commit(connection):
        state.commits.append(state.lease_active)
        transaction_end(connection)

    def after_sql(_connection, _cursor, statement, *_):
        if statement.upper().startswith('INSERT INTO AUDIT_EVENT'):
            state.staged_audit = True

    listeners = [('begin', transaction_begin), ('commit', commit),
        ('rollback', transaction_end), ('after_cursor_execute', after_sql)]
    engine = db.engine
    for name, callback in listeners:
        event.listen(engine, name, callback)

    original_validate = runtime.admin_session._validate_actor
    def validate_actor(session, actor, *, lock=False, now=None):
        state.order.append('actor_lock' if lock else 'actor_read')
        return original_validate(session, actor, lock=lock, now=now)
    monkeypatch.setattr(runtime.admin_session, '_validate_actor', validate_actor)
    original_lock = addresses.legacy.lock_address_writers
    def namespace_lock(session):
        state.order.append('namespace_lock')
        return original_lock(session)
    monkeypatch.setattr(addresses.legacy, 'lock_address_writers', namespace_lock)

    class Observer:
        def __init__(self, transport, *, clock=None):
            # Keep the real credential/keyword validation in VercelTransport.
            assert type(transport) is runtime.VercelTransport

        def observe(self, plan):
            assert not state.active_transactions, 'Provider I/O has an open application transaction'
            assert not state.lease_active, 'Operation lease was acquired before provider observation'
            state.order.append('observe')
            state.observations.append(plan)
            observation = AddressObservation(plan_digest(plan), plan.host, plan.kind,
                plan.project_id, state.clock['now'], min(plan.valid_until, state.clock['now'] + 600))
            if state.observe_hook:
                state.observe_hook(plan)
            assert not state.active_transactions, 'Provider callback left an open application transaction'
            return replace(observation, **state.observation_change)
    monkeypatch.setattr(runtime, 'AddressVercelAdapter', Observer)

    @contextmanager
    def lease(_config):
        state.lease_active = True
        state.lease_enters += 1
        state.order.append('lease_enter')
        def revalidate(_config):
            state.lease_checks += 1
            allowed = state.lease_hook(state.lease_checks) if state.lease_hook else True
            return SimpleNamespace(allowed=allowed, enabled=True, epoch=6)
        try:
            yield SimpleNamespace(revalidate=revalidate)
        finally:
            state.lease_active = False
            state.order.append('lease_exit')
    monkeypatch.setattr(runtime, 'global_writer_authority_lease', lease)
    original_run = runtime.run_operation
    def run(action, slug, body, actor, config):
        return original_run(action, slug, body, actor, config, clock=lambda: state.clock['now'])
    monkeypatch.setattr(runtime, 'run_operation', run)
    try:
        yield state
    finally:
        for name, callback in listeners:
            event.remove(engine, name, callback)


def body(http):
    return {'kind': 'platform', 'expected_revision': http.revision,
        'frontend_deployment': 'dpl_1234567890', 'frontend_source': 'a' * 40, 'routes_sha256': ROUTES_HASH}


def post(http, action, value=None, *, token=None):
    return http.client.post(f'/api/admin/tenants/{SLUG}/addresses/' + action,
        json=body(http) if value is None else value,
        headers={'Authorization': 'Bearer ' + (http.bearer if token is None else token)})


def approved(http):
    result = post(http, 'activation-plan')
    assert result.status_code == 200, result.json
    return {**body(http), 'approved_plan_sha256': result.json['plan_sha256']}


def no_activation(http, *, expected_audits=1, expected_status='pending_platform'):
    db.session.remove()
    tenant = db.session.get(TenantProfile, TENANT_ID)
    row = addresses.read_store(tenant)['platform']
    assert row['status'] == expected_status
    assert AuditEvent.query.filter_by(event_type='organization.address.activate').count() == 0
    assert AuditEvent.query.count() == expected_audits
    db.session.remove()
    assert not http.lease_active


def another_token(http, *, actor_id=1, provider='clerk', audience='tenant_owner',
        sid='synthetic-other-clerk-session', expires_at=None):
    actor = db.session.get(User, actor_id)
    token = issue_token({'user_id': actor_id, 'session_kind': provider, 'auth_provider': provider,
        'clerk_sid': sid, 'sv': auth_session_version(actor), 'exp': expires_at or http.now + 3600,
        'audience': audience, 'auth_intent': audience})
    db.session.remove()
    return token


def test_absent_runtime_is_explicit_and_cannot_observe_or_activate(http):
    http.app.config.pop('ORGANIZATION_ADDRESS_VERCEL_TOKEN')
    assert runtime.runtime_available(http.app.config) is False
    result = http.client.get(f'/api/admin/tenants/{SLUG}/addresses',
        headers={'Authorization': 'Bearer ' + http.bearer})
    assert result.status_code == 200, result.json
    assert result.json['capabilities']['activation'] == {
        'mode': 'platform_operator', 'available': False, 'can_plan': False, 'can_activate': False,
        'reason_code': 'operator_runtime_unavailable', 'provider_changes_supported': False}
    denied = post(http, 'activation-plan')
    assert denied.status_code == 503 and denied.json['reason_code'] == 'address_operator_runtime_unavailable'
    denied = post(http, 'activate', {**body(http), 'approved_plan_sha256': 'a' * 64})
    assert denied.status_code == 503 and denied.json['reason_code'] == 'address_operator_runtime_unavailable'
    assert not http.observations and http.lease_enters == 0
    no_activation(http)


def test_runtime_availability_requires_exact_server_configuration(http, monkeypatch):
    baseline = dict(http.app.config)
    assert runtime.runtime_available(baseline) is True
    for change in [
        {'ORGANIZATION_ADDRESS_ACTIVATION_ENABLED': False},
        {'ORGANIZATION_ADDRESS_ACTIVATION_ENABLED': 'true'},
        {'ORGANIZATION_ADDRESS_VERCEL_TOKEN': ''},
        {'ORGANIZATION_ADDRESS_VERCEL_TOKEN': 'x' * 15},
        {'ORGANIZATION_ADDRESS_VERCEL_TOKEN': 'x' * 4097},
        {'ORGANIZATION_ADDRESS_VERCEL_TOKEN': SYNTHETIC_TOKEN + '\n'},
        {'ORGANIZATION_ADDRESS_VERCEL_TEAM_ID': 'team_foreign'},
        {'BACKEND_VERSION': 'main'}, {'BACKEND_VERSION': 'b' * 39},
    ]:
        assert runtime.runtime_available({**baseline, **change}) is False
    monkeypatch.delenv('VERCEL', raising=False)
    monkeypatch.delenv('VERCEL_ENV', raising=False)
    assert runtime.runtime_available({**baseline, 'TESTING': False}) is False
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('VERCEL_ENV', 'preview')
    assert runtime.runtime_available({**baseline, 'TESTING': False}) is False
    monkeypatch.setenv('VERCEL_ENV', 'production')
    assert runtime.runtime_available({**baseline, 'TESTING': False}) is True


@pytest.mark.parametrize('credential', ['tenant_admin', 'native', 'portal', 'revoked'])
def test_real_credentials_without_current_clerk_platform_authority_are_rejected(http, credential):
    if credential == 'tenant_admin':
        token, status = another_token(http, actor_id=2), 403
    elif credential == 'native':
        token, status = another_token(http, provider='native'), 401
    elif credential == 'portal':
        token, status = another_token(http, audience='tenant_portal'), 401
    else:
        revoke_provider_session(SID, reason='synthetic_before_request', commit=True)
        db.session.remove()
        token, status = http.bearer, 401
    value = {**body(http), 'approved_plan_sha256': 'a' * 64}
    result = post(http, 'activate', value, token=token)
    assert result.status_code == status, result.json
    assert not http.observations and http.lease_enters == 0
    no_activation(http)


def test_plan_has_no_write_or_provider_transaction_and_activation_uses_lock_order(http):
    value = approved(http)
    assert len(http.observations) == 1 and http.lease_enters == 0
    assert 'namespace_lock' not in http.order and not http.commits
    assert not http.active_transactions
    http.order.clear()
    result = post(http, 'activate', value)
    assert result.status_code == 200 and result.json['committed'] is True, result.json
    assert result.json['writes_performed'] is True and result.json['provider_changes_performed'] is False
    assert len(http.observations) == 2 and http.lease_enters == 1 and http.lease_checks == 2
    assert http.commits == [True], 'Activation must commit only under its writer lease'
    assert http.order.index('observe') < http.order.index('lease_enter')
    assert http.order.index('namespace_lock') < http.order.index('actor_lock')
    assert http.order.count('actor_lock') == 1
    assert not http.active_transactions and not http.lease_active
    db.session.remove()
    assert addresses.read_store(db.session.get(TenantProfile, TENANT_ID))['platform']['status'] == 'active'
    assert AuditEvent.query.filter_by(event_type='organization.address.activate').count() == 1
    assert 'no-store' in result.headers['Cache-Control']
    assert SYNTHETIC_TOKEN not in result.get_data(as_text=True)


@pytest.mark.parametrize('action', ['activation-plan', 'activate'])
def test_durable_clerk_revocation_during_observation_prevents_save(http, action):
    value = approved(http) if action == 'activate' else body(http)
    def revoke(_plan):
        assert revoke_provider_session(SID, reason='synthetic_during_observation', commit=True) is True
        db.session.remove()
    http.observe_hook = revoke
    result = post(http, action, value)
    assert result.status_code == 401 and result.json['reason_code'] == 'address_session_revoked', result.json
    no_activation(http)
    assert AuthProviderSession.query.filter_by(provider_session_id=SID).one().revoked_at is not None
    assert db.session.get(AuthSession, http.lineage_id).revoked_at is not None


@pytest.mark.parametrize('action', ['activation-plan', 'activate'])
def test_real_binding_cas_change_during_observation_prevents_save(http, action):
    value = approved(http) if action == 'activate' else body(http)
    def revoke_address(_plan):
        tenant = db.session.get(TenantProfile, TENANT_ID)
        result = addresses.save_addresses(db.session, TenantProfile, User, AuditEvent,
            tenant_id=TENANT_ID, actor_id=1, data={'kind': 'platform', 'operation': 'revoke',
                'expected_revision': addresses.revision(tenant)}, authorize=can_manage_tenant_control_plane,
            config=http.app.config, now=http.clock['now'])
        assert result['saved'] is True
        db.session.remove()
    http.observe_hook = revoke_address
    result = post(http, action, value)
    assert result.status_code == 412 and result.json['reason_code'] == 'address_revision_conflict', result.json
    no_activation(http, expected_audits=2, expected_status='revoked')


def test_final_writer_lease_revalidation_rolls_back_staged_binding_and_audit(http):
    value = approved(http)
    def revalidate(number):
        if number == 2:
            assert http.staged_audit, 'Failure must occur after the actual database flush'
            return False
        return True
    http.lease_hook = revalidate
    result = post(http, 'activate', value)
    assert result.status_code == 503 and result.json['reason_code'] == 'address_writer_authority_denied', result.json
    assert http.lease_checks == 2 and not http.commits
    no_activation(http)


@pytest.mark.parametrize('expiry', ['age', 'proof', 'actor'])
def test_final_freshness_and_actor_expiry_roll_back_staged_writes(http, expiry):
    if expiry == 'actor':
        # The proof remains fresh and unexpired when the actor alone expires.
        http.token_expiry = http.now + 30
        http.bearer = another_token(http, sid='synthetic-final-session-expiry', expires_at=http.token_expiry)
        http.commits.clear()  # The issuer legitimately committed the new lineage.
    value = approved(http)
    if expiry == 'proof':
        http.observation_change = {'valid_until': http.now + 10}
    def advance(number):
        if number == 2:
            assert http.staged_audit
            http.clock['now'] = {'age': http.now + 61, 'proof': http.now + 10,
                'actor': http.token_expiry}[expiry]
        return True
    http.lease_hook = advance
    result = post(http, 'activate', value)
    assert result.status_code == 409 and result.json['reason_code'] == 'address_platform_not_verified', result.json
    assert not http.commits and http.lease_checks == 2
    no_activation(http)


def test_wrong_approved_digest_is_rejected_before_provider_or_write_lease(http):
    value = {**body(http), 'approved_plan_sha256': 'a' * 64}
    result = post(http, 'activate', value)
    assert result.status_code == 412 and result.json['reason_code'] == 'address_plan_conflict', result.json
    assert not http.observations and http.lease_enters == 0
    no_activation(http)


@pytest.mark.parametrize('extra', [{'observation': {}}, {'access_token': SYNTHETIC_TOKEN},
    {'actor_id': 1}, {'host': HOST}, {'tenant_id': TENANT_ID}, {'clock': 1}, {'allowed_aliases': []}])
def test_http_cannot_supply_trust_proofs_credentials_scope_or_clock(http, extra):
    result = post(http, 'activation-plan', {**body(http), **extra})
    assert result.status_code == 400 and result.json['reason_code'] == 'address_request_invalid', result.json
    assert not http.observations and http.lease_enters == 0
    no_activation(http)

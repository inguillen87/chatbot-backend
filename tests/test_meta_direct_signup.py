"""Organization signup stages in a disposable database with an in-memory provider."""
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
from types import SimpleNamespace

import pytest

from models import AuditEvent, MetaDirectSignupSession as Attempt, ProviderConnection, ProviderSender, TenantProfile, User, db
from services import meta_direct_signup as signup
from services import meta_whatsapp_credentials as vault
from services.meta_direct_signup_graph import DirectSignupProof
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from tests.test_meta_credential_operations import NOW, environment


CODE = "synthetic-once-authorization-code"
TOKEN = "synthetic-sealed-business-token"
SDK = "synthetic-sdk-session"
WABA = "810000000000101"
PHONE_ID = "910000000000101"
PHONE = "+15550000101"


class Provider:
    app_id = "710000000000101"

    def __init__(self, *, token=TOKEN, expires_at=NOW + 30 * 86400, phone=PHONE):
        self.token = token
        self.expires_at = expires_at
        self.phone = phone
        self.exchanges = self.subscriptions = self.rechecks = 0
        self.reject = None
        self.pending = False
        self.registered = True
        self.after_exchange = None

    def proof(self):
        credential = vault.MetaCredential(1, self.expires_at, self.token)
        return DirectSignupProof(credential, self.phone, self.registered)

    def exchange_and_verify(self, code, waba, phone_id):
        self.exchanges += 1
        logging.getLogger("direct-fixture").debug("Synthetic provider received %s", code)
        if self.reject:
            raise MetaRuntimeError(self.reject)
        if self.after_exchange:
            self.after_exchange()
        return self.proof()

    def subscribe_and_verify(self, waba, credential):
        assert (credential.access_token, credential.expires_at) == (self.token, self.expires_at)
        self.subscriptions += 1
        logging.getLogger("direct-fixture").debug("Synthetic provider received %s", credential.access_token)
        if self.pending:
            raise MetaRuntimeError("meta_app_subscription_unverified")

    def verify_credential(self, waba, phone_id, credential):
        self.rechecks += 1
        assert (waba, phone_id) == (WABA, PHONE_ID)
        assert (credential.access_token, credential.expires_at) == (self.token, self.expires_at)
        return DirectSignupProof(credential, self.phone, self.registered)


@pytest.fixture
def flow(environment, monkeypatch):
    config = environment.app.config
    config.update({signup.FLAG: True, "SECRET_KEY": "local-signup-test-signing-key-with-32-bytes"})
    owner = User(name="Synthetic direct owner", email="synthetic-direct@example.invalid",
                 rol="admin", password_hash="synthetic-not-a-login")
    db.session.add(owner)
    db.session.flush()
    org = TenantProfile(slug="signup-local", nombre="Local org", tipo="pyme", pyme_id=owner.id)
    db.session.add(org)
    db.session.commit()
    tenant_id, actor_id = org.id, owner.id
    f = SimpleNamespace(config=config, graph=Provider(), now=NOW, allowed=True, writer=True,
                        tenant_id=tenant_id, actor_id=actor_id)

    @contextmanager
    def writer(config):
        yield SimpleNamespace(revalidate=lambda cfg: SimpleNamespace(allowed=f.writer, enabled=True, epoch=1))
    monkeypatch.setattr(signup, "global_writer_authority_lease", writer)

    def authorized(candidate):
        return f.allowed and candidate.id == tenant_id and candidate.pyme_id == actor_id

    def start():
        return signup.start_signup(db.session, config, tenant_id, actor_id, authorize=authorized, clock=lambda: f.now)

    def finish(state, **changes):
        args = dict(state=state, sdk_session=SDK, waba=WABA, phone_id=PHONE_ID, code=CODE,
                    authorize=authorized, tenant_id=tenant_id, actor_id=actor_id,
                    clock=lambda: f.now, graph_factory=lambda *_args, **_kw: f.graph)
        args.update(changes)
        return signup.finish_signup(db.session, config, **args)

    def resume(state):
        return signup.resume_signup(db.session, config, tenant_id, actor_id, state=state,
                                    authorize=authorized, clock=lambda: f.now, graph_factory=lambda *a, **kw: f.graph)

    f.start, f.finish, f.resume = start, finish, resume
    return f


def issued(flow):
    body, status = flow.start()
    assert status == 201
    return body["signup_state"]


def owned(flow):
    connection = ProviderConnection.query.filter_by(tenant_id=flow.tenant_id).one()
    sender = ProviderSender.query.filter_by(tenant_id=flow.tenant_id).one()
    return connection, sender


def assert_empty(flow):
    assert ProviderConnection.query.filter_by(tenant_id=flow.tenant_id).count() == 0
    assert ProviderSender.query.filter_by(tenant_id=flow.tenant_id).count() == 0
    assert AuditEvent.query.filter_by(tenant_id=flow.tenant_id).count() == 0


def test_signup_seals_once_audits_and_redacts_all_observable_values(flow, caplog):
    state = issued(flow)
    with caplog.at_level(logging.DEBUG):
        answer, status = flow.finish(state)
        repeated = flow.finish(state)
    assert status == 200 and repeated == (answer, status)
    assert answer["status"] == "connected" and answer["ok"] is True
    conn, sender = owned(flow)
    assert {sender.status, conn.status} == {"connected"}
    assert sender.provider_connection_id == conn.id and sender.phone_number == PHONE
    assert (conn.external_account_id, conn.external_app_id, sender.phone_number_id) == (WABA, flow.graph.app_id, PHONE_ID)
    assert conn.config[signup.MARKER] is True and conn.credentials_ref == vault.VAULT_REF
    credential = vault.open_token(connection=conn, tenant_id=flow.tenant_id, now=NOW, app_config=flow.config)
    assert credential == flow.graph.proof().credential
    events = AuditEvent.query.filter_by(tenant_id=flow.tenant_id).order_by(AuditEvent.id).all()
    assert [event.event_type for event in events] == ["provider_credential.meta_signup_sealed", "whatsapp.meta_signup_connected"]
    assert all(event.actor_user_id == flow.actor_id for event in events)
    assert flow.graph.exchanges == flow.graph.subscriptions == 1
    attempt = Attempt.query.one()
    public = caplog.text + json.dumps([answer, [event.details for event in events]])
    secrets = (CODE, TOKEN, state, SDK, attempt.code_hash, attempt.state_hash, attempt.provider_session_hash,
               conn.config[PRIVATE_CONFIG_KEY]["ciphertext"], conn.config[PRIVATE_CONFIG_KEY]["nonce"])
    if any(secret in public for secret in secrets):
        pytest.fail("Sensitive signup material escaped the private operation")
    stored = json.dumps([conn.config, {c.name: getattr(attempt, c.name) for c in Attempt.__table__.columns}])
    if any(secret in stored for secret in (CODE, TOKEN, state, SDK)):
        pytest.fail("Raw signup material was persisted")


@pytest.mark.parametrize("reason", ["meta_token_granular_scope_mismatch", "meta_phone_membership_unverified"])
def test_provider_rejection_is_terminal_without_a_binding_or_subscribe(flow, reason):
    state = issued(flow)
    flow.graph.reject = reason
    initial = flow.finish(state)
    assert initial[1] == 422 and initial[0]["reason_code"] == "meta_authority_denied"
    assert flow.finish(state) == initial
    assert flow.graph.exchanges == 1 and flow.graph.subscriptions == 0
    assert_empty(flow)


@pytest.mark.parametrize("registered", [True, False], ids=["subscription", "phone-registration"])
def test_pending_stage_can_resume_without_exchanging_or_resealing(flow, registered):
    state = issued(flow)
    flow.graph.registered = registered
    flow.graph.pending = registered
    body, status = flow.finish(state)
    assert status == 202 and body["status"] == "pending"
    assert body["reason_code"] == ("meta_subscription_unverified" if registered else "meta_phone_registration_pending")
    conn, sender = owned(flow)
    assert sender.status == conn.status == "pending"
    envelope = deepcopy(conn.config[PRIVATE_CONFIG_KEY])
    flow.graph.registered, flow.graph.pending = True, False
    assert flow.finish(state)[1] == 200
    conn, sender = owned(flow)
    assert sender.status == conn.status == "connected" and conn.config[PRIVATE_CONFIG_KEY] == envelope
    assert (flow.graph.exchanges, flow.graph.rechecks) == (1, 1)
    assert flow.graph.subscriptions == (2 if registered else 1)
    assert AuditEvent.query.count() == 2


@pytest.mark.parametrize("field", ["external_account_id", "external_business_id"])
def test_prior_waba_binding_conflicts_before_graph_or_state_claim(flow, field):
    state = issued(flow)
    existing = ProviderConnection.query.filter_by(provider="twilio").one()
    setattr(existing, field, WABA)
    db.session.commit()
    assert flow.finish(state)[1] == 409
    attempt = Attempt.query.one()
    assert attempt.status == "issued" and attempt.code_hash is None
    assert flow.graph.exchanges == flow.graph.subscriptions == 0
    assert_empty(flow)


@pytest.mark.parametrize("reuse", ["code", "sdk"])
def test_new_state_cannot_reuse_a_consumed_code_or_provider_session(flow, reuse):
    original = issued(flow)
    flow.graph.reject = "meta_phone_membership_unverified"
    assert flow.finish(original)[1] == 422
    new_state = issued(flow)
    inputs = {"code": CODE if reuse == "code" else CODE + "-new",
              "sdk_session": SDK if reuse == "sdk" else SDK + "-new"}
    assert flow.finish(new_state, **inputs)[1] == 409
    assert flow.graph.exchanges == 1 and flow.graph.subscriptions == 0
    assert_empty(flow)


def test_tenant_actor_permission_deadline_and_feature_gate(flow):
    state = issued(flow)
    assert flow.finish(state, actor_id=1)[1] == 403
    assert flow.finish(state, tenant_id=46, authorize=lambda _: True)[1] == 403
    flow.allowed = False
    assert flow.finish(state)[1] == 403
    flow.allowed = True
    flow.now = NOW + signup.SESSION_SECONDS
    assert flow.finish(state)[1] == 410
    flow.config[signup.FLAG] = False
    assert flow.start()[1] == flow.finish(state)[1] == 404
    assert not flow.graph.exchanges and Attempt.query.count() == 1
    assert_empty(flow)


def test_revoked_permission_after_exchange_prevents_sealing_and_a_second_exchange(flow):
    state = issued(flow)
    flow.graph.after_exchange = lambda: setattr(flow, "allowed", False)
    assert flow.finish(state)[1] == 403
    assert_empty(flow)
    flow.allowed = True
    body, status = flow.finish(state)
    assert status == 202 and body["reason_code"] == "meta_signup_exchange_unconfirmed"
    assert flow.graph.exchanges == 1 and flow.graph.subscriptions == 0


@pytest.mark.parametrize("change", ["local_sender", "foreign_binding"])
def test_pending_binding_changed_does_not_resume_subscription(flow, change):
    state = issued(flow)
    flow.graph.pending = True
    assert flow.finish(state)[1] == 202
    conn, sender = owned(flow)
    if change == "local_sender":
        sender.status = "suspended"
    else:
        ProviderConnection.query.filter_by(provider="twilio").one().external_business_id = WABA
    db.session.commit()
    assert flow.finish(state)[1] == 409
    assert conn.status == "pending" and sender.status in {"pending", "suspended"}
    assert flow.graph.exchanges == flow.graph.subscriptions == 1
    assert AuditEvent.query.count() == 1


@pytest.mark.parametrize("mismatch", [{"code": CODE + "-different"}, {"sdk_session": SDK + "-different"},
                                       {"waba": "800000000000229"}, {"phone_id": "900000000000229"}])
def test_active_state_cannot_change_the_original_completion(flow, mismatch):
    state = issued(flow)
    assert flow.finish(state)[1] == 200
    before = deepcopy(owned(flow)[0].config)
    assert flow.finish(state, **mismatch)[1] == 409
    assert owned(flow)[0].config == before and AuditEvent.query.count() == 2
    assert flow.graph.exchanges == flow.graph.subscriptions == 1


def expired_pending_signup(flow):
    state = issued(flow)
    flow.graph.registered = False
    assert flow.finish(state)[1] == 202
    flow.now = flow.graph.expires_at
    flow.graph = Provider(token=TOKEN + "-new", expires_at=flow.now + 3600)
    return state


def complete_new_authorization(flow, state, **changes):
    return flow.finish(state, **{"code": CODE + "-new", "sdk_session": SDK + "-new", **changes})


@pytest.mark.parametrize("legacy_revision", [False, True])
def test_reauthorization_rotates_the_expired_pair_and_requires_subscription(flow, legacy_revision):
    old_state = expired_pending_signup(flow)
    conn, sender = owned(flow)
    conn.config = {**conn.config, "operator_note": "keep"}
    if legacy_revision:
        Attempt.query.one().credential_revision = None
    db.session.commit()
    original_ids = (conn.id, sender.id)
    assert flow.resume(old_state)[0]["reason_code"] == "meta_signup_credential_reauthorization_required"
    new_state = issued(flow)
    flow.graph.pending = True
    answer, status = complete_new_authorization(flow, new_state)
    assert status == 202 and answer["reason_code"] == "meta_subscription_unverified"
    conn, sender = owned(flow)
    assert (conn.id, sender.id) == original_ids and sender.provider_connection_id == conn.id
    assert conn.status == sender.status == "pending"
    assert conn.config[signup.MARKER] is True and conn.config["operator_note"] == "keep"
    credential = vault.open_token(connection=conn, tenant_id=flow.tenant_id, now=flow.now, app_config=flow.config)
    assert credential == vault.MetaCredential(2, flow.graph.expires_at, flow.graph.token)
    rotated_config = deepcopy(conn.config)

    flow.graph.pending = False
    for result, status in (flow.resume(old_state), flow.finish(old_state)):
        assert status == 409 and result["reason_code"] == "meta_signup_superseded"
    assert conn.config == rotated_config and conn.status == sender.status == "pending"
    assert (flow.graph.exchanges, flow.graph.rechecks, flow.graph.subscriptions) == (1, 0, 1)

    connected = flow.resume(new_state)
    assert connected[1] == 200 and connected[0]["status"] == "connected"
    assert complete_new_authorization(flow, new_state) == connected
    assert (flow.graph.exchanges, flow.graph.rechecks, flow.graph.subscriptions) == (1, 1, 2)
    conn, sender = owned(flow)
    assert (conn.id, sender.id) == original_ids and conn.status == sender.status == "connected"
    assert conn.config == rotated_config
    events = AuditEvent.query.filter_by(tenant_id=flow.tenant_id).order_by(AuditEvent.id).all()
    assert [(e.event_type, e.details["revision"]) for e in events] == [
        ("provider_credential.meta_signup_sealed", 1),
        ("provider_credential.meta_signup_reauthorized", 2),
        ("whatsapp.meta_signup_connected", 2),
    ]
    assert all(e.actor_user_id == flow.actor_id and e.resource_id == str(conn.id)
               and e.details["sender_id"] == sender.id for e in events)


@pytest.mark.parametrize("change", ["waba", "phone_id", "phone", "foreign_org", "manual", "active", "valid",
                                     "sender_active", "missing_original_pair", "missing_code_claim",
                                     "missing_sdk_claim", "invalid_envelope"])
def test_reauthorization_requires_exact_owned_direct_expired_pair(flow, change):
    expired_pending_signup(flow)
    conn, sender = owned(flow)
    if change == "manual":
        conn.config = {k: v for k, v in conn.config.items() if k != signup.MARKER}
    elif change == "active":
        conn.status = sender.status = "connected"
    elif change == "sender_active":
        sender.status = "connected"
    elif change == "valid":
        flow.now -= 1
    elif change == "missing_original_pair":
        Attempt.query.one().sender_id = None
    elif change == "missing_code_claim":
        Attempt.query.one().code_hash = None
    elif change == "missing_sdk_claim":
        Attempt.query.one().provider_session_hash = None
    elif change == "invalid_envelope":
        config = deepcopy(conn.config)
        config[PRIVATE_CONFIG_KEY]["expires_at"] -= 1
        conn.config = config
    db.session.commit()
    before = (conn.id, sender.id, conn.status, sender.status, deepcopy(conn.config))

    args = {}
    if change == "waba":
        args["waba"] = "810000000000102"
    elif change == "phone_id":
        args["phone_id"] = "910000000000102"
    elif change == "phone":
        flow.graph.phone = "+15550000102"
    if change == "foreign_org":
        other = TenantProfile.query.filter_by(slug="tierra-del-fuego").one()
        args.update(tenant_id=other.id, actor_id=other.municipio_id,
                    authorize=lambda candidate: candidate.id == other.id)
        response, status = signup.start_signup(db.session, flow.config, **args, clock=lambda: flow.now)
        assert status == 201
        state = response["signup_state"]
    else:
        state = issued(flow)
    _, status = complete_new_authorization(flow, state, **args)
    assert status == 409
    assert flow.graph.exchanges == (1 if change == "phone" else 0)
    assert flow.graph.subscriptions == 0
    conn, sender = owned(flow)
    assert (conn.id, sender.id, conn.status, sender.status, conn.config) == before
    assert AuditEvent.query.count() == 1


def test_stale_reauthorization_cannot_replace_the_winners_expired_revision(flow):
    expired_pending_signup(flow)
    slow_state, fast_state = issued(flow), issued(flow)
    slow_graph = flow.graph
    fast_graph = Provider(token=TOKEN + "-winner", expires_at=flow.now + 2)
    fast_graph.registered = False

    def complete_competitor():
        # Both requests see revision 1 before exchanging. Revision 2 is already
        # expired when the slower request returns from its provider call.
        result, status = complete_new_authorization(flow, fast_state, code=CODE + "-fast",
            sdk_session=SDK + "-fast", graph_factory=lambda *a, **kw: fast_graph)
        assert status == 202 and result["status"] == "pending"
        flow.now = fast_graph.expires_at

    slow_graph.after_exchange = complete_competitor
    lost = complete_new_authorization(flow, slow_state)
    assert lost[1] == 409 and complete_new_authorization(flow, slow_state) == lost
    conn, sender = owned(flow)
    assert conn.status == sender.status == "pending"
    winning_credential = vault.open_token(connection=conn, tenant_id=flow.tenant_id, now=flow.now - 1,
                                         app_config=flow.config)
    assert winning_credential == vault.MetaCredential(2, fast_graph.expires_at, fast_graph.token)
    assert slow_graph.exchanges == fast_graph.exchanges == 1
    assert slow_graph.subscriptions == fast_graph.subscriptions == 0
    events = AuditEvent.query.order_by(AuditEvent.id).all()
    assert [e.details["revision"] for e in events] == [1, 2]

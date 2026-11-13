"""Real SQLite admission/claims/outbox with synthetic provider adapters only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from models import (
    ChannelSessionIdentityBinding, ChatSessionContext, WhatsAppInboundTurn,
    WhatsAppOutboundAttempt, db,
)
from services import whatsapp_inbound_turns as queue, whatsapp_inbound_worker as worker
from services.channel_session_identity import (
    ChannelSessionIdentityConflict, derive_channel_session_identity_hmac,
    resolve_channel_session_identity,
)
from services.whatsapp_inbound_durability import (
    WhatsAppInboundDurabilityConfigurationError, resolve_whatsapp_inbound_durability_policy,
)
from services.whatsapp_conversation_uow import ConversationUnitOfWork, ConversationUnitOfWorkError
from tests.test_meta_whatsapp_outbox import CONTACT, _pending_claim, environment
from tests.test_meta_whatsapp_inbound_worker import _post, _stage, _turn
from tests.test_whatsapp_inbound_turns import _complete_with_text, _ingest


def _legacy_config(env):
    env.app.config.update(
        SECRET_KEY="synthetic-existing-flask-key-" + "s" * 32,
        WHATSAPP_INBOUND_DURABILITY_MODE="legacy",
        WHATSAPP_INBOUND_QUEUE_TENANT_IDS="",
        CHANNEL_SESSION_IDENTITY_MODE="shadow",
        CHANNEL_SESSION_IDENTITY_VERSION_V1="v1",
    )
    env.app.config.pop("WHATSAPP_INBOUND_HASH_SECRET", None)
    env.app.config.pop("CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1", None)


def _isolate(env):
    env.app.config.update(
        META_WHATSAPP_PROVIDER_ISOLATION_ENABLED=True,
        META_WHATSAPP_QUEUE_TENANT_IDS=str(env.tenant.id),
        META_WHATSAPP_INBOUND_HASH_SECRET="synthetic-meta-stream-" + "q" * 32,
        META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="synthetic-meta-identity-" + "i" * 32,
        META_CHANNEL_SESSION_IDENTITY_VERSION="meta-v1",
    )


def _resolve(env, provider, **kwargs):
    return resolve_channel_session_identity(
        config=env.app.config, tenant_id=env.tenant.id, owner_user_id=env.owner.id,
        channel="whatsapp", provider=provider, provider_identity="+" + CONTACT, **kwargs,
    )


def _snapshot(row):
    return deepcopy({column.key: getattr(row, column.key) for column in row.__table__.columns})


@pytest.mark.parametrize("identity_mode", ("legacy", "shadow"))
def test_meta_admission_and_worker_preserve_existing_twilio_v1_binding(environment, identity_mode):
    env = environment
    _legacy_config(env)
    legacy = ChatSessionContext(
        chat_session_id="synthetic-historical-twilio", tenant_id=env.tenant.id,
        user_id=env.owner.id, anon_id="+" + CONTACT,
        context_data={"history_marker": "must-stay-identical"},
    )
    db.session.add(legacy)
    db.session.commit()
    twilio = _resolve(env, "twilio")
    assert twilio.outcome == "adopted" and twilio.identity_version == "v1"
    before_binding = _snapshot(db.session.get(ChannelSessionIdentityBinding, twilio.binding_id))
    before_context = _snapshot(db.session.get(ChatSessionContext, twilio.chat_session_id))
    db.session.commit()

    _isolate(env)
    env.app.config["CHANNEL_SESSION_IDENTITY_MODE"] = identity_mode
    tenant_id, turn_id = _stage(env)
    incoming = _turn(turn_id)
    assert incoming.session_identity_version == "meta-v1"
    assert incoming.chat_session_id != twilio.chat_session_id
    assert incoming.session_identity_hmac == derive_channel_session_identity_hmac(
        secret=env.app.config["META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"],
        tenant_id=tenant_id, provider="meta", identity_version="meta-v1",
        provider_identity="+" + CONTACT,
    )
    assert env.calls["post"] == []
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1, report
    assert env.calls["post"] == []
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "accepted"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0
    db.session.expire_all()
    assert _snapshot(db.session.get(ChannelSessionIdentityBinding, twilio.binding_id)) == before_binding
    assert _snapshot(db.session.get(ChatSessionContext, twilio.chat_session_id)) == before_context
    assert env.app.config["WHATSAPP_INBOUND_DURABILITY_MODE"] == "legacy"
    assert env.app.config["CHANNEL_SESSION_IDENTITY_MODE"] == identity_mode
    assert "WHATSAPP_INBOUND_HASH_SECRET" not in env.app.config
    assert "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1" not in env.app.config
    if identity_mode == "shadow":
        replay = _resolve(env, "twilio")
        assert (replay.binding_id, replay.chat_session_id, replay.identity_hmac, replay.identity_version) == (
            twilio.binding_id, twilio.chat_session_id, twilio.identity_hmac, "v1",
        )
    # Exact authenticated replay remains idempotent under the isolated keys.
    _post(env, body="Se cayó otra rama en la plaza")
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["processed"] == 0
    assert WhatsAppOutboundAttempt.query.count() == 1


@pytest.mark.parametrize("resolver", ("generic", "admission"))
def test_isolated_meta_never_adopts_unbound_legacy_context(environment, resolver):
    env = environment
    _legacy_config(env)
    legacy = ChatSessionContext(
        chat_session_id="synthetic-unbound-history", tenant_id=env.tenant.id,
        user_id=env.owner.id, anon_id="+" + CONTACT,
        context_data={"history_marker": "unbound-twilio-history"},
    )
    db.session.add(legacy)
    db.session.commit()
    before = _snapshot(legacy)
    _isolate(env)
    if resolver == "generic":
        meta = _resolve(env, "meta", explicit_legacy_chat_session_id=legacy.chat_session_id)
        assert meta.outcome == "created" and not meta.continuity_preserved
        meta_session = meta.chat_session_id
    else:
        _, turn_id = _stage(env, followup="missing")
        meta_session = _turn(turn_id).chat_session_id
    assert meta_session != legacy.chat_session_id
    db.session.expire_all()
    assert _snapshot(db.session.get(ChatSessionContext, legacy.chat_session_id)) == before


def test_existing_isolated_binding_cannot_share_other_provider_context(environment):
    env = environment
    _legacy_config(env)
    twilio = _resolve(env, "twilio")
    _isolate(env)
    meta = _resolve(env, "meta")
    meta_row = db.session.get(ChannelSessionIdentityBinding, meta.binding_id)
    meta_row.chat_session_id = twilio.chat_session_id
    twilio_row = db.session.get(ChannelSessionIdentityBinding, twilio.binding_id)
    twilio_row.status = "quarantined"
    db.session.commit()
    before = _snapshot(twilio_row)
    with pytest.raises(ChannelSessionIdentityConflict, match="session_identity_provider_context_shared"):
        _resolve(env, "meta")
    db.session.expire_all()
    assert _snapshot(db.session.get(ChannelSessionIdentityBinding, twilio.binding_id)) == before


def test_worker_intersects_each_provider_with_its_own_canaries(environment):
    env = environment
    _legacy_config(env)
    _isolate(env)
    meta_tenant, twilio_tenant = env.tenant.id, env.tenant.id + 10
    env.app.config.update(
        WHATSAPP_INBOUND_DURABILITY_MODE="queue", WHATSAPP_INBOUND_QUEUE_TENANT_IDS=str(twilio_tenant),
        WHATSAPP_INBOUND_HASH_SECRET="synthetic-global-stream-" + "q" * 32,
        CHANNEL_SESSION_IDENTITY_MODE="enforce",
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="synthetic-global-identity-" + "i" * 32,
        META_WHATSAPP_INBOUND_WORKER_ENABLED=True,
        DOMAIN_EFFECT_OUTBOX_MODE="queue", DOMAIN_EFFECT_OUTBOX_SECRET="synthetic-effects-" + "d" * 32,
        DOMAIN_EFFECT_OUTBOX_TENANT_IDS=f"{meta_tenant},{twilio_tenant}",
    )
    assert set(worker._worker_tenant_ids(None)) == {meta_tenant, twilio_tenant}
    assert worker._inbound_worker_providers(meta_tenant) == ("meta",)
    assert worker._outbound_worker_providers(meta_tenant) == ("meta",)
    assert worker._inbound_worker_providers(twilio_tenant) == ("twilio",)
    assert worker._outbound_worker_providers(twilio_tenant) == ("twilio",)
    assert not resolve_whatsapp_inbound_durability_policy(
        env.app.config, tenant_id=twilio_tenant, provider="meta",
    ).queue_enabled
    with pytest.raises(WhatsAppInboundDurabilityConfigurationError, match="not_canary"):
        worker._worker_tenant_ids(twilio_tenant, provider="meta")


def _seed_twilio_queues(env):
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    tenant = env.tenant.id
    for label in ("pending", "sending"):
        incoming = _ingest("SMsynthetic-" + label, tenant_id=tenant, stream_key=label, now=old)
        claim = queue.claim_next_whatsapp_inbound_turn(tenant_id=tenant, stream_key=label,
            providers=("twilio",), now=old)
        assert claim.turn_id == incoming.turn_id
        assert _complete_with_text(claim.turn_id, claim.lease_token, now=old).completed
        if label == "sending":
            pending_row = WhatsAppOutboundAttempt.query.filter_by(stream_key="pending").one()
            pending_row.available_at = datetime.now(timezone.utc) + timedelta(seconds=5)
            db.session.commit()
            claim_out = queue.claim_next_whatsapp_outbound_attempt(tenant_id=tenant,
                providers=("twilio",), now=old, lease_seconds=30)
            assert claim_out is not None
            pending_row.available_at = old
            db.session.commit()
    _ingest("SMsynthetic-received", tenant_id=tenant, stream_key="received", now=old)
    _ingest("SMsynthetic-expired-inbound", tenant_id=tenant, stream_key="expired-inbound", now=old)
    assert queue.claim_next_whatsapp_inbound_turn(tenant_id=tenant, stream_key="expired-inbound",
        providers=("twilio",), now=old, lease_seconds=30) is not None
    db.session.expire_all()
    before_in = {r.id: _snapshot(r) for r in WhatsAppInboundTurn.query.filter_by(provider="twilio")}
    before_out = {r.id: _snapshot(r) for r in WhatsAppOutboundAttempt.query.filter_by(provider="twilio")}
    db.session.commit()
    assert {r["status"] for r in before_out.values()} == {"pending", "sending"}
    return before_in, before_out


def test_meta_only_poller_does_not_claim_or_quarantine_twilio_rows(environment):
    env = environment
    _legacy_config(env)
    _isolate(env)
    before_in, before_out = _seed_twilio_queues(env)
    _stage(env)

    report = worker.run_whatsapp_durable_worker(env.app, once=True, standby_when_legacy=True)

    assert report["mode"] == "queue" and report["cycle_failures"] == 0, report
    assert report["inbound_completed"] == report["outbound_accepted"] == 1, report
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0
    db.session.expire_all()
    assert {r.id: _snapshot(r) for r in WhatsAppInboundTurn.query.filter_by(provider="twilio")} == before_in
    assert {r.id: _snapshot(r) for r in WhatsAppOutboundAttempt.query.filter_by(provider="twilio")} == before_out


def test_invalid_isolated_policy_poisons_uow_even_if_config_is_restored(environment):
    env = environment
    _legacy_config(env)
    _isolate(env)
    claim = _pending_claim(env)
    context = db.session.get(ChatSessionContext, claim.chat_session_id)
    before = deepcopy(context.context_data)
    session = db.session()
    connection = session.connection()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_identity_scope_mismatch"):
        with ConversationUnitOfWork(session=session, transaction=session.get_transaction(),
                claim=claim, clock=env.runtime.now) as unit:
            context.context_data = {**before, "synthetic_wip": True}
            unit.flush()
            key = env.app.config.pop("META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1")
            with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_identity_scope_mismatch"):
                unit.flush()
            env.app.config["META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"] = key
            unit.flush()
    db.session.expire_all()
    assert db.session.get(ChatSessionContext, claim.chat_session_id).context_data == before
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


@pytest.mark.parametrize("providers", ((), ["meta"], ("unknown",), ("twilio", "unknown")))
def test_outbound_claim_rejects_invalid_provider_scope_without_mutation(environment, providers):
    before_in, before_out = _seed_twilio_queues(environment)
    with pytest.raises(queue.WhatsAppTurnValidationError, match="invalid_outbound_claim_providers"):
        queue.claim_next_whatsapp_outbound_attempt(tenant_id=environment.tenant.id, providers=providers)
    db.session.expire_all()
    assert {r.id: _snapshot(r) for r in WhatsAppInboundTurn.query.filter_by(provider="twilio")} == before_in
    assert {r.id: _snapshot(r) for r in WhatsAppOutboundAttempt.query.filter_by(provider="twilio")} == before_out

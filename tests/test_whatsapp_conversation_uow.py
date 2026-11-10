# PREP only: copies of synthetic fixture factories, no test functions selected.
# Extracted from current tests/test_meta_whatsapp_outbox.py. Never imported here.
"""Real Meta parser/ORM/producer/outbox/worker; HTTP and identities synthetic."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import hmac
import json
import secrets
import uuid
from types import SimpleNamespace

from flask import Flask
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import ProviderConnection, ProviderSender, TenantProfile, User, db
from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt
from services import meta_whatsapp_credentials as vault
from services import meta_whatsapp_webhook as webhook
from services import whatsapp_inbound_worker as worker
from services.meta_whatsapp_outbox import (
    MetaReplyContent, MetaReplyBundle, RUNTIME_EXTENSION, produce_meta_service_reply,
    complete_meta_service_reply, verified_inbound_payload, _canonical,
)
from services import meta_whatsapp_outbox as meta_outbox
from services import whatsapp_inbound_turns as turns
from services.meta_whatsapp_runtime import (
    APP_SECRETS_KEY, MetaOutboundRuntime, MetaRuntimeError, native_graph_request,
)


APP, WABA, PHONE = "100001", "200001", "300001"
CONTACT = "5491112345678"
ACK = "wamid.ABC/def+="


@pytest.fixture
def environment(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    app_secret = secrets.token_hex(32)
    app = Flask("meta-outbox-offline-test")
    app.config.update(
        TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CUTOVER_WRITER_FENCE_ENABLED=False,
        TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="synthetic",
        TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({
            "synthetic": base64.b64encode(secrets.token_bytes(32)).decode()}),
        WHATSAPP_INBOUND_DURABILITY_MODE="queue",
        WHATSAPP_INBOUND_HASH_SECRET=secrets.token_hex(32),
        CHANNEL_SESSION_IDENTITY_MODE="enforce",
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=secrets.token_hex(32),
        META_WHATSAPP_GRAPH_VERSION="v25.0",
        **{APP_SECRETS_KEY: {APP: app_secret}},
    )
    db.init_app(app)
    with app.app_context():
        assert str(db.engine.url) == "sqlite:///:memory:"
        db.create_all()
        owner = User(name="Synthetic owner", email="meta-owner@example.invalid",
                     rol="admin", tipo_chat="pyme", password_hash="synthetic-not-a-login")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="meta-outbox-synthetic", nombre="Synthetic institution",
                               tipo="pyme", pyme_id=owner.id, is_active=True)
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug
        connection = ProviderConnection(tenant_id=tenant.id, provider="meta", channel="whatsapp",
            environment="production", status="connected", external_app_id=APP,
            external_account_id=WABA, credentials_ref=vault.VAULT_REF, config={})
        db.session.add(connection)
        db.session.flush()
        connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=connection, tenant_id=tenant.id, access_token=secrets.token_urlsafe(64),
            revision=1, expires_at=now + 3600, now=now, app_config=app.config)}
        sender = ProviderSender(tenant_id=tenant.id, provider_connection_id=connection.id,
            channel="whatsapp", phone_number_id=PHONE, waba_id=WABA, status="ONLINE")
        db.session.add(sender)
        db.session.commit()
        app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(tenant.id)
        calls = {"get": [], "post": [], "twilio": 0}
        def response(value, status=200):
            return SimpleNamespace(status_code=status,
                                   content=json.dumps(value).encode("utf-8"))
        def get(method, url, **kwargs):
            assert method == "GET"
            assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
            path = url.split("graph.facebook.com/", 1)[1]
            calls["get"].append(path)
            if path == "v25.0/debug_token":
                return response({"data": {"is_valid": True, "app_id": APP,
                    "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"],
                    "expires_at": now + 3600, "data_access_expires_at": now + 3000,
                    "granular_scopes": [
                        {"scope": "whatsapp_business_management", "target_ids": [WABA]},
                        {"scope": "whatsapp_business_messaging", "target_ids": [WABA]},
                    ]}})
            if path == "v25.0/" + WABA + "/phone_numbers":
                return response({"data": [{"id": PHONE}]})
            if path == "v25.0/" + WABA + "/subscribed_apps":
                return response({"data": [{"whatsapp_business_api_data": {"id": APP}}]})
            raise AssertionError("Unplanned synthetic Graph GET")
        def post(method, url, **kwargs):
            assert method == "POST"
            assert url == "https://graph.facebook.com/v25.0/" + PHONE + "/messages"
            assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
            # Deliberately retain no synthetic Authorization header in receipts.
            calls["post"].append({"url": url, "json": copy.deepcopy(kwargs["json"])})
            acknowledgement = ACK if len(calls["post"]) == 1 else "wamid.synthetic/" + str(len(calls["post"])) + "+="
            return response({"messaging_product": "whatsapp", "messages": [{"id": acknowledgement}]})
        clock = [now]
        runtime = MetaOutboundRuntime(app.config, get=get, post=post, clock=lambda: clock[0])
        app.extensions[RUNTIME_EXTENSION] = runtime
        def no_twilio(*_args, **_kwargs):
            calls["twilio"] += 1
            raise AssertionError("Meta outbox must not call Twilio")
        monkeypatch.setattr(worker, "Client", no_twilio)
        document = {"object": "whatsapp_business_account", "entry": [{"id": WABA,
            "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp",
                "metadata": {"phone_number_id": PHONE}, "messages": [{
                    "from": CONTACT, "id": "wamid.inbound/ABC+=", "timestamp": str(now - 30),
                    "type": "text", "text": {"body": "Synthetic service inquiry"}}]}}]}]}
        raw = json.dumps(document, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
        from services.meta_whatsapp_cloud import MetaSenderSnapshot
        identity = MetaSenderSnapshot(tenant.id, connection.id, sender.id, APP, WABA, PHONE, "production")
        events = webhook.parse_webhook(raw_body=raw, signature=signature, app_secret=app_secret,
            app_id=APP, now=now, binding_resolver=lambda phone, app_id, waba:
                runtime.load_binding(identity) if (phone, app_id, waba) == (PHONE, APP, WABA)
                else (_ for _ in ()).throw(AssertionError("Foreign synthetic binding")))
        assert len(events) == 1
        yield SimpleNamespace(app=app, tenant=tenant, owner=owner, connection=connection,
            sender=sender, event=events[0], runtime=runtime, calls=calls, now=now,
            clock=clock, response=response)
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _pending_claim(env):
    from services.channel_session_identity import resolve_channel_session_identity
    identity = resolve_channel_session_identity(config=env.app.config, tenant_id=env.tenant.id,
        channel="whatsapp", provider="meta", provider_identity="+" + env.event.contact,
        owner_user_id=env.owner.id)
    binding = env.runtime.load_binding(env.event.sender)
    payload = verified_inbound_payload(env.event, binding, now=env.runtime.now())
    now = datetime.fromtimestamp(env.now, timezone.utc)
    turn = WhatsAppInboundTurn(turn_id=str(uuid.uuid4()), tenant_id=env.tenant.id,
        provider="meta", provider_connection_id=env.connection.id,
        provider_sender_id=env.sender.id, provider_message_sid=env.event.message_id,
        stream_key="synthetic-meta-stream", message_kind="text", payload_json=payload,
        payload_digest=_canonical(payload), status="received", received_at=now,
        available_at=now, created_at=now, updated_at=now, attempt_count=0, max_attempts=8,
        session_identity_binding_id=identity.binding_id,
        session_identity_version=identity.identity_version,
        session_identity_hmac=identity.identity_hmac, chat_session_id=identity.chat_session_id)
    db.session.add(turn)
    db.session.commit()
    claim = turns.claim_next_whatsapp_inbound_turn(tenant_id=env.tenant.id, now=now)
    assert claim is not None and claim.payload["input"]["text"] == env.event.text
    return claim


# New UoW tests only. The two factories above are copied, not imported from or
# collected together with the closed Meta outbox suite. All provider I/O is fake.
from sqlalchemy import event as sa_event, text
from models import ChatSessionContext, PymeTicket, TicketComentario
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services.contact_service import resolve_contact
from services import whatsapp_conversation_turn as common
from services.whatsapp_conversation_contract import (
    ConversationMode, ConversationTurn, ConversationSelection, CollectingConversationSink,
)
from services.whatsapp_conversation_uow import (
    ConversationUnitOfWork, ConversationUnitOfWorkError, ConversationCommitUncertain,
)


@pytest.fixture
def unit_env(environment):
    env = environment
    env.claim = _pending_claim(env)
    env.context = ChatSessionContext.query.filter_by(chat_session_id=env.claim.chat_session_id).one()
    # The engine configuration is unchanged. This fixture explicitly starts a
    # physical SQLite BEGIN before the finalizer may create any SAVEPOINT.
    actual = db.session()
    driver = actual.connection().connection.driver_connection
    if not driver.in_transaction:
        actual.execute(text("BEGIN"))
    assert driver.in_transaction and not actual.new and not actual.dirty and not actual.deleted
    env.actual_session = actual
    return env


def _unit(env):
    return ConversationUnitOfWork(session=env.actual_session,
        transaction=env.actual_session.get_transaction(), claim=env.claim, clock=env.runtime.now)


def _turn(env, unit=None):
    return ConversationTurn(owner_user=env.owner, tenant_profile=env.tenant,
        tenant_id=env.tenant.id, end_user=None, session_context=env.context,
        chat_session_id=env.claim.chat_session_id, contact_identity="+" + env.event.contact,
        message_body=env.event.text, action_id=None, source_event_id=env.event.message_id,
        durable_claim=env.claim, mode=ConversationMode(False, False), original_text=env.event.text,
        unit_of_work=unit)


def _finish(turn, sink, engine=None):
    def default_engine(*, unit_of_work=None, conversation_turn_id=None, **kwargs):
        assert "unit_of_work" not in kwargs and "conversation_turn_id" not in kwargs
        assert unit_of_work is turn.unit_of_work
        if unit_of_work is not None:
            assert conversation_turn_id == turn.durable_claim.turn_id
        else:
            assert conversation_turn_id is None
        assert kwargs["durable_turn_id"] == turn.durable_claim.turn_id
        return {"message_body": "Synthetic contextual reply", "message_type": "text",
                "options_list": [], "contexto_actualizado": {"uow_synthetic_marker": True}}
    return common.finish_conversation_turn(turn,
        ConversationSelection(turn.message_body, None, None, None), sink,
        prepared_response=None, public_base_url="https://example.invalid", engine=engine or default_engine,
        classify_attachment=lambda *_a: {}, extract_contact_details=lambda *_a: {},
        resolve_input_contact=lambda phone, name: resolve_contact(phone, name, tenant=turn.tenant_profile),
        receipt_header=lambda: None, normalize_flow_contract=lambda *_a, **_k: None,
        on_failure=lambda *_a: (_ for _ in ()).throw(AssertionError("unexpected_legacy_fallback")),
        replay_error=RuntimeError, log=lambda *_a, **_k: None,
        safe_reference=lambda *_a: "synthetic-reference", safe_context_metadata=lambda *_a: {})


def _reply(finished):
    assert not finished.legacy_return and not finished.bot_response.get("options_list")
    return MetaReplyBundle((MetaReplyContent("text", finished.bot_response["message_body"]),))


def test_common_crm_and_real_finalizer_share_one_outer_commit(unit_env, monkeypatch):
    env = unit_env
    observed = []
    monkeypatch.setattr(common, "emit_crm_contact_update",
        lambda tenant, payload: observed.append((tenant.id,
            WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status)))
    commits = []
    def count_commit(session):
        commits.append("nested" if session.in_nested_transaction() else "outer")
    sa_event.listen(env.actual_session, "before_commit", count_commit)
    try:
        with _unit(env) as unit:
            finished = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False))
            assert observed == [] and InteractionEvent.query.count() == 1
            result = unit.commit_reply(_reply(finished), runtime=env.runtime)
            assert result.committed is True and result.receipt.committed is False
            assert result.after_commit_failures == ()
        assert commits == ["nested", "outer"]
    finally:
        sa_event.remove(env.actual_session, "before_commit", count_commit)
    assert observed == [(env.tenant.id, "completed")]
    assert env.context.context_data["uow_synthetic_marker"] is True
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 1
    assert WhatsAppOutboundAttempt.query.count() == 1
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_unsupported_output_rolls_back_flushed_common_crm_and_context(unit_env):
    env = unit_env
    with pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported"):
        with _unit(env) as unit:
            _finish(_turn(env, unit), CollectingConversationSink(supports_media=False))
            assert InteractionEvent.query.count() == 1  # Actual flush, not a fake write.
            unit.commit_reply(MetaReplyBundle((MetaReplyContent("image", "unsupported"),)), runtime=env.runtime)
    db.session.expire_all()
    assert "uow_synthetic_marker" not in env.context.context_data
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "processing"


@pytest.mark.parametrize("operation", ("commit", "caught-commit", "rollback", "savepoint"))
def test_unported_engine_transaction_control_poison_survives_a_caught_error(unit_env, operation):
    env = unit_env
    def unported_engine(**kwargs):
        env.context.context_data["unported_marker"] = True
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(env.context, "context_data")
        try:
            if operation in {"commit", "caught-commit"}:
                db.session.commit()
            elif operation == "rollback":
                db.session.rollback()
            else:
                with db.session.begin_nested():
                    pass
        except ConversationUnitOfWorkError:
            if operation != "caught-commit":
                raise
        return {"message_body": "This must not finalize", "message_type": "text", "options_list": []}
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_"):
        with _unit(env) as unit:
            result = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False), unported_engine)
            unit.commit_reply(_reply(result), runtime=env.runtime)
    db.session.expire_all()
    assert "unported_marker" not in env.context.context_data
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("wrong", ("foreign-tenant", "missing-profile", "different-source-event"))
def test_common_rejects_wrong_server_turn_scope_before_effects(unit_env, wrong):
    env = unit_env
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_turn_scope_mismatch"):
        with _unit(env) as unit:
            turn = _turn(env, unit)
            changes = {"foreign-tenant": {"tenant_id": env.tenant.id + 1},
                "missing-profile": {"tenant_profile": None},
                "different-source-event": {"source_event_id": "wamid.different/+= "}}[wrong]
            _finish(replace(turn, **changes), CollectingConversationSink(supports_media=False))
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_pending_caller_work_is_rejected_without_implicit_flush(unit_env):
    env = unit_env
    pending = Contact(id=str(uuid.uuid4()), tenant_id=env.tenant.id, name="Synthetic unrelated WIP")
    db.session.add(pending)
    flushes = []
    def observe_flush(*args):
        flushes.append(True)
    sa_event.listen(env.actual_session, "before_flush", observe_flush)
    try:
        with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_pending_caller_work"):
            _unit(env)
        assert flushes == [] and pending in db.session.new
    finally:
        sa_event.remove(env.actual_session, "before_flush", observe_flush)
    db.session.rollback()


def test_same_engine_other_session_is_not_the_common_session(unit_env):
    env = unit_env
    # A deliberately foreign test Session is only the adversarial input. The
    # implementation must never allocate or accept it as its business Session.
    with Session(bind=db.engine) as foreign:
        with foreign.begin():
            with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_session_not_scoped"):
                ConversationUnitOfWork(session=foreign, transaction=foreign.get_transaction(), claim=env.claim)


@pytest.mark.parametrize("wrong", ("bool-tenant", "other-turn", "other-effect-key"))
def test_writer_scope_rejects_cross_turn_or_tenant_before_any_effect(unit_env, wrong):
    env = unit_env
    values = {"session": env.actual_session, "tenant_id": env.tenant.id,
              "turn_id": env.claim.turn_id,
              "effect_key": f"whatsapp:{env.tenant.id}:{env.claim.turn_id}:test_effect"}
    values.update({"bool-tenant": {"tenant_id": True},
                   "other-turn": {"turn_id": "synthetic-other-turn"},
                   "other-effect-key": {"effect_key": f"whatsapp:{env.tenant.id}:other:test_effect"}}[wrong])
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_writer_"):
        with _unit(env) as unit:
            unit.require_writer_scope(**values)
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_expired_lease_rolls_back_common_crm_before_outbox(unit_env):
    env = unit_env
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_lease_not_owned"):
        with _unit(env) as unit:
            finished = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False))
            env.clock[0] = int(env.claim.leased_until.replace(tzinfo=timezone.utc).timestamp()) + 1
            unit.commit_reply(_reply(finished), runtime=env.runtime)
    db.session.expire_all()
    assert "uow_synthetic_marker" not in env.context.context_data
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_empty_bundle_completes_same_turn_without_claiming_delivery(unit_env):
    env = unit_env
    with _unit(env) as unit:
        env.context.context_data = {"synthetic_no_automatic_reply": True}
        result = unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    row = WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one()
    assert row.status == "completed" and result.receipt.outbound_attempt_ids == ()
    assert row.result_json["delivery_verified"] is False and row.result_json["reply_staged"] is False
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_best_effort_socket_failure_does_not_rewind_committed_receipt(unit_env, monkeypatch):
    env = unit_env
    def socket_failure(*_args):
        raise RuntimeError("synthetic_socket_failure")
    monkeypatch.setattr(common, "emit_crm_contact_update", socket_failure)
    with _unit(env) as unit:
        finished = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False))
        result = unit.commit_reply(_reply(finished), runtime=env.runtime)
    assert result.committed is True and result.after_commit_failures == ("crm_contact_update",)
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "completed"
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 1
    assert env.calls["post"] == []


def test_session_commit_inside_real_finalizer_savepoint_cannot_commit_outer(unit_env):
    env = unit_env
    actual_get = env.runtime.get
    attempts = []
    def try_wrong_commit(method, url, **kwargs):
        if not attempts:
            attempts.append(True)
            # Real Session.commit targets the root even with a SAVEPOINT open.
            # This synthetic verifier fault must not release the outer unit.
            env.actual_session.commit()
        return actual_get(method, url, **kwargs)
    env.runtime = replace(env.runtime, get=try_wrong_commit)
    with pytest.raises((ConversationUnitOfWorkError, MetaRuntimeError)):
        with _unit(env) as unit:
            finished = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False))
            unit.commit_reply(_reply(finished), runtime=env.runtime)
    db.session.expire_all()
    assert attempts == [True]
    assert "uow_synthetic_marker" not in env.context.context_data
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "processing"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_legacy_common_without_unit_keeps_its_context_commit(unit_env, monkeypatch):
    env = unit_env
    # Observe only the external realtime seam. Actual CRM ORM writes and common
    # commit remain real; this is common legacy compatibility, not Twilio HTTP.
    import services.crm_intelligence as crm
    emitted = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_a: emitted.append(True))
    result = _finish(_turn(env), CollectingConversationSink(supports_media=False))
    assert not result.legacy_return and emitted == [True]
    db.session.rollback()
    db.session.expire_all()
    assert env.context.context_data["uow_synthetic_marker"] is True
    assert InteractionEvent.query.count() == 1


def test_human_comment_is_atomic_with_zero_automatic_reply(unit_env, monkeypatch):
    env = unit_env
    ticket = PymeTicket(tenant_id=env.tenant.id, pregunta="Synthetic human conversation",
        nro_ticket=880001, anon_id="+" + env.event.contact, estado="en_vivo")
    db.session.add(ticket)
    env.context.context_data = {"human_chat_in_progress": True,
        "ticket_id": 880001, "tipo_ticket": "pyme"}
    db.session.flush()
    env.context.context_data["ticket_id"] = ticket.id
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(env.context, "context_data")
    db.session.commit()  # Explicit fixture setup before ownership, not a UoW write.
    env.actual_session.execute(text("BEGIN"))
    # Changed behavior: the human writer receives the real Unit. The existing
    # queue policy is installed only in this synthetic app, never in a provider.
    env.app.config.update(DOMAIN_EFFECT_OUTBOX_MODE="queue",
        DOMAIN_EFFECT_OUTBOX_SECRET="synthetic-human-uow-" + "x" * 32,
        DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(env.tenant.id))
    emitted = []
    monkeypatch.setattr(common, "_emit_live_chat_event", lambda payload: emitted.append(payload))
    with _unit(env) as unit:
        sink = CollectingConversationSink(supports_media=False)
        result = common.prepare_conversation_turn(_turn(env, unit), sink,
            on_failure=lambda *_a: (_ for _ in ()).throw(AssertionError("false_success")),
            log=lambda *_a, **_k: None, safe_reference=lambda *_a: "synthetic")
        assert result.handled and sink.snapshot() == () and emitted == []
        assert TicketComentario.query.one().pyme_ticket_id == ticket.id
        committed = unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    from models import TicketDomainEffectReceipt, DomainEffectOutbox
    assert committed.committed and committed.receipt.outbound_attempt_ids == ()
    assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 1
    assert DomainEffectOutbox.query.count() == 1 and len(emitted) == 1
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_db_commit_ack_uncertain_is_not_reported_as_rollback_or_retried(unit_env):
    env = unit_env
    def lose_ack(session):
        if not session.in_nested_transaction():
            raise RuntimeError("synthetic_database_commit_ack_loss")
    sa_event.listen(env.actual_session, "after_commit", lose_ack)
    try:
        with pytest.raises(ConversationCommitUncertain, match="conversation_uow_commit_uncertain"):
            with _unit(env) as unit:
                unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    finally:
        sa_event.remove(env.actual_session, "after_commit", lose_ack)
    # No re-execution is attempted. A separate readback, not this exception,
    # decides the persisted status; providers have not been called at all.
    env.actual_session.close()  # A committed transaction whose callback raised may remain closed-to-SQL.
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "completed"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_legacy_actual_human_turn_still_returns_with_zero_bot_emissions(unit_env, monkeypatch):
    env = unit_env
    ticket = PymeTicket(tenant_id=env.tenant.id, pregunta="Synthetic legacy human conversation",
        nro_ticket=880002, anon_id="+" + env.event.contact, estado="en_vivo")
    db.session.add(ticket)
    db.session.flush()
    env.context.context_data = {"human_chat_in_progress": True,
        "ticket_id": ticket.id, "tipo_ticket": "pyme"}
    db.session.commit()  # Synthetic precondition, no UoW has started.
    import socket_service
    import services.email_service as email_service
    monkeypatch.setattr(socket_service, "emit_new_chat_message", lambda *_a: None)
    monkeypatch.setattr(email_service, "enviar_email_ticket_admin", lambda *_a, **_k: True)
    sink = CollectingConversationSink(supports_media=False)
    prepared = common.prepare_conversation_turn(_turn(env), sink,
        on_failure=lambda *_a: (_ for _ in ()).throw(AssertionError("legacy_human_failed")),
        log=lambda *_a, **_k: None, safe_reference=lambda *_a: "synthetic")
    assert prepared.handled and prepared.legacy_return == ("OK", 200)
    assert sink.snapshot() == () and TicketComentario.query.count() == 1
    assert WhatsAppOutboundAttempt.query.count() == 0


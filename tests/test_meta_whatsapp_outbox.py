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
        META_WHATSAPP_OUTBOUND_ENABLED=True,
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


def test_verified_reply_reaches_meta_through_existing_outbox_worker_without_twilio(environment):
    env = environment
    receipt = produce_meta_service_reply(env.event,
        MetaReplyContent(kind="text", body="Synthetic calculated service reply"), runtime=env.runtime)
    assert receipt.outcome == "created" and receipt.committed is True
    assert len(receipt.outbound_attempt_ids) == 1
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=receipt.turn_id).one()
    attempt = WhatsAppOutboundAttempt.query.filter_by(attempt_id=receipt.outbound_attempt_ids[0]).one()
    assert turn.provider == attempt.provider == "meta"
    assert turn.status == "completed" and attempt.status == "pending"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "accepted", "A verified Meta outbox row must dispatch via Meta, not Twilio credentials"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0
    db.session.expire_all()
    stored = WhatsAppOutboundAttempt.query.filter_by(attempt_id=attempt.attempt_id).one()
    assert stored.status == "accepted" and stored.provider_message_sid == ACK
    again = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert again.status == "idle"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def _stage(env, reply=None, event=None):
    return produce_meta_service_reply(event or env.event,
        reply if reply is not None else MetaReplyContent("text", "Calculated reply"),
        runtime=env.runtime)


def _row(attempt_id):
    db.session.expire_all()
    return WhatsAppOutboundAttempt.query.filter_by(attempt_id=attempt_id).one()


def _use_runtime(env, **changes):
    env.runtime = replace(env.runtime, **changes)
    env.app.extensions[RUNTIME_EXTENSION] = env.runtime


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


def test_duplicate_bundle_is_exact_and_has_no_second_turn_or_send(environment):
    env = environment
    bundle = MetaReplyBundle((MetaReplyContent("text", "Greeting"), MetaReplyContent("text", "Menu")))
    first = _stage(env, bundle)
    again = _stage(env, bundle)
    assert again.outcome == "duplicate" and again.outbound_attempt_ids == first.outbound_attempt_ids
    assert WhatsAppInboundTurn.query.count() == 1 and WhatsAppOutboundAttempt.query.count() == 2
    with pytest.raises(MetaRuntimeError, match="meta_reply_event_conflict"):
        _stage(env, MetaReplyBundle(tuple(reversed(bundle.messages))))
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_bundle_fifo_preserves_order_and_individual_wamids(environment):
    env = environment
    receipt = _stage(env, MetaReplyBundle((MetaReplyContent("text", "First"), MetaReplyContent("text", "Second"))))
    first = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert first.status == "accepted"
    assert _row(receipt.outbound_attempt_ids[1]).status == "pending"
    second = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert second.status == "accepted"
    assert [c["json"]["text"]["body"] for c in env.calls["post"]] == ["First", "Second"]
    assert [_row(i).provider_message_sid for i in receipt.outbound_attempt_ids] == [ACK, "wamid.synthetic/2+="]
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert env.calls["twilio"] == 0


@pytest.mark.parametrize("mode", ["timeout", "empty4xx", "malformed2xx", "invalid_wamid"])
def test_ambiguous_first_message_quarantines_and_blocks_second(environment, mode):
    env = environment
    original = env.runtime.post
    def ambiguous(*args, **kwargs):
        original(*args, **kwargs)
        if mode == "timeout":
            raise TimeoutError("synthetic only")
        if mode == "empty4xx":
            return env.response({}, 400)
        if mode == "invalid_wamid":
            return env.response({"messaging_product": "whatsapp", "messages": [{"id": "invalid id"}]})
        return env.response({})
    _use_runtime(env, post=ambiguous)
    receipt = _stage(env, MetaReplyBundle((MetaReplyContent("text", "First"), MetaReplyContent("text", "Second"))))
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "send_uncertain"
    assert _row(receipt.outbound_attempt_ids[0]).status == "send_uncertain"
    assert _row(receipt.outbound_attempt_ids[1]).status == "pending"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def test_structured_provider_rejection_cancels_only_unsent_bundle_tail(environment):
    env = environment
    original = env.runtime.post
    def denied(*args, **kwargs):
        original(*args, **kwargs)
        return env.response({"error": {"code": 131047, "message": "not logged"}}, 400)
    _use_runtime(env, post=denied)
    receipt = _stage(env, MetaReplyBundle((MetaReplyContent("text", "First"), MetaReplyContent("text", "Second"))))
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead"
    assert [_row(i).status for i in receipt.outbound_attempt_ids] == ["dead", "cancelled"]
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def test_db_ack_failure_after_post_uses_same_outer_hook_and_never_retries(environment, monkeypatch):
    env = environment
    receipt = _stage(env)
    def broken_ack(*_args):
        raise RuntimeError("synthetic DB ACK failure")
    def broken_inner_uncertain(*_args):
        raise RuntimeError("synthetic inner persistence failure")
    monkeypatch.setattr(meta_outbox, "_accept_meta_attempt", broken_ack)
    monkeypatch.setattr(meta_outbox, "uncertain_whatsapp_outbound_attempt", broken_inner_uncertain)
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "send_uncertain"  # outer worker persists via its own hook
    assert _row(receipt.outbound_attempt_ids[0]).status == "send_uncertain"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def test_advancing_clock_across_authority_gets_producer_and_dispatch(environment, monkeypatch):
    env = environment
    original = env.runtime.get
    def advancing(*args, **kwargs):
        answer = original(*args, **kwargs)
        env.clock[0] += 2
        return answer
    _use_runtime(env, get=advancing)
    monkeypatch.setattr(turns, "_utc_now", lambda: datetime.fromtimestamp(env.clock[0], timezone.utc))
    receipt = _stage(env)
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "accepted" and env.clock[0] >= env.now + 12
    assert _row(receipt.outbound_attempt_ids[0]).provider_message_sid == ACK
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


@pytest.mark.parametrize("revoked", ["sender", "connection", "credential", "subscription", "canary"])
def test_retired_authority_never_reaches_post_or_twilio(environment, revoked):
    env = environment
    receipt = _stage(env)
    if revoked == "sender":
        env.sender.status = "OFFLINE"
    elif revoked == "connection":
        env.connection.status = "disconnected"
    elif revoked == "credential":
        env.connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=env.connection, tenant_id=env.tenant.id,
            access_token=secrets.token_urlsafe(64), revision=2,
            expires_at=env.now + 3600, now=env.now, app_config=env.app.config)}
    elif revoked == "subscription":
        original = env.runtime.get
        def missing(method, url, **kwargs):
            return env.response({"data": []}) if url.endswith("/subscribed_apps") else original(method, url, **kwargs)
        _use_runtime(env, get=missing)
    else:
        env.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = "99999"
    db.session.commit()
    if revoked == "canary":
        from services.whatsapp_inbound_durability import WhatsAppInboundDurabilityConfigurationError
        with pytest.raises(WhatsAppInboundDurabilityConfigurationError):
            worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    else:
        assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "dead"
    assert _row(receipt.outbound_attempt_ids[0]).status == ("pending" if revoked == "canary" else "dead")
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("which", ["future", "exact24h"])
def test_service_window_boundaries_fail_before_new_rows(environment, which):
    env = environment
    event = replace(env.event, timestamp=env.now + 1 if which == "future" else env.now - 86400)
    with pytest.raises(MetaRuntimeError, match="meta_service_reply_window_invalid"):
        _stage(env, event=event)
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == []


def test_payload_is_copied_and_complete_bundle_rejects_media_without_partial_rows(environment):
    env = environment
    rows = [{"id": "one", "title": "First choice"}]
    receipt = _stage(env, MetaReplyContent("interactive_list", "Choose", rows=rows))
    rows[0]["title"] = "Changed outside persisted envelope"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert env.calls["post"][0]["json"]["interactive"]["action"]["sections"][0]["rows"][0]["title"] == "First choice"
    other = replace(env.event, message_id="wamid.other", event_key="meta:" + "b" * 64)
    with pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported"):
        _stage(env, MetaReplyBundle((MetaReplyContent("text", "Valid first"), MetaReplyContent("audio", "unsupported"))), other)
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 1
    assert _row(receipt.outbound_attempt_ids[0]).status == "accepted"


def test_empty_bundle_completes_same_claim_and_has_no_delivery_claim(environment):
    env = environment
    claim = _pending_claim(env)
    result = complete_meta_service_reply(claim, MetaReplyBundle(()), runtime=env.runtime)
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=claim.turn_id).one()
    assert result.turn_id == claim.turn_id and result.outbound_attempt_ids == ()
    assert turn.status == "completed" and turn.result_json["outbound_count"] == 0
    assert turn.result_json["reply_staged"] is False and turn.result_json["delivery_verified"] is False
    assert "input" not in turn.payload_json and env.event.text not in json.dumps(turn.payload_json)
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("finish", ["rollback", "commit"])
def test_external_session_flushed_business_write_and_completion_are_one_transaction(environment, finish):
    env = environment
    claim = _pending_claim(env)
    owner_id, old_name = env.owner.id, env.owner.name
    with Session(bind=db.engine, expire_on_commit=False) as session:
        owner = session.get(User, owner_id)
        owner.name = "Explicitly flushed synthetic business effect"
        session.flush()
        result = complete_meta_service_reply(claim, MetaReplyContent("text", "Reply"),
                                             runtime=env.runtime, session=session)
        assert result.committed is False and result.turn_id == claim.turn_id
        assert len(result.outbound_attempt_ids) == 1
        getattr(session, finish)()
    db.session.expire_all()
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=claim.turn_id).one()
    if finish == "rollback":
        assert db.session.get(User, owner_id).name == old_name
        assert turn.status == "processing" and turn.payload_json == claim.payload
        assert WhatsAppOutboundAttempt.query.count() == 0
    else:
        assert db.session.get(User, owner_id).name == "Explicitly flushed synthetic business effect"
        assert turn.status == "completed" and WhatsAppOutboundAttempt.query.count() == 1
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_unflushed_caller_work_is_not_flushed_or_rolled_back(environment):
    env = environment
    with Session(bind=db.engine) as session:
        owner = session.get(User, env.owner.id)
        owner.name = "Caller owns this pending edit"
        with pytest.raises(MetaRuntimeError, match="meta_outbox_session_has_pending_work"):
            produce_meta_service_reply(env.event, MetaReplyContent("text", "Reply"),
                                        runtime=env.runtime, session=session)
        assert owner in session.dirty and owner.name == "Caller owns this pending edit"
        session.rollback()
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_claim_payload_tampering_fails_before_send(environment):
    env = environment
    receipt = _stage(env)
    claim = turns.claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert claim is not None
    invalid = replace(claim, payload={**claim.payload, "message": {"to": "5499999999999"}})
    result = meta_outbox.dispatch_meta_outbound_claim(invalid, on_post_started=lambda: None, runtime=env.runtime)
    assert result.status == "dead" and env.calls["post"] == []
    assert _row(receipt.outbound_attempt_ids[0]).status == "dead"


@pytest.mark.parametrize("rule", ["blocked", "rate", "quiet"])
def test_existing_enterprise_rules_are_enforced_before_meta_post(environment, rule):
    from models import WhatsAppEnterpriseRule
    from utils.time_utils import get_local_now
    env = environment
    record = WhatsAppEnterpriseRule(tenant_id=env.tenant.id)
    if rule == "blocked":
        record.blocked_keywords = ["calculated"]
    elif rule == "rate":
        record.max_outbound_per_hour = 1
    else:
        record.quiet_hours_start = get_local_now().hour
        record.quiet_hours_end = (get_local_now().hour + 1) % 24
    db.session.add(record)
    db.session.commit()
    receipt = _stage(env, MetaReplyBundle((MetaReplyContent("text", "Calculated first"), MetaReplyContent("text", "Calculated second"))))
    first = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    if rule == "rate":
        assert first.status == "accepted"
        assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "dead"
        assert len(env.calls["post"]) == 1
    else:
        assert first.status == "dead" and env.calls["post"] == []
    assert env.calls["twilio"] == 0


def test_native_adapter_denies_testing_before_socket(environment, monkeypatch):
    import services.meta_whatsapp_runtime as runtime_module
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Native socket must never be constructed")
    monkeypatch.setattr(runtime_module.http.client, "HTTPSConnection", no_socket)
    with pytest.raises(MetaRuntimeError, match="meta_native_network_disabled_in_tests"):
        native_graph_request("GET", "https://graph.facebook.com/v25.0/debug_token", allow_redirects=False, verify=True)


def test_native_adapter_requires_app_context_before_socket(monkeypatch):
    import services.meta_whatsapp_runtime as runtime_module
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Native socket must never be constructed")
    monkeypatch.setattr(runtime_module.http.client, "HTTPSConnection", no_socket)
    with pytest.raises(MetaRuntimeError, match="meta_graph_app_context_required"):
        native_graph_request("GET", "https://graph.facebook.com/v25.0/debug_token", allow_redirects=False, verify=True)


def test_unknown_persisted_provider_with_twilio_connection_is_not_twilio_fallback(environment):
    env = environment
    env.app.config.update(TWILIO_ACCOUNT_SID="AC" + "1" * 32,
        TWILIO_AUTH_TOKEN="synthetic-twilio-token", PUBLIC_API_BASE_URL="https://api.example.invalid")
    connection = ProviderConnection(tenant_id=env.tenant.id, provider="twilio", channel="whatsapp",
        environment="production", status="connected", external_account_id=env.app.config["TWILIO_ACCOUNT_SID"], config={})
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(tenant_id=env.tenant.id, provider_connection_id=connection.id,
        channel="whatsapp", phone_number="+15005550001", sender_id="whatsapp:+15005550001", status="active")
    db.session.add(sender)
    db.session.flush()
    assert worker.resolve_twilio_runtime_credentials(tenant=env.tenant,
        provider_connection=connection, app_config=env.app.config).ready is True
    now = datetime.fromtimestamp(env.now, timezone.utc)
    turn = WhatsAppInboundTurn(turn_id=str(uuid.uuid4()), tenant_id=env.tenant.id,
        provider="unsupported", provider_connection_id=connection.id, provider_sender_id=sender.id,
        provider_message_sid="synthetic-event", stream_key="unknown-stream", message_kind="text",
        payload_digest="a" * 64, payload_json={}, status="completed", received_at=now,
        available_at=now, created_at=now, updated_at=now, completed_at=now)
    db.session.add(turn)
    db.session.flush()
    row = WhatsAppOutboundAttempt(attempt_id=str(uuid.uuid4()), tenant_id=env.tenant.id,
        inbound_turn_id=turn.id, provider="unsupported", provider_connection_id=connection.id, provider_sender_id=sender.id,
        stream_key=turn.stream_key, sequence_no=1, message_kind="text", idempotency_key="unknown-provider-key",
        payload_digest="b" * 64, payload_json={"body": "Never to Twilio",
            "from_": "whatsapp:+15005550001", "to": "whatsapp:+5491112345678"}, status="pending",
        available_at=now, created_at=now, updated_at=now)
    db.session.add(row)
    db.session.commit()
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead" and _row(row.attempt_id).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_canary_removed_during_authority_get_is_rechecked_before_post(environment):
    env = environment
    receipt = _stage(env)
    original = env.runtime.get
    def withdraw(method, url, **kwargs):
        result = original(method, url, **kwargs)
        if url.endswith("/subscribed_apps"):
            env.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = "99999"
        return result
    _use_runtime(env, get=withdraw)
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead" and _row(receipt.outbound_attempt_ids[0]).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_claimed_inbox_expired_lease_is_not_completed(environment):
    env = environment
    claim = _pending_claim(env)
    env.clock[0] += 181
    with pytest.raises(MetaRuntimeError, match="meta_turn_lease_not_owned"):
        complete_meta_service_reply(claim, MetaReplyContent("text", "Reply"), runtime=env.runtime)
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


@pytest.mark.parametrize("field", ["data_access_expires_at", "granular_scopes", "app_id"])
def test_debug_token_authority_fields_fail_closed_without_post(environment, field):
    env = environment
    original = env.runtime.get
    def invalid(method, url, **kwargs):
        result = original(method, url, **kwargs)
        if url.endswith("/debug_token"):
            value = json.loads(result.content)
            value["data"][field] = (env.now if field == "data_access_expires_at"
                else [] if field == "granular_scopes" else "999999")
            return env.response(value)
        return result
    _use_runtime(env, get=invalid)
    with pytest.raises(MetaRuntimeError):
        _stage(env)
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == []


def test_native_stream_read1_rechecks_budget_between_chunks_without_socket(environment, monkeypatch):
    import services.meta_whatsapp_runtime as runtime_module
    from services.outbox_execution_budget import activate_outbox_execution_budget
    env = environment
    env.app.config["TESTING"] = False
    monkeypatch.setenv("TESTING", "0")
    mono = [0.0]
    observed = {"reads": 0, "closed": False}
    class Response:
        status = 200
        def read(self, _n):
            raise AssertionError("read() can hide a slow stream; read1() is required")
        def read1(self, _n):
            observed["reads"] += 1
            mono[0] += 5
            return b"{"
    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.sock = SimpleNamespace(settimeout=lambda _timeout: None)
        def set_debuglevel(self, _level): pass
        def connect(self): pass
        def request(self, *_args, **_kwargs): pass
        def getresponse(self): return Response()
        def close(self): observed["closed"] = True
    monkeypatch.setattr(runtime_module.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: mono[0])
    with activate_outbox_execution_budget(deadline_monotonic=10.0, clock=lambda: mono[0]):
        with pytest.raises(MetaRuntimeError, match="meta_graph_transport_unavailable"):
            native_graph_request("GET", "https://graph.facebook.com/v25.0/debug_token", allow_redirects=False, verify=True)
    assert observed == {"reads": 1, "closed": True}


def test_bundle_bound_is_explicit_and_does_not_truncate(environment):
    env = environment
    with pytest.raises(MetaRuntimeError, match="meta_reply_bundle_too_large"):
        _stage(env, MetaReplyBundle(tuple(MetaReplyContent("text", "Reply") for _ in range(65))))
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == []


@pytest.mark.parametrize("changed", ["recipient", "tenant"])
def test_persisted_envelope_cannot_replace_signed_recipient_or_scope(environment, changed):
    env = environment
    receipt = _stage(env)
    row = _row(receipt.outbound_attempt_ids[0])
    payload = copy.deepcopy(row.payload_json)
    if changed == "recipient":
        # Also alter source and digest: the immutable parent turn must still
        # prevent an internally consistent but foreign recipient substitution.
        payload["source"]["recipient"] = "5499999999999"
        payload["message"]["to"] = "5499999999999"
    else:
        payload["identity"]["tenant_id"] = env.tenant.id + 999
    row.payload_json = payload
    row.payload_digest = _canonical(payload)
    db.session.commit()
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead"
    assert _row(receipt.outbound_attempt_ids[0]).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_lost_lease_after_post_preserves_uncertainty_without_resend(environment):
    from datetime import timedelta
    from sqlalchemy import update
    env = environment
    receipt = _stage(env)
    original = env.runtime.post
    def lose_lease(*args, **kwargs):
        response = original(*args, **kwargs)
        with Session(bind=db.engine) as session:
            session.execute(update(WhatsAppOutboundAttempt).where(
                WhatsAppOutboundAttempt.attempt_id == receipt.outbound_attempt_ids[0]).values(
                    lease_token="replacement-owner",
                    leased_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
            session.commit()
        return response
    _use_runtime(env, post=lose_lease)
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "lost_lease"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    row = _row(receipt.outbound_attempt_ids[0])
    assert row.status == "send_uncertain" and row.provider_message_sid is None
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def test_same_waba_two_phone_bindings_keep_tenants_recipients_and_outboxes_separate(environment):
    from services.meta_whatsapp_cloud import MetaSenderSnapshot
    env = environment
    other_phone, other_contact = "300002", "5491112345679"
    owner = User(name="Synthetic second owner", email="second-meta@example.invalid",
                 rol="admin", tipo_chat="pyme", password_hash="synthetic-not-a-login")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="meta-second-synthetic", nombre="Synthetic second institution",
                           tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
    connection = ProviderConnection(tenant_id=tenant.id, provider="meta", channel="whatsapp",
        environment="production", status="connected", external_app_id=APP,
        external_account_id=WABA, credentials_ref=vault.VAULT_REF, config={})
    db.session.add(connection)
    db.session.flush()
    connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(
        connection=connection, tenant_id=tenant.id, access_token=secrets.token_urlsafe(64),
        revision=1, expires_at=env.now + 3600, now=env.now, app_config=env.app.config)}
    sender = ProviderSender(tenant_id=tenant.id, provider_connection_id=connection.id,
        channel="whatsapp", phone_number_id=other_phone, waba_id=WABA, status="ONLINE")
    db.session.add(sender)
    db.session.commit()
    env.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = f"{env.tenant.id},{tenant.id}"
    original = env.runtime.get
    def shared_waba(method, url, **kwargs):
        if url.endswith("/phone_numbers"):
            return env.response({"data": [{"id": PHONE}, {"id": other_phone}]})
        return original(method, url, **kwargs)
    def post(method, url, **kwargs):
        assert method == "POST" and kwargs["allow_redirects"] is False and kwargs["verify"] is True
        assert url in {f"https://graph.facebook.com/v25.0/{p}/messages" for p in (PHONE, other_phone)}
        env.calls["post"].append({"url": url, "json": copy.deepcopy(kwargs["json"])})
        return env.response({"messaging_product": "whatsapp", "messages": [{"id": ACK}]})
    _use_runtime(env, get=shared_waba, post=post)
    identities = {PHONE: env.event.sender,
        other_phone: MetaSenderSnapshot(tenant.id, connection.id, sender.id, APP, WABA, other_phone, "production")}
    # One correctly signed real parser batch, shared WABA, two distinct PhoneIDs.
    changes = []
    for phone, recipient, suffix in [(PHONE, CONTACT, "first"), (other_phone, other_contact, "second")]:
        changes.append({"field": "messages", "value": {"messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone}, "messages": [{"from": recipient,
                "id": "wamid.shared/" + suffix, "timestamp": str(env.now - 20),
                "type": "text", "text": {"body": "Synthetic shared WABA input"}}]}})
    raw = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": changes}]}).encode()
    secret = env.app.config[APP_SECRETS_KEY][APP]
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    parsed = webhook.parse_webhook(raw_body=raw, signature=signature, app_secret=secret,
        app_id=APP, now=env.now, binding_resolver=lambda phone, app_id, waba:
            env.runtime.load_binding(identities[phone]) if app_id == APP and waba == WABA
            else (_ for _ in ()).throw(AssertionError("Foreign tuple")))
    assert len(parsed) == 2
    receipts = [_stage(env, MetaReplyContent("text", "Scoped service reply"), item) for item in parsed]
    rows = [_row(r.outbound_attempt_ids[0]) for r in receipts]
    assert [row.tenant_id for row in rows] == [env.tenant.id, tenant.id]
    assert [row.provider_sender_id for row in rows] == [env.sender.id, sender.id]
    assert [row.payload_json["message"]["to"] for row in rows] == [CONTACT, other_contact]
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert _row(receipts[1].outbound_attempt_ids[0]).status == "pending"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant.id).status == "accepted"
    assert [(call["url"].split("/")[-2], call["json"]["to"]) for call in env.calls["post"]] == [
        (PHONE, CONTACT), (other_phone, other_contact)]
    assert WhatsAppInboundTurn.query.count() == 2 and WhatsAppOutboundAttempt.query.count() == 2
    assert env.calls["twilio"] == 0

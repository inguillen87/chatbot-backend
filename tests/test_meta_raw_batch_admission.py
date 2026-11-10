"""New RAW/ORM admission cases; never import or collect a closed test suite.

Graph GETs, identities, app secret, credentials and accepted WAMIDs are synthetic.
SQLite tests do not establish PostgreSQL lock/concurrency behavior or HTTP ACK.
"""
import base64
import copy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace
import uuid

from flask import Flask
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import InvalidRequestError, PendingRollbackError

from database import db
from models import User, TenantProfile, ProviderConnection, ProviderSender
from models import ChannelSessionIdentityBinding, ChatSessionContext, WhatsAppInboundTurn, WhatsAppOutboundAttempt
from services import meta_whatsapp_credentials as vault
from services import meta_whatsapp_batch_admission as batch
from services import meta_whatsapp_webhook as webhook
from services import whatsapp_inbound_turns as turns
from services.meta_whatsapp_outbox import MetaReplyBundle, MetaReplyContent, complete_meta_service_reply
from services.meta_whatsapp_runtime import APP_SECRETS_KEY, MetaOutboundRuntime


APP = "100001"
PHONE = "12025550101"
NOW = int(datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc).timestamp())


def _canary_entry(scope, *, app_id=APP, contact=PHONE):
    return {"app_id": app_id, "waba_id": scope.waba, "phone_number_id": scope.phone_id,
            "tenant_id": scope.tenant, "contact": contact}


@pytest.fixture
def raw_env():
    # New factory based on the current models and synthetic credential contract;
    # no imports/pytest inheritance from test_meta_whatsapp_outbox or identity19.
    app = Flask("synthetic-meta-raw-batch")
    secret = secrets.token_hex(32)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False, CUTOVER_WRITER_FENCE_ENABLED=False,
        TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="synthetic",
        TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({
            "synthetic": base64.b64encode(secrets.token_bytes(32)).decode()}),
        WHATSAPP_INBOUND_DURABILITY_MODE="queue", WHATSAPP_INBOUND_HASH_SECRET=secrets.token_hex(32),
        CHANNEL_SESSION_IDENTITY_MODE="enforce", CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=secrets.token_hex(32),
        META_WHATSAPP_GRAPH_VERSION="v25.0", **{APP_SECRETS_KEY: {APP: secret}})
    db.init_app(app)
    with app.app_context():
        assert str(db.engine.url) == "sqlite:///:memory:"
        db.create_all()
        scopes, token_scopes = [], {}
        for index, label in enumerate(("a", "b"), 1):
            owner = User(name="Synthetic owner " + label, email="raw-" + label + "@example.invalid",
                rol="admin", tipo_chat="pyme", password_hash="synthetic-not-a-login")
            db.session.add(owner)
            db.session.flush()
            tenant = TenantProfile(slug="synthetic-raw-" + label, nombre="Synthetic " + label,
                tipo="pyme", pyme_id=owner.id, is_active=True)
            db.session.add(tenant)
            db.session.flush()
            owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
            waba, phone_id = str(200000 + index), str(300000 + index)
            connection = ProviderConnection(tenant_id=tenant.id, provider="meta", channel="whatsapp",
                environment="production", status="connected", external_app_id=APP,
                external_account_id=waba, credentials_ref=vault.VAULT_REF, config={})
            db.session.add(connection)
            db.session.flush()
            token = secrets.token_urlsafe(64)
            connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(connection=connection,
                tenant_id=tenant.id, access_token=token, revision=1, expires_at=NOW + 300000,
                now=NOW, app_config=app.config)}
            sender = ProviderSender(tenant_id=tenant.id, provider_connection_id=connection.id,
                channel="whatsapp", phone_number_id=phone_id, waba_id=waba, status="ONLINE")
            db.session.add(sender)
            db.session.flush()
            scope = SimpleNamespace(owner=owner.id, tenant=tenant.id, connection=connection.id,
                sender=sender.id, waba=waba, phone_id=phone_id)
            scopes.append(scope)
            token_scopes[token] = scope
        db.session.commit()
        app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = ",".join(str(s.tenant) for s in scopes)
        app.config[batch.CANARY_RECIPIENTS_KEY] = json.dumps([_canary_entry(scope) for scope in scopes])
        trace, clock, post = [], [NOW], []
        def response(value):
            return SimpleNamespace(status_code=200, content=json.dumps(value).encode())
        def get(method, url, **kwargs):
            assert method == "GET" and kwargs["allow_redirects"] is False and kwargs["verify"] is True
            trace.append("GET")
            path = url.split("graph.facebook.com/", 1)[1]
            if path == "v25.0/debug_token":
                assert kwargs["headers"]["Authorization"] == "Bearer " + APP + "|" + secret
                scope = token_scopes[kwargs["params"]["input_token"]]
                return response({"data": {"is_valid": True, "app_id": APP,
                    "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"],
                    "expires_at": NOW + 300000, "data_access_expires_at": NOW + 300000,
                    "granular_scopes": [
                        {"scope": "whatsapp_business_management", "target_ids": [scope.waba]},
                        {"scope": "whatsapp_business_messaging", "target_ids": [scope.waba]}]}})
            scope = token_scopes[kwargs["headers"]["Authorization"].removeprefix("Bearer ")]
            if path == "v25.0/" + scope.waba + "/phone_numbers":
                return response({"data": [{"id": scope.phone_id}]})
            if path == "v25.0/" + scope.waba + "/subscribed_apps":
                return response({"data": [{"whatsapp_business_api_data": {"id": APP}}]})
            raise AssertionError("Unplanned synthetic GET")
        def no_post(*_args, **_kwargs):
            post.append("forbidden")
            raise AssertionError("RAW admission cannot POST")
        runtime = MetaOutboundRuntime(app.config, get=get, post=no_post, clock=lambda: clock[0])
        def sql(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                trace.append("DML")
        def orm(state):
            if getattr(state.statement, "_for_update_arg", None) is not None:
                trace.append("LOCK_INTENT")  # SQLite does not enforce this lock.
        active = db.session()
        event.listen(db.engine, "before_cursor_execute", sql)
        event.listen(active, "do_orm_execute", orm)
        env = SimpleNamespace(app=app, secret=secret, scopes=scopes, a=scopes[0], b=scopes[1],
            session=active, runtime=runtime, trace=trace, clock=clock, posts=post)
        try:
            yield env
        finally:
            active.rollback()
            event.remove(active, "do_orm_execute", orm)
            event.remove(db.engine, "before_cursor_execute", sql)
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def _begin(env):
    if not env.session.connection().connection.driver_connection.in_transaction:
        env.session.execute(text("BEGIN"))
    assert not env.session.in_nested_transaction()
    return env.session.get_transaction()


def _message(suffix="one", *, phone=PHONE, stamp=NOW - 30, kind="text"):
    result = {"from": phone, "id": "wamid.raw/" + suffix + "+=", "timestamp": str(stamp), "type": kind}
    if kind == "text":
        result["text"] = {"body": "Synthetic inquiry " + suffix}
    elif kind == "location":
        result["location"] = {"latitude": 0, "longitude": -68.3}
    elif kind == "interactive":
        result["interactive"] = {"type": "list_reply", "list_reply": {"id": "synthetic-selection"}}
    else:
        result[kind] = {"id": "synthetic-media"}
    return result


def _status(wamid, state, *, phone=PHONE, stamp=NOW - 10):
    return {"id": wamid, "recipient_id": phone, "timestamp": str(stamp), "status": state}


def _document(*changes):
    # changes are trusted synthetic factory inputs, never a replacement parser.
    entries = []
    for scope, messages, statuses in changes:
        value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": scope.phone_id}}
        if messages:
            value["messages"] = messages
        if statuses:
            value["statuses"] = statuses
        entries.append({"id": scope.waba, "changes": [{"field": "messages", "value": value}]})
    return {"object": "whatsapp_business_account", "entry": entries}


def _signed(env, document):
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(env.secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, signature


def _stage(env, document, *, signature=None):
    raw, computed = _signed(env, document)
    transaction = _begin(env)
    return batch.stage_meta_webhook_batch(env.session, transaction, raw_body=raw,
        signature=computed if signature is None else signature, app_id=APP, runtime=env.runtime)


def _counts(env):
    with env.session.no_autoflush:
        return tuple(len(env.session.scalars(select(model)).all()) for model in
            (ChannelSessionIdentityBinding, ChatSessionContext, WhatsAppInboundTurn))


def _snapshot(row):
    return {column.name: copy.deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def _accepted(env, *, count=1, scope=None):
    """Real admission+claim+completion; provider ACK state is a declared fixture."""
    scope = env.a if scope is None else scope
    document = _document((scope, [_message()], []))
    _stage(env, document)
    env.session.commit()
    claim = turns.claim_next_whatsapp_inbound_turn(tenant_id=scope.tenant,
        now=datetime.fromtimestamp(NOW, timezone.utc))
    assert claim is not None and claim.provider == "meta"
    complete_meta_service_reply(claim, MetaReplyBundle(tuple(
        MetaReplyContent("text", "Synthetic reply " + str(i)) for i in range(count))), runtime=env.runtime)
    env.session.expire_all()
    rows = env.session.scalars(select(WhatsAppOutboundAttempt).where(
        WhatsAppOutboundAttempt.tenant_id == scope.tenant).order_by(WhatsAppOutboundAttempt.id)).all()
    # No Graph POST occurred. These are synthetic ACK fixtures, not delivery.
    first = rows[0]
    first.status, first.provider_status = "accepted", "accepted"
    first.provider_message_sid = "wamid.synthetic/ACK+="
    first.accepted_at = first.completed_at = datetime.fromtimestamp(NOW, timezone.utc)
    env.session.commit()
    env.trace.clear()
    return document, first.id


@pytest.mark.parametrize("owner_action", ("commit", "rollback"))
def test_signed_whole_batch_stages_identity_before_inbox_and_owner_controls_ack(raw_env, owner_action):
    e = raw_env
    commits = []
    def committed(_session):
        commits.append("owner")
    event.listen(e.session, "after_commit", committed)
    try:
        e.trace.clear()
        result = _stage(e, _document((e.a, [_message("two"), _message("one", stamp=NOW - 31)], [])))
        assert result.committed is False and len(result.events) == 2
        assert all(item.outcome == "staged" for item in result.events)
        assert _counts(e) == (1, 1, 2) and commits == [] and e.posts == []
        rows = e.session.scalars(select(WhatsAppInboundTurn).order_by(WhatsAppInboundTurn.id)).all()
        assert all(row.session_identity_binding_id and row.chat_session_id for row in rows)
        assert rows[0].provider_message_sid == "wamid.raw/one+="
        assert max(i for i, entry in enumerate(e.trace) if entry == "GET") < e.trace.index("LOCK_INTENT")
        assert e.trace.index("LOCK_INTENT") < e.trace.index("DML")
        getattr(e.session, owner_action)()
        assert len(commits) == (1 if owner_action == "commit" else 0)
        # A future route may emit ACK only after its own successful commit.
        assert _counts(e) == ((1, 1, 2) if owner_action == "commit" else (0, 0, 0))
    finally:
        event.remove(e.session, "after_commit", committed)


@pytest.mark.parametrize("failure", ("signature", "second_content", "second_scope", "unknown_status", "future", "new_expired"))
def test_whole_batch_invalid_before_first_dml(raw_env, failure):
    e = raw_env
    document = _document((e.a, [_message()], []))
    signature = None
    if failure == "signature":
        signature = "sha256=" + "0" * 64
    elif failure == "second_content":
        document = _document((e.a, [_message(), _message("bad", kind="image")], []))
    elif failure == "second_scope":
        e.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(e.a.tenant)
        document = _document((e.a, [_message()], []), (e.b, [_message("b")], []))
    elif failure == "unknown_status":
        document = _document((e.a, [_message()], [_status("wamid.unknown+=", "delivered")]))
    else:
        stamp = NOW + 1 if failure == "future" else NOW - 86400
        document = _document((e.a, [_message(), _message("invalid-time", stamp=stamp)], []))
    e.trace.clear()
    with pytest.raises(Exception):
        _stage(e, document, signature=signature)
    assert "DML" not in e.trace and _counts(e) == (0, 0, 0) and e.posts == []
    if failure == "signature":
        assert "GET" not in e.trace
    with pytest.raises(batch.MetaBatchAdmissionError, match="owner_rollback_required"):
        e.session.commit()
    e.session.rollback()


def test_real_unique_failure_in_second_event_poisoned_until_whole_owner_rollback(raw_env, monkeypatch):
    e = raw_env
    # Only this module's turn UUID producer is fixed; identity UUIDs stay real.
    monkeypatch.setattr(batch, "uuid", SimpleNamespace(uuid4=lambda: uuid.UUID(int=1)))
    with pytest.raises(batch.MetaBatchAdmissionError, match="storage_failed"):
        _stage(e, _document((e.a, [_message("one"), _message("two")], [])))
    with pytest.raises((batch.MetaBatchAdmissionError, PendingRollbackError, InvalidRequestError)):
        e.session.commit()
    e.session.rollback()
    assert _counts(e) == (0, 0, 0) and e.posts == []


def test_caught_late_error_cannot_commit_first_flushed_event(raw_env, monkeypatch):
    e = raw_env
    another_contact = "56912345678"
    e.app.config[batch.CANARY_RECIPIENTS_KEY] = json.dumps([
        _canary_entry(e.a), _canary_entry(e.a, contact=another_contact), _canary_entry(e.b),
    ])
    original, calls = batch.stage_meta_channel_session_identity, []
    def second_fails(*args, **kwargs):
        calls.append(kwargs["provider_identity"])
        if len(calls) == 2:
            raise RuntimeError("synthetic second-event failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(batch, "stage_meta_channel_session_identity", second_fails)
    with pytest.raises(batch.MetaBatchAdmissionError, match="meta_batch_rejected"):
        _stage(e, _document((e.a, [_message("one"), _message("two", phone=another_contact)], [])))
    assert len(e.session.scalars(select(WhatsAppInboundTurn)).all()) == 1
    with pytest.raises(batch.MetaBatchAdmissionError, match="owner_rollback_required"):
        e.session.commit()
    e.session.rollback()
    assert _counts(e) == (0, 0, 0)
    result = _stage(e, _document((e.a, [_message("one")], [])))
    assert result.events[0].outcome == "staged"
    e.session.commit()
    assert _counts(e) == (1, 1, 1)


def test_recipient_policy_is_required_in_raw_port_and_retirement_rolls_back_staged_work(raw_env, monkeypatch):
    e = raw_env
    key = batch.CANARY_RECIPIENTS_KEY
    scope = e.app.config.pop(key)
    document = _document((e.a, [_message()], []))
    with pytest.raises(batch.MetaBatchAdmissionError, match="meta_batch_canary_scope_unavailable"):
        _stage(e, document)
    assert e.trace == [] and _counts(e) == (0, 0, 0)
    e.session.rollback()
    e.app.config[key] = scope

    original = batch.stage_meta_channel_session_identity

    def retire_after_identity(*args, **kwargs):
        result = original(*args, **kwargs)
        e.app.config[key] = json.dumps([_canary_entry(e.a, contact="12025550102")])
        return result

    monkeypatch.setattr(batch, "stage_meta_channel_session_identity", retire_after_identity)
    with pytest.raises(batch.MetaBatchAdmissionError, match="meta_batch_canary_scope_retired"):
        _stage(e, document)
    assert _counts(e) == (1, 1, 1)
    with pytest.raises(batch.MetaBatchAdmissionError, match="owner_rollback_required"):
        e.session.commit()
    e.session.rollback()
    assert _counts(e) == (0, 0, 0) and e.posts == []


def test_exact_duplicate_preserves_inbox_lease_identity_and_late_window(raw_env):
    e = raw_env
    document = _document((e.a, [_message()], []))
    _stage(e, document)
    e.session.commit()
    row = e.session.scalar(select(WhatsAppInboundTurn))
    row.status, row.lease_token = "processing", "synthetic-lease"
    row.leased_until = datetime.fromtimestamp(NOW + 30, timezone.utc)
    row.attempt_count = 1
    e.session.commit()
    before, identity_before = _snapshot(row), _snapshot(e.session.scalar(select(ChannelSessionIdentityBinding)))
    e.clock[0] = NOW + 86401
    result = _stage(e, document)
    assert result.events[0].outcome == "duplicate" and _counts(e) == (1, 1, 1)
    assert _snapshot(row) == before
    assert _snapshot(e.session.scalar(select(ChannelSessionIdentityBinding))) == identity_before
    assert e.posts == []


@pytest.mark.parametrize("scrub", (False, True))
def test_completed_duplicate_uses_real_completion_and_privacy_scrub_without_recreation(raw_env, scrub):
    e = raw_env
    document, attempt_id = _accepted(e)
    if scrub:
        e.session.rollback()
        result = turns.scrub_expired_whatsapp_inbound_payloads(tenant_id=e.a.tenant,
            now=datetime.fromtimestamp(NOW + 1, timezone.utc))
        assert result["scrubbed"] == 1
    e.session.expire_all()
    row = e.session.scalar(select(WhatsAppInboundTurn))
    before = _snapshot(row)
    identity_before = _snapshot(e.session.scalar(select(ChannelSessionIdentityBinding)))
    e.clock[0] = NOW + 86401
    result = _stage(e, document)
    assert result.events[0].outcome == "duplicate"
    assert _snapshot(row) == before and _snapshot(e.session.scalar(select(ChannelSessionIdentityBinding))) == identity_before
    assert e.session.get(WhatsAppOutboundAttempt, attempt_id).provider_message_sid == "wamid.synthetic/ACK+="
    assert e.posts == []


@pytest.mark.parametrize("kind", ("text", "location", "interactive"))
def test_same_contact_two_tenants_remains_separate_and_readable_schema_is_official(raw_env, kind):
    e = raw_env
    result = _stage(e, _document((e.b, [_message(kind=kind)], []), (e.a, [_message(kind=kind)], [])))
    rows = e.session.scalars(select(WhatsAppInboundTurn).order_by(WhatsAppInboundTurn.tenant_id)).all()
    assert len(result.events) == 2 and _counts(e) == (2, 2, 2)
    assert len({row.chat_session_id for row in rows}) == 2 and len({row.stream_key for row in rows}) == 2
    assert {row.tenant_id for row in rows} == {e.a.tenant, e.b.tenant}
    assert all(row.payload_json["reference"]["identity"]["tenant_id"] == row.tenant_id for row in rows)
    if kind == "location":
        assert all(row.payload_json["input"]["location"] == [0, -68.3] for row in rows)
    if kind == "interactive":
        assert all(row.payload_json["input"]["selection"] == "synthetic-selection" for row in rows)


def test_multi_sender_batch_accepts_fresh_authority_when_graph_crosses_a_second(raw_env):
    e = raw_env
    original = e.runtime.get

    def get(*args, **kwargs):
        response = original(*args, **kwargs)
        e.clock[0] += 1
        return response

    e.runtime = replace(e.runtime, get=get)
    result = _stage(e, _document((e.a, [_message("a")], []), (e.b, [_message("b")], [])))
    assert [item.outcome for item in result.events] == ["staged", "staged"]
    assert e.clock[0] > NOW and _counts(e) == (2, 2, 2)
    assert e.posts == []


@pytest.mark.parametrize("state", ("sent", "delivered", "read"))
def test_known_meta_wamid_status_is_scoped_no_post_and_exact_repeat_is_noop(raw_env, state):
    e = raw_env
    _document_before, identifier = _accepted(e)
    document = _document((e.a, [], [_status("wamid.synthetic/ACK+=", state)]))
    result = _stage(e, document)
    row = e.session.get(WhatsAppOutboundAttempt, identifier)
    assert result.events[0].outcome == "staged" and row.provider_status == state
    assert row.status == "accepted" and row.lease_token is None and row.leased_until is None
    e.session.commit()
    before = _snapshot(row)
    repeated = _stage(e, document)
    assert repeated.events[0].outcome == "duplicate_or_stale" and _snapshot(row) == before
    assert _counts(e) == (1, 1, 1) and e.posts == []


@pytest.mark.parametrize("bad", ("wrong_sender", "wrong_recipient", "lease"))
def test_status_cannot_correlate_by_recipient_or_cross_scope(raw_env, bad):
    e = raw_env
    _ignored, identifier = _accepted(e)
    row = e.session.get(WhatsAppOutboundAttempt, identifier)
    scope, phone = e.a, PHONE
    if bad == "wrong_sender":
        scope = e.b
    elif bad == "wrong_recipient":
        phone = "56912345678"
    elif bad == "lease":
        row.status, row.lease_token = "sending", "synthetic-send-lease"
        row.leased_until = datetime.fromtimestamp(NOW + 30, timezone.utc)
    e.session.commit()
    before = _snapshot(row)
    e.trace.clear()
    with pytest.raises(batch.MetaBatchAdmissionError):
        _stage(e, _document((scope, [], [_status("wamid.synthetic/ACK+=", "delivered", phone=phone)])))
    assert "DML" not in e.trace and _snapshot(row) == before and e.posts == []


def test_same_wamid_in_two_tenants_updates_only_the_verified_sender(raw_env):
    e = raw_env
    _, first_id = _accepted(e)
    _, second_id = _accepted(e, scope=e.b)
    first = e.session.get(WhatsAppOutboundAttempt, first_id)
    second = e.session.get(WhatsAppOutboundAttempt, second_id)
    assert first.provider_message_sid == second.provider_message_sid
    before = _snapshot(second)

    _stage(e, _document((e.a, [], [_status(first.provider_message_sid, "delivered")])))
    e.session.commit()

    assert first.provider_status == "delivered"
    assert _snapshot(second) == before and e.posts == []


def test_failed_status_cancels_only_unsent_meta_bundle_and_delivered_does_not_regress(raw_env):
    e = raw_env
    _ignored, identifier = _accepted(e, count=2)
    first = e.session.get(WhatsAppOutboundAttempt, identifier)
    second = e.session.scalar(select(WhatsAppOutboundAttempt).where(WhatsAppOutboundAttempt.sequence_no == 2))
    _stage(e, _document((e.a, [], [_status("wamid.synthetic/ACK+=", "failed")])) )
    assert first.status == "failed" and second.status == "cancelled" and e.posts == []
    e.session.rollback()
    _stage(e, _document((e.a, [], [_status("wamid.synthetic/ACK+=", "delivered")])) )
    e.session.commit()
    before = _snapshot(first)
    _stage(e, _document((e.a, [], [_status("wamid.synthetic/ACK+=", "failed")])) )
    assert _snapshot(first) == before and second.status == "pending"


@pytest.mark.parametrize("retire", ("sender", "canary", "secret"))
def test_authority_retired_during_remote_get_is_rechecked_before_dml(raw_env, retire):
    e = raw_env
    original = e.runtime.get
    changed = []
    def get(*args, **kwargs):
        response = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            if retire == "sender":
                # Direct SQL simulates persisted retirement; it is fixture DML,
                # not production admission DML. Flush-free ORM snapshot follows.
                e.session.execute(ProviderSender.__table__.update().where(ProviderSender.id == e.a.sender).values(status="OFFLINE"))
            elif retire == "canary":
                e.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(e.b.tenant)
            else:
                e.app.config[APP_SECRETS_KEY] = {APP: secrets.token_hex(32)}
        return response
    e.runtime = replace(e.runtime, get=get)
    with pytest.raises(Exception):
        _stage(e, _document((e.a, [_message()], [])))
    assert _counts(e) == (0, 0, 0) and e.posts == []


def test_pending_caller_work_is_preserved_without_flush_or_rollback(raw_env):
    e = raw_env
    transaction = _begin(e)
    owner = e.session.get(User, e.a.owner)
    owner.name = "Synthetic pending caller work"
    e.trace.clear()
    raw, signature = _signed(e, _document((e.a, [_message()], [])))
    with pytest.raises(batch.MetaBatchAdmissionError, match="pending_caller_work"):
        batch.stage_meta_webhook_batch(e.session, transaction, raw_body=raw, signature=signature,
            app_id=APP, runtime=e.runtime)
    assert owner.name == "Synthetic pending caller work" and owner in e.session.dirty
    assert e.session.get_transaction() is transaction and "DML" not in e.trace and "GET" not in e.trace

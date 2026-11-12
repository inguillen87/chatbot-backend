"""HTTP adapter checks with real admission, SQLite and synthetic Graph GETs."""

import hashlib
import hmac
import json
import secrets

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import create_app
from config import TestingConfig, _meta_callback_credentials
from database import db
from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt
from routes import meta_whatsapp_callback as callback
from services.meta_whatsapp_batch_admission import CANARY_RECIPIENTS_KEY
from services.meta_whatsapp_outbox import RUNTIME_EXTENSION
from services.meta_whatsapp_runtime import APP_SECRETS_KEY
from services.meta_whatsapp_webhook import MAX_BODY_BYTES
from services.outbox_execution_budget import OutboxExecutionBudgetExceeded
from tests.test_meta_raw_batch_admission import (
    APP, PHONE, _accepted, _canary_entry, _counts, _document, _message, _signed, _status, raw_env,
)


URL = f"/webhook/meta/whatsapp/{APP}"
OTHER_APP = "100002"


@pytest.fixture
def http_env(raw_env):
    env = raw_env
    env.verify_token = secrets.token_hex(32)
    env.other_secret = secrets.token_hex(32)
    env.app.config.update(META_WHATSAPP_CALLBACK_ENABLED=True,
        META_WHATSAPP_CALLBACK_VERIFY_TOKENS={APP: env.verify_token, OTHER_APP: secrets.token_hex(32)})
    env.app.config[APP_SECRETS_KEY][OTHER_APP] = env.other_secret
    rows = json.loads(env.app.config[CANARY_RECIPIENTS_KEY])
    env.app.config[CANARY_RECIPIENTS_KEY] = json.dumps([*rows, _canary_entry(env.a, app_id=OTHER_APP)])
    env.app.extensions[RUNTIME_EXTENSION] = env.runtime
    env.app.register_blueprint(callback.meta_whatsapp_callback_bp)
    env.client = env.app.test_client()
    return env


def _post(env, document, *, url=URL, signature=None):
    raw, computed = _signed(env, document)
    return env.client.post(url, data=raw, content_type="application/json",
        headers={"X-Hub-Signature-256": computed if signature is None else signature})


@pytest.mark.parametrize("enabled", (False, "true", True))
def test_create_app_registers_callback_only_for_explicit_boolean(enabled):
    class CallbackConfig(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        SESSION_TYPE = "null"
        RATELIMIT_STORAGE_URI = "memory://"
        META_WHATSAPP_CALLBACK_ENABLED = enabled

    app = create_app(CallbackConfig)
    assert ("meta_whatsapp_callback.callback" in app.view_functions) is (enabled is True)
    assert "tdf_meta_sandbox.callback" in app.view_functions


def test_credential_registry_rejects_duplicate_keys_values_and_malformed_entries(monkeypatch):
    name = "SYNTHETIC_CALLBACK_REGISTRY"
    secret, other = secrets.token_hex(32), secrets.token_hex(32)
    monkeypatch.setenv(name, json.dumps({APP: secret, OTHER_APP: other}))
    assert _meta_callback_credentials(name, max_length=256) == {APP: secret, OTHER_APP: other}
    for value in (f'{{"{APP}":"{secret}","{APP}":"{other}"}}',
                  json.dumps({APP: secret, OTHER_APP: secret}),
                  json.dumps({APP: secret, "wrong": other}),
                  json.dumps({APP: [secret]})):
        monkeypatch.setenv(name, value)
        assert _meta_callback_credentials(name, max_length=256) == {}


@pytest.mark.parametrize("enabled", (False, "true"))
def test_disabled_adapter_never_reads_payload_or_queries_graph(http_env, enabled):
    env = http_env
    env.app.config["META_WHATSAPP_CALLBACK_ENABLED"] = enabled
    response = _post(env, _document((env.a, [_message()], [])))
    assert response.status_code == 404 and response.headers["Cache-Control"] == "private, no-store"
    assert env.trace == [] and _counts(env) == (0, 0, 0)


def test_challenge_is_plain_text_bound_to_exact_configured_app(http_env):
    env = http_env
    query = [("hub.mode", "subscribe"), ("hub.verify_token", env.verify_token), ("hub.challenge", "000123")]
    response = env.client.get(URL, query_string=[*query, ("ignored", "inert")])
    assert response.status_code == 200 and response.mimetype == "text/plain"
    assert response.data == b"000123" and response.headers["Cache-Control"] == "private, no-store"
    assert env.client.get(URL, query_string=[*query, ("hub.challenge", "456")]).status_code == 400
    assert env.client.get(URL, query_string=query[:-1]).status_code == 400
    assert env.client.get(URL, query_string=[*query, ("ignored", "x" * 4096)]).status_code == 400
    assert env.client.get(URL.replace(APP, OTHER_APP), query_string=query).status_code == 403
    assert env.client.get(URL.replace(APP, "100003"), query_string=query).status_code == 404
    assert env.trace == [] and _counts(env) == (0, 0, 0)


@pytest.mark.parametrize("case", ("absent", "other_app_only", "duplicate", "conflicting_tenant", "wildcard", "plus", "duplicate_key"))
def test_challenge_and_post_fail_closed_without_exact_recipient_scope(http_env, case):
    env = http_env
    row = _canary_entry(env.a)
    registries = {
        "absent": None,
        "other_app_only": json.dumps([_canary_entry(env.a, app_id=OTHER_APP)]),
        "duplicate": json.dumps([row, row]),
        "conflicting_tenant": json.dumps([row, {**row, "tenant_id": env.b.tenant}]),
        "wildcard": json.dumps([{**row, "contact": "*"}]),
        "plus": json.dumps([{**row, "contact": "+" + PHONE}]),
        "duplicate_key": '[' + json.dumps(row)[:-1] + ',"contact":"12025550102"}]',
    }
    env.app.config[CANARY_RECIPIENTS_KEY] = registries[case]
    query = {"hub.mode": "subscribe", "hub.verify_token": env.verify_token, "hub.challenge": "123"}

    assert env.client.get(URL, query_string=query).status_code == 404
    assert _post(env, _document((env.a, [_message()], []))).status_code == 404
    assert env.trace == [] and _counts(env) == (0, 0, 0)


@pytest.mark.parametrize("case", ("other_contact", "other_tenant", "other_phone_id", "account_only", "mixed_account_only"))
def test_verified_request_outside_canary_scope_holds_the_entire_batch(http_env, case):
    env = http_env
    row = _canary_entry(env.a)
    env.app.config[CANARY_RECIPIENTS_KEY] = json.dumps([row])
    document = _document((env.a, [_message()], []))
    if case == "other_contact":
        document = _document((env.a, [_message(), _message("second", phone="12025550102")], []))
    elif case == "other_tenant":
        env.app.config[CANARY_RECIPIENTS_KEY] = json.dumps([{**row, "tenant_id": env.b.tenant}])
    elif case == "other_phone_id":
        document["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = env.b.phone_id
    else:
        account_change = {"field": "account_update", "value": {}}
        changes = document["entry"][0]["changes"]
        changes[:] = [account_change] if case == "account_only" else [*changes, account_change]

    response = _post(env, document)

    assert response.status_code == 503 and response.json == {"reason_code": "meta_callback_batch_held"}
    assert "DML" not in env.trace and _counts(env) == (0, 0, 0) and env.posts == []
    if case in {"other_tenant", "other_phone_id", "account_only"}:
        assert "GET" not in env.trace


@pytest.mark.parametrize("case", ("wrong_recipient", "recipient_missing", "recipient_opaque"))
def test_status_requires_the_explicit_contact_before_correlation(http_env, case):
    env = http_env
    _, attempt_id = _accepted(env)
    status = _status("wamid.synthetic/ACK+=", "delivered")
    if case == "recipient_missing":
        del status["recipient_id"]
    elif case == "recipient_opaque":
        status["recipient_user_id"] = "synthetic-opaque-identity"
    else:
        status["recipient_id"] = "12025550102"

    assert _post(env, _document((env.a, [], [status]))).status_code == 503
    assert "DML" not in env.trace and env.posts == []
    assert env.session.get(WhatsAppOutboundAttempt, attempt_id).provider_status == "accepted"


@pytest.mark.parametrize("case", ("missing_signature", "different_app_signature", "raw_bytes_changed",
                                  "query", "mime", "encoding", "oversized", "unbounded_oversized"))
def test_invalid_request_is_rejected_before_graph_or_dml(http_env, case):
    env = http_env
    raw, signature = _signed(env, _document((env.a, [_message()], [])))
    url, media = URL, "application/json"
    headers, overrides = {"X-Hub-Signature-256": signature}, {}
    expected = 403
    if case == "missing_signature":
        headers.clear()
    elif case == "different_app_signature":
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(env.other_secret.encode(), raw, hashlib.sha256).hexdigest()
    elif case == "raw_bytes_changed":
        raw += b" "
    elif case == "query":
        url += "?tenant=synthetic"
        expected = 400
    elif case == "mime":
        media, expected = "text/plain", 400
    elif case == "encoding":
        headers["Content-Encoding"] = "gzip"
        expected = 400
    else:
        raw, expected = b"x" * (MAX_BODY_BYTES + 1), 413
        if case == "unbounded_oversized":
            overrides = {"wsgi.input_terminated": True, "CONTENT_LENGTH": ""}
    response = env.client.post(url, data=raw, content_type=media, headers=headers, environ_overrides=overrides)
    assert response.status_code == expected
    assert env.trace == [] and _counts(env) == (0, 0, 0) and env.posts == []


def test_signed_batch_acknowledges_only_persisted_turns_and_redelivery_is_idempotent(http_env):
    env = http_env
    document = _document((env.a, [_message("one"), _message("two")], []))
    response = _post(env, document)
    assert response.status_code == 200 and response.data == b""
    assert response.headers["Cache-Control"] == "private, no-store"
    assert _counts(env) == (1, 1, 2) and env.posts == []
    original = set(env.session.scalars(select(WhatsAppInboundTurn.turn_id)))
    env.session.rollback()
    response = _post(env, document)
    assert response.status_code == 200 and _counts(env) == (1, 1, 2)
    assert set(env.session.scalars(select(WhatsAppInboundTurn.turn_id))) == original
    assert env.trace.count("GET") == 6 and env.posts == []


@pytest.mark.parametrize("case", ("bound_to_other_app", "mismatched_waba", "unsupported_media", "before_commit", "deadline"))
def test_signed_unaccepted_batch_returns_hold_with_no_partial_writes(http_env, monkeypatch, case):
    env = http_env
    document = _document((env.a, [_message()], []))
    url, signature = URL, None
    error_detail = "synthetic private exception details"
    if case == "bound_to_other_app":
        url = URL.replace(APP, OTHER_APP)
        raw, _ = _signed(env, document)
        signature = "sha256=" + hmac.new(env.other_secret.encode(), raw, hashlib.sha256).hexdigest()
    elif case == "mismatched_waba":
        document["entry"][0]["id"] = env.b.waba
    elif case == "unsupported_media":
        document = _document((env.a, [_message(), _message("image", kind="image")], []))

    def fail_commit(_session):
        raise RuntimeError(error_detail)

    def fail_deadline():
        raise OutboxExecutionBudgetExceeded(error_detail)

    if case == "before_commit":
        event.listen(Session, "before_commit", fail_commit)
    if case == "deadline":
        monkeypatch.setattr(callback, "require_outbox_time_remaining", fail_deadline)
    try:
        response = _post(env, document, url=url, signature=signature)
    finally:
        if case == "before_commit":
            event.remove(Session, "before_commit", fail_commit)
    assert response.status_code == 503 and response.json == {"reason_code": "meta_callback_batch_held"}
    assert _counts(env) == (0, 0, 0) and env.posts == []
    if case in {"before_commit", "deadline"}:
        assert "DML" in env.trace  # Verify rollback after real staged writes.
    elif case in {"bound_to_other_app", "mismatched_waba"}:
        assert "GET" not in env.trace


def test_ambiguous_commit_has_no_ack_and_redelivery_preserves_native_turn(http_env):
    env = http_env
    document = _document((env.a, [_message()], []))

    def after_commit(_session):
        raise RuntimeError("synthetic commit response lost")

    event.listen(Session, "after_commit", after_commit)
    try:
        response = _post(env, document)
    finally:
        event.remove(Session, "after_commit", after_commit)
    assert response.status_code == 503 and _counts(env) == (1, 1, 1)
    original = env.session.scalar(select(WhatsAppInboundTurn.turn_id))
    env.session.rollback()
    assert _post(env, document).status_code == 200
    assert _counts(env) == (1, 1, 1) and env.session.scalar(select(WhatsAppInboundTurn.turn_id)) == original
    assert env.posts == []

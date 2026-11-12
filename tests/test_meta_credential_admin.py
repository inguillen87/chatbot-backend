"""Offline HTTP auth + real SQLite persistence; no provider/Production access."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from extensions import limiter
from models import AuditEvent, AuthProviderSession, ProviderConnection, ProviderSender, User, db
from routes import meta_credential_admin as route
from services import meta_credential_admin_runtime as runtime
from services.auth_session_lifecycle import issue_token
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from utils import meta_credential_http as boundary
from utils.auth_helpers import auth_session_version
from tests.test_meta_credential_operations import environment, TOKEN, KEY, APP_SECRET


@pytest.fixture
def http(environment, monkeypatch):
    app = environment.app
    app.config.update(SECRET_KEY="synthetic-only-jwt-signing-secret", META_CREDENTIAL_ADMIN_ENABLED=True,
        AUTH_TOKEN_COOKIE_NAME="auth_token", RATELIMIT_STORAGE_URI="memory://", RATELIMIT_ENABLED=True)
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "operator@example.invalid")
    monkeypatch.setattr(boundary, "runtime_is_production", lambda: True)
    boundary.register_meta_credential_http_boundary(app)
    app.register_blueprint(route.meta_credential_admin_bp)
    limiter.init_app(app)
    limiter.reset()
    now = int(time.time())
    actor = db.session.get(User, 1)
    bearer = issue_token({"user_id": 1, "session_kind": "clerk", "auth_provider": "clerk",
        "clerk_sid": "synthetic-clerk-admin-session", "sv": auth_session_version(actor), "exp": now + 3600,
        "audience": "tenant_owner", "auth_intent": "tenant_owner"})
    db.session.remove()
    def graph(method, url, **kwargs):
        result = environment.graph(method, url, **kwargs)
        if url.endswith("/debug_token"):
            payload = json.loads(result.content)
            payload["data"].update(expires_at=now + 7200, data_access_expires_at=now + 7200)
            result.content = json.dumps(payload).encode()
        return result
    monkeypatch.setattr(runtime, "native_graph_request", graph)
    decision = SimpleNamespace(allowed=True, enabled=True, epoch=6, reason_code="runtime_is_global_writer_owner")
    monkeypatch.setattr(runtime, "evaluate_global_writer_authority", lambda _cfg: decision)
    lease_state = {"active": False, "enters": 0}
    @contextmanager
    def lease(_cfg):
        lease_state["active"] = True
        lease_state["enters"] += 1
        try:
            yield SimpleNamespace(revalidate=lambda _cfg: decision)
        finally:
            lease_state["active"] = False
    monkeypatch.setattr(runtime, "global_writer_authority_lease", lease)
    client = app.test_client()
    yield SimpleNamespace(app=app, client=client, bearer=bearer, now=now, graph=graph,
        calls=environment.calls, decision=decision, lease=lease_state)


def post(http, action, body=None, *, headers=None):
    return http.client.post(boundary.PREFIX + "/" + action,
        json=body or {"target": "junin-production"},
        headers=headers if headers is not None else {"Authorization": "Bearer " + http.bearer})


def body(target="junin-production"):
    return {"target": target, "access_token": TOKEN,
            "expected_revision": 0 if target == "junin-production" else 6, "expected_writer_epoch": 6}


def approved(http, target="junin-production"):
    value = body(target)
    plan = post(http, "plan", value)
    assert plan.status_code == 200, plan.json
    value.update(expires_at=plan.json["plan"]["expires_at"],
        approved_plan_sha256=plan.json["plan_sha256"],
        expected_database_identity_sha256=plan.json["plan"]["database_identity_sha256"])
    return value, plan.json


def assert_private(result):
    assert "no-store" in result.headers["Cache-Control"]
    for secret in (TOKEN, KEY, APP_SECRET, "ciphertext", "nonce"):
        assert secret not in result.get_data(as_text=True)


def test_missing_auth_never_reads_provider_or_writes(http):
    result = post(http, "plan", body(), headers={})
    assert result.status_code == 401
    assert not http.calls and AuditEvent.query.count() == 0
    assert_private(result)


@pytest.mark.parametrize("origin", [None, "null", "https://evil.invalid", "https://www.chatboc.ar.evil.invalid",
    "https://www.chatboc.ar.", "https://www.chatboc.ar:444", "https://www.chatboc.ar:443",
    " https://www.chatboc.ar", "https://www.chatboc.ar ", "http://www.chatboc.ar", "https://www.chatboc.ar/"])
def test_cookie_requires_exact_production_origin(http, origin):
    http.client.set_cookie("auth_token", http.bearer)
    result = post(http, "status", headers={} if origin is None else {"Origin": origin})
    assert result.status_code == 403 and not http.calls
    assert_private(result)


def test_existing_httponly_clerk_cookie_works_without_bearer(http):
    http.client.set_cookie("auth_token", http.bearer)
    result = post(http, "status", headers={"Origin": "https://www.chatboc.ar"})
    assert result.status_code == 200, result.json
    assert result.json["expected_writer_epoch"] == 6
    assert result.json["expected_revision"] == 0 and result.json["current_revision"] is None
    assert result.json["phone_number_id"] == "660753250460901" and result.json["operation_available"] is True
    assert_private(result)


def test_current_clerk_issuer_cookie_authorizes_real_admin_operation(http):
    # Exercise the actual issuer and durable lineage, not a hand-built copy of
    # its claims. A real in-memory server-side session avoids filesystem/network
    # access; only the already-verified upstream Clerk identity is synthetic.
    from cachelib.simple import SimpleCache
    from flask_session.cachelib import CacheLibSessionInterface
    from services.clerk_auth_service import _issue_clerk_chatboc_token
    http.app.session_interface = CacheLibSessionInterface(http.app, client=SimpleCache())
    with http.app.test_request_context("/auth/clerk/session", method="POST"):
        token = _issue_clerk_chatboc_token(db.session.get(User, 1), None,
            {"sid": "synthetic-real-issuer-session", "sub": "synthetic-clerk-operator"})
    http.client.set_cookie("auth_token", token)
    result = post(http, "status", headers={"Origin": "https://www.chatboc.ar"})
    assert result.status_code == 200 and result.json["operation_available"] is True, result.json
    planned = post(http, "plan", body(), headers={"Origin": "https://www.chatboc.ar"})
    assert planned.status_code == 200 and planned.json["plan"]["actor_user_id"] == 1, planned.json
    assert_private(result)
    assert_private(planned)


def test_invalid_explicit_bearer_cannot_fall_back_to_good_cookie(http):
    http.client.set_cookie("auth_token", http.bearer)
    result = post(http, "status", headers={"Origin": "https://www.chatboc.ar", "Authorization": "Bearer bad"})
    assert result.status_code == 401


def test_tenant_admin_has_no_platform_authority(http):
    actor = db.session.get(User, 2)
    token = issue_token({"user_id": 2, "session_kind": "clerk", "auth_provider": "clerk",
        "clerk_sid": "synthetic-tenant-admin", "sv": auth_session_version(actor), "exp": http.now + 3600})
    result = post(http, "plan", body(), headers={"Authorization": "Bearer " + token})
    assert result.status_code == 403 and not http.calls


def test_portal_audience_cannot_authorize_platform_operation(http):
    actor = db.session.get(User, 1)
    token = issue_token({"user_id": 1, "session_kind": "clerk", "auth_provider": "clerk",
        "clerk_sid": "synthetic-portal-session", "sv": auth_session_version(actor), "exp": http.now + 3600,
        "audience": "tenant_portal", "auth_intent": "tenant_portal"})
    result = post(http, "status", headers={"Authorization": "Bearer " + token})
    assert result.status_code == 401 and not http.calls


def test_user_role_alone_cannot_bypass_email_allowlist(http, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "different@example.invalid")
    assert post(http, "status").status_code in {401, 403}


@pytest.mark.parametrize("extra", [{"actor_user_id": 1}, {"actor_user_id": 2}, {"tenant_id": 22},
    {"provider": "twilio"}, {"runtime": "vercel"}, {"database_url": "forbidden"}])
def test_client_cannot_override_actor_scope_runtime_or_database(http, extra):
    value = {**body(), **extra}
    result = post(http, "plan", value)
    assert result.status_code == 400 and not http.calls and AuditEvent.query.count() == 0


def test_flag_off_and_wrong_runtime_fail_before_auth(http, monkeypatch):
    http.app.config["META_CREDENTIAL_ADMIN_ENABLED"] = False
    assert post(http, "status").status_code == 404
    http.app.config["META_CREDENTIAL_ADMIN_ENABLED"] = True
    monkeypatch.setattr(boundary, "runtime_is_production", lambda: False)
    assert post(http, "status").status_code == 503
    assert not http.calls


def test_body_is_bounded_and_query_auth_and_get_are_rejected(http):
    headers = {"Authorization": "Bearer " + http.bearer}
    assert http.client.post(boundary.PREFIX + "/plan", data="x" * 16385,
        content_type="application/json", headers=headers).status_code == 413
    assert http.client.post(boundary.PREFIX + "/plan", data="x=1", headers=headers).status_code == 415
    assert http.client.post(boundary.PREFIX + "/status?token=not-a-credential", json={"target": "junin-production"},
        headers=headers).status_code == 400
    assert http.client.get(boundary.PREFIX + "/status", headers=headers).status_code in {404, 405}
    assert not http.calls


def test_duplicate_json_keys_rejected_without_provider(http):
    result = http.client.post(boundary.PREFIX + "/status", data='{"target":"tdf-sandbox","target":"junin-production"}',
        content_type="application/json", headers={"Authorization": "Bearer " + http.bearer})
    assert result.status_code == 400 and not http.calls


def test_plan_is_read_only_and_derives_finite_provider_expiry(http):
    result = post(http, "plan", body())
    assert result.status_code == 200, result.json
    assert result.json["plan"]["actor_user_id"] == 1
    assert result.json["plan"]["expires_at"] == http.now + 7200
    assert result.json["expiry"]["source"] == "provider"
    assert len(http.calls) == 3 and AuditEvent.query.count() == 0
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1
    assert http.lease["enters"] == 0
    assert_private(result)


def test_indefinite_provider_uses_visible_local_policy_and_stable_apply(http, monkeypatch):
    def indefinite(method, url, **kwargs):
        result = http.graph(method, url, **kwargs)
        if url.endswith("/debug_token"):
            payload = json.loads(result.content)
            payload["data"].update(expires_at=0, data_access_expires_at=0)
            result.content = json.dumps(payload).encode()
        return result
    monkeypatch.setattr(runtime, "native_graph_request", indefinite)
    value, plan = approved(http)
    assert plan["expiry"]["source"] == "local_policy" and plan["expiry"]["provider_expires_at"] is None
    assert http.now + runtime.LOCAL_EXPIRY_SECONDS <= plan["plan"]["expires_at"] <= int(time.time()) + runtime.LOCAL_EXPIRY_SECONDS
    result = post(http, "apply", value)
    assert result.status_code == 200 and result.json["committed"] is True, result.json
    assert result.json["expires_at"] == plan["plan"]["expires_at"]


def test_apply_commits_under_live_lease_and_status_recovers_safe_receipt(http):
    value, plan = approved(http)
    commits = []
    def on_commit(_connection):
        commits.append(http.lease["active"])
    event.listen(db.engine, "commit", on_commit)
    try:
        result = post(http, "apply", value)
    finally:
        event.remove(db.engine, "commit", on_commit)
    assert result.status_code == 200 and result.json["committed"] is True, result.json
    assert commits == [True] and http.lease["active"] is False
    readback = post(http, "status")
    assert readback.status_code == 200, readback.json
    assert readback.json["current_revision"] == 1 and readback.json["operation_available"] is False
    assert readback.json["last_operation"]["plan_sha256"] == plan["plan_sha256"]
    assert AuditEvent.query.count() == 1
    assert_private(result)
    assert_private(readback)


def test_tdf_rotation_is_exact_cas_and_preserves_unrelated_data(http):
    value, _ = approved(http, "tdf-sandbox")
    before = db.session.get(ProviderConnection, 3)
    original = (before.status, before.external_app_id, before.external_account_id, before.config["retain_note"])
    db.session.rollback()
    result = post(http, "apply", value)
    assert result.status_code == 200 and result.json["revision"] == 7, result.json
    after = db.session.get(ProviderConnection, 3)
    assert (after.status, after.external_app_id, after.external_account_id, after.config["retain_note"]) == original
    again = post(http, "apply", value)
    assert again.status_code == 409 and AuditEvent.query.count() == 1


@pytest.mark.parametrize("change", ["token", "plan", "database", "expiry", "epoch"])
def test_apply_binds_reviewed_token_plan_database_expiry_and_epoch(http, change):
    value, _ = approved(http)
    if change == "token":
        value["access_token"] = "differentSyntheticToken_1234567890"
    elif change == "plan":
        value["approved_plan_sha256"] = "a" * 64
    elif change == "database":
        value["expected_database_identity_sha256"] = "b" * 64
    elif change == "expiry":
        value["expires_at"] -= 1
    else:
        http.decision.epoch = 7
    result = post(http, "apply", value)
    assert result.status_code == 409 and ProviderConnection.query.count() == 2 and AuditEvent.query.count() == 0
    assert_private(result)


def test_revoked_clerk_session_cannot_use_prior_plan(http):
    value, _ = approved(http)
    provider = AuthProviderSession.query.filter_by(provider_session_id="synthetic-clerk-admin-session").one()
    provider.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    assert post(http, "apply", value).status_code == 401
    assert AuditEvent.query.count() == 0


def test_commit_response_loss_returns_uncertain_and_does_not_retry(http, monkeypatch):
    value, plan = approved(http)
    original = runtime._transaction
    @contextmanager
    def lost_response(engine, *, read_only):
        with original(engine, read_only=read_only) as session:
            yield session
        if not read_only:
            raise RuntimeError("synthetic lost commit acknowledgement " + TOKEN)
    monkeypatch.setattr(runtime, "_transaction", lost_response)
    result = post(http, "apply", value)
    assert result.status_code == 503 and result.json["database_outcome"] == "unverified"
    assert result.json["requires_status_check"] is True and "committed" not in result.json
    assert http.lease["enters"] == 1
    readback = post(http, "status")
    assert readback.json["current_revision"] == 1
    assert readback.json["last_operation"]["plan_sha256"] == plan["plan_sha256"]
    assert_private(result)


def test_sensitive_third_party_logging_and_exception_are_redacted(http, monkeypatch, caplog):
    def provider_failure(*args, **kwargs):
        try:
            raise RuntimeError(TOKEN + KEY)
        except RuntimeError:
            logging.getLogger("synthetic-third-party").exception("provider raw %s %s", TOKEN, APP_SECRET)
            raise
    monkeypatch.setattr(runtime, "native_graph_request", provider_failure)
    with caplog.at_level(logging.DEBUG):
        result = post(http, "plan", body())
        logging.getLogger("ordinary-request").warning("ordinary log remains visible")
    assert "ordinary log remains visible" in caplog.text
    for secret in (TOKEN, KEY, APP_SECRET):
        assert secret not in caplog.text
    assert_private(result)


def test_rate_limit_failure_is_closed_before_provider(http, monkeypatch):
    def denied(*args, **kwargs):
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(limiter.limiter, "hit", denied)
    result = post(http, "plan", body())
    assert result.status_code == 503 and not http.calls
    assert_private(result)


def test_cors_preflight_only_allows_exact_panel_post(http):
    result = http.client.options(boundary.PREFIX + "/plan", headers={"Origin": "https://www.chatboc.ar",
        "Access-Control-Request-Method": "POST"})
    assert result.status_code == 204 and result.headers["Access-Control-Allow-Credentials"] == "true"
    assert http.client.options(boundary.PREFIX + "/plan", headers={"Origin": "https://evil.invalid",
        "Access-Control-Request-Method": "POST"}).status_code == 403
    assert not http.calls


def test_disabled_actor_cannot_use_prior_plan(http):
    value, _ = approved(http)
    actor = db.session.get(User, 1)
    actor.accesibilidad = {"auth": {"disabled": True}}
    db.session.commit()
    assert post(http, "apply", value).status_code == 401
    assert AuditEvent.query.count() == 0


def test_failure_after_staging_rolls_back_sender_connection_and_audit(http, monkeypatch):
    value, _ = approved(http)
    original = runtime.ops.apply_operation
    def fail_after_staging(*args, **kwargs):
        original(*args, **kwargs)
        raise runtime.ops.MetaCredentialOperationError("meta_operation_revision_conflict")
    monkeypatch.setattr(runtime.ops, "apply_operation", fail_after_staging)
    result = post(http, "apply", value)
    assert result.status_code == 409
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1
    assert AuditEvent.query.count() == 0 and http.lease["active"] is False


def test_final_lease_revalidation_failure_rolls_back_all_writes(http, monkeypatch):
    value, _ = approved(http)
    calls = []
    @contextmanager
    def lease(_cfg):
        def revalidate(_cfg):
            calls.append(True)
            return SimpleNamespace(allowed=len(calls) == 1, enabled=True, epoch=6)
        yield SimpleNamespace(revalidate=revalidate)
    monkeypatch.setattr(runtime, "global_writer_authority_lease", lease)
    result = post(http, "apply", value)
    assert result.status_code == 409 and len(calls) == 2
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1
    assert AuditEvent.query.count() == 0


def test_operator_expiry_cannot_exceed_local_cap(http):
    value = {**body(), "expires_at": http.now + runtime.LOCAL_EXPIRY_SECONDS + 3600}
    result = post(http, "plan", value)
    assert result.status_code == 409 and not http.calls


def test_rate_exhaustion_returns_429_without_provider_calls(http, monkeypatch):
    monkeypatch.setattr(limiter.limiter, "hit", lambda *_args: False)
    result = post(http, "plan", body())
    assert result.status_code == 429 and not http.calls


def test_status_exposes_missing_runtime_configuration_without_opening_tokens(http):
    http.app.config["META_WHATSAPP_APP_SECRETS"] = {}
    result = post(http, "status")
    assert result.status_code == 200 and result.json["operation_available"] is False
    assert result.json["reason_code"] == "meta_operation_app_authority_unconfigured"
    assert not http.calls
    assert_private(result)


def test_successful_apply_does_not_leak_sql_parameters_or_cached_body(http, caplog):
    cache = []
    @http.app.after_request
    def check_cache(result):
        from flask import request
        cache.append((request.__dict__.get("_cached_data"), request.__dict__.get("_cached_json")))
        return result
    value, _ = approved(http)
    cache.clear()
    with caplog.at_level(logging.DEBUG):
        result = post(http, "apply", value)
    assert result.status_code == 200, result.json
    assert cache == [(None, None)]
    for secret in (TOKEN, KEY, APP_SECRET, "ciphertext", "nonce"):
        assert secret not in caplog.text
    assert_private(result)

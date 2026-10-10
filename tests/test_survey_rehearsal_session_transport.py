"""Offline real Clerk exchange -> durable Chatboc cookie -> rehearsal proofs.

Only the remote verified Clerk identity and local writer/runtime boundary are
substituted. The exchange, issuer, JWT, AuthSession and HTTP guards are real.
"""
import pytest
from datetime import datetime, timedelta
import jwt

from database import db
from models import AuditEvent, AuthSession, AuthProviderSession
from services import production_survey_rehearsal as service
from tests.test_production_survey_rehearsal import (
    rehearsal_app, h, _admin, _create, _ledger_counts, _public,
)
from utils.auth_helpers import generar_token

ORIGIN = "https://chatboc.ar"


def _exchange(h, monkeypatch):
    claims = {"sub": "user_rehearsal_transport", "sid": "sess_rehearsal_transport",
              "email": h.sa.email, "email_verified": True}
    profile = {"id": claims["sub"], "first_name": "Cuenta sintética",
               "primary_email_address_id": "email_transport",
               "email_addresses": [{"id": "email_transport", "email_address": h.sa.email,
                                    "verification": {"status": "verified"}}]}
    monkeypatch.setattr("routes.auth.verify_clerk_session_token", lambda token: claims)
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda value: profile)
    monkeypatch.setitem(h.app.config, "CLERK_SESSION_RETURN_TOKEN", False)
    monkeypatch.setitem(h.app.config, "CLERK_SESSION_COOKIE_ENABLED", True)
    response = h.client.post("/api/auth/clerk/session", json={"auth_intent": "tenant_owner"},
                             headers={"Authorization": "Bearer synthetic.remote.clerk"})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["session_transport"] == "cookie" and body["audience"] == "tenant_owner"
    assert "token" not in body and body["user"]["role"] == "super_admin"
    row = db.session.get(AuthSession, body["session_retirement"]["lineage_id"])
    assert row.provider == "clerk" and row.audience == "tenant_owner" and row.revoked_at is None
    return row, h.client.get_cookie("auth_token").value


def _cookie_create(h, *, origin=ORIGIN):
    headers = {"Idempotency-Key": "cookie-create-intent-0001"}
    if origin is not None:
        headers["Origin"] = origin
    return h.client.post(_admin(h.tenant.slug), json={}, headers=headers)


def test_real_exchange_cookie_lists_creates_and_resolves_same_intent(h, monkeypatch):
    _exchange(h, monkeypatch)
    response = h.client.get(_admin(h.tenant.slug))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["create_action"]["can_create"] is True
    created = _cookie_create(h)
    assert created.status_code == 201, created.get_json()
    exact = h.client.get(_admin(h.tenant.slug) + "/status?submission_id=cookie-create-intent-0001")
    assert exact.status_code == 200 and exact.get_json()["run_id"] == created.get_json()["run_id"]


def test_real_exchange_bearer_owner_audience_is_supported_without_cookie(h, monkeypatch):
    _, token = _exchange(h, monkeypatch)
    client = h.app.test_client()
    response = client.get(_admin(h.tenant.slug), headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.get_json()


def test_cookie_vote_receipt_and_reload_account_status_are_authoritative(h, monkeypatch):
    run = _create(h)["run_id"]
    _exchange(h, monkeypatch)
    base = _public(h.tenant.slug, run)
    before = h.client.get(base + "/respond/status", headers={"Origin": ORIGIN})
    assert before.status_code == 200 and before.get_json()["participated"] is False
    response = h.client.post(base + "/respond", json={"submission_id": "cookie-vote-intent-0001", "option_id": "yes"},
                            headers={"Origin": ORIGIN, "Idempotency-Key": "cookie-vote-intent-0001"})
    assert response.status_code == 201, response.get_json()
    fresh = h.app.test_client()
    fresh.set_cookie("auth_token", h.client.get_cookie("auth_token").value)
    status = fresh.get(base + "/respond/status", headers={"Origin": ORIGIN})
    assert status.status_code == 200 and status.get_json()["participated"] is True
    exact = fresh.get(base + "/respond/status?submission_id=cookie-vote-intent-0001")
    assert exact.status_code == 200 and exact.get_json()["receipt"] == response.get_json()["receipt"]
    assert _ledger_counts() == (1, 1)


@pytest.mark.parametrize("retirement", ["lineage", "provider", "version", "missing"])
def test_real_exchange_retired_or_missing_authority_never_falls_back_to_cookie(h, monkeypatch, retirement):
    row, _ = _exchange(h, monkeypatch)
    if retirement == "lineage":
        row.revoked_at = service._now()
    elif retirement == "provider":
        db.session.get(AuthProviderSession, ("clerk", row.provider_session_id)).revoked_at = service._now()
    elif retirement == "version":
        row.actor_version += 1
    else:
        db.session.delete(row)
    db.session.commit()
    assert h.client.get(_admin(h.tenant.slug)).status_code in {401, 403}
    assert _cookie_create(h).status_code in {401, 403}
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


@pytest.mark.parametrize("authorization", ["Bearer malformed", "Bearer ", "Basic synthetic"])
def test_explicit_invalid_authorization_does_not_fall_back_to_live_cookie(h, monkeypatch, authorization):
    _exchange(h, monkeypatch)
    response = h.client.get(_admin(h.tenant.slug), headers={"Authorization": authorization})
    assert response.status_code in {401, 403}


def test_explicit_native_foreign_bearer_wins_cookie_and_keeps_scope_guard(h, monkeypatch):
    _exchange(h, monkeypatch)
    response = h.client.get(_admin(h.tenant.slug), headers=h.headers["other"])
    assert response.status_code == 403


@pytest.mark.parametrize("origin", [None, "null", "https://outside.test.invalid", "https://chatboc.ar.outside.test.invalid"])
def test_cookie_write_requires_exact_trusted_origin_and_has_no_effect(h, monkeypatch, origin):
    _exchange(h, monkeypatch)
    assert _cookie_create(h, origin=origin).status_code == 403
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


def test_cookie_write_requires_json_even_if_body_is_parseable(h, monkeypatch):
    _exchange(h, monkeypatch)
    response = h.client.post(_admin(h.tenant.slug), data="{}", content_type="text/plain",
                             headers={"Origin": ORIGIN, "Idempotency-Key": "plain-cookie-intent-0001"})
    assert response.status_code == 400
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


def test_query_or_body_token_is_not_an_official_rehearsal_transport(h, monkeypatch):
    _, token = _exchange(h, monkeypatch)
    fresh = h.app.test_client()
    assert fresh.get(_admin(h.tenant.slug) + "?token=" + token).status_code == 401
    assert fresh.get_cookie("auth_token") is None
    run = _create(h)["run_id"]
    response = fresh.post(_public(h.tenant.slug, run) + "/respond",
        json={"token": token, "submission_id": "body-transport-0001", "option_id": "yes"},
        headers={"Idempotency-Key": "body-transport-0001", "Origin": ORIGIN})
    assert response.status_code == 401 and _ledger_counts() == (0, 0)


@pytest.mark.parametrize("kind", ["widget", "demo"])
def test_widget_and_demo_sessions_never_gain_rehearsal_authority(h, kind):
    token = generar_token(h.actor.id, h.actor.rol, h.actor.tipo_chat, h.actor.municipio_id, h.actor.pyme_id, extra_claims={
        "session_kind": kind, "demo_mode": kind == "demo", "tenant_id": h.tenant.id,
        "tenant_slug": h.tenant.slug})
    assert h.client.get(_admin(h.tenant.slug), headers={"Authorization": "Bearer " + token}).status_code in {401, 403}
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


def test_valid_native_cookie_actor_cannot_inherit_a_different_clerk_cookie_actor(h, monkeypatch):
    _, clerk_token = _exchange(h, monkeypatch)
    client = h.app.test_client()
    login = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"})
    assert login.status_code == 200
    client.set_cookie("auth_token", clerk_token)
    assert client.get(_admin(h.tenant.slug)).status_code == 401


def test_real_native_cookie_uses_current_account_without_granting_superadmin(h):
    client = h.app.test_client()
    login = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"})
    assert login.status_code == 200
    assert client.get(_admin(h.tenant.slug)).status_code == 200
    assert client.get(_admin(h.foreign.slug)).status_code == 403
    assert client.post(_admin(h.tenant.slug), json={},
        headers={"Origin": ORIGIN, "Idempotency-Key": "ordinary-create-0001"}).status_code == 403
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


@pytest.mark.parametrize("old_state", ["retired", "expired"])
def test_current_native_flask_session_cannot_rescue_old_same_actor_jwt_lineage(h, old_state):
    client = h.app.test_client()
    first = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"}).get_json()
    second = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"}).get_json()
    assert first["session_retirement"]["lineage_id"] != second["session_retirement"]["lineage_id"]
    current = db.session.get(AuthSession, second["session_retirement"]["lineage_id"])
    old = db.session.get(AuthSession, first["session_retirement"]["lineage_id"])
    assert current.revoked_at is None
    if old_state == "retired":
        old.revoked_at = service._now()
    else:
        old.expires_at = service._now() - timedelta(seconds=1)
    db.session.commit()
    client.set_cookie("auth_token", first["token"])
    response = client.get(_admin(h.tenant.slug))
    assert response.status_code == 401, response.get_json()


def test_native_flask_session_cannot_rescue_expired_real_refresh_jwt_with_live_lineage(h, monkeypatch):
    from services.auth_session_lifecycle import refresh_native_token
    client = h.app.test_client()
    login = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"}).get_json()
    token, _ = refresh_native_token(login["token"], expires_at=service._now() + timedelta(seconds=1))
    row = db.session.get(AuthSession, login["session_retirement"]["lineage_id"])
    assert row.revoked_at is None and row.expires_at.replace(tzinfo=service._now().tzinfo) > service._now() + timedelta(hours=1)
    class LaterJWTClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=10)
    monkeypatch.setattr(jwt.api_jwt, "datetime", LaterJWTClock)
    client.set_cookie("auth_token", token)
    assert client.get(_admin(h.tenant.slug)).status_code == 401


def test_cookie_exact_jwt_retirement_before_commit_rolls_back_vote(h, monkeypatch):
    run = _create(h)["run_id"]
    client = h.app.test_client()
    first = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"}).get_json()
    second = client.post("/auth/login", json={"email": h.owner.email, "password": "synthetic-offline-password"}).get_json()
    client.set_cookie("auth_token", first["token"])
    original_commit = service._commit
    def retire_chosen_jwt_then_commit(lease, actor_id, tenant, **kwargs):
        chosen = db.session.get(AuthSession, first["session_retirement"]["lineage_id"])
        chosen.revoked_at = service._now()
        db.session.flush()
        assert db.session.get(AuthSession, second["session_retirement"]["lineage_id"]).revoked_at is None
        return original_commit(lease, actor_id, tenant, **kwargs)
    monkeypatch.setattr(service, "_commit", retire_chosen_jwt_then_commit)
    response = client.post(_public(h.tenant.slug, run) + "/respond",
        json={"submission_id": "retire-chosen-cookie-0001", "option_id": "yes"},
        headers={"Origin": ORIGIN, "Idempotency-Key": "retire-chosen-cookie-0001"})
    assert response.status_code == 401 and _ledger_counts() == (0, 0)


def test_even_configured_foreign_site_cannot_use_cookie_writes(h, monkeypatch):
    _exchange(h, monkeypatch)
    monkeypatch.setitem(h.app.config, "CORS_CREDENTIALS_ALLOWED_ORIGINS", ("https://outside.test.invalid",))
    assert _cookie_create(h, origin="https://outside.test.invalid").status_code == 403
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


@pytest.mark.parametrize("suffix,method", [("/respond", "OPTIONS"), ("/respond/status", "GET")])
@pytest.mark.parametrize("origin", [ORIGIN, "https://www.chatboc.ar"])
def test_only_exact_authenticated_rehearsal_endpoints_have_credentialed_cors(h, suffix, method, origin):
    run = _create(h)["run_id"]
    response = h.client.open(_public(h.tenant.slug, run) + suffix, method=method,
        headers={"Origin": origin, "Access-Control-Request-Method": "POST" if suffix == "/respond" else "GET"})
    assert response.headers.get("Access-Control-Allow-Origin") == origin
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"


@pytest.mark.parametrize("suffix", ["", "/results", "/respond/extra"])
def test_metadata_results_and_unrelated_public_paths_remain_cookieless_cors(h, suffix):
    run = _create(h)["run_id"]
    response = h.client.get(_public(h.tenant.slug, run) + suffix, headers={"Origin": ORIGIN})
    assert "Access-Control-Allow-Credentials" not in response.headers


def test_untrusted_authenticated_rehearsal_origin_is_not_reflected(h):
    run = _create(h)["run_id"]
    response = h.client.get(_public(h.tenant.slug, run) + "/respond/status",
                            headers={"Origin": "https://outside.test.invalid"})
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert "Access-Control-Allow-Origin" not in response.headers

import base64
import hashlib
import hmac
import json
import time

import jwt

from database import db
from models import Rubro, TenantFollower, TenantProfile, User, WebhookDelivery
from services.clerk_auth_service import ClerkAuthError, ClerkNotConfigured, upsert_user_from_clerk
from utils.auth_helpers import auth_session_version


def _profile():
    return {
        "id": "user_route_1",
        "first_name": "Laura",
        "last_name": "Admin",
        "primary_email_address_id": "email_1",
        "email_addresses": [
            {
                "id": "email_1",
                "email_address": "laura@chatboc.test",
                "verification": {"status": "verified"},
            }
        ],
        "external_accounts": [{"provider": "oauth_linkedin_oidc"}],
    }


def _create_route_tenant(
    slug: str,
    *,
    active: bool = True,
    tenant_type: str = "pyme",
) -> TenantProfile:
    owner = User(
        name=f"Owner {slug}",
        email=f"owner-{slug}@chatboc.test",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("owner-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo=tenant_type,
        municipio_id=owner.id if tenant_type == "municipio" else None,
        pyme_id=owner.id if tenant_type != "municipio" else None,
        is_active=active,
    )
    db.session.add(tenant)
    db.session.commit()
    return tenant


def test_registration_logs_never_include_submitted_secrets(client, monkeypatch):
    with client.application.app_context():
        rubro = Rubro(nombre="Pyme segura", clave="pyme-segura", es_publico=False)
        db.session.add(rubro)
        db.session.commit()
        rubro_id = rubro.id

    submitted_secrets = {
        "password": "NeverLog-Password-789!",
        "empresa_token": "entity-secret-never-log",
        "clerk_token": "clerk-secret-never-log",
        "session_token": "session-secret-never-log",
        "anon_id": "anon-secret-never-log",
    }
    emitted_logs: list[str] = []

    def _capture_log(message, *args, **kwargs):
        del kwargs
        emitted_logs.append(str(message) % args if args else str(message))

    for level in ("info", "warning", "error"):
        monkeypatch.setattr(client.application.logger, level, _capture_log)

    response = client.post(
        "/auth/register",
        json={
            "name": "Secure Logging Owner",
            "email": "secure-logging@chatboc.test",
            "nombre_empresa": "Secure Logging Company",
            "rubro": rubro_id,
            "tipo_chat": "pyme",
            "acepto_terminos": True,
            **submitted_secrets,
        },
    )

    assert response.status_code == 201
    log_text = "\n".join(emitted_logs)
    assert "[register] Registration attempt metadata=" in log_text
    for secret in submitted_secrets.values():
        assert secret not in log_text
    assert "secure-logging@chatboc.test" not in log_text


def test_clerk_config_contract(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CHATBOC_SUPERADMIN_EMAILS", raising=False)
    monkeypatch.delenv("CLERK_SUPERADMIN_EMAILS", raising=False)
    monkeypatch.delenv("CHATBOC_SUPERADMIN_EMAIL", raising=False)
    monkeypatch.delenv("CLERK_SUPERADMIN_EMAIL", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_test_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")

    resp = client.get("/auth/clerk/config")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == (
        "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
    )
    assert "Origin" in resp.headers["Vary"]
    payload = resp.get_json()
    assert payload["contract_version"] == "auth.clerk.v1"
    assert payload["enabled"] is True
    assert payload["session_sync_endpoint"] == "/auth/clerk/session"
    assert payload["auth_intents"] == {
        "default": "tenant_owner",
        "supported": ["tenant_owner", "tenant_portal"],
        "tenant_owner": {
            "requires_tenant_slug": False,
            "may_require_onboarding": True,
        },
        "tenant_portal": {
            "requires_tenant_slug": True,
            "may_require_onboarding": False,
            "effective_role": "usuario",
        },
    }
    assert payload["session_transport"] == {
        "preferred": "http_only_cookie",
        "cookie_same_site": "Lax",
        "cookie_path": "/",
        "secure_in_production": True,
        "production_body_token": "omitted_by_default",
    }
    assert {
        "user.created",
        "user.updated",
        "user.deleted",
        "session.ended",
        "session.removed",
        "session.revoked",
    }.issubset(set(payload["webhook_required_events"]))
    assert payload["oauth_callback_path"] == "/sso-callback"
    assert payload["publishable_key"] == "pk_test_public"
    assert payload["environment"] == "development"
    assert payload["production_ready"] is False
    assert payload["ready_for_session_sync"] is True
    assert any(item["code"] == "development_key_in_use" for item in payload["configuration_warnings"])
    assert "facebook" in payload["social_providers"]
    assert "linkedin" in payload["social_providers"]
    assert payload["superadmin_policy"] == {
        "mode": "email_allowlist",
        "default_owner_guardrail": True,
        "allowlist_env_configured": False,
    }


def test_clerk_config_contract_api_alias(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_test_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")

    resp = client.get("/api/auth/clerk/config")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == (
        "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
    )
    assert "Origin" in resp.headers["Vary"]
    payload = resp.get_json()
    assert payload["contract_version"] == "auth.clerk.v1"
    assert payload["enabled"] is True
    assert payload["session_sync_endpoint"] == "/auth/clerk/session"
    assert payload["publishable_key"] == "pk_test_public"
    assert payload["environment"] == "development"
    assert payload["production_ready"] is False
    assert payload["ready_for_session_sync"] is True


def test_clerk_auth_response_defaults_to_cookie_only_in_local_dev(client, monkeypatch):
    from routes.auth import _clerk_auth_response, _resolve_clerk_session_transport_policy

    for key in (
        "CLERK_SESSION_RETURN_TOKEN",
        "ENV",
        "FLASK_ENV",
        "RENDER",
        "RENDER_EXTERNAL_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(client.application.config, "ENV", "dev")
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", None)

    with client.application.test_request_context("/auth/clerk/session"):
        policy = _resolve_clerk_session_transport_policy()
        response, status_code = _clerk_auth_response(
            {"token": "chatboc.jwt"},
            200,
            transport_policy=policy,
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    cookie = response.headers.get("Set-Cookie") or ""
    assert cookie.startswith("auth_token=chatboc.jwt")
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie


def test_clerk_auth_response_treats_render_as_production_when_config_env_is_dev(
    client,
    monkeypatch,
):
    from routes.auth import _clerk_auth_response, _resolve_clerk_session_transport_policy

    monkeypatch.delenv("CLERK_SESSION_RETURN_TOKEN", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://chatboc-backend.onrender.com")
    monkeypatch.setitem(client.application.config, "ENV", "dev")
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", None)

    with client.application.test_request_context("/auth/clerk/session"):
        policy = _resolve_clerk_session_transport_policy()
        response, status_code = _clerk_auth_response(
            {"token": "chatboc.jwt"},
            200,
            transport_policy=policy,
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    assert "Secure" in (response.headers.get("Set-Cookie") or "")


def test_clerk_auth_response_treats_flask_env_production_as_production(
    client,
    monkeypatch,
):
    from routes.auth import _clerk_auth_response, _resolve_clerk_session_transport_policy

    monkeypatch.delenv("CLERK_SESSION_RETURN_TOKEN", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setitem(client.application.config, "ENV", "dev")
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", None)

    with client.application.test_request_context("/auth/clerk/session"):
        policy = _resolve_clerk_session_transport_policy()
        response, status_code = _clerk_auth_response(
            {"token": "chatboc.jwt"},
            200,
            transport_policy=policy,
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    assert "Secure" in (response.headers.get("Set-Cookie") or "")


def test_clerk_auth_response_body_token_requires_explicit_opt_in(client, monkeypatch):
    from routes.auth import _clerk_auth_response, _resolve_clerk_session_transport_policy

    monkeypatch.setenv("CLERK_SESSION_RETURN_TOKEN", "true")
    monkeypatch.setenv("CLERK_SESSION_COOKIE_ENABLED", "false")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setitem(client.application.config, "ENV", "dev")
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", None)

    with client.application.test_request_context("/auth/clerk/session"):
        policy = _resolve_clerk_session_transport_policy()
        response, status_code = _clerk_auth_response(
            {"token": "chatboc.jwt"},
            200,
            transport_policy=policy,
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["session_transport"] == "bearer"
    assert payload["token"] == "chatboc.jwt"
    assert response.headers.get("Set-Cookie") is None


def test_clerk_auth_response_rejects_cookie_disabled_without_body_opt_in(
    client,
    monkeypatch,
):
    from routes.auth import _clerk_auth_response, _resolve_clerk_session_transport_policy

    monkeypatch.setenv("CLERK_SESSION_COOKIE_ENABLED", "false")
    monkeypatch.delenv("CLERK_SESSION_RETURN_TOKEN", raising=False)
    monkeypatch.setitem(client.application.config, "ENV", "dev")

    with client.application.test_request_context("/auth/clerk/session"):
        policy = _resolve_clerk_session_transport_policy()
        response, status_code = _clerk_auth_response(
            {"token": "chatboc.jwt"},
            200,
            transport_policy=policy,
        )

    payload = response.get_json()
    assert status_code == 503
    assert payload["session_transport"] == "unavailable"
    assert payload["reason_code"] == "clerk_session_transport_unavailable"
    assert "token" not in payload
    assert response.headers.get("Set-Cookie") is None
    assert response.headers["Cache-Control"] == "no-store"


def test_clerk_auth_response_uses_the_resolved_transport_snapshot(client, monkeypatch):
    from routes.auth import _clerk_auth_response, _resolve_clerk_session_transport_policy

    monkeypatch.setenv("CLERK_SESSION_COOKIE_ENABLED", "true")
    monkeypatch.delenv("CLERK_SESSION_RETURN_TOKEN", raising=False)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", None)

    with client.application.test_request_context("/auth/clerk/session"):
        policy = _resolve_clerk_session_transport_policy()
        monkeypatch.setenv("CLERK_SESSION_COOKIE_ENABLED", "false")
        monkeypatch.setenv("CLERK_SESSION_RETURN_TOKEN", "true")
        response, status_code = _clerk_auth_response(
            {"token": "chatboc.jwt"},
            200,
            transport_policy=policy,
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    assert (response.headers.get("Set-Cookie") or "").startswith(
        "auth_token=chatboc.jwt"
    )


def test_invalid_clerk_transport_rejects_both_routes_before_side_effects(
    client,
    monkeypatch,
):
    monkeypatch.setenv("CLERK_SESSION_COOKIE_ENABLED", "false")
    monkeypatch.delenv("CLERK_SESSION_RETURN_TOKEN", raising=False)

    with client.application.app_context():
        counts_before = (User.query.count(), TenantProfile.query.count())

    unexpected_calls: list[str] = []

    def unexpected(name):
        def _unexpected(*args, **kwargs):
            del args, kwargs
            unexpected_calls.append(name)
            raise AssertionError(f"{name} must not run for an invalid transport policy")

        return _unexpected

    for target in (
        "routes.auth.verify_clerk_session_token",
        "routes.auth.fetch_trusted_clerk_profile",
        "routes.auth.upsert_user_from_clerk",
        "routes.auth.resolve_clerk_session_tenant",
        "routes.auth.complete_clerk_onboarding",
        "routes.auth.build_chatboc_session_payload",
        "routes.auth._send_verification_email",
        "services.clerk_auth_service.send_verification_email",
        "services.clerk_auth_service.send_onboarding_whatsapp",
    ):
        monkeypatch.setattr(target, unexpected(target))
    monkeypatch.setattr(db.session, "add", unexpected("db.session.add"))
    monkeypatch.setattr(db.session, "commit", unexpected("db.session.commit"))
    monkeypatch.setattr(db.session, "flush", unexpected("db.session.flush"))

    for path in ("/auth/clerk/session", "/auth/clerk/onboarding"):
        response = client.post(
            path,
            headers={"Authorization": "Bearer must-not-be-verified"},
            json={"tenant_name": "Must not be created"},
        )

        assert response.status_code == 503
        assert response.get_json() == {
            "error": (
                "Clerk session transport is disabled; enable the HttpOnly cookie "
                "or explicitly opt in to bearer response transport."
            ),
            "reason_code": "clerk_session_transport_unavailable",
            "session_transport": "unavailable",
        }
        assert response.headers["Cache-Control"] == "no-store"

    with client.application.app_context():
        counts_after = (User.query.count(), TenantProfile.query.count())

    assert unexpected_calls == []
    assert counts_after == counts_before


def test_clerk_routes_do_not_reflect_untrusted_origin(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_test_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")

    resp = client.get("/auth/clerk/config", headers={"Origin": "https://evil.example"})

    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") != "https://evil.example"


def test_clerk_config_marks_production_ready_only_with_live_key_webhook_and_allowlist(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_live_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://chatboc.clerk.accounts.dev/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-server-identity")
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_secret_value")
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setenv("CLERK_SOCIAL_PROVIDERS", "google,linkedin")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar,https://www.chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")

    resp = client.get("/auth/clerk/config")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["environment"] == "production"
    assert payload["production_ready"] is True
    assert payload["production_requirements"] == {
        "live_publishable_key": True,
        "session_verification": True,
        "backend_identity_api": True,
        "webhook_secret": True,
        "authorized_parties": True,
        "authorized_party_required": True,
        "superadmin_allowlist": True,
        "custom_domain_or_production_instance": True,
    }
    assert "whsec_secret_value" not in str(payload)


def test_clerk_config_hides_social_providers_for_live_key_until_explicitly_enabled(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_live_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")
    monkeypatch.delenv("CLERK_SOCIAL_PROVIDERS", raising=False)

    resp = client.get("/auth/clerk/config")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["enabled"] is False
    assert payload["social_providers"] == []
    assert any(item["code"] == "oauth_providers_missing" for item in payload["configuration_warnings"])


def test_clerk_config_exposes_explicit_social_providers_for_live_key(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_live_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_SOCIAL_PROVIDERS", "google, linkedin")
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-server-identity")
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_secret_value")
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar,https://www.chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")

    resp = client.get("/auth/clerk/config")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["enabled"] is True
    assert payload["social_providers"] == ["google", "linkedin"]
    assert not any(item["code"] == "oauth_providers_missing" for item in payload["configuration_warnings"])


def test_clerk_production_contract_disables_session_sync_without_required_azp(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_live_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-server-identity")
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_secret_value")
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "false")

    resp = client.get("/auth/clerk/config")

    payload = resp.get_json()
    assert payload["production_ready"] is False
    assert payload["enabled"] is False
    assert payload["ready_for_session_sync"] is False
    assert payload["authorized_party_required"] is False
    assert any(item["code"] == "authorized_party_not_required" for item in payload["configuration_warnings"])


def test_clerk_config_stays_disabled_without_jwt_verification(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_test_public")
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)

    resp = client.get("/auth/clerk/config")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["enabled"] is False
    assert payload["publishable_key_configured"] is True
    assert payload["ready_for_session_sync"] is False
    assert any(item["code"] == "jwt_verification_missing" for item in payload["configuration_warnings"])


def test_clerk_session_sync_returns_chatboc_token_and_onboarding(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_route_1", "sid": "sess_route_1", "email": "laura@chatboc.test", "email_verified": True},
    )
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda claims: _profile())

    resp = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer clerk.jwt.token"},
        json={"user": _profile()},
    )

    assert resp.status_code == 202
    payload = resp.get_json()
    assert payload["auth_provider"] == "clerk"
    assert payload["auth_intent"] == "tenant_owner"
    assert payload["audience"] == "tenant_owner"
    assert payload["token"] is None
    assert payload["session_transport"] == "pending_onboarding"
    assert payload["user"]["email"] == "laura@chatboc.test"
    assert payload["onboarding"]["required"] is True
    assert payload["onboarding"]["modal"]["vertical_presets"]["pyme"]["primary_goal"] == "ventas"
    assert payload["onboarding"]["modal"]["profile_picture_policy"] == "consented_upload_or_social_only"

    with client.application.app_context():
        user = User.query.filter_by(email="laura@chatboc.test").first()
        assert user is not None
        assert user.accesibilidad["auth"]["clerk"]["social_providers"] == ["linkedin"]


def test_clerk_portal_session_links_follower_without_role_escalation(client, monkeypatch):
    with client.application.app_context():
        tenant = _create_route_tenant("public-portal")
        tenant_id = tenant.id

    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_portal_route",
            "sid": "sess_portal_route",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {**_profile(), "id": "user_portal_route"},
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={
            "auth_intent": "tenant_portal",
            "tenant_slug": "public-portal",
            "role": "super_admin",
            "user": {"role": "super_admin", "tenant_id": 999999},
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["auth_intent"] == "tenant_portal"
    assert payload["audience"] == "tenant_portal"
    assert payload["tenant"]["slug"] == "public-portal"
    assert payload["user"]["role"] == "usuario"
    assert payload["user"]["tipo_chat"] == "pyme"
    assert payload["onboarding"]["required"] is False
    assert payload["onboarding"]["status"] == "portal_ready"
    assert payload["channel_activation"] == {
        "available": False,
        "status": "not_applicable",
        "reason_code": "tenant_portal",
        "channels": [],
    }
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    assert (response.headers.get("Set-Cookie") or "").startswith("auth_token=")

    with client.application.app_context():
        user = User.query.filter_by(email="laura@chatboc.test").first()
        assert user is not None
        assert user.rol == "usuario"
        assert user.tenant_id is None
        assert TenantFollower.query.filter_by(
            user_id=user.id,
            tenant_id=tenant_id,
        ).count() == 1


def test_clerk_portal_session_rejects_persisted_admin_without_token_or_follower(
    client,
    monkeypatch,
):
    with client.application.app_context():
        tenant = _create_route_tenant("portal-admin-conflict")
        admin = User(
            name="Laura Admin",
            email="laura@chatboc.test",
            rol="admin",
            tipo_chat="pyme",
        )
        admin.set_password("admin-password")
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        tenant_id = tenant.id

    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_admin_portal_conflict",
            "sid": "sess_admin_portal_conflict",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {**_profile(), "id": "user_admin_portal_conflict"},
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={"auth_intent": "tenant_portal", "tenant_slug": "portal-admin-conflict"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["reason_code"] == "portal_identity_conflict"
    assert "token" not in payload
    assert response.headers.get("Set-Cookie") is None
    with client.application.app_context():
        assert User.query.get(admin_id).rol == "admin"
        assert TenantFollower.query.filter_by(
            user_id=admin_id,
            tenant_id=tenant_id,
        ).count() == 0


def test_clerk_portal_session_uses_municipal_tenant_chat_type(client, monkeypatch):
    with client.application.app_context():
        _create_route_tenant("municipio-publico", tenant_type="municipio")

    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_municipal_portal",
            "sid": "sess_municipal_portal",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {**_profile(), "id": "user_municipal_portal"},
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={"auth_intent": "tenant_portal", "tenant_slug": "municipio-publico"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["user"]["role"] == "usuario"
    assert payload["user"]["tipo_chat"] == "municipio"
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    cookie = response.headers.get("Set-Cookie") or ""
    cookie_token = cookie.split(";", 1)[0].partition("=")[2]
    decoded = jwt.decode(
        cookie_token,
        client.application.config["SECRET_KEY"],
        algorithms=["HS256"],
    )
    assert decoded["rol"] == "usuario"
    assert decoded["tipo_chat"] == "municipio"
    assert decoded["municipio_id"] is None
    assert decoded["pyme_id"] is None
    assert decoded["empresa_id"] is None


def test_clerk_portal_session_requires_existing_tenant(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_missing_portal",
            "sid": "sess_missing_portal",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda claims: _profile())

    missing_slug = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={"intent": "tenant_portal"},
    )
    unknown_tenant = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={"intent": "tenant_portal", "tenant_slug": "does-not-exist"},
    )

    assert missing_slug.status_code == 400
    assert missing_slug.get_json()["reason_code"] == "tenant_slug_required"
    assert unknown_tenant.status_code == 404
    assert unknown_tenant.get_json()["reason_code"] == "tenant_not_found"


def test_clerk_session_rejects_unknown_auth_intent(client):
    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer clerk.jwt.token"},
        json={"auth_intent": "admin_from_browser"},
    )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_auth_intent"


def test_clerk_portal_session_rejects_invalid_tenant_slug(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_invalid_portal_slug",
            "sid": "sess_invalid_portal_slug",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda claims: _profile())

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={"intent": "tenant_portal", "tenant_slug": "../admin"},
    )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "tenant_slug_invalid"


def test_clerk_portal_session_rejects_inactive_tenant(client, monkeypatch):
    with client.application.app_context():
        _create_route_tenant("inactive-public-portal", active=False)

    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_inactive_portal",
            "sid": "sess_inactive_portal",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda claims: _profile())

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={"intent": "tenant_portal", "tenant_slug": "inactive-public-portal"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "tenant_inactive"


def test_clerk_owner_session_uses_secure_cookie_transport_in_production(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setitem(client.application.config, "ENV", "prod")
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", ".chatboc.test")
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_cookie_owner",
            "sid": "sess_cookie_owner",
            "email": "guillen.marce@gmail.com",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {
            **_profile(),
            "id": "user_cookie_owner",
            "email_addresses": [
                {
                    "id": "email_1",
                    "email_address": "guillen.marce@gmail.com",
                    "verification": {"status": "verified"},
                }
            ],
        },
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer owner.clerk.token"},
        json={"auth_intent": "tenant_owner"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["auth_intent"] == "tenant_owner"
    assert payload["audience"] == "tenant_owner"
    assert payload["user"]["role"] == "super_admin"
    assert payload["onboarding"]["required"] is False
    assert payload["session_transport"] == "cookie"
    assert "token" not in payload
    cookie = response.headers.get("Set-Cookie") or ""
    assert cookie.startswith("auth_token=")
    assert "Domain=chatboc.test" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "SameSite=Lax" in cookie
    assert response.headers["Cache-Control"] == "no-store"
    cookie_token = cookie.split(";", 1)[0].partition("=")[2]
    decoded = jwt.decode(
        cookie_token,
        client.application.config["SECRET_KEY"],
        algorithms=["HS256"],
    )
    assert decoded["auth_intent"] == "tenant_owner"
    assert decoded["audience"] == "tenant_owner"


def test_clerk_onboarding_rejects_portal_intent(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {
            "sub": "user_portal_onboarding",
            "sid": "sess_portal_onboarding",
            "email": "laura@chatboc.test",
            "email_verified": True,
        },
    )

    response = client.post(
        "/auth/clerk/onboarding",
        headers={"Authorization": "Bearer portal.clerk.token"},
        json={
            "auth_intent": "tenant_portal",
            "tenant_name": "Must Not Exist",
            "terms_accepted": True,
            "terms_version": "2026-07-11",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "owner_intent_required"
    with client.application.app_context():
        assert TenantProfile.query.filter_by(slug="must-not-exist").first() is None


def test_clerk_session_sync_rejects_inactive_tenant(client, monkeypatch):
    with client.application.app_context():
        user = upsert_user_from_clerk(
            {"sub": "user_inactive", "sid": "sess_inactive", "email": "inactive@chatboc.test", "email_verified": True},
            _profile() | {
                "id": "user_inactive",
                "email_addresses": [
                    {
                        "id": "email_1",
                        "email_address": "inactive@chatboc.test",
                        "verification": {"status": "verified"},
                    }
                ],
            },
            profile_is_trusted=True,
        )
        db.session.flush()
        tenant = TenantProfile(
            slug="inactive-route-tenant",
            nombre="Inactive Route Tenant",
            tipo="pyme",
            pyme_id=user.id,
            is_active=False,
        )
        db.session.add(tenant)
        db.session.flush()
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        db.session.commit()
        user_id = user.id

    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_inactive", "sid": "sess_inactive", "email": "inactive@chatboc.test", "email_verified": True},
    )
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda claims: _profile())
    monkeypatch.setattr(
        "routes.auth.upsert_user_from_clerk",
        lambda *args, **kwargs: db.session.get(User, user_id),
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer inactive.clerk.token"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "tenant_inactive"


def test_clerk_session_sync_rejects_revoked_sid(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: (_ for _ in ()).throw(ClerkAuthError("Clerk session has been revoked")),
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer revoked.clerk.token"},
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "invalid_clerk_session"


def test_clerk_session_sync_fails_closed_when_active_session_lookup_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: (_ for _ in ()).throw(
            ClerkNotConfigured("Clerk Backend API session lookup is unavailable")
        ),
    )

    response = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer active.clerk.token"},
    )

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "clerk_not_configured"


def test_clerk_linked_account_cannot_use_legacy_login(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.test")
    with client.application.app_context():
        user = upsert_user_from_clerk(
            {"sub": "user_linked", "sid": "sess_linked", "email": "linked@chatboc.test", "email_verified": True},
            {
                "id": "user_linked",
                "first_name": "Linked",
                "primary_email_address_id": "email_linked",
                "email_addresses": [
                    {
                        "id": "email_linked",
                        "email_address": "linked@chatboc.test",
                        "verification": {"status": "verified"},
                    }
                ],
            },
            profile_is_trusted=True,
        )
        user.set_password("legacy-password")
        db.session.commit()

    response = client.post(
        "/auth/login",
        json={"email": "linked@chatboc.test", "password": "legacy-password"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "clerk_required"


def test_clerk_session_sync_allows_only_configured_superadmin(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_super_route", "sid": "sess_super_route", "email": "guillen.marce@gmail.com", "email_verified": True},
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {
            **_profile(),
            "id": "user_super_route",
            "email_addresses": [
                {
                    "id": "email_1",
                    "email_address": "guillen.marce@gmail.com",
                    "verification": {"status": "verified"},
                }
            ],
        },
    )

    resp = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer clerk.jwt.token"},
        json={
            "user": {
                "id": "user_super_route",
                "first_name": "Marcelo",
                "primary_email_address_id": "email_super",
                "email_addresses": [
                    {
                        "id": "email_super",
                        "email_address": "guillen.marce@gmail.com",
                        "verification": {"status": "verified"},
                    }
                ],
            }
        },
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["user"]["role"] == "super_admin"
    assert payload["onboarding"]["required"] is False
    assert payload["onboarding"]["status"] == "platform_admin"


def test_clerk_session_ignores_forged_browser_superadmin_profile(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_attacker", "email": "attacker@chatboc.test", "email_verified": True},
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {
            "id": "user_attacker",
            "first_name": "Attacker",
            "primary_email_address_id": "email_attacker",
            "email_addresses": [
                {
                    "id": "email_attacker",
                    "email_address": "attacker@chatboc.test",
                    "verification": {"status": "verified"},
                }
            ],
        },
    )

    resp = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer clerk.jwt.token"},
        json={
            "user": {
                "id": "user_attacker",
                "email": "guillen.marce@gmail.com",
                "email_verified": True,
            }
        },
    )

    assert resp.status_code == 202
    payload = resp.get_json()
    assert payload["user"]["email"] == "attacker@chatboc.test"
    assert payload["user"]["role"] == "usuario"


def test_clerk_onboarding_route_creates_tenant(client, monkeypatch):
    monkeypatch.setenv("CLERK_SESSION_RETURN_TOKEN", "true")
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_route_2", "sid": "sess_route_2", "email": "owner2@chatboc.test", "email_verified": True},
    )
    monkeypatch.setattr(
        "routes.auth.fetch_trusted_clerk_profile",
        lambda claims: {
            **_profile(),
            "id": "user_route_2",
            "email_addresses": [
                {
                    "id": "email_1",
                    "email_address": "owner2@chatboc.test",
                    "verification": {"status": "verified"},
                }
            ],
        },
    )
    monkeypatch.setattr("services.clerk_auth_service.send_verification_email", lambda *args, **kwargs: True)
    monkeypatch.setattr("services.clerk_auth_service.send_onboarding_whatsapp", lambda *args, **kwargs: True)

    resp = client.post(
        "/auth/clerk/onboarding",
        headers={"Authorization": "Bearer clerk.jwt.token"},
        json={
            "user": {
                "id": "user_route_2",
                "email": "owner2@chatboc.test",
                "first_name": "Owner",
            },
            "tenant_name": "Colegio Modelo",
            "vertical": "colegio",
            "rubro": "educacion",
            "telefono": "+5492613000001",
            "primary_goal": "whatsapp_ai",
            "plan": "full",
            "terms_accepted": True,
            "terms_version": "2026-07-11",
        },
    )

    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["tenant"]["slug"] == "colegio-modelo"
    assert payload["onboarding"]["required"] is False
    assert payload["auth_intent"] == "tenant_owner"
    assert payload["session_transport"] == "cookie_and_body"
    assert payload["token"]
    onboarding_cookie = resp.headers.get("Set-Cookie") or ""
    assert onboarding_cookie.startswith("auth_token=")
    assert "HttpOnly" in onboarding_cookie
    assert "Path=/" in onboarding_cookie
    assert "SameSite=Lax" in onboarding_cookie

    with client.application.app_context():
        tenant = TenantProfile.query.filter_by(slug="colegio-modelo").first()
        assert tenant is not None
        assert tenant.plan == "free"
        assert tenant.configuracion["auth"]["provider"] == "clerk"
        assert tenant.configuracion["onboarding"]["requested_plan"] == "full"
        assert tenant.configuracion["onboarding"]["granted_plan"] == "free"
        assert tenant.configuracion["provisioning"]["status"] == "plan_required"


def test_clerk_webhook_syncs_user_with_valid_signature(client, monkeypatch):
    secret_raw = b"webhook-secret"
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_" + base64.b64encode(secret_raw).decode())
    body = {
        "type": "user.created",
        "data": {
            "id": "user_webhook_1",
            "first_name": "Webhook",
            "primary_email_address_id": "email_1",
            "email_addresses": [
                {
                    "id": "email_1",
                    "email_address": "webhook@chatboc.test",
                    "verification": {"status": "verified"},
                }
            ],
        },
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    msg_id = "msg_123"
    ts = str(int(time.time()))
    signed = b".".join([msg_id.encode(), ts.encode(), raw])
    signature = base64.b64encode(hmac.new(secret_raw, signed, hashlib.sha256).digest()).decode()

    resp = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers={
            "svix-id": msg_id,
            "svix-timestamp": ts,
            "svix-signature": f"v1,{signature}",
        },
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "synced"
    with client.application.app_context():
        assert User.query.filter_by(email="webhook@chatboc.test").first() is not None


def test_terminal_clerk_webhook_disconnects_session_and_user_rooms(client, monkeypatch):
    with client.application.app_context():
        profile = {
            **_profile(),
            "id": "user_webhook_disconnect",
            "email_addresses": [
                {
                    "id": "email_1",
                    "email_address": "disconnect@chatboc.test",
                    "verification": {"status": "verified"},
                }
            ],
        }
        upsert_user_from_clerk(
            {
                "sub": "user_webhook_disconnect",
                "sid": "sess_webhook_disconnect",
                "email": "disconnect@chatboc.test",
                "email_verified": True,
            },
            profile,
            profile_is_trusted=True,
        )
        db.session.commit()

    disconnected = []
    monkeypatch.setattr("routes.auth.verify_clerk_webhook_signature", lambda *args, **kwargs: None)

    def _disconnect(**kwargs):
        disconnected.append(kwargs)
        return 2

    monkeypatch.setattr("socket_service.disconnect_clerk_session_sockets", _disconnect)
    response = client.post(
        "/auth/clerk/webhook",
        data=json.dumps(
            {
                "type": "session.revoked",
                "data": {
                    "id": "sess_webhook_disconnect",
                    "user_id": "user_webhook_disconnect",
                },
            }
        ).encode("utf-8"),
        content_type="application/json",
        headers={"svix-id": "msg_disconnect_123"},
    )

    assert response.status_code == 200
    assert response.get_json()["disconnected_sockets"] == 2
    assert disconnected == [
        {
            "clerk_session_id": "sess_webhook_disconnect",
            "clerk_user_id": "user_webhook_disconnect",
        }
    ]


def test_clerk_webhook_duplicate_does_not_revoke_or_disconnect_twice(client, monkeypatch):
    monkeypatch.setattr("routes.auth.verify_clerk_webhook_signature", lambda *args, **kwargs: None)
    sync_calls = []
    disconnect_calls = []

    def _sync(event, *, commit=True):
        assert commit is False
        sync_calls.append(event)
        return {"status": "sessions_revoked", "event_type": event["type"]}

    def _disconnect(**kwargs):
        disconnect_calls.append(kwargs)
        return 1

    monkeypatch.setattr("routes.auth.sync_clerk_webhook_event", _sync)
    monkeypatch.setattr("socket_service.disconnect_clerk_session_sockets", _disconnect)
    raw = json.dumps(
        {
            "type": "session.revoked",
            "data": {"id": "sess_duplicate", "user_id": "user_duplicate"},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"svix-id": "msg_duplicate_123"}

    first = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers=headers,
    )
    duplicate = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers=headers,
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.get_json() == {
        "status": "duplicate_ignored",
        "event_type": "session.revoked",
    }
    assert len(sync_calls) == 1
    assert disconnect_calls == [
        {
            "clerk_session_id": "sess_duplicate",
            "clerk_user_id": "user_duplicate",
        }
    ]


def test_clerk_webhook_active_delivery_returns_retryable_503(client, monkeypatch):
    from services.webhook_delivery_service import claim_delivery

    monkeypatch.setattr("routes.auth.verify_clerk_webhook_signature", lambda *args, **kwargs: None)
    sync_calls = []
    monkeypatch.setattr(
        "routes.auth.sync_clerk_webhook_event",
        lambda event, **_kwargs: sync_calls.append(event) or {"status": "synced"},
    )
    raw = json.dumps(
        {"type": "user.updated", "data": {"id": "user_processing"}},
        separators=(",", ":"),
    ).encode("utf-8")
    claim_delivery("clerk", "msg_processing_123", "user.updated", raw)

    response = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers={"svix-id": "msg_processing_123"},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.get_json()["reason_code"] == "clerk_webhook_in_progress"
    assert sync_calls == []


def test_clerk_webhook_failed_delivery_is_retryable(client, monkeypatch):
    from models import WebhookDelivery

    monkeypatch.setattr("routes.auth.verify_clerk_webhook_signature", lambda *args, **kwargs: None)
    attempts = 0

    def _sync(_event, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary upstream failure token=must-not-persist")
        return {"status": "synced", "event_type": "user.updated"}

    monkeypatch.setattr("routes.auth.sync_clerk_webhook_event", _sync)
    raw = json.dumps(
        {"type": "user.updated", "data": {"id": "user_retry"}},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"svix-id": "msg_retry_123"}

    failed = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers=headers,
    )
    retried = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers=headers,
    )

    assert failed.status_code == 500
    assert retried.status_code == 200
    assert retried.get_json()["status"] == "synced"
    receipt = WebhookDelivery.query.filter_by(
        provider="clerk",
        event_id="msg_retry_123",
    ).one()
    assert receipt.status == WebhookDelivery.STATUS_PROCESSED
    assert receipt.attempts == 2
    assert receipt.last_error is None


def test_clerk_webhook_effect_and_receipt_retry_atomically(client, monkeypatch):
    profile = {
        **_profile(),
        "id": "user_atomic_webhook",
        "email_addresses": [
            {
                "id": "email_1",
                "email_address": "atomic-webhook@chatboc.test",
                "verification": {"status": "verified"},
            }
        ],
    }
    user = upsert_user_from_clerk(
        {
            "sub": "user_atomic_webhook",
            "sid": "sess_atomic_webhook",
            "email": "atomic-webhook@chatboc.test",
            "email_verified": True,
        },
        profile,
        profile_is_trusted=True,
    )
    db.session.commit()
    user_id = user.id

    real_commit = db.session.commit
    commit_attempts = 0

    def fail_first_atomic_commit():
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise RuntimeError("simulated commit interruption")
        return real_commit()

    monkeypatch.setattr(db.session, "commit", fail_first_atomic_commit)
    monkeypatch.setattr(
        "routes.auth.verify_clerk_webhook_signature",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "socket_service.disconnect_clerk_session_sockets",
        lambda **_kwargs: 1,
    )
    raw = json.dumps(
        {
            "type": "session.revoked",
            "data": {
                "id": "sess_atomic_webhook",
                "user_id": "user_atomic_webhook",
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"svix-id": "msg_atomic_webhook"}

    failed = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers=headers,
    )
    retried = client.post(
        "/auth/clerk/webhook",
        data=raw,
        content_type="application/json",
        headers=headers,
    )

    assert failed.status_code == 500
    assert retried.status_code == 200
    assert auth_session_version(db.session.get(User, user_id)) == 2
    receipt = WebhookDelivery.query.filter_by(
        provider="clerk",
        event_id="msg_atomic_webhook",
    ).one()
    assert receipt.status == WebhookDelivery.STATUS_PROCESSED
    assert receipt.attempts == 2

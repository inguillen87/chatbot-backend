import base64
import hashlib
import hmac
import json
import time

from database import db
from models import TenantProfile, User


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
    payload = resp.get_json()
    assert payload["contract_version"] == "auth.clerk.v1"
    assert payload["enabled"] is True
    assert payload["session_sync_endpoint"] == "/auth/clerk/session"
    assert payload["oauth_callback_path"] == "/sso-callback"
    assert payload["publishable_key"] == "pk_test_public"
    assert payload["ready_for_session_sync"] is True
    assert payload["configuration_warnings"] == []
    assert "facebook" in payload["social_providers"]
    assert "linkedin" in payload["social_providers"]
    assert payload["superadmin_policy"] == {
        "mode": "email_allowlist",
        "default_owner_guardrail": True,
        "allowlist_env_configured": False,
    }


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
    assert payload["enabled"] is True
    assert payload["social_providers"] == []
    assert any(item["code"] == "oauth_providers_missing" for item in payload["configuration_warnings"])


def test_clerk_config_exposes_explicit_social_providers_for_live_key(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.delenv("VITE_CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_live_public")
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.test/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_SOCIAL_PROVIDERS", "google, linkedin")

    resp = client.get("/auth/clerk/config")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["enabled"] is True
    assert payload["social_providers"] == ["google", "linkedin"]
    assert not any(item["code"] == "oauth_providers_missing" for item in payload["configuration_warnings"])


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
        lambda token: {"sub": "user_route_1", "email": "laura@chatboc.test", "email_verified": True},
    )

    resp = client.post(
        "/auth/clerk/session",
        headers={"Authorization": "Bearer clerk.jwt.token"},
        json={"user": _profile()},
    )

    assert resp.status_code == 202
    payload = resp.get_json()
    assert payload["auth_provider"] == "clerk"
    assert payload["token"]
    assert payload["user"]["email"] == "laura@chatboc.test"
    assert payload["onboarding"]["required"] is True

    with client.application.app_context():
        user = User.query.filter_by(email="laura@chatboc.test").first()
        assert user is not None
        assert user.accesibilidad["auth"]["clerk"]["social_providers"] == ["linkedin"]


def test_clerk_session_sync_allows_only_configured_superadmin(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_super_route", "email": "guillen.marce@gmail.com", "email_verified": True},
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


def test_clerk_onboarding_route_creates_tenant(client, monkeypatch):
    monkeypatch.setattr(
        "routes.auth.verify_clerk_session_token",
        lambda token: {"sub": "user_route_2", "email": "owner2@chatboc.test", "email_verified": True},
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
        },
    )

    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["tenant"]["slug"] == "colegio-modelo"
    assert payload["onboarding"]["required"] is False

    with client.application.app_context():
        tenant = TenantProfile.query.filter_by(slug="colegio-modelo").first()
        assert tenant is not None
        assert tenant.configuracion["auth"]["provider"] == "clerk"


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

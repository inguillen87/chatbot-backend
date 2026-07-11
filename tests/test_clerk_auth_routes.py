import base64
import hashlib
import hmac
import json
import time

from database import db
from models import TenantProfile, User
from services.clerk_auth_service import ClerkAuthError, ClerkNotConfigured, upsert_user_from_clerk


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
    payload = resp.get_json()
    assert payload["contract_version"] == "auth.clerk.v1"
    assert payload["enabled"] is True
    assert payload["session_sync_endpoint"] == "/auth/clerk/session"
    assert payload["publishable_key"] == "pk_test_public"
    assert payload["environment"] == "development"
    assert payload["production_ready"] is False
    assert payload["ready_for_session_sync"] is True


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
    assert payload["token"] is None
    assert payload["user"]["email"] == "laura@chatboc.test"
    assert payload["onboarding"]["required"] is True
    assert payload["onboarding"]["modal"]["vertical_presets"]["pyme"]["primary_goal"] == "ventas"
    assert payload["onboarding"]["modal"]["profile_picture_policy"] == "consented_upload_or_social_only"

    with client.application.app_context():
        user = User.query.filter_by(email="laura@chatboc.test").first()
        assert user is not None
        assert user.accesibilidad["auth"]["clerk"]["social_providers"] == ["linkedin"]


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
    )

    assert response.status_code == 200
    assert response.get_json()["disconnected_sockets"] == 2
    assert disconnected == [
        {
            "clerk_session_id": "sess_webhook_disconnect",
            "clerk_user_id": "user_webhook_disconnect",
        }
    ]

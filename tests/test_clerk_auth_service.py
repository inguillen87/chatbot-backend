from datetime import datetime, timezone

import jwt

from database import db
from models import TenantProfile, User
import services.clerk_auth_service as clerk_service
from services.clerk_auth_service import (
    ClerkAuthError,
    ClerkNotConfigured,
    ClerkTenantInactive,
    build_chatboc_session_payload,
    build_onboarding_contract,
    complete_clerk_onboarding,
    extract_clerk_identity,
    fetch_trusted_clerk_profile,
    is_clerk_session_revoked,
    sync_clerk_webhook_event,
    upsert_user_from_clerk,
    verify_active_clerk_session,
    verify_clerk_session_token,
)
from utils.auth_helpers import generar_token, user_from_token


def _claims(sub="user_clerk_123", email="owner@chatboc.test"):
    return {
        "sub": sub,
        "sid": "sess_chatboc_123",
        "sts": "active",
        "email": email,
        "email_verified": True,
        "iat": int(datetime.now(timezone.utc).timestamp()),
    }


def _profile(email="owner@chatboc.test"):
    return {
        "id": "user_clerk_123",
        "first_name": "Marcelo",
        "last_name": "Owner",
        "primary_email_address_id": "email_1",
        "email_addresses": [
            {
                "id": "email_1",
                "email_address": email,
                "verification": {"status": "verified"},
            }
        ],
        "external_accounts": [
            {"provider": "oauth_facebook"},
            {"provider": "oauth_linkedin_oidc"},
        ],
        "image_url": "https://img.clerk.test/users/user_clerk_123.jpg",
    }


def test_upsert_user_from_clerk_creates_user_with_social_metadata(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()

        assert user.id is not None
        assert user.email == "owner@chatboc.test"
        assert user.rol == "usuario"
        assert user.email_verified is True
        auth_meta = user.accesibilidad["auth"]
        assert auth_meta["provider"] == "clerk"
        assert auth_meta["clerk"]["user_id"] == "user_clerk_123"
        assert auth_meta["clerk"]["social_providers"] == ["facebook", "linkedin"]
        assert user.accesibilidad["identity"]["avatar_url"] == "https://img.clerk.test/users/user_clerk_123.jpg"
        assert user.accesibilidad["identity"]["avatar_source"] == "clerk"


def test_upsert_user_from_clerk_links_existing_email_without_resetting_identity(client):
    with client.application.app_context():
        existing = User(name="Existing", email="owner@chatboc.test", rol="usuario")
        existing.set_password("legacy-password")
        db.session.add(existing)
        db.session.commit()
        previous_hash = existing.password_hash

        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()

        assert user.id == existing.id
        assert user.password_hash == previous_hash
        assert user.accesibilidad["auth"]["clerk"]["user_id"] == "user_clerk_123"


def test_clerk_superadmin_role_is_limited_to_allowlisted_email(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        allowed = upsert_user_from_clerk(
            _claims(sub="user_superadmin", email="guillen.marce@gmail.com"),
            _profile(email="guillen.marce@gmail.com") | {"id": "user_superadmin"},
            profile_is_trusted=True,
        )
        db.session.commit()

        assert allowed.rol == "super_admin"
        payload = build_chatboc_session_payload(allowed, clerk_claims=_claims(sub="user_superadmin", email="guillen.marce@gmail.com"))
        assert payload["user"]["role"] == "super_admin"
        assert payload["onboarding"]["required"] is False
        assert payload["onboarding"]["status"] == "platform_admin"
        decoded = jwt.decode(
            payload["token"],
            client.application.config["SECRET_KEY"],
            algorithms=["HS256"],
        )
        assert decoded["auth_provider"] == "clerk"
        assert decoded["session_kind"] == "clerk"
        assert decoded["clerk_sid"] == "sess_chatboc_123"
        assert decoded["clerk_user_id"] == "user_superadmin"
        assert decoded["jti"]
        assert decoded["sv"] == 1


def test_clerk_session_is_not_issued_before_onboarding(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()

        payload = build_chatboc_session_payload(user, clerk_claims=_claims())

        assert payload["onboarding"]["required"] is True
        assert payload["token"] is None


def test_inactive_tenant_cannot_receive_or_reuse_clerk_session(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.flush()
        tenant = TenantProfile(
            slug="inactive-clerk-tenant",
            nombre="Inactive Clerk Tenant",
            tipo="pyme",
            pyme_id=user.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        db.session.commit()
        complete_clerk_onboarding(
            user,
            {"terms_accepted": True, "terms_version": "2026-07-11"},
        )

        active_token = build_chatboc_session_payload(
            user,
            tenant,
            clerk_claims=_claims(),
        )["token"]
        assert active_token
        assert user_from_token(active_token).id == user.id

        tenant.is_active = False
        db.session.commit()

        assert user_from_token(active_token) is None
        try:
            build_chatboc_session_payload(user, tenant, clerk_claims=_claims())
        except ClerkTenantInactive:
            pass
        else:
            raise AssertionError("Inactive tenants must not receive new Chatboc sessions")


def test_clerk_linked_user_cannot_reuse_legacy_password_token(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        user.set_password("legacy-password")
        db.session.commit()
        legacy_token = generar_token(user.id, user.rol, user.tipo_chat, None, None)

        assert user_from_token(legacy_token) is None


def test_clerk_superadmin_role_is_removed_from_non_allowlisted_email(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        existing = User(name="Legacy Super", email="legacy-super@chatboc.test", rol="super_admin")
        existing.set_password("legacy-password")
        db.session.add(existing)
        db.session.commit()

        user = upsert_user_from_clerk(
            _claims(sub="user_legacy_super", email="legacy-super@chatboc.test"),
            _profile(email="legacy-super@chatboc.test") | {"id": "user_legacy_super"},
            profile_is_trusted=True,
        )
        db.session.commit()

        assert user.id == existing.id
        assert user.rol == "usuario"
        assert build_chatboc_session_payload(user)["user"]["role"] == "usuario"


def test_untrusted_browser_profile_cannot_claim_existing_superadmin(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        existing = User(name="Platform Owner", email="guillen.marce@gmail.com", rol="super_admin")
        existing.set_password("legacy-password")
        db.session.add(existing)
        db.session.commit()

        forged_profile = _profile(email="guillen.marce@gmail.com") | {"id": "attacker_clerk_id"}
        try:
            upsert_user_from_clerk({"sub": "attacker_clerk_id"}, forged_profile)
        except ClerkNotConfigured:
            pass
        else:
            raise AssertionError("Untrusted browser profile must not provide an account-linking email")

        db.session.refresh(existing)
        assert existing.accesibilidad is None
        assert existing.rol == "super_admin"


def test_unverified_clerk_email_cannot_link_existing_account(client):
    with client.application.app_context():
        existing = User(name="Existing", email="owner@chatboc.test", rol="usuario")
        existing.set_password("legacy-password")
        db.session.add(existing)
        db.session.commit()

        try:
            upsert_user_from_clerk(
                {"sub": "user_unverified", "email": "owner@chatboc.test", "email_verified": False},
            )
        except ClerkAuthError as exc:
            assert "Verified Clerk email" in str(exc)
        else:
            raise AssertionError("Unverified Clerk email must not link an existing account")


def test_trusted_profile_email_and_verification_are_atomic(client):
    with client.application.app_context():
        identity = extract_clerk_identity(
            _claims(email="victim@chatboc.test"),
            _profile(email="attacker@chatboc.test"),
            profile_is_trusted=True,
        )

        assert identity["email"] == "attacker@chatboc.test"
        assert identity["email_verified"] is True
        assert identity["identity_trusted"] is True


def test_trusted_profile_lookup_fails_closed_on_provider_error(client, monkeypatch):
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-backend-secret")

    def _raise(*args, **kwargs):
        raise clerk_service.requests.RequestException("timeout")

    monkeypatch.setattr(clerk_service.requests, "get", _raise)
    with client.application.app_context():
        try:
            fetch_trusted_clerk_profile(_claims())
        except ClerkNotConfigured as exc:
            assert "unavailable" in str(exc)
        else:
            raise AssertionError("Backend API failures must not fall back to incomplete claims")


def test_verify_clerk_session_rejects_untrusted_authorized_party(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.ar")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar,https://www.chatboc.ar")

    class _SigningKey:
        key = "public-key"

    class _JwksClient:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    monkeypatch.setattr(clerk_service, "PyJWKClient", _JwksClient)
    monkeypatch.setattr(
        clerk_service.jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "user_attacker",
            "azp": "https://evil.example",
            "exp": 9999999999,
        },
    )

    with client.application.app_context():
        try:
            verify_clerk_session_token("header.payload.signature")
        except ClerkAuthError as exc:
            assert "authorized party" in str(exc)
        else:
            raise AssertionError("Clerk token from an untrusted azp must be rejected")


def test_verify_clerk_session_requires_real_session_id(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.ar")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")

    class _SigningKey:
        key = "public-key"

    class _JwksClient:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    monkeypatch.setattr(clerk_service, "PyJWKClient", _JwksClient)
    monkeypatch.setattr(
        clerk_service.jwt,
        "decode",
        lambda *args, **kwargs: {"sub": "user_template", "azp": "https://chatboc.ar", "exp": 9999999999},
    )

    with client.application.app_context():
        try:
            verify_clerk_session_token("header.payload.signature")
        except ClerkAuthError as exc:
            assert "session id" in str(exc).lower()
        else:
            raise AssertionError("JWT templates without sid must not be accepted as Clerk sessions")


def test_verify_clerk_session_rejects_pending_session(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.ar")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")

    class _SigningKey:
        key = "public-key"

    class _JwksClient:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    monkeypatch.setattr(clerk_service, "PyJWKClient", _JwksClient)
    monkeypatch.setattr(
        clerk_service.jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "user_pending",
            "sid": "sess_pending",
            "sts": "pending",
            "azp": "https://chatboc.ar",
            "exp": 9999999999,
        },
    )

    with client.application.app_context():
        try:
            verify_clerk_session_token("header.payload.signature")
        except ClerkAuthError as exc:
            assert "not active" in str(exc).lower()
        else:
            raise AssertionError("Pending Clerk sessions must not be exchanged")


def test_verify_clerk_session_confirms_active_sid_with_backend_api(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.ar")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-backend-secret")

    class _SigningKey:
        key = "public-key"

    class _JwksClient:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "sess_active_lookup", "user_id": "user_active_lookup", "status": "active"}

    requested = []
    monkeypatch.setattr(clerk_service, "PyJWKClient", _JwksClient)
    monkeypatch.setattr(
        clerk_service.jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "user_active_lookup",
            "sid": "sess_active_lookup",
            "sts": "active",
            "azp": "https://chatboc.ar",
            "exp": 9999999999,
        },
    )
    monkeypatch.setattr(
        clerk_service.requests,
        "get",
        lambda url, **kwargs: requested.append((url, kwargs)) or _Response(),
    )

    with client.application.app_context():
        claims = verify_clerk_session_token("header.payload.signature")

    assert claims["sid"] == "sess_active_lookup"
    assert requested[0][0] == "https://api.clerk.com/v1/sessions/sess_active_lookup"
    assert requested[0][1]["headers"]["Authorization"] == "Bearer test-clerk-backend-secret"
    assert requested[0][1]["timeout"] == 5


def test_verify_clerk_session_rejects_terminal_sid_and_caches_revocation(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.ar")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-backend-secret")

    class _SigningKey:
        key = "public-key"

    class _JwksClient:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "sess_revoked_lookup", "user_id": "user_revoked_lookup", "status": "revoked"}

    calls = []
    monkeypatch.setattr(clerk_service, "PyJWKClient", _JwksClient)
    monkeypatch.setattr(
        clerk_service.jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "user_revoked_lookup",
            "sid": "sess_revoked_lookup",
            "sts": "active",
            "azp": "https://chatboc.ar",
            "exp": 9999999999,
        },
    )
    monkeypatch.setattr(
        clerk_service.requests,
        "get",
        lambda *args, **kwargs: calls.append(args[0]) or _Response(),
    )

    with client.application.app_context():
        for _ in range(2):
            try:
                verify_clerk_session_token("header.payload.signature")
            except ClerkAuthError as exc:
                assert "not active" in str(exc).lower() or "revoked" in str(exc).lower()
            else:
                raise AssertionError("Terminal Clerk sessions must never be re-exchanged")

    assert calls == ["https://api.clerk.com/v1/sessions/sess_revoked_lookup"]
    assert is_clerk_session_revoked("sess_revoked_lookup") is True


def test_verify_clerk_session_fails_closed_when_backend_api_is_unavailable(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "pk_live_public")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.ar")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://chatboc.ar")
    monkeypatch.setenv("CLERK_REQUIRE_AZP", "true")
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-clerk-backend-secret")

    class _SigningKey:
        key = "public-key"

    class _JwksClient:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    monkeypatch.setattr(clerk_service, "PyJWKClient", _JwksClient)
    monkeypatch.setattr(
        clerk_service.jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "user_outage_lookup",
            "sid": "sess_outage_lookup",
            "sts": "active",
            "azp": "https://chatboc.ar",
            "exp": 9999999999,
        },
    )

    def _raise(*args, **kwargs):
        raise clerk_service.requests.RequestException("timeout")

    monkeypatch.setattr(clerk_service.requests, "get", _raise)
    with client.application.app_context():
        try:
            verify_clerk_session_token("header.payload.signature")
        except ClerkNotConfigured as exc:
            assert "session lookup is unavailable" in str(exc).lower()
        else:
            raise AssertionError("Production session exchange must fail closed on Clerk outage")


def test_onboarding_contract_requires_tenant_until_created(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()

        contract = build_onboarding_contract(user)

        assert contract["required"] is True
        assert contract["submit_endpoint"] == "/auth/clerk/onboarding"
        assert contract["modal"]["social_login"]["required_dashboard_setup"] == []
        assert contract["modal"]["social_login"]["connection_aliases"]["linkedin"] == "linkedin_oidc"
        assert contract["modal"]["whatsapp_business_requirements"] == {
            "production_enabled_by_default": False,
            "required_plan": "full",
            "required_provider_setup": ["meta_business", "twilio_whatsapp_sender"],
            "free_plan_state": "created_without_waba_provisioning",
            "message": "El tenant se crea en modo free. WhatsApp productivo y creacion real de plantillas se activan con plan Full y sender Meta/Twilio configurado.",
        }
        assert contract["modal"]["plan_policy"] == {
            "self_service_plan": "free",
            "requested_plan_allowed": False,
            "productive_plan": "full",
            "upgrade_requires": "superadmin_or_commercial_approval",
            "message": "El registro publico siempre crea un espacio Free. El plan Full se solicita para revision comercial y solo se concede desde administracion.",
        }
        assert contract["modal"]["profile_picture_policy"] == "consented_upload_or_social_only"
        assert "municipio" in contract["modal"]["vertical_presets"]
        assert contract["modal"]["vertical_presets"]["municipio"]["primary_goal"] == "crm_reclamos"
        assert any(module["id"] == "analytics_heatmaps" for module in contract["modal"]["starter_modules"])


def test_complete_clerk_onboarding_creates_tenant_without_password_reset(client, monkeypatch):
    with client.application.app_context():
        monkeypatch.setattr("services.clerk_auth_service.send_verification_email", lambda *args, **kwargs: True)
        monkeypatch.setattr("services.clerk_auth_service.send_onboarding_whatsapp", lambda *args, **kwargs: True)

        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        user.set_password("legacy-password")
        db.session.commit()
        previous_hash = user.password_hash

        tenant = complete_clerk_onboarding(
            user,
            {
                "tenant_name": "Municipalidad Demo",
                "vertical": "municipio",
                "rubro": "gobierno",
                "telefono": "+5492613000000",
                "ciudad": "Junin",
                "primary_goal": "crm_reclamos",
                "preferred_channels": ["whatsapp", "web"],
                "plan": "full",
                "terms_accepted": True,
                "terms_version": "2026-07-11",
            },
        )

        assert tenant.id is not None
        assert tenant.slug == "municipalidad-demo"
        assert tenant.tipo == "municipio"
        assert tenant.plan == "free"
        assert tenant.configuracion["auth"]["provider"] == "clerk"
        assert tenant.configuracion["provisioning"]["status"] == "plan_required"
        assert tenant.configuracion["provisioning"]["blocked_reason"] == "plan_full_required"
        assert tenant.configuracion["onboarding"]["status"] == "completed"
        assert tenant.configuracion["onboarding"]["primary_goal"] == "crm_reclamos"
        assert tenant.configuracion["onboarding"]["preferred_channels"] == ["whatsapp", "web"]
        assert tenant.configuracion["onboarding"]["requested_plan"] == "full"
        assert tenant.configuracion["onboarding"]["granted_plan"] == "free"
        assert tenant.configuracion["onboarding"]["plan_policy"] == "self_service_creates_free_until_admin_upgrade"
        assert tenant.configuracion["onboarding"]["starter_modules"] == [
            "crm_operativo",
            "whatsapp_widget",
            "analytics_heatmaps",
        ]
        assert tenant.configuracion["onboarding"]["terms"]["version"] == "2026-07-11"

        refreshed = User.query.get(user.id)
        assert refreshed.password_hash == previous_hash
        assert refreshed.tenant_id == tenant.id
        assert refreshed.rol in {"admin", "municipio_admin", "admin_municipio"}
        assert refreshed.acepto_terminos is True
        assert refreshed.accesibilidad["auth"]["clerk"]["terms"]["source"] == "clerk_tenant_onboarding"

        payload = build_chatboc_session_payload(refreshed, tenant, clerk_claims=_claims())
        assert payload["tenant"]["slug"] == "municipalidad-demo"
        assert payload["onboarding"]["required"] is False
        assert payload["user"]["avatar_url"] == "https://img.clerk.test/users/user_clerk_123.jpg"
        assert payload["user"]["picture"] == payload["user"]["avatar_url"]
        assert payload["user"]["avatar_consent"] is True
        assert payload["user"]["profile_picture_consent"] is True
        assert payload["user"]["identity"]["policy"] == "consented_upload_or_social_only"
        assert payload["user"]["identity"]["fallback"] == "deterministic_identity_avatar"

        assert TenantProfile.query.filter_by(slug="municipalidad-demo").count() == 1


def test_complete_clerk_onboarding_requires_explicit_terms(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()

        try:
            complete_clerk_onboarding(
                user,
                {
                    "tenant_name": "Tenant sin consentimiento",
                    "vertical": "pyme",
                    "rubro": "ventas",
                },
            )
        except ClerkAuthError as exc:
            assert "aceptar" in str(exc).lower()
        else:
            raise AssertionError("Tenant onboarding must require explicit terms consent")

        assert TenantProfile.query.filter_by(slug="tenant-sin-consentimiento").first() is None
        db.session.refresh(user)
        assert user.acepto_terminos is False


def test_complete_clerk_onboarding_rejects_missing_terms_version(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()

        try:
            complete_clerk_onboarding(
                user,
                {
                    "tenant_name": "Tenant sin version",
                    "vertical": "pyme",
                    "rubro": "ventas",
                    "terms_accepted": True,
                },
            )
        except ClerkAuthError as exc:
            assert "version" in str(exc).lower()
        else:
            raise AssertionError("Onboarding must require the exact server terms version")

        assert TenantProfile.query.filter_by(slug="tenant-sin-version").first() is None


def test_existing_tenant_must_accept_current_server_terms(client, monkeypatch):
    monkeypatch.setattr("services.clerk_auth_service.send_verification_email", lambda *args, **kwargs: True)
    monkeypatch.setattr("services.clerk_auth_service.send_onboarding_whatsapp", lambda *args, **kwargs: True)

    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()
        tenant = complete_clerk_onboarding(
            user,
            {
                "tenant_name": "Empresa Legal",
                "vertical": "pyme",
                "rubro": "ventas",
                "terms_accepted": True,
                "terms_version": "2026-07-11",
            },
        )

        monkeypatch.setenv("CHATBOC_TERMS_VERSION", "2026-08-01")
        pending = build_onboarding_contract(user, tenant)
        assert pending["required"] is True
        assert pending["status"] == "terms_pending"
        assert pending["modal"]["mode"] == "terms_only"

        try:
            complete_clerk_onboarding(
                user,
                {"terms_accepted": True, "terms_version": "2026-07-11"},
            )
        except ClerkAuthError as exc:
            assert "version" in str(exc).lower()
        else:
            raise AssertionError("The browser cannot choose an outdated legal version")

        accepted_tenant = complete_clerk_onboarding(
            user,
            {"terms_accepted": True, "terms_version": "2026-08-01"},
        )
        assert accepted_tenant.id == tenant.id
        assert build_onboarding_contract(user, tenant)["required"] is False
        assert user.accesibilidad["auth"]["clerk"]["terms"]["version"] == "2026-08-01"


def test_clerk_user_deleted_revokes_local_sessions_and_credentials(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        user.set_password("legacy-password")
        user.token = "known-static-token"
        user.entity_token = "known-entity-token"
        db.session.commit()
        user.rol = "super_admin"
        user.email = "guillen.marce@gmail.com"
        db.session.commit()
        session_token = build_chatboc_session_payload(
            user,
            clerk_claims=_claims(email="guillen.marce@gmail.com"),
        )["token"]

        result = sync_clerk_webhook_event({"type": "user.deleted", "data": {"id": "user_clerk_123"}})

        assert result["status"] == "marked_deleted"
        db.session.refresh(user)
        assert user.rol == "usuario"
        assert user.check_password("legacy-password") is False
        assert user.token != "known-static-token"
        assert user.entity_token is None
        assert user.accesibilidad["auth"]["clerk"]["disabled"] is True
        assert user_from_token(session_token) is None


def test_terminal_clerk_session_event_revokes_chatboc_jwt(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    with client.application.app_context():
        claims = {
            **_claims(email="guillen.marce@gmail.com"),
            "sid": "sess_terminal_webhook",
        }
        user = upsert_user_from_clerk(
            claims,
            _profile(email="guillen.marce@gmail.com"),
            profile_is_trusted=True,
        )
        db.session.commit()
        session_token = build_chatboc_session_payload(user, clerk_claims=claims)["token"]
        assert user_from_token(session_token).id == user.id

        result = sync_clerk_webhook_event(
            {
                "type": "session.ended",
                "data": {"id": "sess_terminal_webhook", "user_id": "user_clerk_123"},
            }
        )

        assert result["status"] == "sessions_revoked"
        assert result["session_version"] == 2
        assert result["clerk_session_id"] == "sess_terminal_webhook"
        assert is_clerk_session_revoked("sess_terminal_webhook") is True
        monkeypatch.setattr(
            clerk_service.requests,
            "get",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("local revocation must win")),
        )
        try:
            verify_active_clerk_session(claims)
        except ClerkAuthError as exc:
            assert "revoked" in str(exc).lower()
        else:
            raise AssertionError("Webhook-revoked sid must not be exchangeable again")
        assert user_from_token(session_token) is None


def test_user_updated_after_delete_cannot_resurrect_account(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        db.session.commit()
        user_id = user.id

        sync_clerk_webhook_event({"type": "user.deleted", "data": {"id": "user_clerk_123"}})
        result = sync_clerk_webhook_event({"type": "user.updated", "data": _profile()})

        assert result["status"] == "ignored_disabled"
        refreshed = User.query.get(user_id)
        assert refreshed.accesibilidad["auth"]["clerk"]["disabled"] is True
        try:
            upsert_user_from_clerk(_claims(), _profile(), profile_is_trusted=True)
        except ClerkAuthError as exc:
            assert "disabled" in str(exc).lower()
        else:
            raise AssertionError("Deleted Clerk users must require an explicit recovery flow")

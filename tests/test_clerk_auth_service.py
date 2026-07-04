from datetime import datetime, timezone

from database import db
from models import TenantProfile, User
from services.clerk_auth_service import (
    build_chatboc_session_payload,
    build_onboarding_contract,
    complete_clerk_onboarding,
    upsert_user_from_clerk,
)


def _claims(sub="user_clerk_123", email="owner@chatboc.test"):
    return {"sub": sub, "email": email, "email_verified": True, "iat": int(datetime.now(timezone.utc).timestamp())}


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
        user = upsert_user_from_clerk(_claims(), _profile())
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

        user = upsert_user_from_clerk(_claims(), _profile())
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
        )
        db.session.commit()

        assert allowed.rol == "super_admin"
        payload = build_chatboc_session_payload(allowed)
        assert payload["user"]["role"] == "super_admin"
        assert payload["onboarding"]["required"] is False
        assert payload["onboarding"]["status"] == "platform_admin"


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
        )
        db.session.commit()

        assert user.id == existing.id
        assert user.rol == "usuario"
        assert build_chatboc_session_payload(user)["user"]["role"] == "usuario"


def test_onboarding_contract_requires_tenant_until_created(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile())
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

        user = upsert_user_from_clerk(_claims(), _profile())
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

        refreshed = User.query.get(user.id)
        assert refreshed.password_hash == previous_hash
        assert refreshed.tenant_id == tenant.id
        assert refreshed.rol in {"admin", "municipio_admin", "admin_municipio"}

        payload = build_chatboc_session_payload(refreshed, tenant)
        assert payload["tenant"]["slug"] == "municipalidad-demo"
        assert payload["onboarding"]["required"] is False
        assert payload["user"]["avatar_url"] == "https://img.clerk.test/users/user_clerk_123.jpg"
        assert payload["user"]["picture"] == payload["user"]["avatar_url"]
        assert payload["user"]["avatar_consent"] is True
        assert payload["user"]["profile_picture_consent"] is True
        assert payload["user"]["identity"]["policy"] == "consented_upload_or_social_only"
        assert payload["user"]["identity"]["fallback"] == "deterministic_identity_avatar"

        assert TenantProfile.query.filter_by(slug="municipalidad-demo").count() == 1

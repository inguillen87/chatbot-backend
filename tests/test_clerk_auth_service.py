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
    }


def test_upsert_user_from_clerk_creates_user_with_social_metadata(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile())
        db.session.commit()

        assert user.id is not None
        assert user.email == "owner@chatboc.test"
        assert user.email_verified is True
        auth_meta = user.accesibilidad["auth"]
        assert auth_meta["provider"] == "clerk"
        assert auth_meta["clerk"]["user_id"] == "user_clerk_123"
        assert auth_meta["clerk"]["social_providers"] == ["facebook", "linkedin"]


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


def test_onboarding_contract_requires_tenant_until_created(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims(), _profile())
        db.session.commit()

        contract = build_onboarding_contract(user)

        assert contract["required"] is True
        assert contract["submit_endpoint"] == "/auth/clerk/onboarding"
        assert "facebook" in contract["modal"]["social_login"]["required_dashboard_setup"]
        assert "linkedin" in contract["modal"]["social_login"]["required_dashboard_setup"]


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
            },
        )

        assert tenant.id is not None
        assert tenant.slug == "municipalidad-demo"
        assert tenant.tipo == "municipio"
        assert tenant.configuracion["auth"]["provider"] == "clerk"
        assert tenant.configuracion["onboarding"]["status"] == "completed"

        refreshed = User.query.get(user.id)
        assert refreshed.password_hash == previous_hash
        assert refreshed.tenant_id == tenant.id
        assert refreshed.rol in {"admin", "municipio_admin", "admin_municipio"}

        payload = build_chatboc_session_payload(refreshed, tenant)
        assert payload["tenant"]["slug"] == "municipalidad-demo"
        assert payload["onboarding"]["required"] is False

        assert TenantProfile.query.filter_by(slug="municipalidad-demo").count() == 1

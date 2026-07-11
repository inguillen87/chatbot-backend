from database import db
from init_tenants import _revoke_unauthorized_superadmins
from models import TenantProfile, User
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import generar_token, user_from_token
from utils.roles import superadmin_email_allowlist


def test_legacy_fixed_superadmin_is_downgraded_and_credentials_are_rotated(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        legacy = User(
            name="Legacy Platform Admin",
            email="marcelo@chatboc.ar",
            rol="super_admin",
            token="known-static-token",
            entity_token="known-entity-token",
        )
        legacy.set_password("Marcelog123")
        db.session.add(legacy)
        db.session.commit()
        previous_hash = legacy.password_hash

        assert _revoke_unauthorized_superadmins() == 1

        db.session.refresh(legacy)
        assert legacy.rol == "usuario"
        assert legacy.password_hash != previous_hash
        assert legacy.check_password("Marcelog123") is False
        assert legacy.token != "known-static-token"
        assert legacy.entity_token is None


def test_unauthorized_superadmin_role_does_not_bypass_tenant_boundary(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        owner = User(name="Owner", email="owner@chatboc.test", rol="admin")
        owner.set_password("owner-password")
        attacker = User(name="Legacy Super", email="legacy@chatboc.test", rol="super_admin")
        attacker.set_password("legacy-password")
        db.session.add_all([owner, attacker])
        db.session.flush()
        tenant = TenantProfile(slug="secure-tenant", nombre="Secure Tenant", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        assert _is_authorized_for_tenant(attacker, tenant_id=tenant.id) is False


def test_allowlisted_superadmin_keeps_global_access(client, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        owner = User(name="Owner", email="owner2@chatboc.test", rol="admin")
        owner.set_password("owner-password")
        allowed = User(name="Platform Owner", email="guillen.marce@gmail.com", rol="super_admin")
        allowed.set_password("random-password")
        db.session.add_all([owner, allowed])
        db.session.flush()
        tenant = TenantProfile(slug="global-tenant", nombre="Global Tenant", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        assert _revoke_unauthorized_superadmins() == 0
        assert _is_authorized_for_tenant(allowed, tenant_id=tenant.id) is True


def test_superadmin_cannot_bypass_clerk_with_local_password_or_legacy_jwt(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.chatboc.test")
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with client.application.app_context():
        allowed = User(name="Platform Owner", email="guillen.marce@gmail.com", rol="super_admin")
        allowed.set_password("legacy-password")
        db.session.add(allowed)
        db.session.commit()
        legacy_token = generar_token(allowed.id, allowed.rol, "plataforma", None, None)
        assert user_from_token(legacy_token) is None

    regular = client.post(
        "/auth/login",
        json={"email": "guillen.marce@gmail.com", "password": "legacy-password"},
    )
    admin = client.post(
        "/auth/admin/login",
        json={"email": "guillen.marce@gmail.com", "password": "legacy-password"},
    )

    assert regular.status_code == 403
    assert regular.get_json()["reason_code"] == "clerk_required"
    assert admin.status_code == 403
    assert admin.get_json()["reason_code"] == "clerk_required"


def test_legacy_aliases_cannot_expand_unique_superadmin_allowlist(client, monkeypatch):
    monkeypatch.delenv("CLERK_SUPERADMIN_EMAILS", raising=False)
    monkeypatch.setenv("CHATBOC_SUPERADMIN_EMAILS", "attacker@example.com")
    monkeypatch.setenv("CHATBOC_SUPERADMIN_EMAIL", "ops@example.com")

    assert superadmin_email_allowlist() == {"guillen.marce@gmail.com"}

"""HTTP contracts used to compare published identity with admin configuration."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from database import db
from models import TenantProfile, User, WidgetSettings
from services.auth_session_lifecycle import issue_token
from utils.auth_helpers import auth_session_version


@pytest.fixture
def presence_tenants(client):
    rows = []
    for tenant_id, slug in ((42, "presence-a"), (73, "presence-b")):
        owner = User(
            name=f"Owner {slug}", email=f"{slug}@example.invalid",
            rol="admin", tipo_chat="municipio", tenant_slug=slug,
        )
        owner.set_password("synthetic-presence-password")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            id=tenant_id, slug=slug, nombre=f"Institution {slug}",
            tipo="municipio", municipio_id=owner.id, plan="free",
            logo_url=f"https://assets.example.invalid/{slug}.svg",
            dominio=f"{slug}.example.invalid",
            tema={"primaryColor": "#135724", "secondaryColor": "#FFFFFF"},
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.municipio_id = owner.id
        rows.append((owner, tenant))
    db.session.commit()
    return rows


def _headers(user, tenant):
    now = datetime.now(timezone.utc)
    payload = {"user_id": user.id, "exp": now + timedelta(hours=1)}
    if user.rol == "super_admin":
        user.accesibilidad = {"auth": {
            "provider": "clerk", "session_version": auth_session_version(user),
            "clerk": {"user_id": f"synthetic_presence_superadmin_{user.id}"},
        }}
        payload.update(
            rol=user.rol, auth_provider="clerk", session_kind="clerk",
            sid="sess_synthetic_presence", clerk_sid="sess_synthetic_presence",
            jti="synthetic_presence_jti", sv=auth_session_version(user), iat=now,
        )
    return {"Authorization": f"Bearer {issue_token(payload)}", "X-Tenant": tenant.slug}


@pytest.mark.parametrize("path", ("/api/public/tenant", "/public/tenant"))
def test_published_identity_uses_actual_row_in_both_namespaces(client, presence_tenants, path):
    owner, tenant = presence_tenants[0]
    other = presence_tenants[1][1]
    response = client.get(f"{path}?tenant={tenant.slug}", headers={"X-Tenant": other.slug})

    assert response.status_code == 200
    published = response.get_json()["tenant"]
    assert published["id"] == tenant.id != owner.id
    assert published["slug"] == tenant.slug
    assert published["nombre"] == tenant.nombre
    assert published["logo_url"] == tenant.logo_url
    assert published["dominio"] == tenant.dominio
    assert published["theme_config"]["light"]["primary"] == "#135724"


def test_api_identity_preserves_legacy_fields_and_limits_added_data(client, presence_tenants):
    _owner, tenant = presence_tenants[0]
    tenant.configuracion = {
        "provider_credentials": {"api_key": "synthetic-provider-secret"},
        "capabilities_json": {"internal_only": "synthetic-private-capability"},
    }
    db.session.add(WidgetSettings(tenant_id=tenant.id, theme_config={
        "mode": "dark", "light": {"primary": "#246813", "api_key": "synthetic-theme-secret"},
        "provider_credentials": {"token": "synthetic-widget-secret"},
        "capabilities_json": {"private": "synthetic-theme-capability"},
    }))
    db.session.commit()

    response = client.get(f"/api/public/tenant?tenant={tenant.slug}")

    assert response.status_code == 200
    body = response.get_json()
    assert {key: body[key] for key in ("slug", "nombre", "logo_url", "tipo", "tipo_chat", "tema")} == {
        "slug": tenant.slug, "nombre": tenant.nombre, "logo_url": tenant.logo_url,
        "tipo": tenant.tipo, "tipo_chat": tenant.tipo, "tema": tenant.tema,
    }
    assert set(body["tenant"]) == {"id", "slug", "nombre", "logo_url", "dominio", "theme_config"}
    assert body["tenant"]["theme_config"]["mode"] == "dark"
    assert body["tenant"]["theme_config"]["light"]["primary"] == "#246813"
    serialized = json.dumps(body)
    for private_value in (
        "synthetic-provider-secret", "synthetic-private-capability", "synthetic-theme-secret",
        "synthetic-widget-secret", "synthetic-theme-capability",
    ):
        assert private_value not in serialized
    assert tenant.widget_settings.theme_config["light"]["api_key"] == "synthetic-theme-secret"


@pytest.mark.parametrize("path", ("/api/public/tenant", "/public/tenant"))
def test_unknown_explicit_slug_has_no_successful_identity_fallback(client, presence_tenants, path):
    response = client.get(
        f"{path}?tenant=presence-unknown", headers={"X-Tenant": presence_tenants[0][1].slug},
    )

    assert response.status_code == 404
    assert not response.get_json().get("tenant")


def test_api_id_selector_keeps_envelope_bound_to_resolved_row(client, presence_tenants):
    tenant = presence_tenants[0][1]
    other = presence_tenants[1][1]
    response = client.get(
        f"/api/public/tenant?tenant_id={tenant.id}", headers={"X-Tenant": other.slug},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["tenant"]["id"] == tenant.id
    assert body["tenant"]["slug"] == body["slug"] == tenant.slug


def test_admin_config_identity_matches_authorized_actual_row(client, presence_tenants):
    owner, tenant = presence_tenants[0]
    response = client.get(f"/api/admin/tenants/{tenant.slug}/config", headers=_headers(owner, tenant))

    assert response.status_code == 200
    assert response.get_json()["tenant"]["id"] == tenant.id != owner.id
    assert response.get_json()["tenant"]["slug"] == tenant.slug


def test_superadmin_config_id_is_from_url_row_despite_other_tenant_context(client, presence_tenants):
    tenant = presence_tenants[0][1]
    other = presence_tenants[1][1]
    superadmin = User(name="Presence Superadmin", email="presence-sa@example.invalid", rol="super_admin")
    superadmin.set_password("synthetic-presence-sa-password")
    db.session.add(superadmin)
    db.session.commit()
    response = client.get(
        f"/api/admin/tenants/{other.slug}/config", headers=_headers(superadmin, tenant),
    )

    assert response.status_code == 200
    assert response.get_json()["tenant"]["id"] == other.id
    assert response.get_json()["tenant"]["slug"] == other.slug


def test_admin_config_mismatched_membership_does_not_expose_identity(client, presence_tenants):
    owner, tenant = presence_tenants[0]
    other = presence_tenants[1][1]
    response = client.get(f"/api/admin/tenants/{other.slug}/config", headers=_headers(owner, tenant))

    assert response.status_code == 403
    assert "tenant" not in response.get_json()


def test_admin_config_unknown_slug_does_not_substitute_caller_row(client, presence_tenants):
    owner, tenant = presence_tenants[0]
    response = client.get("/api/admin/tenants/presence-unknown/config", headers=_headers(owner, tenant))

    assert response.status_code == 404
    assert "tenant" not in response.get_json()

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app import db
from models import TenantProfile, User
from models_memory import Contact


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed_crm_tenants():
    admin_a = User(email="crm-admin-a@test.com", name="Admin A", rol="admin", tipo_chat="pyme")
    admin_a.set_password("pass")
    admin_b = User(email="crm-admin-b@test.com", name="Admin B", rol="admin", tipo_chat="pyme")
    admin_b.set_password("pass")
    viewer = User(email="crm-viewer@test.com", name="Viewer", rol="usuario", tipo_chat="pyme")
    viewer.set_password("pass")
    db.session.add_all([admin_a, admin_b, viewer])
    db.session.flush()

    tenant_a = TenantProfile(slug="crm-a", nombre="CRM A", tipo="pyme", pyme_id=admin_a.id)
    tenant_b = TenantProfile(slug="crm-b", nombre="CRM B", tipo="pyme", pyme_id=admin_b.id)
    db.session.add_all([tenant_a, tenant_b])
    db.session.flush()

    admin_a.tenant_id = tenant_a.id
    admin_a.tenant_slug = tenant_a.slug
    admin_b.tenant_id = tenant_b.id
    admin_b.tenant_slug = tenant_b.slug
    viewer.tenant_id = tenant_a.id
    viewer.tenant_slug = tenant_a.slug

    db.session.add(
        Contact(
            id=str(uuid4()),
            tenant_id=tenant_a.id,
            name="Marcelo Vecino",
            phone="+5492613000000",
            type="neighbor",
            preferences={"conversation_status": "en_seguimiento"},
        )
    )
    db.session.commit()
    return admin_a, admin_b, viewer, tenant_a, tenant_b


def test_crm_tenant_operator_can_read_own_contacts(client, app):
    admin_a, _, _, tenant_a, _ = _seed_crm_tenants()

    response = client.get(
        f"/api/admin/tenants/{tenant_a.slug}/contacts",
        headers=_auth_headers(app, admin_a, tenant_a.slug),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["contacts"]) == 1
    assert payload["contacts"][0]["name"] == "Marcelo Vecino"


def test_crm_tenant_operator_cannot_read_another_tenant(client, app):
    admin_a, _, _, _, tenant_b = _seed_crm_tenants()

    response = client.get(
        f"/api/admin/tenants/{tenant_b.slug}/contacts",
        headers=_auth_headers(app, admin_a, tenant_b.slug),
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["contract_version"] == "shared.error.v1"
    assert payload["reason_code"] == "tenant_access_denied"
    assert payload["action_hint"] == "switch_tenant"


def test_crm_tenant_viewer_role_cannot_operate_crm(client, app):
    _, _, viewer, tenant_a, _ = _seed_crm_tenants()

    response = client.get(
        f"/api/admin/tenants/{tenant_a.slug}/contacts",
        headers=_auth_headers(app, viewer, tenant_a.slug),
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["contract_version"] == "shared.error.v1"
    assert payload["reason_code"] == "insufficient_permissions"
    assert payload["action_hint"] == "ask_admin"

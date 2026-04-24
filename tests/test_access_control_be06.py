from datetime import datetime, timedelta

import jwt

from app import db
from models import AuditEvent, OrgUnit, TenantProfile, User


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed_data():
    admin = User(email="be06-admin@test.com", name="Admin", rol="admin", tipo_chat="pyme")
    admin.set_password("pass")
    member = User(email="be06-member@test.com", name="Member", rol="usuario", tipo_chat="pyme")
    member.set_password("pass")
    db.session.add_all([admin, member])
    db.session.flush()

    tenant = TenantProfile(slug="be06-tenant", nombre="BE06 Tenant", tipo="pyme", pyme_id=admin.id)
    db.session.add(tenant)
    db.session.commit()
    return admin, member, tenant


def test_be06_org_unit_role_and_audit_flow(client, app):
    admin, member, tenant = _seed_data()
    headers = _auth_headers(app, admin, tenant.slug)

    create_ou = client.post("/api/admin/org-units", headers=headers, json={"name": "Ventas"})
    assert create_ou.status_code == 201
    ou_id = create_ou.get_json()["id"]

    assign_role = client.post(f"/api/admin/users/{member.id}/roles", headers=headers, json={"role": "empleado"})
    assert assign_role.status_code in (200, 201)

    assign_ou = client.post(f"/api/admin/users/{member.id}/org-units", headers=headers, json={"org_unit_id": ou_id})
    assert assign_ou.status_code in (200, 201)

    audit = client.get("/api/admin/audit/events", headers=headers)
    assert audit.status_code == 200
    payload = audit.get_json()
    assert any(item.get("event_type") == "org_unit.created" for item in payload)
    assert OrgUnit.query.filter_by(id=ou_id, tenant_id=tenant.id).first() is not None
    assert AuditEvent.query.filter_by(tenant_id=tenant.id).count() >= 3

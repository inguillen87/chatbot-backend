from datetime import datetime, timedelta, timezone

import jwt

from app import db
from models import AdminAuditLog, TenantProfile, User


def _sa_headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_super_admin_sales_hot_leads_lists_latest_signal(client, app):
    sa = User(email="sa-hotleads@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner-hotleads@test.com", name="Owner", rol="pyme", tipo_chat="pyme")
    owner.set_password("pass")
    tenant = TenantProfile(
        slug="tenant-hot-lead",
        nombre="Tenant Hot Lead",
        tipo="pyme",
        pyme_id=1,
        configuracion={"sales_funnel": {"contact_status": "contactado", "close_status": "abierto"}},
    )
    db.session.add_all([sa, owner])
    db.session.flush()
    tenant.pyme_id = owner.id
    db.session.add(tenant)
    db.session.flush()

    newer = datetime.now(timezone.utc) - timedelta(hours=1)
    older = datetime.now(timezone.utc) - timedelta(days=5)
    db.session.add_all(
        [
            AdminAuditLog(
                admin_user_id=sa.id,
                action="tenant_demo_whatsapp_activated",
                target_object=tenant.slug,
                details={
                    "tenant_id": tenant.id,
                    "sales_signal": "hot_lead",
                    "activation_state": {"activation_count": 1, "activated_at": older.isoformat()},
                },
                created_at=older,
            ),
            AdminAuditLog(
                admin_user_id=sa.id,
                action="tenant_demo_whatsapp_activated",
                target_object=tenant.slug,
                details={
                    "tenant_id": tenant.id,
                    "sales_signal": "hot_lead",
                    "activation_state": {"activation_count": 1, "activated_at": newer.isoformat()},
                },
                created_at=newer,
            ),
        ]
    )
    db.session.commit()

    resp = client.get("/api/admin/sales/hot-leads", headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["tenant_slug"] == "tenant-hot-lead"
    assert payload["items"][0]["sales_signal"] == "hot_lead"
    assert payload["items"][0]["funnel"]["contact_status"] == "contactado"
    assert payload["items"][0]["is_open"] is True


def test_super_admin_sales_hot_leads_only_open_filter(client, app):
    sa = User(email="sa-hotleads-filter@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner-hotleads-filter@test.com", name="Owner", rol="pyme", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.flush()

    open_tenant = TenantProfile(
        slug="tenant-open-lead",
        nombre="Tenant Open",
        tipo="pyme",
        pyme_id=owner.id,
        configuracion={"sales_funnel": {"close_status": "abierto"}},
    )
    closed_tenant = TenantProfile(
        slug="tenant-closed-lead",
        nombre="Tenant Closed",
        tipo="pyme",
        pyme_id=owner.id,
        configuracion={"sales_funnel": {"close_status": "cerrado_ganado"}},
    )
    db.session.add_all([open_tenant, closed_tenant])
    db.session.flush()

    now = datetime.now(timezone.utc)
    db.session.add_all(
        [
            AdminAuditLog(
                admin_user_id=sa.id,
                action="tenant_demo_whatsapp_activated",
                target_object=open_tenant.slug,
                details={"tenant_id": open_tenant.id, "sales_signal": "hot_lead", "activation_state": {"activation_count": 1}},
                created_at=now - timedelta(hours=2),
            ),
            AdminAuditLog(
                admin_user_id=sa.id,
                action="tenant_demo_whatsapp_activated",
                target_object=closed_tenant.slug,
                details={"tenant_id": closed_tenant.id, "sales_signal": "hot_lead", "activation_state": {"activation_count": 1}},
                created_at=now - timedelta(hours=3),
            ),
        ]
    )
    db.session.commit()

    only_open_resp = client.get("/api/admin/sales/hot-leads", headers=_sa_headers(app, sa))
    assert only_open_resp.status_code == 200
    only_open_payload = only_open_resp.get_json()
    assert only_open_payload["total"] == 1
    assert only_open_payload["items"][0]["tenant_slug"] == "tenant-open-lead"

    include_closed_resp = client.get("/api/admin/sales/hot-leads?only_open=false", headers=_sa_headers(app, sa))
    assert include_closed_resp.status_code == 200
    include_closed_payload = include_closed_resp.get_json()
    assert include_closed_payload["total"] == 2

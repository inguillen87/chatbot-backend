from datetime import datetime, timedelta

import jwt

from app import db
from models import TenantProfile, User
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed():
    admin = User(email="wa-rules-admin@test.com", name="Admin", rol="admin", tipo_chat="pyme")
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(slug="wa-rules-tenant", nombre="WA Rules", tipo="pyme", pyme_id=admin.id)
    db.session.add(tenant)
    db.session.commit()
    return admin, tenant


def test_whatsapp_rules_update_and_notification_enforcement(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    put_resp = client.put(
        "/api/admin/whatsapp/rules",
        headers=headers,
        json={"enforce_template_outside_24h": True, "max_outbound_per_hour": 99, "blocked_keywords": ["prohibido"]},
    )
    assert put_resp.status_code == 200

    get_resp = client.get("/api/admin/whatsapp/rules", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.get_json()["enforce_template_outside_24h"] is True

    create = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "whatsapp",
            "recipient": "+5491112345678",
            "body": "mensaje normal",
            "idempotency_key": "wa-rule-1",
            "metadata": {"within_24h_window": False, "is_template": False},
        },
    )
    assert create.status_code == 201

    dispatch = client.post("/api/workers/notifications/dispatch", headers=headers)
    assert dispatch.status_code == 200
    data = dispatch.get_json()
    assert data["failed"] >= 1


def test_whatsapp_rules_uses_contact_state_24h_window(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    svc = WhatsAppEnterpriseRulesService(tenant.id)
    svc.register_inbound_activity(recipient="+5491112345678")
    db.session.commit()

    put_resp = client.put(
        "/api/admin/whatsapp/rules",
        headers=headers,
        json={"enforce_template_outside_24h": True},
    )
    assert put_resp.status_code == 200

    create = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "whatsapp",
            "recipient": "+5491112345678",
            "body": "mensaje freeform permitido por inbound reciente",
            "idempotency_key": "wa-rule-state-1",
            "metadata": {"is_template": False},
        },
    )
    assert create.status_code == 201

    dispatch = client.post("/api/workers/notifications/dispatch", headers=headers)
    assert dispatch.status_code == 200
    payload = dispatch.get_json()
    assert payload["sent"] >= 1


def test_whatsapp_template_catalog_and_policy_test_endpoint(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    created = client.post(
        "/api/admin/templates",
        headers=headers,
        json={
            "key": "wa_promo_1",
            "body_template": "Hola ${name}",
            "health_status": "healthy",
        },
    )
    assert created.status_code == 201
    template_id = created.get_json()["id"]

    listed = client.get("/api/admin/templates", headers=headers)
    assert listed.status_code == 200
    data = listed.get_json()
    assert any(t["id"] == template_id and t["health_status"] == "healthy" for t in data)

    patched = client.patch(
        f"/api/admin/templates/{template_id}",
        headers=headers,
        json={"health_status": "degraded", "is_active": False},
    )
    assert patched.status_code == 200
    assert patched.get_json()["updated"] is True

    policy = client.post(
        "/api/notifications/whatsapp/test",
        headers=headers,
        json={
            "recipient": "+5491112345678",
            "body": "hola",
            "metadata": {"within_24h_window": False, "is_template": False},
        },
    )
    assert policy.status_code == 200
    policy_data = policy.get_json()
    assert policy_data["allowed"] is False
    assert policy_data["reason"] == "template_required"

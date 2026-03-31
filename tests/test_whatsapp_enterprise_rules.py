from datetime import datetime, timedelta

import jwt

from app import db
from models import TenantProfile, User


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

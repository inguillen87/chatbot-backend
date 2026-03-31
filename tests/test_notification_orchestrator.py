from datetime import datetime, timedelta

import jwt

from app import db
from models import Notification, NotificationAttempt, TenantProfile, User


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed_admin_tenant():
    admin = User(email="notif-admin@test.com", name="Admin", rol="admin", tipo_chat="pyme")
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()

    tenant = TenantProfile(slug="notif-tenant", nombre="Notif Tenant", tipo="pyme", pyme_id=admin.id)
    db.session.add(tenant)
    db.session.commit()
    return admin, tenant


def test_notification_idempotency(client, app):
    admin, tenant = _seed_admin_tenant()
    headers = _auth_headers(app, admin, tenant.slug)

    first = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "in_app",
            "recipient": f"user:{admin.id}",
            "user_id": admin.id,
            "body": "Hola",
            "idempotency_key": "idem-1",
        },
    )
    assert first.status_code == 201
    first_payload = first.get_json()
    assert first_payload["created"] is True

    second = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "in_app",
            "recipient": f"user:{admin.id}",
            "user_id": admin.id,
            "body": "Hola duplicado",
            "idempotency_key": "idem-1",
        },
    )
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["created"] is False
    assert second_payload["id"] == first_payload["id"]


def test_notification_retry_attempts(client, app):
    admin, tenant = _seed_admin_tenant()
    headers = _auth_headers(app, admin, tenant.slug)

    queued = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "email",
            "recipient": "x@test.com",
            "subject": "Retry",
            "body": "will fail",
            "idempotency_key": "idem-retry-1",
            "max_retries": 2,
            "metadata": {"force_fail": True},
        },
    )
    assert queued.status_code == 201
    notif_id = queued.get_json()["id"]

    dispatch1 = client.post("/api/workers/notifications/dispatch", headers=headers, json={"limit": 20})
    assert dispatch1.status_code == 200
    data1 = dispatch1.get_json()
    assert data1["failed"] >= 1

    notif = Notification.query.filter_by(id=notif_id).first()
    assert notif is not None
    assert notif.attempt_count == 1
    assert notif.next_retry_at is not None

    notif.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()

    dispatch2 = client.post("/api/workers/notifications/dispatch", headers=headers, json={"limit": 20})
    assert dispatch2.status_code == 200

    attempts = NotificationAttempt.query.filter_by(notification_id=notif_id).all()
    assert len(attempts) >= 2

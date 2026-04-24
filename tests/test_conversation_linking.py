from datetime import datetime, timedelta

import jwt

from app import db
from models import ConversationLinkRequest, TenantProfile, User
from services.conversation_resolver import ConversationResolver


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed_conversation():
    owner = User(email="link-owner@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(slug="link-tenant", nombre="Link Tenant", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.flush()

    resolver = ConversationResolver(tenant.id)
    resolved = resolver.resolve_or_create(chat_session_id="web-session-link", channel="web", user_id=owner.id)
    resolver.append_message(
        conversation_id=resolved.conversation.id,
        channel_session_id=resolved.channel_session.id,
        sender_type="user",
        body="hola",
        sender_user_id=owner.id,
        direction="in",
    )
    db.session.commit()
    return owner, tenant, resolved


def test_link_whatsapp_happy_path(client, app, monkeypatch):
    owner, tenant, resolved = _seed_conversation()
    emitted = {}

    def _fake_emit(payload):
        emitted["payload"] = payload

    monkeypatch.setattr("routes.conversations.emit_conversation_linked", _fake_emit)

    headers = _auth_headers(app, owner, tenant.slug)
    create_resp = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={"conversation_id": resolved.conversation.id, "whatsapp_number": "+5491112345678"},
    )
    assert create_resp.status_code == 201
    body = create_resp.get_json()["link_request"]
    link_row = ConversationLinkRequest.query.filter_by(id=body["id"]).first()
    assert link_row is not None
    assert link_row.otp_code != body["otp_code"]
    assert len(link_row.otp_code) > 12

    confirm_resp = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={
            "deep_link_token": body["deep_link_token"],
            "otp_code": body["otp_code"],
            "whatsapp_chat_session_id": "wa-session-1",
        },
    )
    assert confirm_resp.status_code == 200
    payload = confirm_resp.get_json()
    assert payload["linked"] is True
    assert payload["status"] == "linked"
    assert emitted["payload"]["event"] == "conversation.linked"


def test_link_whatsapp_expired(client, app):
    owner, tenant, resolved = _seed_conversation()
    headers = _auth_headers(app, owner, tenant.slug)

    create_resp = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={
            "conversation_id": resolved.conversation.id,
            "whatsapp_number": "+5491112345678",
            "ttl_minutes": 0,
        },
    )
    assert create_resp.status_code == 201
    body = create_resp.get_json()["link_request"]

    req = ConversationLinkRequest.query.filter_by(id=body["id"]).first()
    req.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()

    confirm_resp = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={"deep_link_token": body["deep_link_token"], "otp_code": body["otp_code"]},
    )
    assert confirm_resp.status_code == 410


def test_link_whatsapp_invalid_otp(client, app):
    owner, tenant, resolved = _seed_conversation()
    headers = _auth_headers(app, owner, tenant.slug)

    create_resp = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={"conversation_id": resolved.conversation.id, "whatsapp_number": "+5491112345678"},
    )
    body = create_resp.get_json()["link_request"]

    confirm_resp = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={"deep_link_token": body["deep_link_token"], "otp_code": "999999"},
    )
    assert confirm_resp.status_code == 400


def test_link_whatsapp_repeated_confirm(client, app):
    owner, tenant, resolved = _seed_conversation()
    headers = _auth_headers(app, owner, tenant.slug)

    create_resp = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={"conversation_id": resolved.conversation.id, "whatsapp_number": "+5491112345678"},
    )
    body = create_resp.get_json()["link_request"]

    first = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={"deep_link_token": body["deep_link_token"], "otp_code": body["otp_code"]},
    )
    assert first.status_code == 200
    assert first.get_json()["status"] == "linked"

    second = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={"deep_link_token": body["deep_link_token"], "otp_code": body["otp_code"]},
    )
    assert second.status_code == 200
    assert second.get_json()["status"] == "already_confirmed"


def test_link_whatsapp_deeplink_without_otp(client, app):
    owner, tenant, resolved = _seed_conversation()
    headers = _auth_headers(app, owner, tenant.slug)

    create_resp = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={"conversation_id": resolved.conversation.id, "whatsapp_number": "+5491112345678"},
    )
    body = create_resp.get_json()["link_request"]

    confirm_resp = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={"deep_link_token": body["deep_link_token"]},
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.get_json()["status"] == "linked"


def test_link_whatsapp_status_endpoint(client, app):
    owner, tenant, resolved = _seed_conversation()
    headers = _auth_headers(app, owner, tenant.slug)

    create_resp = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={"conversation_id": resolved.conversation.id, "whatsapp_number": "+5491112345678"},
    )
    assert create_resp.status_code == 201
    link_request = create_resp.get_json()["link_request"]

    status_resp = client.get(f"/api/conversations/link/{link_request['id']}", headers=headers)
    assert status_resp.status_code == 200
    status_payload = status_resp.get_json()
    assert status_payload["id"] == link_request["id"]
    assert status_payload["status"] == "pending"

    confirm_resp = client.post(
        "/api/conversations/link/confirm",
        headers=headers,
        json={"deep_link_token": link_request["deep_link_token"], "otp_code": link_request["otp_code"]},
    )
    assert confirm_resp.status_code == 200

    status_after = client.get(f"/api/conversations/link/{link_request['id']}", headers=headers)
    assert status_after.status_code == 200
    assert status_after.get_json()["status"] == "confirmed"


def test_link_whatsapp_rate_limit_per_target_number(client, app):
    owner, tenant, resolved = _seed_conversation()
    headers = _auth_headers(app, owner, tenant.slug)

    for idx in range(3):
        resp = client.post(
            "/api/conversations/link/whatsapp",
            headers=headers,
            json={
                "conversation_id": resolved.conversation.id,
                "whatsapp_number": "+5491112345678",
                "ttl_minutes": 10,
            },
        )
        assert resp.status_code == 201, f"unexpected status on iteration {idx + 1}"

    limited = client.post(
        "/api/conversations/link/whatsapp",
        headers=headers,
        json={
            "conversation_id": resolved.conversation.id,
            "whatsapp_number": "+5491112345678",
            "ttl_minutes": 10,
        },
    )
    assert limited.status_code == 429
    assert "too many link requests" in limited.get_json()["error"]

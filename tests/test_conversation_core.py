from datetime import datetime, timedelta

import jwt

from app import db
from models import ChatSessionContext, Conversation, Message, TenantProfile, User
from routes.chat import _persist_core_conversation_messages
from services.conversation_resolver import ConversationResolver


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def test_conversation_resolver_links_chat_session_context(app):
    with app.app_context():
        owner = User(email="conv-owner@test.com", name="Owner", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(slug="conv-tenant", nombre="Conv Tenant", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.flush()

        ctx = ChatSessionContext(chat_session_id="legacy-chat-1", tenant_id=tenant.id, user_id=owner.id, context_data={})
        db.session.add(ctx)
        db.session.commit()

        resolver = ConversationResolver(tenant.id)
        resolved = resolver.resolve_or_create(
            chat_session_id="legacy-chat-1",
            channel="web",
            channel_identity="anon:1",
            user_id=owner.id,
        )
        resolver.append_message(
            conversation_id=resolved.conversation.id,
            channel_session_id=resolved.channel_session.id,
            sender_type="user",
            body="Hola",
            sender_user_id=owner.id,
            direction="in",
        )
        db.session.commit()

        ctx_db = ChatSessionContext.query.get("legacy-chat-1")
        assert ctx_db.conversation_id == resolved.conversation.id
        assert ctx_db.channel_session_id == resolved.channel_session.id
        assert Conversation.query.filter_by(id=resolved.conversation.id, tenant_id=tenant.id).count() == 1
        assert Message.query.filter_by(conversation_id=resolved.conversation.id, tenant_id=tenant.id).count() == 1


def test_conversation_timeline_endpoint_enforces_tenant_and_returns_timeline(client, app):
    owner = User(email="timeline-owner@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    outsider = User(email="timeline-outsider@test.com", name="Other", rol="usuario", tipo_chat="pyme")
    outsider.set_password("pass")
    db.session.add_all([owner, outsider])
    db.session.flush()

    tenant = TenantProfile(slug="timeline-tenant", nombre="Timeline Tenant", tipo="pyme", pyme_id=owner.id)
    tenant2 = TenantProfile(slug="timeline-tenant-2", nombre="Timeline Tenant 2", tipo="pyme", pyme_id=outsider.id)
    db.session.add_all([tenant, tenant2])
    db.session.flush()

    resolver = ConversationResolver(tenant.id)
    resolved = resolver.resolve_or_create(chat_session_id="legacy-chat-2", channel="web", user_id=owner.id)
    resolver.append_message(
        conversation_id=resolved.conversation.id,
        channel_session_id=resolved.channel_session.id,
        sender_type="user",
        body="primer mensaje",
        sender_user_id=owner.id,
        direction="in",
    )
    resolver.append_message(
        conversation_id=resolved.conversation.id,
        channel_session_id=resolved.channel_session.id,
        sender_type="assistant",
        body="respuesta",
        direction="out",
    )
    db.session.commit()

    ok_headers = _auth_headers(app, owner, "timeline-tenant")
    resp_ok = client.get(f"/api/conversations/{resolved.conversation.id}/timeline", headers=ok_headers)
    assert resp_ok.status_code == 200
    payload = resp_ok.get_json()
    assert payload["conversation"]["legacy_chat_session_id"] == "legacy-chat-2"
    assert [item["body"] for item in payload["timeline"]] == ["primer mensaje", "respuesta"]

    denied_headers = _auth_headers(app, outsider, "timeline-tenant")
    resp_denied = client.get(f"/api/conversations/{resolved.conversation.id}/timeline", headers=denied_headers)
    assert resp_denied.status_code == 403


def test_chat_persistence_helper_writes_core_messages(client, app):
    with app.app_context():
        owner = User(email="core-helper-owner@test.com", name="Owner", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(slug="core-helper-tenant", nombre="Core Helper Tenant", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.flush()

        db.session.add(
            ChatSessionContext(
                chat_session_id="core-helper-chat",
                tenant_id=tenant.id,
                user_id=owner.id,
                context_data={},
            )
        )
        db.session.commit()

        _persist_core_conversation_messages(
            tenant_id=tenant.id,
            chat_session_id="core-helper-chat",
            anon_id="anon-xyz",
            actor_user=owner,
            user_message="hola core",
            bot_payload={"respuesta": "respuesta core", "fuente": "test"},
        )
        db.session.commit()

        conversation = Conversation.query.filter_by(tenant_id=tenant.id, legacy_chat_session_id="core-helper-chat").first()
        assert conversation is not None
        items = Message.query.filter_by(conversation_id=conversation.id).order_by(Message.id.asc()).all()
        assert [m.body for m in items] == ["hola core", "respuesta core"]

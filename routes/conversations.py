from __future__ import annotations

from flask import Blueprint, abort, g, jsonify, request

from models import AdminAuditLog, Conversation, Message
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.tenant import require_tenant

conversations_bp = Blueprint("conversations_bp", __name__)


def _serialize_message(msg: Message) -> dict:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "channel_session_id": msg.channel_session_id,
        "sender_type": msg.sender_type,
        "sender_user_id": msg.sender_user_id,
        "direction": msg.direction,
        "body": msg.body,
        "metadata": msg.meta_payload or {},
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


@conversations_bp.route("/api/conversations/<string:conversation_id>/timeline", methods=["GET"])
@token_requerido
@require_tenant
def get_conversation_timeline(user, conversation_id):
    tenant = g.tenant_profile

    if not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado para este tenant.")

    conversation = Conversation.query.filter_by(id=conversation_id, tenant_id=tenant.id).first()
    if conversation is None:
        return jsonify({"error": "Conversación no encontrada"}), 404

    messages = (
        Message.query.filter_by(conversation_id=conversation.id, tenant_id=tenant.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .all()
    )

    # Audit read access for cross-channel conversation history.
    try:
        from models import db

        db.session.add(
            AdminAuditLog(
                admin_user_id=user.id,
                action="conversation_timeline_view",
                target_object=conversation.id,
                details={
                    "tenant_id": tenant.id,
                    "message_count": len(messages),
                    "source": "api.conversations.timeline",
                },
                ip_address=request.remote_addr,
            )
        )
        db.session.commit()
    except Exception:
        from models import db

        db.session.rollback()

    return jsonify(
        {
            "conversation": {
                "id": conversation.id,
                "tenant_id": conversation.tenant_id,
                "legacy_chat_session_id": conversation.legacy_chat_session_id,
                "status": conversation.status,
            },
            "timeline": [_serialize_message(msg) for msg in messages],
        }
    )

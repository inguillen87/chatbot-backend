from __future__ import annotations

from flask import Blueprint, abort, current_app, g, jsonify, request

from cutover_writer_fence import cutover_writer_view
from models import AdminAuditLog, ChannelSession, Conversation, ConversationLinkRequest, Message, db
from services.conversation_linking import ConversationLinkingService
from socket_service import emit_conversation_linked
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
@cutover_writer_view
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


@conversations_bp.route("/api/conversations/link/whatsapp", methods=["POST"])
@token_requerido
@require_tenant
def create_whatsapp_link(user):
    tenant = g.tenant_profile
    if not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado para este tenant.")

    payload = request.get_json(silent=True) or {}
    service = ConversationLinkingService(tenant.id)

    try:
        link_request, otp_code = service.create_whatsapp_link_request(
            conversation_id=payload.get("conversation_id"),
            chat_session_id=payload.get("chat_session_id"),
            whatsapp_number=payload.get("whatsapp_number"),
            requested_by_user_id=user.id,
            ttl_minutes=int(payload.get("ttl_minutes") or 10),
        )
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 429
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    db.session.add(
        AdminAuditLog(
            admin_user_id=user.id,
            action="conversation_link_whatsapp_requested",
            target_object=link_request.conversation_id,
            details={
                "tenant_id": tenant.id,
                "link_request_id": link_request.id,
                "target_channel": "whatsapp",
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    response_payload = {
        "link_request": {
            "id": link_request.id,
            "conversation_id": link_request.conversation_id,
            "target_channel": link_request.target_channel,
            "target_identity": link_request.target_identity,
            "deep_link_token": link_request.deep_link_token,
            "expires_at": link_request.expires_at.isoformat() if link_request.expires_at else None,
            "status": link_request.status,
        }
    }
    # OTP is returned only in testing/dev flows. In production it should be delivered via provider.
    if current_app.config.get("TESTING"):
        response_payload["link_request"]["otp_code"] = otp_code

    return jsonify(response_payload), 201


@conversations_bp.route("/api/conversations/link/confirm", methods=["POST"])
@token_requerido
@require_tenant
def confirm_whatsapp_link(user):
    tenant = g.tenant_profile
    if not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado para este tenant.")

    payload = request.get_json(silent=True) or {}
    service = ConversationLinkingService(tenant.id)

    try:
        link_request, linked_session, already_confirmed = service.confirm_whatsapp_link(
            deep_link_token=payload.get("deep_link_token"),
            otp_code=payload.get("otp_code"),
            whatsapp_chat_session_id=payload.get("whatsapp_chat_session_id"),
        )
    except LookupError:
        return jsonify({"error": "link request not found"}), 404
    except TimeoutError:
        db.session.commit()
        return jsonify({"error": "link request expired"}), 410
    except PermissionError:
        return jsonify({"error": "invalid otp"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    web_sessions = (
        ChannelSession.query.filter_by(
            tenant_id=tenant.id,
            conversation_id=link_request.conversation_id,
            channel="web",
        )
        .order_by(ChannelSession.created_at.asc(), ChannelSession.id.asc())
        .all()
    )
    web_session_id = web_sessions[0].id if web_sessions else None

    event_payload = {
        "tenant_type": "conversation",
        "tenant_id": tenant.id,
        "conversation_id": link_request.conversation_id,
        "event": "conversation.linked",
        "source_channel_session_id": web_session_id,
        "target_channel_session_id": linked_session.id,
        "target_channel": "whatsapp",
        "target_identity": link_request.target_identity,
        "status": "already_confirmed" if already_confirmed else "linked",
    }
    emit_conversation_linked(event_payload)

    db.session.add(
        AdminAuditLog(
            admin_user_id=user.id,
            action="conversation_link_whatsapp_confirmed",
            target_object=link_request.conversation_id,
            details={
                "tenant_id": tenant.id,
                "link_request_id": link_request.id,
                "status": event_payload["status"],
                "target_channel_session_id": linked_session.id,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "linked": True,
            "status": event_payload["status"],
            "conversation_id": link_request.conversation_id,
            "source_channel_session_id": web_session_id,
            "target_channel_session_id": linked_session.id,
        }
    )


@conversations_bp.route("/api/conversations/link/<string:link_request_id>", methods=["GET"])
@cutover_writer_view
@token_requerido
@require_tenant
def get_link_request_status(user, link_request_id: str):
    tenant = g.tenant_profile
    if not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado para este tenant.")

    link_request = ConversationLinkRequest.query.filter_by(id=link_request_id, tenant_id=tenant.id).first()
    if not link_request:
        return jsonify({"error": "link request not found"}), 404

    db.session.add(
        AdminAuditLog(
            admin_user_id=user.id,
            action="conversation_link_whatsapp_status_view",
            target_object=link_request.conversation_id,
            details={
                "tenant_id": tenant.id,
                "link_request_id": link_request.id,
                "status": link_request.status,
            },
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify(
        {
            "id": link_request.id,
            "conversation_id": link_request.conversation_id,
            "target_channel": link_request.target_channel,
            "target_identity": link_request.target_identity,
            "status": link_request.status,
            "expires_at": link_request.expires_at.isoformat() if link_request.expires_at else None,
            "confirmed_at": link_request.confirmed_at.isoformat() if link_request.confirmed_at else None,
        }
    )

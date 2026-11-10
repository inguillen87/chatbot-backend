"""Server-only municipal writer boundary; never a context/LLM capability field.

These helpers do not admit a webhook, select a tenant from text, send a provider
request, or turn an unported branch into an atomic one. Default legacy is kept.
"""
from copy import deepcopy
from functools import wraps

from extensions import db
from services.actions.base_action_handler import BaseActionHandler
from utils.db_utils import safe_flag_modified
from services.whatsapp_conversation_contract import ConversationMode, ConversationTurn
from services.whatsapp_conversation_uow import ConversationUnitOfWork, ConversationUnitOfWorkError


def private_municipal_arguments(unit_of_work, conversation_turn_id):
    if unit_of_work is None:
        return {}
    if not isinstance(unit_of_work, ConversationUnitOfWork):
        raise ConversationUnitOfWorkError("municipal_uow_server_unit_required")
    unit_of_work.require_writer_scope(session=db.session(),
        tenant_id=unit_of_work.claim.tenant_id, turn_id=conversation_turn_id)
    return {"unit_of_work": unit_of_work, "conversation_turn_id": conversation_turn_id}


def require_municipal_context(context, unit_of_work, conversation_turn_id):
    if unit_of_work is None:
        return
    with db.session.no_autoflush:
        private_municipal_arguments(unit_of_work, conversation_turn_id)
        profile = context.get("tenant_profile")
        chat_context = context.get("chat_db_context_obj")
        if (profile is None or getattr(profile, "tipo", None) != "municipio"
                or chat_context is None or context.get("channel") != "whatsapp"
                or context.get("durable_turn_id") != conversation_turn_id
                or context.get("chat_db_context_data") is not chat_context.context_data):
            unit_of_work.fail("municipal_uow_context_scope_mismatch")
        # Reuse the Unit's actual persisted owner/end-user/source/phone checks.
        # This DTO itself is not identity/authentication proof.
        unit_of_work.require_turn(ConversationTurn(
            owner_user=context.get("user_obj"), tenant_profile=profile,
            tenant_id=context.get("tenant_id"), end_user=context.get("viewer_user_obj"),
            session_context=chat_context, chat_session_id=context.get("chat_session_uuid"),
            contact_identity=context.get("anon_id"),
            source_event_id=context.get("source_event_id"), durable_claim=unit_of_work.claim,
            message_body=context.get("user_input_raw") or "", action_id=None,
            mode=ConversationMode(False, False), unit_of_work=unit_of_work))


def persist_municipal_changes(unit_of_work):
    if unit_of_work is None:
        db.session.commit()
    else:
        unit_of_work.flush()


class MunicipalUnitHandler:
    """Code-level marker for explicitly ported handlers, never request data."""


class MunicipalUnitAction(MunicipalUnitHandler, BaseActionHandler):
    def __init__(self, context, *, unit_of_work=None, conversation_turn_id=None):
        super().__init__(context)
        self.unit_of_work, self.conversation_turn_id = unit_of_work, conversation_turn_id
        require_municipal_context(context, unit_of_work, conversation_turn_id)


def municipal_unit_action(method):
    @wraps(method)
    def wrapped(self, action_data):
        unit = self.unit_of_work
        if unit is None:
            return method(self, action_data)
        try:
            require_municipal_context(self.context, unit, self.conversation_turn_id)
            result = method(self, action_data)
            require_supported_municipal_output(result, unit)
            flush_municipal_context(self.context, unit, self.conversation_turn_id)
            return result
        except Exception as exc:
            unit.fail("municipal_uow_action_failed", cause=exc)
    return wrapped


def flush_municipal_context(context, unit_of_work, conversation_turn_id):
    require_municipal_context(context, unit_of_work, conversation_turn_id)
    if unit_of_work is not None:
        safe_flag_modified(context["chat_db_context_obj"], "context_data")
        unit_of_work.flush()


def municipal_handler(handler_type, *args, unit_of_work=None, conversation_turn_id=None, **kwargs):
    if unit_of_work is None:
        return handler_type(*args, **kwargs)
    private_municipal_arguments(unit_of_work, conversation_turn_id)
    if not isinstance(handler_type, type) or not issubclass(handler_type, MunicipalUnitHandler):
        unit_of_work.fail("municipal_uow_handler_not_ported")
    return handler_type(*args, **kwargs, unit_of_work=unit_of_work,
        conversation_turn_id=conversation_turn_id)


def unported_municipal_call(unit_of_work, code, callee, *args, **kwargs):
    """Stop before invoking the particular existing provider/unsupported path."""
    if unit_of_work is not None:
        unit_of_work.fail(code)
    return callee(*args, **kwargs)


def _emit_ticket(payload):
    from socket_service import emit_new_ticket
    emit_new_ticket(payload)


def _emit_live_request(payload, room):
    from socket_service import socketio
    socketio.emit("live_chat_request", payload, room=room)


def municipal_ticket_socket(context, payload, *, unit_of_work=None, conversation_turn_id=None):
    if unit_of_work is None:
        _emit_ticket(payload)
        return
    require_municipal_context(context, unit_of_work, conversation_turn_id)
    copied = deepcopy(payload)
    # Best-effort notification only. No email/SIGEM/provider callback accepted.
    unit_of_work.defer_after_commit("municipal_ticket_update", lambda: _emit_ticket(copied))


def municipal_live_request_socket(context, payload, room, *, unit_of_work=None, conversation_turn_id=None):
    if unit_of_work is None:
        _emit_live_request(payload, room)
        return
    require_municipal_context(context, unit_of_work, conversation_turn_id)
    owner_id = context["tenant_profile"].municipio_id
    if room != f"municipio_{owner_id}":
        unit_of_work.fail("municipal_uow_socket_scope_mismatch")
    copied = deepcopy(payload)
    unit_of_work.defer_after_commit("municipal_ticket_update", lambda: _emit_live_request(copied, room))


def require_supported_municipal_output(response, unit_of_work):
    if unit_of_work is None or not isinstance(response, dict):
        return
    if (response.get("_twilio_pre_messages") or response.get("generar_audio")
            or any(response.get(key) for key in ("audio_url", "image_url", "media_url",
                "document_url", "video_url", "sticker_url", "delayed_payload"))):
        unit_of_work.fail("municipal_uow_output_transport_not_ported")

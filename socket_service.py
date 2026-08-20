from flask_socketio import SocketIO, join_room, emit
from flask import current_app, request
from config import ALLOWED_ORIGINS
from models import ChatSessionContext, EncEncuesta, EncLink, User, TenantProfile, db, TicketComentario, MunicipioTicket, PymeTicket
from services.ticket_service import servicio_tickets # Reutilizamos el servicio de tickets
from services.tts_orchestrator import generar_audio
from services.live_chat_access import LiveChatAccessError, build_ticket_room, verify_ticket_room_token
from services.employee_ticket_access import employee_ticket_category_access_allows
from services.omnichannel_message_policy import (
    OmnichannelMessagePolicyError,
    normalize_omnichannel_reply_body,
)
from services.survey_tenant_scope import (
    SurveyTenantScopeError,
    resolve_survey_storage_tenant_profile,
)
from utils.auth_helpers import user_from_token
from utils.response_utils import ensure_buttons_compatibility
from utils.roles import canonical_role, is_authorized_superadmin_user
from typing import Any, Optional, Set
from urllib.parse import urlparse
from uuid import UUID
import jwt
import os
import re

SOCKET_CORS_ORIGINS = list(
    dict.fromkeys(list(ALLOWED_ORIGINS) + ["https://chatboc.ar", "https://www.chatboc.ar"])
)

TICKET_OPERATOR_ROLES = {"admin", "empleado", "manager", "supervisor"}
PUBLIC_TICKET_COMMENT_ORIGINS = {
    "",
    "admin",
    "agent",
    "api",
    "chat",
    "cliente",
    "ciudadano",
    "email",
    "operator",
    "portal",
    "public_tracking",
    "pwa",
    "sms",
    "tracking_page",
    "web",
    "whatsapp",
    "widget",
}
TENANT_TICKET_INVALIDATION_CONTRACT_VERSION = "tickets.collection.invalidated.v1"

SURVEY_EFFECT_WORKER_ROLE = "survey-effect-worker"
_SOCKET_QUEUE_CHANNEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class SurveyRealtimeTransportError(RuntimeError):
    """A worker cannot prove that its Socket.IO event enters shared pub/sub."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_fail_closed_socketio_redis_manager(
    queue_url: str,
    *,
    channel: str,
    timeout_seconds: float = 2,
):
    """Build the worker's write-only Redis manager with publish confirmation.

    ``python-socketio`` retries a Redis publish once and then normally logs and
    returns ``None``. Its caller discards that return value, which would let an
    outbox worker mark an event successful after a definite broker failure.
    The worker-specific subclass converts that exhausted publish into a
    sanitized exception. A return value of zero is still a valid Redis publish
    (there simply were no subscribers at that instant).
    """

    from socketio.redis_manager import RedisManager

    class _FailClosedWorkerRedisManager(RedisManager):
        def _publish(self, data):
            subscriber_count = super()._publish(data)
            if subscriber_count is None:
                raise SurveyRealtimeTransportError(
                    "survey_realtime_shared_transport_publish_failed"
                )
            return subscriber_count

    return _FailClosedWorkerRedisManager(
        str(queue_url),
        channel=str(channel),
        write_only=True,
        redis_options={
            "socket_connect_timeout": float(timeout_seconds),
            "socket_timeout": float(timeout_seconds),
            "retry_on_timeout": False,
        },
    )


def _survey_realtime_process_role() -> str:
    configured = ""
    try:
        configured = str(current_app.config.get("CHATBOC_PROCESS_ROLE") or "")
    except RuntimeError:
        configured = ""
    return (configured or os.getenv("CHATBOC_PROCESS_ROLE", "")).strip().lower()


def _survey_realtime_queue_config() -> tuple[str, str, float]:
    queue_url = str(
        current_app.config.get("SOCKETIO_MESSAGE_QUEUE_URL")
        or os.getenv("SOCKETIO_MESSAGE_QUEUE_URL", "")
        or os.getenv("SOCKETIO_REDIS_URL", "")
    ).strip()
    channel = str(
        current_app.config.get(
            "SOCKETIO_MESSAGE_QUEUE_CHANNEL",
            "chatboc-realtime-v1",
        )
        or ""
    ).strip()
    try:
        timeout = float(
            current_app.config.get(
                "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS",
                2,
            )
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurveyRealtimeTransportError(
            "survey_realtime_queue_timeout_invalid"
        ) from exc
    if timeout < 0.1 or timeout > 10:
        raise SurveyRealtimeTransportError(
            "survey_realtime_queue_timeout_invalid"
        )
    return queue_url, channel, timeout


def ensure_survey_realtime_transport_ready() -> dict[str, Any]:
    """Verify the delivery boundary used by a durable realtime effect.

    The web process may use its in-process Socket.IO manager when no shared
    queue is configured. The permanent survey-effect worker is a different
    process and therefore has no such fallback: it must be a write-only Redis
    publisher on the exact queue/channel configured for the web process, and
    Redis must answer immediately before the effect is allowed to emit.

    This verifies the transport, not browser delivery. Realtime remains
    at-least-once and consumers deduplicate the stable outbox ``event_id``.
    """

    process_role = _survey_realtime_process_role()
    queue_url, channel, timeout = _survey_realtime_queue_config()
    shared_required = process_role == SURVEY_EFFECT_WORKER_ROLE
    if not queue_url:
        if shared_required:
            raise SurveyRealtimeTransportError(
                "survey_realtime_shared_transport_not_configured"
            )
        return {
            "transport": "socketio_in_process",
            "shared": False,
            "verified": True,
        }

    parsed = urlparse(queue_url)
    if parsed.scheme.lower() not in {"redis", "rediss"} or not parsed.hostname:
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_transport_url_invalid"
        )
    if not _SOCKET_QUEUE_CHANNEL_PATTERN.fullmatch(channel):
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_transport_channel_invalid"
        )

    manager = getattr(getattr(socketio, "server", None), "manager", None)
    try:
        from socketio.redis_manager import RedisManager
    except Exception as exc:  # pragma: no cover - dependency import guard
        raise SurveyRealtimeTransportError(
            "survey_realtime_redis_manager_unavailable"
        ) from exc
    if not isinstance(manager, RedisManager):
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_manager_not_initialized"
        )
    if str(getattr(manager, "redis_url", "") or "") != queue_url:
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_manager_url_mismatch"
        )
    if str(getattr(manager, "channel", "") or "") != channel:
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_manager_channel_mismatch"
        )
    manager_write_only = bool(getattr(manager, "write_only", False))
    if shared_required and not manager_write_only:
        raise SurveyRealtimeTransportError(
            "survey_realtime_worker_manager_not_write_only"
        )
    if not shared_required and manager_write_only:
        raise SurveyRealtimeTransportError(
            "survey_realtime_web_manager_not_subscribed"
        )

    try:
        from redis import Redis

        redis_client = Redis.from_url(
            queue_url,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            retry_on_timeout=False,
        )
        try:
            reachable = redis_client.ping() is True
        finally:
            redis_client.close()
    except Exception as exc:
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_transport_unreachable"
        ) from exc
    if not reachable:
        raise SurveyRealtimeTransportError(
            "survey_realtime_shared_transport_unreachable"
        )

    return {
        "transport": "socketio_redis_pubsub",
        "shared": True,
        "verified": True,
        "channel": channel,
        "publisher_mode": "write_only" if manager_write_only else "subscriber",
    }


def _clerk_user_id_for_user(user: Optional[User]) -> str:
    metadata = user.accesibilidad if user and isinstance(getattr(user, "accesibilidad", None), dict) else {}
    auth_meta = metadata.get("auth") if isinstance(metadata.get("auth"), dict) else {}
    clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
    return str(clerk_meta.get("user_id") or "").strip()


def _decode_chatboc_socket_token(token: str) -> dict:
    try:
        return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return {}


def _socket_request_token(payload: Any = None) -> Optional[str]:
    """Resolve the Chatboc session from Socket.IO auth or its HttpOnly cookie."""

    if isinstance(payload, dict):
        explicit_token = str(payload.get("token") or "").strip()
        if explicit_token:
            return explicit_token

    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    cookies = getattr(request, "cookies", None)
    if cookies is None:
        return None

    cookie_token = str(cookies.get(cookie_name) or "").strip()
    return cookie_token or None


def _clerk_identity_rooms(user: Optional[User], token: str) -> list[str]:
    claims = _decode_chatboc_socket_token(token)
    if str(claims.get("auth_provider") or "").strip().lower() != "clerk":
        return []
    if str(claims.get("session_kind") or "").strip().lower() != "clerk":
        return []

    sid = str(claims.get("clerk_sid") or claims.get("sid") or "").strip()
    clerk_user_id = str(claims.get("clerk_user_id") or "").strip() or _clerk_user_id_for_user(user)
    rooms: list[str] = []
    if sid:
        rooms.append(f"clerk_session:{sid}")
    if clerk_user_id:
        rooms.append(f"clerk_user:{clerk_user_id}")
    return rooms


def disconnect_clerk_session_sockets(
    *,
    clerk_session_id: str | None = None,
    clerk_user_id: str | None = None,
) -> int:
    """Disconnect every socket bound to a terminal Clerk session or user."""

    identity_rooms = []
    if str(clerk_session_id or "").strip():
        identity_rooms.append(f"clerk_session:{str(clerk_session_id).strip()}")
    if str(clerk_user_id or "").strip():
        identity_rooms.append(f"clerk_user:{str(clerk_user_id).strip()}")

    socket_sids: set[str] = set()
    for room in identity_rooms:
        for participant in socketio.server.manager.get_participants("/", room):
            socket_sid = participant[0] if isinstance(participant, (tuple, list)) else participant
            if socket_sid:
                socket_sids.add(str(socket_sid))

    for socket_sid in socket_sids:
        socketio.server.disconnect(socket_sid, namespace="/")
    return len(socket_sids)

def _resolve_socket_async_mode() -> str:
    """Use threading by default; allow explicit override via env."""
    forced_mode = (os.getenv("SOCKETIO_ASYNC_MODE") or "").strip().lower()
    if forced_mode:
        return forced_mode
    return "threading"


socketio = SocketIO(
    cors_allowed_origins=SOCKET_CORS_ORIGINS,
    # Engine.IO expects cookie settings as None/str/dict. Using boolean True
    # can break on newer versions when composing SID cookies.
    cookie={"name": "io", "path": "/", "httponly": True},
    async_mode=_resolve_socket_async_mode(),
    path="/api/socket.io",
)


def _get_owner_user(user: Optional[User]) -> Optional[User]:
    if not user:
        return None

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id:
        owner = User.query.get(empresa_id)
        if owner:
            return owner

    return user


def _is_ticket_operator(user: Optional[User]) -> bool:
    if not user:
        return False
    if is_authorized_superadmin_user(user):
        return True
    return canonical_role(getattr(user, "rol", None)) in TICKET_OPERATOR_ROLES


def _tenant_for_operator(user: Optional[User]) -> Optional[TenantProfile]:
    if not user:
        return None

    owner = _get_owner_user(user)
    tenant_id = getattr(user, "tenant_id", None) or getattr(owner, "tenant_id", None)
    if tenant_id:
        tenant = db.session.get(TenantProfile, tenant_id)
        if tenant:
            return tenant

    tenant_slug = str(
        getattr(user, "tenant_slug", None) or getattr(owner, "tenant_slug", None) or ""
    ).strip()
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if tenant:
            return tenant

    owner_id = getattr(owner, "id", None)
    if not owner_id:
        return None
    return (
        TenantProfile.query.filter_by(municipio_id=owner_id).first()
        or TenantProfile.query.filter_by(pyme_id=owner_id).first()
    )


def _user_can_operate_tenant(user: Optional[User], tenant: Optional[TenantProfile]) -> bool:
    if not user or not tenant or not _is_ticket_operator(user):
        return False
    if is_authorized_superadmin_user(user):
        return True

    owner = _get_owner_user(user)
    if getattr(user, "tenant_id", None) == tenant.id or getattr(owner, "tenant_id", None) == tenant.id:
        return True

    user_slug = str(
        getattr(user, "tenant_slug", None) or getattr(owner, "tenant_slug", None) or ""
    ).strip().lower()
    if user_slug and user_slug == str(getattr(tenant, "slug", "") or "").strip().lower():
        return True

    owner_id = getattr(owner, "id", None)
    municipality_scope = {
        getattr(user, "municipio_id", None),
        getattr(owner, "municipio_id", None),
        owner_id,
    } - {None}
    business_scope = {
        getattr(user, "pyme_id", None),
        getattr(owner, "pyme_id", None),
        owner_id,
    } - {None}
    return bool(
        getattr(tenant, "municipio_id", None) in municipality_scope
        or getattr(tenant, "pyme_id", None) in business_scope
    )


def _get_rooms_for_user(user: Optional[User]) -> list[str]:
    rooms: Set[str] = set()
    if not _is_ticket_operator(user):
        return []

    owner = _get_owner_user(user)

    tenant = _tenant_for_operator(user)
    if tenant:
        rooms.update(_get_rooms_for_tenant_slug(tenant.slug))

    usuario_tipo = getattr(user, "tipo_chat", None)
    owner_tipo = getattr(owner, "tipo_chat", None)

    municipio_id = (
        getattr(user, "municipio_id", None)
        or getattr(owner, "municipio_id", None)
    )
    if not municipio_id and (owner_tipo == "municipio"):
        municipio_id = getattr(owner, "id", None)
    if (usuario_tipo == "municipio" or owner_tipo == "municipio") and municipio_id:
        rooms.add(f"municipio_{municipio_id}")

    pyme_id = getattr(user, "pyme_id", None) or getattr(owner, "pyme_id", None)
    if not pyme_id and owner_tipo == "pyme":
        pyme_id = getattr(owner, "id", None)

    if (usuario_tipo == "pyme" or owner_tipo == "pyme") and pyme_id:
        rooms.add(f"pyme_{pyme_id}")

    return list(rooms)

def _get_rooms_for_tenant_slug(tenant_slug: Optional[str]) -> list[str]:
    if not tenant_slug:
        return []

    tenant = TenantProfile.query.filter_by(slug=str(tenant_slug).strip()).first()
    if not tenant:
        return []

    rooms: Set[str] = set()
    if tenant.municipio_id:
        rooms.add(f"municipio_{tenant.municipio_id}")
    if tenant.pyme_id:
        rooms.add(f"pyme_{tenant.pyme_id}")
    rooms.add(f"crm_{tenant.id}")
    rooms.add(f"tenant_{tenant.id}")
    if tenant.slug:
        rooms.add(f"tenant_slug_{tenant.slug}")
    return list(rooms)


def _user_can_access_tenant_slug(user: Optional[User], tenant_slug: Optional[str]) -> bool:
    if not user or not tenant_slug or not _is_ticket_operator(user):
        return False
    tenant = TenantProfile.query.filter_by(slug=str(tenant_slug).strip()).first()
    return _user_can_operate_tenant(user, tenant)



def _merge_rooms_for_subscription(user: User, tenant_slug: Optional[str]) -> list[str]:
    """Merge user-derived and tenant-derived rooms without dropping either scope."""

    rooms = list(_get_rooms_for_user(user))
    tenant_rooms = _get_rooms_for_tenant_slug(tenant_slug) if _user_can_access_tenant_slug(user, tenant_slug) else []
    for room in tenant_rooms:
        if room not in rooms:
            rooms.append(room)
    return rooms


def _merge_authenticated_socket_rooms(user: User, tenant_slug: Optional[str], token: str) -> list[str]:
    rooms = _merge_rooms_for_subscription(user, tenant_slug)
    for room in _clerk_identity_rooms(user, token):
        if room not in rooms:
            rooms.append(room)
    return rooms

def _resolve_tenant_ticket_room(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    explicit_room = str(payload.get("socket_room") or "").strip()
    if re.fullmatch(r"(?:tenant|municipio|pyme|crm)_[1-9][0-9]*", explicit_room):
        return explicit_room

    tenant_type = payload.get("tenant_type") or payload.get("tipo")
    tenant_profile_id = payload.get("tenant_profile_id") or payload.get("tenant_id")
    if tenant_profile_id:
        return f"tenant_{tenant_profile_id}"

    if tenant_type == "municipio":
        municipio_id = payload.get("municipio_id")
        if municipio_id:
            return f"municipio_{municipio_id}"
    elif tenant_type == "pyme":
        pyme_id = payload.get("pyme_id")
        if pyme_id:
            return f"pyme_{pyme_id}"

    ticket_id = payload.get("id") or payload.get("ticket_id")
    if ticket_id and tenant_type in {"municipio", "pyme"}:
        try:
            TicketModel = MunicipioTicket if tenant_type == "municipio" else PymeTicket
            ticket_obj = db.session.get(TicketModel, ticket_id)
            tenant_profile_id = getattr(ticket_obj, "tenant_id", None) if ticket_obj else None
            if tenant_profile_id:
                return f"tenant_{tenant_profile_id}"
            if tenant_type == "municipio":
                municipio_id = getattr(ticket_obj, "municipio_id", None) if ticket_obj else None
                if municipio_id:
                    return f"municipio_{municipio_id}"
        except Exception:
            current_app.logger.exception(
                "Error resolving socket room for ticket %s of type %s", ticket_id, tenant_type
            )

    tenant_slug = str(payload.get("tenant_slug") or payload.get("tenant") or "").strip()
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if tenant:
            return f"tenant_{tenant.id}"

    return None


def _resolve_ticket_room(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    explicit_room = payload.get("socket_room")
    if explicit_room:
        return explicit_room
    return _resolve_tenant_ticket_room(payload)


def emit_ticket_update(data: Any) -> None:
    """Invalidate tenant ticket collections without broadcasting case data."""

    _emit_tenant_ticket_invalidation(data)


def _emit_public_ticket_state_event(event_name: str, data: Any) -> bool:
    """Emit only citizen-safe ticket state to the signed ticket room."""

    if not isinstance(data, dict):
        return False
    ticket_type = data.get("tipo") or data.get("tenant_type")
    ticket_id = data.get("ticket_id") or data.get("ticketId") or data.get("id")
    try:
        public_room = build_ticket_room(ticket_type, ticket_id)
    except LiveChatAccessError:
        return False

    payload = {
        "contract_version": "live_chat.public_state.v1",
        "event": event_name,
        "ticket_id": int(ticket_id),
        "ticketId": int(ticket_id),
        "tenant_type": str(ticket_type or "").strip().lower(),
        "tipo": str(ticket_type or "").strip().lower(),
        "estado": data.get("estado") or data.get("status"),
        "previous_status": data.get("previous_status"),
        "assignment_state": data.get("assignment_state"),
        "changed_at": data.get("changed_at") or data.get("updated_at"),
        "socket_room": public_room,
    }
    socketio.emit(event_name, payload, room=public_room)
    return True


def emit_crm_contact_update(tenant: TenantProfile, contact_payload: Any) -> None:
    """Invalidate broad CRM contact collections without broadcasting PII."""
    if not tenant:
        return

    payload = _build_collection_invalidation("contacts")
    rooms = _get_rooms_for_tenant_slug(getattr(tenant, "slug", None))
    if not rooms and getattr(tenant, "id", None):
        rooms = [f"crm_{tenant.id}", f"tenant_{tenant.id}"]

    event_names = ("crm.contact.updated", "crm_contact_updated")
    if rooms:
        for room in rooms:
            for event_name in event_names:
                socketio.emit(event_name, payload, room=room)
        return

    for event_name in event_names:
        socketio.emit(event_name, payload)


def emit_crm_notification_update(tenant: TenantProfile, notification_payload: Any) -> None:
    """Invalidate broad notification collections without recipient data."""
    if not tenant:
        return

    payload = _build_collection_invalidation("notifications")
    rooms = _get_rooms_for_tenant_slug(getattr(tenant, "slug", None))
    if not rooms and getattr(tenant, "id", None):
        rooms = [f"crm_{tenant.id}", f"tenant_{tenant.id}"]

    event_names = ("crm.notification.updated", "crm_notification_updated")
    if rooms:
        for room in rooms:
            for event_name in event_names:
                socketio.emit(event_name, payload, room=room)
        return

    for event_name in event_names:
        socketio.emit(event_name, payload)


def emit_ticket_status_changed(data: Any) -> None:
    """Invalidate operator collections and publish citizen-safe state only."""

    emit_ticket_update(data)
    _emit_public_ticket_state_event('ticket.status.changed', data)


def emit_ticket_assignment_changed(data: Any) -> None:
    """Invalidate operator collections and publish citizen-safe state only."""

    emit_ticket_update(data)
    _emit_public_ticket_state_event('ticket.assignment.changed', data)


def emit_ticket_presence_changed(data: Any) -> None:
    """Invalidate scoped collections without exposing viewer presence broadly."""

    _emit_tenant_ticket_invalidation(data)


def emit_conversation_message_read(data: Any) -> None:
    """Invalidate scoped collections without exposing read receipts broadly."""

    _emit_tenant_ticket_invalidation(data)

def emit_conversation_linked(data: Any) -> None:
    """Invalidate scoped collections without exposing linked identities broadly."""

    _emit_tenant_ticket_invalidation(data)


def emit_notification_status_changed(data: Any) -> None:
    """Invalidate notification collections without broad recipient leakage."""
    if isinstance(data, dict):
        tenant_slug = data.get("tenant_slug") or data.get("tenant")
        tenant_id = data.get("tenant_id")
        tenant = None
        if tenant_slug:
            tenant = TenantProfile.query.filter_by(slug=str(tenant_slug).strip()).first()
        elif tenant_id:
            tenant = db.session.get(TenantProfile, tenant_id)
        if tenant:
            emit_crm_notification_update(tenant, data)


def emit_ticket_unread_changed(data: Any) -> None:
    """Invalidate ticket collections without leaking case-level unread state."""

    _emit_tenant_ticket_invalidation(data)


def emit_tenant_update(tenant_slug: str, event_name: str, data: Any = None) -> None:
    """Emit an event to the tenant's specific room for real-time portal updates."""
    if tenant_slug:
        # Emit generic content update signal
        socketio.emit('tenant_content_update', {'type': event_name}, room=tenant_slug)
        # Emit specific event
        if data:
            socketio.emit(event_name, data, room=tenant_slug)


def emit_new_ticket(data: Any) -> None:
    """Invalidate the operator collection without broadcasting the new case."""

    _emit_tenant_ticket_invalidation(data)


def _is_public_ticket_comment(comment: Any) -> bool:
    if not isinstance(comment, dict):
        return False
    if comment.get("is_internal") is True or comment.get("internal") is True:
        return False
    visibility = str(comment.get("visibility") or "").strip().lower()
    if visibility and visibility not in {"public", "customer", "citizen"}:
        return False
    origin = str(comment.get("origen") or comment.get("origin") or "").strip().lower()
    return origin in PUBLIC_TICKET_COMMENT_ORIGINS


def _build_public_ticket_comment_event(data: dict[str, Any], room: str) -> Optional[dict[str, Any]]:
    comment = data.get("comment") or data.get("comentario") or data.get("message")
    if not _is_public_ticket_comment(comment):
        return None

    allowed_comment_keys = {
        "attachmentInfo",
        "autor",
        "comentario",
        "es_admin",
        "estado_ticket",
        "fecha",
        "id",
        "origen",
        "texto",
    }
    public_comment = {key: value for key, value in comment.items() if key in allowed_comment_keys}
    message_text = public_comment.get("comentario") or public_comment.get("texto") or ""
    ticket_id = data.get("ticket_id") or data.get("ticketId")
    return {
        "contract_version": "live_chat.public_message.v1",
        "ticket_id": ticket_id,
        "ticketId": ticket_id,
        "nro_ticket": data.get("nro_ticket"),
        "tenant_type": data.get("tenant_type") or data.get("tipo"),
        "tipo": data.get("tipo") or data.get("tenant_type"),
        "estado": data.get("estado"),
        "socket_room": room,
        "actor": "agent" if public_comment.get("es_admin") else "neighbor",
        "mensaje": message_text,
        "comment": public_comment,
        "message": public_comment,
    }


def _build_tenant_ticket_invalidation() -> dict[str, Any]:
    """Return a tenant-wide refetch signal with no ticket or actor metadata."""

    return {
        "contract_version": TENANT_TICKET_INVALIDATION_CONTRACT_VERSION,
        "resource": "tickets",
        "reason": "collection_changed",
        "refetch": True,
    }


def _build_collection_invalidation(resource: str) -> dict[str, Any]:
    """Return the only payload shape permitted in broad operator rooms."""

    return {
        "contract_version": "collections.invalidated.v1",
        "resource": str(resource or "").strip().lower(),
        "reason": "collection_changed",
        "refetch": True,
    }


def _emit_tenant_ticket_invalidation(data: Any) -> bool:
    """Emit the one allowlisted payload permitted in a broad operator room."""

    room = _resolve_tenant_ticket_room(data)
    if not room:
        current_app.logger.warning("Dropped unscoped tenant ticket invalidation")
        return False
    socketio.emit("ticket_update", _build_tenant_ticket_invalidation(), room=room)
    return True


def emit_ticket_comment(data: Any) -> None:
    """Publish a comment through the category-safe live-chat boundary."""

    emit_new_chat_message(data)

def emit_new_chat_message(data: Any) -> None:
    """Broadcast public chat data and an opaque tenant-wide refetch signal."""
    if not isinstance(data, dict):
        current_app.logger.warning("Dropped malformed live chat socket event")
        return

    ticket_type = data.get("tenant_type") or data.get("tipo")
    ticket_id = data.get("ticket_id") or data.get("ticketId")
    try:
        public_room = build_ticket_room(ticket_type, ticket_id)
    except LiveChatAccessError:
        public_room = None

    if public_room:
        public_payload = _build_public_ticket_comment_event(data, public_room)
        if public_payload:
            socketio.emit("new_chat_message", public_payload, room=public_room)

    if not _emit_tenant_ticket_invalidation(data):
        current_app.logger.warning(
            "Dropped unscoped admin live chat event ticket_type=%s ticket_id=%s",
            ticket_type,
            ticket_id,
        )


def _tenant_slug_for_survey_tenant_id(tenant_id: Any) -> str:
    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        return ""

    try:
        tenant = resolve_survey_storage_tenant_profile(normalized_tenant_id)
    except SurveyTenantScopeError:
        current_app.logger.error(
            "Dropped survey realtime event with non-canonical or ambiguous "
            "tenant scope tenant_id=%s",
            normalized_tenant_id,
        )
        return ""
    return str(getattr(tenant, "slug", "") or "").strip()


def _is_valid_survey_room_segment(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(
        normalized
        and len(normalized) <= 160
        and ":" not in normalized
        and not any(ch.isspace() for ch in normalized)
    )


def _survey_candidates_for_slug(slug_publico: str) -> list[EncEncuesta]:
    normalized_slug = str(slug_publico or "").strip().lower()
    if not normalized_slug:
        return []

    candidates: dict[int, EncEncuesta] = {}
    for survey in (
        EncEncuesta.query.filter_by(slug=normalized_slug)
        .limit(3)
        .all()
    ):
        candidates[survey.id] = survey
    for link in (
        EncLink.query.filter_by(slug_publico=normalized_slug)
        .limit(5)
        .all()
    ):
        if link.encuesta:
            candidates[link.encuesta.id] = link.encuesta

    if not candidates:
        base_slug, separator, alias_token = normalized_slug.rpartition("-")
        if separator and len(alias_token) >= 6 and all(ch in "0123456789abcdef" for ch in alias_token):
            for survey in (
                EncEncuesta.query.filter_by(slug=base_slug)
                .limit(3)
                .all()
            ):
                candidates[survey.id] = survey

    if not candidates and re.fullmatch(r"[0-9a-z]{5,12}", normalized_slug):
        for link in (
            EncLink.query.filter(EncLink.slug_publico.endswith(f"-{normalized_slug}", autoescape=True))
            .limit(5)
            .all()
        ):
            if link.encuesta:
                candidates[link.encuesta.id] = link.encuesta
    return list(candidates.values())


def _resolve_survey_tenant_slug(slug_publico: str, data: Any, tenant_slug: str | None) -> str:
    slug = str(slug_publico or "").strip()
    if not _is_valid_survey_room_segment(slug):
        return ""
    try:
        candidates = _survey_candidates_for_slug(slug)
        if len(candidates) == 1:
            return _tenant_slug_for_survey_tenant_id(candidates[0].tenant_id)

        tenant_id_hint = data.get("tenant_id") if isinstance(data, dict) else None
        try:
            normalized_tenant_id = int(tenant_id_hint)
        except (TypeError, ValueError):
            normalized_tenant_id = None
        if normalized_tenant_id is not None:
            tenant_matches = [
                survey for survey in candidates if int(survey.tenant_id or 0) == normalized_tenant_id
            ]
            if len(tenant_matches) == 1:
                return _tenant_slug_for_survey_tenant_id(tenant_matches[0].tenant_id)

        tenant_hint = str(tenant_slug or "").strip()
        if not tenant_hint and isinstance(data, dict):
            tenant_hint = str(data.get("tenant_slug") or data.get("tenant") or "").strip()
        if _is_valid_survey_room_segment(tenant_hint):
            slug_matches = [
                survey
                for survey in candidates
                if _tenant_slug_for_survey_tenant_id(survey.tenant_id).lower() == tenant_hint.lower()
            ]
            if len(slug_matches) == 1:
                return _tenant_slug_for_survey_tenant_id(slug_matches[0].tenant_id)

        current_app.logger.warning(
            "Dropped unresolved or ambiguous survey socket event slug=%s candidates=%s",
            slug,
            len(candidates),
        )
        return ""
    except Exception:
        current_app.logger.exception("Survey socket tenant resolution failed slug=%s", slug)
        return ""


def _survey_realtime_rooms(slug_publico: str, data: Any = None, tenant_slug: str | None = None) -> list[str]:
    slug = str(slug_publico or "").strip()
    if not _is_valid_survey_room_segment(slug):
        return []
    resolved_tenant = _resolve_survey_tenant_slug(slug, data, tenant_slug)
    if not resolved_tenant:
        return []
    return [f"encuesta:{resolved_tenant}:{slug}"]


def _authorized_survey_room(room: str) -> str:
    normalized_room = str(room or "").strip()
    parts = normalized_room.split(":")
    if (
        len(parts) != 3
        or parts[0] != "encuesta"
        or not all(_is_valid_survey_room_segment(part) for part in parts[1:])
    ):
        return ""

    tenant_slug, slug_publico = parts[1:]
    canonical_rooms = _survey_realtime_rooms(slug_publico, tenant_slug=tenant_slug)
    if canonical_rooms == [normalized_room]:
        return normalized_room
    return ""


def _is_authorized_survey_room(room: str) -> bool:
    return bool(_authorized_survey_room(room))


def emit_survey_update(slug_publico: str, data: Any, tenant_slug: str | None = None) -> None:
    """Emit a live update for a specific survey/poll."""
    rooms = _survey_realtime_rooms(slug_publico, data, tenant_slug=tenant_slug)
    if not rooms:
        return
    if isinstance(data, dict) and data.get("contract_version") == "surveys.live_results.v2":
        legacy_payload = data.get("legacy_results")
        modern_payload = {key: value for key, value in data.items() if key != "legacy_results"}
        for room in rooms:
            socketio.emit('survey_update', legacy_payload or modern_payload, room=room)
            socketio.emit('survey_update_v2', modern_payload, room=room)
            socketio.emit('survey.vote.created', modern_payload, room=room)
        return
    for room in rooms:
        socketio.emit('survey_update', data, room=room)
        socketio.emit('survey.vote.created', data, room=room)


def emit_survey_comment(slug_publico: str, data: Any, tenant_slug: str | None = None) -> None:
    """Emit a live comment for a specific survey/poll."""
    for room in _survey_realtime_rooms(slug_publico, data, tenant_slug=tenant_slug):
        socketio.emit('survey_comment', data, room=room)




def send_welcome_message(sid, auth):
    """Sends a welcome message to a newly connected anonymous client."""
    from services.municipio_responder import responder_municipio
    from models import User, ChatSessionContext, Rubro, db
    from uuid import uuid4
    from flask import g

    current_app.logger.info(f"Anonymous connection on web channel detected for sid: {sid}. Sending welcome message.")
    with current_app.app_context():
        owner_user = User.query.filter_by(tipo_chat='municipio', rol='admin').first()
        if not owner_user:
            current_app.logger.error("Default municipality user with role 'admin' and tipo_chat 'municipio' not found.")
            return

        rubro = owner_user.rubro
        if not rubro:
            current_app.logger.error(f"Rubro not found for user {owner_user.id}")
            return

        chat_session_uuid = str(uuid4())
        chat_db_context = ChatSessionContext(
            chat_session_id=chat_session_uuid,
            user_id=owner_user.id,
            context_data={}
        )
        db.session.add(chat_db_context)
        db.session.commit()

        anon_id = str(uuid4())
        if 'viewer' in g:
            del g.viewer

        respuesta = responder_municipio(
            pregunta_original="__INIT__",
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=None,
            chat_db_context=chat_db_context,
            anon_id=anon_id,
            channel='web',
            chat_session_uuid=chat_session_uuid
        )

        ensure_buttons_compatibility(respuesta)

        if respuesta.get("generar_audio"):
            try:
                audio_url = generar_audio(text=respuesta["message_body"])
                if audio_url:
                    respuesta["audio_url"] = audio_url
            except Exception as e:
                current_app.logger.error(f"Error generating welcome audio: {e}")

        emit('message', respuesta, room=sid)
        current_app.logger.info(f"Welcome message sent to sid: {sid}")

@socketio.on('connect')
def on_connect(auth):
    """
    Handles new Socket.IO connections.
    Authenticates the user if a token is provided.
    For anonymous web connections, sends a welcome message.
    """
    current_app.logger.info(f"Socket.IO client connected: {request.sid}")
    auth_payload = auth if isinstance(auth, dict) else {}
    token = _socket_request_token(auth_payload)
    channel = auth_payload.get('channel')

    if token:
        try:
            user = user_from_token(str(token))
            if not user:
                current_app.logger.warning(
                    "Socket.IO connection rejected for sid %s due to invalid or revoked token.",
                    request.sid,
                )
                return False

            tenant_slug = auth_payload.get('tenant_slug') or getattr(user, 'tenant_slug', None)
            rooms = _merge_authenticated_socket_rooms(user, tenant_slug, str(token))
            for room in rooms:
                join_room(room)
                current_app.logger.debug(
                    "Socket.IO sid %s joined room %s for user %s", request.sid, room, user.id
                )

            current_app.logger.info(
                "Socket.IO token validated successfully for sid: %s (rooms=%s)",
                request.sid,
                rooms,
            )
        except Exception as e:
            current_app.logger.exception("Socket.IO unexpected connect error for sid %s: %s", request.sid, e)
            return False
    elif channel == 'web':
        # Defer the welcome message to a separate thread to not block the connection
        socketio.start_background_task(send_welcome_message, request.sid, auth)


@socketio.on('subscribe_ticket_updates')
def on_subscribe_ticket_updates(data):
    payload = data if isinstance(data, dict) else {}
    token = _socket_request_token(payload)
    tenant_slug = payload.get('tenant_slug')
    if not token:
        emit('subscription_error', {'error': 'missing_token'})
        return

    user = user_from_token(str(token))
    if not user:
        current_app.logger.warning("Socket subscribe rejected for sid %s: invalid or revoked token", request.sid)
        emit('subscription_error', {'error': 'invalid_token'})
        return

    if tenant_slug and not _user_can_access_tenant_slug(user, tenant_slug):
        current_app.logger.warning(
            "Socket tenant subscription rejected user=%s tenant_slug=%s",
            user.id,
            tenant_slug,
        )
        emit('subscription_error', {'error': 'tenant_forbidden'})
        return

    rooms = _merge_authenticated_socket_rooms(user, tenant_slug, str(token))
    for room in rooms:
        join_room(room)
    emit('subscribed_ticket_updates', {'rooms': rooms or []})

@socketio.on('join')
def on_join(data):
    payload = data if isinstance(data, dict) else {}
    room = str(payload.get('room') or '').strip()
    if not room:
        emit('join_error', {'error': 'missing_room'})
        return

    authorized_survey_room = _authorized_survey_room(room)
    if authorized_survey_room:
        join_room(authorized_survey_room)
        current_app.logger.debug("Client joined public survey room: %s", authorized_survey_room)
        return

    if room.startswith('ticket_'):
        try:
            access = verify_ticket_room_token(
                str(payload.get('access_token') or payload.get('ticket_token') or ''),
                expected_room=room,
            )
            TicketModel = MunicipioTicket if access['ticket_type'] == 'municipio' else PymeTicket
            ticket = db.session.get(TicketModel, access['ticket_id'])
            if not ticket:
                raise LiveChatAccessError('ticket_not_found')
            if str(getattr(ticket, 'estado', '') or '').strip().lower() in {'cerrado', 'resuelto', 'closed'}:
                raise LiveChatAccessError('ticket_closed')
        except LiveChatAccessError as exc:
            error_code = str(exc) or 'invalid_access_token'
            current_app.logger.warning(
                "Socket ticket room join rejected room=%s reason=%s",
                room,
                error_code,
            )
            emit('join_error', {'error': error_code, 'room': room})
            return

        join_room(room)
        emit('join_ack', {'room': room, 'access_mode': 'signed_ticket_room'})
        return

    # Legacy web-chat session rooms remain isolated by their high-entropy UUID.
    # Tenant/operator rooms are joined during authenticated connect, never here.
    if payload.get('channel') == 'web' and len(room) <= 128:
        try:
            UUID(room)
        except (TypeError, ValueError, AttributeError):
            pass
        else:
            if db.session.get(ChatSessionContext, room):
                join_room(room)
                return

    current_app.logger.warning("Socket generic room join rejected room=%s", room)
    emit('join_error', {'error': 'room_not_joinable', 'room': room})

@socketio.on('new_chat')
def on_new_chat(data):
    # Public and operator messages must pass through persisted HTTP/actions or
    # the authenticated send_chat_message handler. Never relay arbitrary room data.
    current_app.logger.warning("Rejected unsupported client-side new_chat relay")
    emit('chat_error', {'error': 'event_not_supported'})

@socketio.on('send_chat_message')
def handle_send_chat_message(data):
    """
    Manejador para cuando un agente envía un mensaje en el chat de un ticket.
    Guarda el mensaje, lo emite por socket y envía notificaciones a otros canales (Email, SMS, WhatsApp).
    """
    payload = data if isinstance(data, dict) else {}
    token = _socket_request_token(payload)
    room = payload.get('room')
    ticket_id = payload.get('ticket_id')
    ticket_type = payload.get('ticket_type')
    message_text = payload.get('message')

    if not all([token, room, ticket_id, ticket_type]):
        current_app.logger.error("Socket 'send_chat_message' recibio datos incompletos")
        return

    current_user = user_from_token(str(token))
    if not current_user:
        current_app.logger.warning("Token invalido o revocado en 'send_chat_message'")
        emit('chat_error', {'error': 'invalid_token'})
        return
    for identity_room in _clerk_identity_rooms(current_user, str(token)):
        join_room(identity_room)
    if not _is_ticket_operator(current_user):
        current_app.logger.warning(
            "Intento de envio de mensaje de chat por usuario no autorizado: %s",
            getattr(current_user, 'id', None),
        )
        emit('chat_error', {'error': 'operator_forbidden'})
        return

    TicketModel = MunicipioTicket if ticket_type == "municipio" else PymeTicket if ticket_type == "pyme" else None
    ticket_obj = db.session.get(TicketModel, ticket_id) if TicketModel else None
    if not ticket_obj:
        current_app.logger.warning(
            "Socket chat message rejected: unknown ticket type=%s id=%s",
            ticket_type,
            ticket_id,
        )
        emit('chat_error', {'error': 'ticket_not_found'})
        return

    owner_user = _get_owner_user(current_user)
    ticket_tenant_id = getattr(ticket_obj, 'tenant_id', None)
    ticket_tenant = db.session.get(TenantProfile, ticket_tenant_id) if ticket_tenant_id else None
    same_tenant = _user_can_operate_tenant(current_user, ticket_tenant)
    if ticket_type == 'municipio':
        allowed_scope = {
            getattr(current_user, 'municipio_id', None),
            getattr(owner_user, 'municipio_id', None),
            getattr(owner_user, 'id', None) if getattr(owner_user, 'tipo_chat', None) == 'municipio' else None,
        }
        scoped = getattr(ticket_obj, 'municipio_id', None) in (allowed_scope - {None})
    else:
        # Rubro is shared taxonomy, not a tenant boundary. Legacy PyME tickets
        # without tenant_id therefore fail closed for realtime writes.
        scoped = False
    if not (same_tenant or scoped):
        current_app.logger.warning(
            "Socket chat message rejected: user=%s cannot access %s ticket=%s",
            current_user.id,
            ticket_type,
            ticket_id,
        )
        emit('chat_error', {'error': 'ticket_forbidden'})
        return

    if not employee_ticket_category_access_allows(current_user, ticket_obj):
        current_app.logger.warning(
            "Socket chat message rejected by category scope user=%s type=%s",
            current_user.id,
            ticket_type,
        )
        emit('chat_error', {'error': 'ticket_not_found'})
        return

    try:
        message_text = normalize_omnichannel_reply_body(message_text)
    except OmnichannelMessagePolicyError as exc:
        emit('chat_error', {'error': exc.reason_code})
        return

    room = build_ticket_room(ticket_type, ticket_id)

    # Guardar el comentario en la base de datos
    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id,
        tipo_ticket=ticket_type,
        comentario_data={
            "comentario": message_text,
            "user_id": current_user.id,
            "es_admin": True, # Los mensajes desde el panel siempre son de un admin/empleado
            "emit_socket": False,
        }
    )

    if nuevo_comentario:
        db.session.commit()
        # ServicioTickets owns every email/SMS/WhatsApp effect. Queue
        # canaries staged those effects with the comment transaction, while
        # legacy tenants already used the existing direct path. This handler
        # only owns the realtime room event because emit_socket=False above.
        emit_new_chat_message({
            'socket_room': room,
            'ticket_id': ticket_id,
            'tenant_type': ticket_type,
            'tenant_profile_id': getattr(ticket_obj, 'tenant_id', None),
            'municipio_id': getattr(ticket_obj, 'municipio_id', None),
            'message': nuevo_comentario.to_dict(),
        })

    else:
        current_app.logger.error(f"No se pudo guardar el comentario para el ticket {ticket_type} {ticket_id}")


def _is_authorized_location_room(room: str) -> bool:
    normalized = str(room or "").strip()
    if normalized.startswith(
        ("clerk_session:", "clerk_user:", "ticket_", "tenant_", "tenant_slug_", "municipio_", "pyme_", "crm_")
    ):
        return True
    try:
        UUID(normalized)
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _resolve_location_response_room(data: Any) -> Optional[str]:
    socket_sid = str(request.sid or "").strip()
    joined_rooms = set(socketio.server.rooms(socket_sid, namespace="/") or [])
    joined_rooms.discard(socket_sid)
    authorized_rooms = {room for room in joined_rooms if _is_authorized_location_room(room)}

    requested_room = str(data.get("room") or "").strip() if isinstance(data, dict) else ""
    if requested_room:
        return requested_room if requested_room in authorized_rooms else None
    if not authorized_rooms:
        return None

    def priority(room: str) -> tuple[int, str]:
        if room.startswith("clerk_session:"):
            return (0, room)
        if room.startswith("ticket_"):
            return (1, room)
        try:
            UUID(room)
            return (2, room)
        except (TypeError, ValueError, AttributeError):
            return (3, room)

    return sorted(authorized_rooms, key=priority)[0]


@socketio.on('location')
def on_location(data):
    """Geocode location only for sockets already bound to an authorized room."""

    payload = data if isinstance(data, dict) else {}
    response_room = _resolve_location_response_room(payload)
    if not response_room:
        current_app.logger.warning("Rejected unscoped socket location event sid=%s", request.sid)
        emit('location_error', {'error': 'authorized_room_required'})
        return

    try:
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError):
        emit('location_error', {'error': 'invalid_coordinates'})
        return
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        emit('location_error', {'error': 'invalid_coordinates'})
        return

    from services.municipio_responder import handle_location_update

    response = handle_location_update({**payload, "lat": lat, "lon": lon})
    socketio.emit('message', response, room=response_room)

"""Ingesta omnicanal para canales adicionales.

Este módulo normaliza interacciones entrantes desde canales como
Facebook Messenger, Telegram, email o IVR y las consolida en tickets
existentes, generando uno nuevo cuando no haya uno abierto.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import func, or_

from models import (
    ChatSessionContext,
    MunicipioTicket,
    OmnichannelChannelSession,
    OmnichannelConversation,
    OmnichannelMessage,
    PymeTicket,
    TicketComentario,
    User,
    db,
)
from services.ticket_service import servicio_tickets
from utils.time_utils import get_local_now

logger = logging.getLogger(__name__)

OPEN_STATES = {"nuevo", "en_proceso", "en_vivo", "esperando_agente_en_vivo"}


def _normalize_channel(raw: Optional[str]) -> str:
    if not raw:
        return "omnichannel"
    return str(raw).strip().lower() or "omnichannel"


def _normalize_direction(raw: Optional[str]) -> str:
    if not raw:
        return "inbound"
    direction = str(raw).strip().lower()
    return direction if direction in {"inbound", "outbound", "internal"} else "inbound"


def _conversation_contact_filters(user: Optional[User], anon_id: Optional[str]):
    filters = []
    if user and getattr(user, "id", None):
        filters.append(OmnichannelConversation.user_id == user.id)
    if anon_id:
        filters.append(OmnichannelConversation.anon_id == anon_id)
    return filters


def resolve_or_create_conversation(
    *,
    tenant_id: Optional[int],
    channel: str,
    user: Optional[User] = None,
    anon_id: Optional[str] = None,
    external_key: Optional[str] = None,
    legacy_chat_session_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    create_if_missing: bool = True,
) -> Optional[OmnichannelConversation]:
    """Resolve a canonical omnichannel conversation and channel session."""

    conversation: Optional[OmnichannelConversation] = None
    channel_session: Optional[OmnichannelChannelSession] = None
    metadata = metadata or {}

    if external_key:
        channel_session = (
            OmnichannelChannelSession.query.filter_by(channel=channel, external_key=external_key)
            .order_by(OmnichannelChannelSession.last_activity_at.desc())
            .first()
        )
    if not channel_session and legacy_chat_session_id:
        channel_session = (
            OmnichannelChannelSession.query.filter_by(
                channel=channel, legacy_chat_session_id=legacy_chat_session_id
            )
            .order_by(OmnichannelChannelSession.last_activity_at.desc())
            .first()
        )

    if channel_session:
        conversation = channel_session.conversation

    if not conversation:
        filters = _conversation_contact_filters(user, anon_id)
        if tenant_id:
            filters.append(OmnichannelConversation.tenant_id == tenant_id)
        if filters:
            conversation = (
                OmnichannelConversation.query.filter(*filters)
                .order_by(OmnichannelConversation.last_activity_at.desc())
                .first()
            )

    if not conversation and legacy_chat_session_id:
        session_ctx = ChatSessionContext.query.filter_by(chat_session_id=legacy_chat_session_id).first()
        if session_ctx and isinstance(session_ctx.context_data, dict):
            candidate_id = session_ctx.context_data.get("conversation_id")
            if candidate_id:
                conversation = OmnichannelConversation.query.get(candidate_id)

    if not conversation and not create_if_missing:
        return None

    if not conversation:
        conversation = OmnichannelConversation(
            tenant_id=tenant_id,
            user_id=getattr(user, "id", None),
            anon_id=anon_id or getattr(user, "anon_id", None),
            tags=[],
        )
        db.session.add(conversation)
        db.session.flush()

    if not channel_session:
        channel_session = OmnichannelChannelSession(
            conversation_id=conversation.id,
            channel=channel,
            external_key=external_key,
            legacy_chat_session_id=legacy_chat_session_id,
            metadata_json=metadata or {},
        )
        db.session.add(channel_session)
    else:
        merged_meta = dict(channel_session.metadata_json or {})
        merged_meta.update(metadata or {})
        channel_session.metadata_json = merged_meta
        if external_key and not channel_session.external_key:
            channel_session.external_key = external_key
        if legacy_chat_session_id and not channel_session.legacy_chat_session_id:
            channel_session.legacy_chat_session_id = legacy_chat_session_id

    now = get_local_now()
    channel_session.last_activity_at = now
    conversation.last_activity_at = now
    if tenant_id and not conversation.tenant_id:
        conversation.tenant_id = tenant_id
    if user and not conversation.user_id:
        conversation.user_id = user.id
    if anon_id and not conversation.anon_id:
        conversation.anon_id = anon_id

    if legacy_chat_session_id:
        session_ctx = ChatSessionContext.query.filter_by(chat_session_id=legacy_chat_session_id).first()
        if not session_ctx:
            session_ctx = ChatSessionContext(
                chat_session_id=legacy_chat_session_id,
                tenant_id=tenant_id,
                user_id=getattr(user, "id", None),
                anon_id=anon_id or getattr(user, "anon_id", None),
                context_data={},
            )
            db.session.add(session_ctx)
        context_data = session_ctx.context_data if isinstance(session_ctx.context_data, dict) else {}
        context_data["conversation_id"] = conversation.id
        session_ctx.context_data = context_data

    db.session.flush()
    return conversation


def append_conversation_message(
    *,
    conversation: OmnichannelConversation,
    channel: str,
    direction: str,
    payload: Dict[str, Any],
    attachments: Optional[list[dict]] = None,
    external_message_id: Optional[str] = None,
    external_key: Optional[str] = None,
    legacy_chat_session_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> OmnichannelMessage:
    """Append a normalized message to the canonical conversation timeline."""

    channel_session = None
    if external_key:
        channel_session = (
            OmnichannelChannelSession.query.filter_by(
                conversation_id=conversation.id,
                channel=channel,
                external_key=external_key,
            )
            .order_by(OmnichannelChannelSession.last_activity_at.desc())
            .first()
        )
    if not channel_session and legacy_chat_session_id:
        channel_session = (
            OmnichannelChannelSession.query.filter_by(
                conversation_id=conversation.id,
                channel=channel,
                legacy_chat_session_id=legacy_chat_session_id,
            )
            .order_by(OmnichannelChannelSession.last_activity_at.desc())
            .first()
        )
    if not channel_session:
        channel_session = OmnichannelChannelSession(
            conversation_id=conversation.id,
            channel=channel,
            external_key=external_key,
            legacy_chat_session_id=legacy_chat_session_id,
            metadata_json=metadata or {},
        )
        db.session.add(channel_session)
        db.session.flush()

    message = OmnichannelMessage(
        conversation_id=conversation.id,
        channel_session_id=channel_session.id,
        direction=_normalize_direction(direction),
        payload=payload or {},
        attachments=attachments or [],
        external_message_id=external_message_id,
    )
    now = get_local_now()
    channel_session.last_activity_at = now
    conversation.last_activity_at = now
    db.session.add(message)
    db.session.flush()
    return message


def get_conversation_timeline(conversation_id: str) -> Dict[str, Any] | None:
    conversation = OmnichannelConversation.query.get(conversation_id)
    if not conversation:
        return None
    channel_sessions = [
        session.to_dict()
        for session in conversation.channel_sessions.order_by(OmnichannelChannelSession.created_at.asc()).all()
    ]
    messages = [
        message.to_dict()
        for message in conversation.messages.order_by(OmnichannelMessage.created_at.asc()).all()
    ]
    return {
        "conversation": conversation.to_dict(),
        "channel_sessions": channel_sessions,
        "messages": messages,
    }


def _deduplicate_contact(contacto: Dict[str, Any]) -> User:
    """Busca o crea un contacto base usando email/telefono/anon_id."""

    email = (contacto.get("email") or "").strip().lower() or None
    telefono = (contacto.get("telefono") or contacto.get("phone") or "").strip() or None
    anon_id = (contacto.get("external_id") or contacto.get("anon_id") or "").strip() or None

    user: Optional[User] = None

    if email:
        user = User.query.filter(func.lower(User.email) == email).first()
    if not user and telefono:
        user = User.query.filter(User.telefono == telefono).first()
    if not user and anon_id:
        user = User.query.filter(User.anon_id == anon_id).first()

    if not user:
        generated_email = email or f"omni+{uuid.uuid4().hex[:12]}@placeholder.local"
        user = User(
            name=contacto.get("nombre") or contacto.get("name") or "Contacto omnicanal",
            email=generated_email,
            telefono=telefono,
            anon_id=anon_id,
            rol="usuario",
        )
        db.session.add(user)
        db.session.flush()
    else:
        updated = False
        if not user.telefono and telefono:
            user.telefono = telefono
            updated = True
        if not user.anon_id and anon_id:
            user.anon_id = anon_id
            updated = True
        if updated:
            db.session.flush()

    return user


def _buscar_ticket_abierto(
    tipo_ticket: str,
    user: User,
    tenant_id: Optional[int],
) -> Optional[MunicipioTicket | PymeTicket]:
    if tipo_ticket == "pyme":
        query = PymeTicket.query.filter(
            or_(PymeTicket.user_id == user.id, PymeTicket.anon_id == user.anon_id)
        )
        if tenant_id:
            query = query.filter(PymeTicket.rubro_id == tenant_id)
        return (
            query.filter(PymeTicket.estado.in_(OPEN_STATES))
            .order_by(PymeTicket.fecha.desc())
            .first()
        )

    query = MunicipioTicket.query.filter(
        or_(MunicipioTicket.user_id == user.id, MunicipioTicket.anon_id == user.anon_id)
    )
    if tenant_id:
        query = query.filter(MunicipioTicket.municipio_id == tenant_id)
    return (
        query.filter(MunicipioTicket.estado.in_(OPEN_STATES))
        .order_by(MunicipioTicket.fecha.desc())
        .first()
    )


def registrar_interaccion_omnicanal(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    """Persistir una interacción externa en el ticket apropiado.

    El payload puede provenir de un webhook heterogéneo, por lo que se valida y
    normaliza antes de impactar en la base de datos. Siempre se devuelven
    mensajes de error estructurados (exito=False, motivo=...).
    """

    if not isinstance(payload, dict):
        return {"exito": False, "motivo": "payload_invalido"}

    canal = _normalize_channel(payload.get("canal"))
    tipo_ticket = (payload.get("tipo_ticket") or "municipio").strip().lower()
    if tipo_ticket not in {"municipio", "pyme"}:
        return {"exito": False, "motivo": "tipo_ticket_invalido"}

    tenant_id = payload.get("tenant_id") or payload.get("municipio_id") or payload.get("rubro_id")
    contacto = payload.get("contacto") or {}
    mensaje = (payload.get("mensaje") or payload.get("transcripcion") or "").strip()
    external_key = (
        payload.get("external_key")
        or contacto.get("telefono")
        or contacto.get("phone")
        or contacto.get("email")
        or contacto.get("external_id")
    )
    legacy_chat_session_id = payload.get("chat_session_id") or payload.get("legacy_chat_session_id")
    external_message_id = payload.get("external_message_id") or payload.get("message_id")

    if not mensaje and not payload.get("asunto") and not payload.get("detalles"):
        return {"exito": False, "motivo": "mensaje_requerido"}

    user = _deduplicate_contact(contacto)
    conversation = resolve_or_create_conversation(
        tenant_id=tenant_id,
        channel=canal,
        user=user,
        anon_id=user.anon_id,
        external_key=external_key,
        legacy_chat_session_id=legacy_chat_session_id,
        metadata={"tipo_ticket": tipo_ticket},
    )

    ticket_existente = _buscar_ticket_abierto(tipo_ticket, user, tenant_id)
    nuevo_ticket = False

    if not ticket_existente:
        ticket_data: Dict[str, Any] = {
            "user_id": user.id,
            "anon_id": user.anon_id,
            "asunto": payload.get("asunto") or f"Interacción {canal}",
            "categoria": payload.get("categoria") or payload.get("topic"),
            "pregunta": mensaje or payload.get("asunto") or "Consulta desde canal externo",
            "detalles": payload.get("detalles") or mensaje,
            "canal_ingreso": canal,
            "telefono_vecino": contacto.get("telefono") or contacto.get("phone"),
            "email_vecino": contacto.get("email"),
            "nombre_vecino": contacto.get("nombre") or contacto.get("name"),
            "municipio_id": tenant_id if tipo_ticket == "municipio" else None,
            "rubro_id": tenant_id if tipo_ticket == "pyme" else None,
        }

        creation_result = servicio_tickets.crear_nuevo_ticket(
            tipo_ticket, ticket_data, return_object=True
        )
        ticket_existente = creation_result if not isinstance(creation_result, dict) else None
        nuevo_ticket = True

    if not ticket_existente:
        logger.error("No se pudo crear o recuperar el ticket para la interacción omnicanal")
        db.session.rollback()
        return {"exito": False, "motivo": "ticket_no_disponible"}

    if mensaje:
        comentario = TicketComentario(
            comentario=mensaje,
            user_id=user.id,
            es_admin=False,
            origen=canal,
        )
        comentario.anon_id = user.anon_id
        if tipo_ticket == "municipio":
            comentario.municipio_ticket = ticket_existente  # type: ignore[assignment]
            if hasattr(ticket_existente, "ultima_actividad"):
                ticket_existente.ultima_actividad = get_local_now()
        else:
            comentario.pyme_ticket = ticket_existente  # type: ignore[assignment]
        db.session.add(comentario)

        if conversation:
            append_conversation_message(
                conversation=conversation,
                channel=canal,
                direction=payload.get("direction") or "inbound",
                payload={
                    "text": mensaje,
                    "subject": payload.get("asunto"),
                    "details": payload.get("detalles"),
                    "ticket_id": ticket_existente.id,
                    "ticket_type": tipo_ticket,
                },
                attachments=list(payload.get("attachments") or []),
                external_message_id=external_message_id,
                external_key=external_key,
                legacy_chat_session_id=legacy_chat_session_id,
                metadata={"tipo_ticket": tipo_ticket},
            )

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Error guardando interacción omnicanal: %s", exc, exc_info=True)
        db.session.rollback()
        return {"exito": False, "motivo": "db_error"}

    return {
        "exito": True,
        "nuevo_ticket": nuevo_ticket,
        "ticket_id": ticket_existente.id,
        "canal": canal,
        "conversation_id": conversation.id if conversation else None,
    }

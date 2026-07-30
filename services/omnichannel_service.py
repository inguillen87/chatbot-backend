"""Ingesta omnicanal para canales adicionales.

Este módulo normaliza interacciones entrantes desde canales como
Facebook Messenger, Telegram, email o IVR y las consolida en tickets
existentes, generando uno nuevo cuando no haya uno abierto.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm.attributes import flag_modified

from models import (
    MunicipioTicket,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TicketComentario,
    User,
    db,
)
from services.ticket_service import servicio_tickets
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    municipio_ticket_scope_filter,
    normalize_municipio_ticket_write_scope,
)
from utils.time_utils import get_local_now

logger = logging.getLogger(__name__)

OPEN_STATES = {
    "nuevo",
    "open",
    "pendiente",
    "in_progress",
    "en_proceso",
    "en_vivo",
    "esperando_agente_en_vivo",
    "waiting_customer",
}
def _normalize_channel(raw: Optional[str]) -> str:
    if not raw:
        return "omnichannel"
    return str(raw).strip().lower() or "omnichannel"


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_ticket_type(raw: Any) -> Optional[str]:
    value = str(raw or "municipio").strip().lower()
    if value in {"commerce", "comercio", "tenant", "tenant_ticket"}:
        return "pyme"
    if value in {"municipio", "pyme"}:
        return value
    return None


def _contact_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("contacto") or payload.get("contact") or {}
    return raw if isinstance(raw, dict) else {}


def _deduplicate_contact(contacto: Dict[str, Any]) -> User:
    """Busca o crea un contacto base usando email/telefono/anon_id."""

    contacto = contacto if isinstance(contacto, dict) else {}
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
        try:
            user.set_password(uuid.uuid4().hex)
        except Exception:  # pragma: no cover - defensive for test doubles
            user.password_hash = uuid.uuid4().hex
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


def _widget_token_matches(tenant: TenantProfile, token: str) -> bool:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    raw_tokens = cfg.get("widget_tokens")
    if isinstance(raw_tokens, str):
        tokens = [raw_tokens]
    elif isinstance(raw_tokens, list):
        tokens = raw_tokens
    else:
        tokens = []
    return token in {str(item).strip() for item in tokens if str(item).strip()}


def _whatsapp_destination_matches(tenant: TenantProfile, destination: str) -> bool:
    normalized = destination.strip().lower()
    if not normalized:
        return False
    if normalized == str(getattr(tenant, "whatsapp_sender_id", "") or "").strip().lower():
        return True
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    raw_numbers = cfg.get("whatsapp_numbers")
    if isinstance(raw_numbers, str):
        numbers = [raw_numbers]
    elif isinstance(raw_numbers, list):
        numbers = raw_numbers
    else:
        numbers = []
    return normalized in {str(item).strip().lower() for item in numbers if str(item).strip()}


def _resolve_tenant_profile(payload: Dict[str, Any]) -> Optional[TenantProfile]:
    lead_profile = payload.get("lead_profile") if isinstance(payload.get("lead_profile"), dict) else {}

    tenant_id = _as_int(payload.get("tenant_id") or lead_profile.get("tenant_id"))
    if tenant_id:
        tenant = db.session.get(TenantProfile, tenant_id)
        if tenant:
            return tenant

    tenant_slug = _clean(
        payload.get("tenant_slug")
        or payload.get("tenant")
        or payload.get("slug")
        or lead_profile.get("tenant_slug")
    )
    if tenant_slug:
        tenant = (
            TenantProfile.query.filter(func.lower(TenantProfile.slug) == tenant_slug.lower())
            .limit(1)
            .first()
        )
        if tenant:
            return tenant

    pyme_id = _as_int(payload.get("pyme_id") or payload.get("owner_id") or lead_profile.get("pyme_id"))
    if pyme_id:
        tenant = TenantProfile.query.filter_by(pyme_id=pyme_id).first()
        if tenant:
            return tenant

    widget_token = _clean(payload.get("widget_token") or payload.get("widgetToken") or payload.get("token"))
    if widget_token:
        for tenant in TenantProfile.query.all():
            if _widget_token_matches(tenant, widget_token):
                return tenant

    destination = _clean(
        payload.get("whatsapp_destination_number")
        or payload.get("to")
        or payload.get("recipient")
        or payload.get("business_phone")
    )
    if destination:
        for tenant in TenantProfile.query.all():
            if _whatsapp_destination_matches(tenant, destination):
                return tenant

    return None


def _buscar_ticket_abierto(
    tipo_ticket: str,
    user: User,
    tenant: Optional[TenantProfile],
    rubro_id: Optional[int] = None,
) -> Optional[MunicipioTicket | PymeTicket]:
    if tipo_ticket == "pyme":
        filters = [PymeTicket.user_id == user.id]
        if user.anon_id:
            filters.append(PymeTicket.anon_id == user.anon_id)
        query = PymeTicket.query.filter(or_(*filters))
        if tenant:
            query = query.filter(PymeTicket.tenant_id == tenant.id)
        elif rubro_id:
            query = query.filter(PymeTicket.rubro_id == rubro_id)
        return (
            query.filter(PymeTicket.estado.in_(OPEN_STATES))
            .order_by(PymeTicket.fecha.desc())
            .first()
        )

    filters = [MunicipioTicket.user_id == user.id]
    if user.anon_id:
        filters.append(MunicipioTicket.anon_id == user.anon_id)
    query = MunicipioTicket.query.filter(or_(*filters))
    if tenant:
        query = query.filter(municipio_ticket_scope_filter(tenant))
    return (
        query.filter(MunicipioTicket.estado.in_(OPEN_STATES))
        .order_by(MunicipioTicket.fecha.desc())
        .first()
    )


def _buscar_tenant_ticket_abierto(user: User, tenant: TenantProfile) -> Optional[TenantTicket]:
    return (
        TenantTicket.query.filter(
            TenantTicket.tenant_id == tenant.id,
            TenantTicket.user_id == user.id,
            TenantTicket.estado.in_(OPEN_STATES),
        )
        .order_by(TenantTicket.updated_at.desc())
        .first()
    )


def _contact_metadata(contacto: Dict[str, Any], user: User) -> Dict[str, Any]:
    telefono = _clean(contacto.get("telefono") or contacto.get("phone"))
    email = _clean(contacto.get("email"))
    nombre = _clean(contacto.get("nombre") or contacto.get("name")) or "Contacto omnicanal"
    external_id = _clean(contacto.get("external_id") or contacto.get("anon_id"))

    metadata = dict(contacto)
    metadata.update(
        {
            "name": metadata.get("name") or nombre,
            "nombre": metadata.get("nombre") or nombre,
            "phone": metadata.get("phone") or telefono,
            "telefono": metadata.get("telefono") or telefono,
            "email": metadata.get("email") or email,
            "external_id": metadata.get("external_id") or external_id,
            "anon_id": metadata.get("anon_id") or user.anon_id,
            "user_id": user.id,
        }
    )
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def _payload_location(payload: Dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[str]]:
    coords = payload.get("coordenadas") if isinstance(payload.get("coordenadas"), dict) else {}
    lat = _as_float(payload.get("latitud") or payload.get("lat") or payload.get("latitude") or coords.get("lat"))
    lng = _as_float(
        payload.get("longitud")
        or payload.get("lon")
        or payload.get("lng")
        or payload.get("longitude")
        or coords.get("lon")
        or coords.get("lng")
    )
    address = _clean(payload.get("direccion") or payload.get("address") or payload.get("ubicacion"))
    return lat, lng, address


def _pedido_reference(payload: Dict[str, Any]) -> Optional[str]:
    return _clean(
        payload.get("pedido_reference")
        or payload.get("order_reference")
        or payload.get("pedido_id")
        or payload.get("order_id")
    )


def _tenant_ticket_extra(
    payload: Dict[str, Any],
    *,
    tenant: TenantProfile,
    user: User,
    contacto: Dict[str, Any],
    canal: str,
    mensaje: str,
) -> Dict[str, Any]:
    lead_profile = payload.get("lead_profile") if isinstance(payload.get("lead_profile"), dict) else {}
    source = _clean(payload.get("source") or payload.get("origen") or payload.get("lead_source")) or canal
    chat_session_id = _clean(payload.get("chat_session_id") or payload.get("source_chat_session_id"))
    session_id = _clean(payload.get("session_id"))
    conversation_id = (
        _clean(payload.get("conversation_id"))
        or chat_session_id
        or session_id
        or f"{canal}:{user.anon_id or user.id}"
    )
    lat, lng, address = _payload_location(payload)
    contact = _contact_metadata(contacto, user)
    pedido_ref = _pedido_reference(payload)

    extra: Dict[str, Any] = {
        "title": _clean(payload.get("asunto")) or f"Interaccion {canal}",
        "type": "commerce_inbound",
        "priority": _clean(payload.get("priority")) or "medium",
        "channel": canal,
        "conversation_id": conversation_id,
        "chat_session_id": chat_session_id,
        "session_id": session_id,
        "contact_key": _clean(payload.get("contact_key")) or contact.get("external_id") or contact.get("phone"),
        "whatsapp_message_id": _clean(payload.get("whatsapp_message_id") or payload.get("message_id")),
        "contact": contact,
        "lead_profile": {
            **lead_profile,
            "tenant_id": lead_profile.get("tenant_id") or tenant.id,
            "tenant_slug": lead_profile.get("tenant_slug") or tenant.slug,
        },
        "source": source,
        "lead_source": source,
        "anon_id": user.anon_id,
        "user_id": user.id,
        "address": address,
        "preview_text": mensaje or payload.get("detalles") or payload.get("asunto"),
        "comments": [],
        "delivery_contract": {
            "reply_mode": "timeline_only",
            "external_dispatch": False,
            "fallback": "saved_to_crm_no_external_dispatch",
        },
    }
    if pedido_ref:
        extra["pedido_reference"] = pedido_ref
    for key in ("attachments", "source_attachment", "uploaded_file_info", "files"):
        if payload.get(key):
            extra[key] = payload.get(key)
    if lat is not None:
        extra["lat"] = lat
    if lng is not None:
        extra["lng"] = lng
    return extra


def _append_tenant_ticket_comment(extra: Dict[str, Any], *, user: User, canal: str, body: str) -> None:
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    contact = extra.get("contact") if isinstance(extra.get("contact"), dict) else {}
    comments.append(
        {
            "id": uuid.uuid4().hex,
            "type": "message",
            "origin": canal,
            "body": body,
            "visibility": "public",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "actor": {
                "id": user.id,
                "type": "contact",
                "name": contact.get("name") or contact.get("nombre") or "Contacto omnicanal",
            },
        }
    )
    extra["comments"] = comments[-100:]


def _merge_tenant_ticket_extra(
    ticket: TenantTicket,
    payload: Dict[str, Any],
    *,
    tenant: TenantProfile,
    user: User,
    contacto: Dict[str, Any],
    canal: str,
    mensaje: str,
) -> Dict[str, Any]:
    existing = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    incoming = _tenant_ticket_extra(
        payload,
        tenant=tenant,
        user=user,
        contacto=contacto,
        canal=canal,
        mensaje=mensaje,
    )
    merged = dict(existing)

    for key in (
        "channel",
        "conversation_id",
        "chat_session_id",
        "session_id",
        "contact_key",
        "whatsapp_message_id",
        "source",
        "lead_source",
        "anon_id",
        "user_id",
        "address",
        "preview_text",
        "pedido_reference",
    ):
        if incoming.get(key):
            merged[key] = incoming[key]

    existing_contact = merged.get("contact") if isinstance(merged.get("contact"), dict) else {}
    merged["contact"] = {**existing_contact, **incoming.get("contact", {})}

    existing_lead = merged.get("lead_profile") if isinstance(merged.get("lead_profile"), dict) else {}
    merged["lead_profile"] = {**existing_lead, **incoming.get("lead_profile", {})}

    merged["delivery_contract"] = incoming["delivery_contract"]
    for key in ("attachments", "source_attachment", "uploaded_file_info", "files"):
        if incoming.get(key):
            merged[key] = incoming[key]

    return merged


def _registrar_tenant_interaccion(
    payload: Dict[str, Any],
    *,
    tenant: TenantProfile,
    user: User,
    contacto: Dict[str, Any],
    canal: str,
    mensaje: str,
) -> Dict[str, Any]:
    ticket = _buscar_tenant_ticket_abierto(user, tenant)
    nuevo_ticket = False
    if ticket:
        extra = _merge_tenant_ticket_extra(
            ticket,
            payload,
            tenant=tenant,
            user=user,
            contacto=contacto,
            canal=canal,
            mensaje=mensaje,
        )
    else:
        lat, lng, _address = _payload_location(payload)
        extra = _tenant_ticket_extra(
            payload,
            tenant=tenant,
            user=user,
            contacto=contacto,
            canal=canal,
            mensaje=mensaje,
        )
        ticket = TenantTicket(
            tenant_id=tenant.id,
            user_id=user.id,
            categoria=payload.get("categoria") or payload.get("topic") or "omnichannel",
            descripcion=payload.get("detalles") or mensaje or payload.get("asunto") or "Consulta desde canal externo",
            estado="nuevo",
            origen=canal,
            latitud=lat,
            longitud=lng,
            datos_extra=extra,
            fingerprint=_clean(payload.get("idempotency_key") or payload.get("fingerprint")),
        )
        db.session.add(ticket)
        nuevo_ticket = True

    if mensaje:
        _append_tenant_ticket_comment(extra, user=user, canal=canal, body=mensaje)

    ticket.datos_extra = extra
    ticket.updated_at = datetime.now(timezone.utc)
    flag_modified(ticket, "datos_extra")
    db.session.add(ticket)

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Error guardando interaccion omnicanal tenant: %s", exc, exc_info=True)
        db.session.rollback()
        return {"exito": False, "motivo": "db_error"}

    return {
        "exito": True,
        "nuevo_ticket": nuevo_ticket,
        "ticket_id": ticket.id,
        "source_model": "TenantTicket",
        "tenant_id": tenant.id,
        "canal": canal,
        "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}",
    }


def registrar_interaccion_omnicanal(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    """Persistir una interacción externa en el ticket apropiado.

    El payload puede provenir de un webhook heterogéneo, por lo que se valida y
    normaliza antes de impactar en la base de datos. Siempre se devuelven
    mensajes de error estructurados (exito=False, motivo=...).
    """

    if not isinstance(payload, dict):
        return {"exito": False, "motivo": "payload_invalido"}

    canal = _normalize_channel(payload.get("canal"))
    tipo_ticket = _normalize_ticket_type(payload.get("tipo_ticket"))
    if tipo_ticket not in {"municipio", "pyme"}:
        return {"exito": False, "motivo": "tipo_ticket_invalido"}

    tenant = _resolve_tenant_profile(payload)
    municipio_scope: dict[str, Any] | None = None
    if tipo_ticket == "municipio":
        try:
            municipio_scope = normalize_municipio_ticket_write_scope(
                {
                    "tenant_id": payload.get("tenant_id"),
                    "municipio_id": payload.get("municipio_id") or payload.get("owner_id"),
                }
            )
        except TicketTenantScopeError as exc:
            return {"exito": False, "motivo": exc.code}
        tenant = db.session.get(TenantProfile, municipio_scope["tenant_id"])
    rubro_id = _as_int(payload.get("rubro_id"))
    contacto = _contact_from_payload(payload)
    mensaje = (payload.get("mensaje") or payload.get("transcripcion") or "").strip()

    if not mensaje and not payload.get("asunto") and not payload.get("detalles"):
        return {"exito": False, "motivo": "mensaje_requerido"}

    user = _deduplicate_contact(contacto)

    if tipo_ticket == "pyme" and tenant:
        return _registrar_tenant_interaccion(
            payload,
            tenant=tenant,
            user=user,
            contacto=contacto,
            canal=canal,
            mensaje=mensaje,
        )

    ticket_existente = _buscar_ticket_abierto(tipo_ticket, user, tenant, rubro_id)
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
            "municipio_id": municipio_scope["municipio_id"] if municipio_scope else None,
            "tenant_id": (
                municipio_scope["tenant_id"]
                if municipio_scope
                else (tenant.id if tenant else None)
            ),
            "rubro_id": rubro_id if tipo_ticket == "pyme" else None,
            "pyme_id": payload.get("pyme_id"),
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
    }

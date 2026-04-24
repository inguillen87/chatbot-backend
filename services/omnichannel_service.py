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
    MunicipioTicket,
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

    if not mensaje and not payload.get("asunto") and not payload.get("detalles"):
        return {"exito": False, "motivo": "mensaje_requerido"}

    user = _deduplicate_contact(contacto)

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

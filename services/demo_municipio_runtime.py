from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from models import ChatSessionContext, MunicipioTicket, TenantProfile, TicketComentario, User, db


DEMO_MUNICIPIO_SOURCE = "demo_municipio_runtime"
DEMO_MUNICIPIO_CHANNEL = "web_demo_widget"


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _compact(text: str, *, max_len: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "..."


def _tenant_for_owner(owner_user: User | None) -> TenantProfile | None:
    if not owner_user:
        return None
    tenant = (
        getattr(owner_user, "tenant_profile_municipio", None)
        or getattr(owner_user, "tenant_profile_pyme", None)
        or getattr(owner_user, "tenant", None)
    )
    if tenant:
        return tenant
    tenant_id = getattr(owner_user, "tenant_id", None)
    if tenant_id:
        found = db.session.get(TenantProfile, tenant_id)
        if found:
            return found
    return TenantProfile.query.filter_by(municipio_id=owner_user.id).first()


def _location_data(location: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(location, dict):
        return None
    lat = location.get("lat") or location.get("latitude")
    lng = location.get("lng") or location.get("lon") or location.get("longitude")
    address = location.get("address") or location.get("direccion") or location.get("formatted_address")
    data: dict[str, Any] = {}
    if address:
        data["address"] = str(address).strip()
    try:
        if lat is not None:
            data["lat"] = float(lat)
        if lng is not None:
            data["lng"] = float(lng)
    except (TypeError, ValueError):
        data.pop("lat", None)
        data.pop("lng", None)
    return data or None


def _media_kind(attachment_info: dict[str, Any] | None) -> str | None:
    if not isinstance(attachment_info, dict):
        return None
    raw = " ".join(
        str(attachment_info.get(key) or "")
        for key in ("kind", "type", "mime_type", "mimeType", "content_type", "name", "filename")
    ).lower()
    if "image" in raw or raw.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    if "audio" in raw or raw.endswith((".ogg", ".oga", ".mp3", ".wav", ".m4a")):
        return "audio"
    if "video" in raw or raw.endswith((".mp4", ".mov", ".webm")):
        return "video"
    if raw.strip():
        return "file"
    return None


def _media_data(attachment_info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(attachment_info, dict):
        return None
    kind = _media_kind(attachment_info) or "file"
    data = {
        "kind": kind,
        "id": attachment_info.get("id"),
        "label": attachment_info.get("label") or attachment_info.get("name") or attachment_info.get("filename") or kind,
        "url": attachment_info.get("url") or attachment_info.get("preview_url") or attachment_info.get("image_url"),
        "mime_type": attachment_info.get("mime_type") or attachment_info.get("mimeType") or attachment_info.get("content_type"),
        "transcript": attachment_info.get("transcribed_text") or attachment_info.get("transcript"),
    }
    return {key: value for key, value in data.items() if value}


def _classify_demo_intent(
    question: str,
    action_id: str | None,
    attachment_info: dict[str, Any] | None,
    location: dict[str, Any] | None,
) -> dict[str, Any] | None:
    text = _fold(question)
    action = _fold(action_id)
    media = _media_data(attachment_info)
    has_location = bool(_location_data(location))

    if text in {"", "__init__", "hola", "buenas", "buen dia", "buenas tardes"} and not action and not media and not has_location:
        return None

    if "estado" in action or "consultar_estado" in action or "consultar estado" in text:
        return {"kind": "status_lookup", "category": None}

    if any(token in text for token in ("licencia", "registro", "carnet", "turno")):
        return {"kind": "info", "category": "Licencias de conducir"}

    claim_terms = (
        "bache", "baches", "pozo", "pozos", "pavimento", "calle rota", "asfalto",
        "alumbrado", "luminaria", "reclamo", "ticket", "caso", "semaforo",
    )
    tool_terms = (
        "ubicacion", "direccion", "mapa", "maps", "telefono", "whatsapp", "contacto",
        "horario", "horarios", "catalogo", "lista de precios", "precios", "web",
    )
    if ("tool_" in action or any(token in text for token in tool_terms)) and not any(token in text for token in claim_terms):
        return {"kind": "tool_lookup", "category": "Herramientas publicas"}

    if any(token in text for token in ("videollamada", "video llamada", "llamada", "call", "operador", "persona")):
        return {"kind": "human_handoff", "category": "Atencion personalizada"}

    if any(token in text for token in ("encuesta", "votacion", "votar", "sondeo")):
        return {"kind": "survey", "category": "Participacion ciudadana"}

    if any(token in text for token in ("bache", "baches", "pozo", "pozos", "pavimento", "calle rota", "asfalto")):
        return {"kind": "claim", "category": "Baches y calzada", "priority": "Alta"}

    if any(token in text for token in ("alumbrado", "luminaria", "luz", "poste", "farola")) or "💡" in question:
        return {"kind": "claim", "category": "Alumbrado publico", "priority": "Media"}

    if any(token in text for token in ("basura", "residuos", "recoleccion", "contenedor", "limpieza")):
        return {"kind": "claim", "category": "Higiene urbana", "priority": "Media"}

    if any(token in text for token in ("semaforo", "transito", "senal", "señal")):
        return {"kind": "claim", "category": "Transito y senalizacion", "priority": "Alta"}

    if any(token in text for token in ("reclamo", "ticket", "caso", "crear_ticket", "crear ticket")) or "crear_ticket" in action:
        return {"kind": "claim", "category": "Reclamo ciudadano", "priority": "Media"}

    if media or has_location:
        return {"kind": "claim", "category": "Evidencia ciudadana", "priority": "Media"}

    return None


def _context_runtime(chat_db_context: ChatSessionContext | None) -> dict[str, Any]:
    if not chat_db_context:
        return {}
    if not isinstance(chat_db_context.context_data, dict):
        chat_db_context.context_data = {}
    runtime = chat_db_context.context_data.setdefault(DEMO_MUNICIPIO_SOURCE, {})
    if not isinstance(runtime, dict):
        runtime = {}
        chat_db_context.context_data[DEMO_MUNICIPIO_SOURCE] = runtime
    runtime.setdefault("ticket_ids", [])
    runtime.setdefault("events", [])
    return runtime


def _details(ticket: MunicipioTicket) -> dict[str, Any]:
    try:
        parsed = json.loads(ticket.detalles or "{}")
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _last_ticket(runtime: dict[str, Any]) -> MunicipioTicket | None:
    ticket_id = runtime.get("last_ticket_id")
    if not ticket_id:
        ids = runtime.get("ticket_ids") or []
        ticket_id = ids[-1] if ids else None
    try:
        return db.session.get(MunicipioTicket, int(ticket_id)) if ticket_id else None
    except Exception:
        return None


def _append_comment(ticket: MunicipioTicket, *, text: str, anon_id: str | None, media: dict[str, Any] | None, location: dict[str, Any] | None) -> None:
    pieces = []
    if text:
        pieces.append(_compact(text, max_len=500))
    if media:
        pieces.append(f"Adjunto {media.get('kind')}: {media.get('label') or media.get('id') or 'archivo'}")
    if location:
        location_label = location.get("address") or f"{location.get('lat')}, {location.get('lng')}"
        pieces.append(f"Ubicacion: {location_label}")
    comment_text = " | ".join(piece for piece in pieces if piece) or "Actualizacion recibida desde demo"
    db.session.add(
        TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario=comment_text,
            anon_id=anon_id,
            origen="chat",
            estado_ticket=ticket.estado,
        )
    )


def _upsert_ticket(
    *,
    question: str,
    intent: dict[str, Any],
    owner_user: User | None,
    anon_id: str | None,
    chat_session_id: str | None,
    chat_db_context: ChatSessionContext | None,
    attachment_info: dict[str, Any] | None,
    location: dict[str, Any] | None,
    demo_session_payload: dict[str, Any] | None,
) -> tuple[MunicipioTicket, bool, dict[str, Any], dict[str, Any] | None]:
    tenant = _tenant_for_owner(owner_user)
    runtime = _context_runtime(chat_db_context)
    media = _media_data(attachment_info)
    loc = _location_data(location)
    category = str(intent.get("category") or "Reclamo ciudadano")
    priority = str(intent.get("priority") or "Media")

    ticket = _last_ticket(runtime)
    should_update_last = bool(
        ticket
        and (media or loc)
        and ticket.categoria
        and _fold(ticket.categoria) in {_fold(category), "reclamo ciudadano", "evidencia ciudadana"}
    )
    created = False
    if not should_update_last:
        ticket = MunicipioTicket(
            pregunta=_compact(question or "Reclamo iniciado desde demo"),
            asunto=f"Demo reclamo - {category}",
            categoria=category,
            estado="nuevo" if loc or media else "pendiente_datos",
            canal_ingreso=DEMO_MUNICIPIO_CHANNEL,
            anon_id=anon_id,
            municipio_id=getattr(owner_user, "id", None),
            user_id=getattr(owner_user, "id", None),
            tenant_id=getattr(tenant, "id", None),
        )
        db.session.add(ticket)
        db.session.flush()
        created = True

    if question:
        ticket.pregunta = _compact(question or ticket.pregunta)
    ticket.categoria = category if category != "Evidencia ciudadana" or not ticket.categoria else ticket.categoria
    if loc:
        ticket.direccion = loc.get("address") or ticket.direccion
        ticket.latitud = loc.get("lat", ticket.latitud)
        ticket.longitud = loc.get("lng", ticket.longitud)
        if ticket.estado == "pendiente_datos":
            ticket.estado = "nuevo"
    if media and media.get("kind") == "image" and media.get("url"):
        ticket.foto_url_directa = str(media["url"])[:255]

    details = _details(ticket)
    details.setdefault("demo_runtime", True)
    details.setdefault("source", DEMO_MUNICIPIO_SOURCE)
    details["chat_session_id"] = chat_session_id
    details["demo_session_payload"] = demo_session_payload or {}
    details["priority"] = priority
    details.setdefault("media", [])
    if media:
        details["media"].append(media)
    if loc:
        details["location"] = loc
    details.setdefault("events", [])
    details["events"].append(
        {
            "type": "ticket_created" if created else "ticket_updated",
            "question": _compact(question, max_len=160),
            "media_kind": media.get("kind") if media else None,
            "has_location": bool(loc),
        }
    )
    ticket.detalles = json.dumps(details, ensure_ascii=False)

    _append_comment(ticket, text=question, anon_id=anon_id, media=media, location=loc)
    db.session.flush()

    ticket_ids = runtime.setdefault("ticket_ids", [])
    if ticket.id not in ticket_ids:
        ticket_ids.append(ticket.id)
    runtime["last_ticket_id"] = ticket.id
    runtime.setdefault("events", []).append({"type": "ticket_created" if created else "ticket_updated", "ticket_id": ticket.id})
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")
    return ticket, created, details, media


def _ticket_payload(ticket: MunicipioTicket, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "nro_ticket": ticket.nro_ticket,
        "consulta_pin": ticket.consulta_pin,
        "status": ticket.estado,
        "category": ticket.categoria,
        "address": ticket.direccion,
        "lat": ticket.latitud,
        "lng": ticket.longitud,
        "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}",
        "public_status_hint": {
            "ticket": ticket.nro_ticket,
            "pin": ticket.consulta_pin,
        },
        "media": details.get("media") or [],
    }


def _response_for_info(question: str) -> dict[str, Any]:
    message = (
        "Para licencia de conducir puedo guiar el tramite sin sacarte de la conversacion: "
        "tipo de licencia, documentacion, turno disponible y seguimiento. "
        "Si queres, decime si es primera vez o renovacion y lo dejamos como caso trazable."
    )
    return {
        "message_body": message,
        "respuesta": message,
        "fuente": DEMO_MUNICIPIO_SOURCE,
        "accion_backend": "demo_info_tramite",
        "actions": [
            {
                "label": "Guia de tramite",
                "status": "ready",
                "detail": "Orienta al vecino y puede convertir la consulta en caso si faltan datos.",
                "creates": "information_case",
                "fields": [
                    {"label": "Tramite", "value": "Licencia de conducir"},
                    {"label": "Siguiente dato", "value": "primera vez o renovacion"},
                ],
            }
        ],
        "botones": [
            {"texto": "Primera vez", "action_id": "licencia_primera_vez"},
            {"texto": "Renovacion", "action_id": "licencia_renovacion"},
            {"texto": "Hablar con una persona", "action_id": "human_handoff"},
        ],
    }


def _response_for_status(chat_db_context: ChatSessionContext | None) -> dict[str, Any]:
    runtime = _context_runtime(chat_db_context)
    ticket = _last_ticket(runtime)
    if not ticket:
        message = "Todavia no tengo un ticket creado en esta demo. Enviame el reclamo, foto o ubicacion y lo creo para seguimiento."
        return {"message_body": message, "respuesta": message, "fuente": DEMO_MUNICIPIO_SOURCE, "actions": []}
    message = f"El reclamo demo #{ticket.nro_ticket} esta en estado {ticket.estado}. PIN de consulta: {ticket.consulta_pin}."
    details = _details(ticket)
    return {
        "message_body": message,
        "respuesta": message,
        "fuente": DEMO_MUNICIPIO_SOURCE,
        "accion_backend": "demo_consultar_estado",
        "ticket": _ticket_payload(ticket, details),
        "actions": [
            {
                "label": "Estado consultado",
                "status": ticket.estado,
                "detail": "Devuelve codigo, PIN y proximo paso del reclamo.",
                "creates": "status_event",
                "fields": [
                    {"label": "Ticket", "value": ticket.nro_ticket},
                    {"label": "PIN", "value": ticket.consulta_pin},
                ],
            }
        ],
    }


def _response_for_handoff() -> dict[str, Any]:
    message = (
        "Puedo dejar este caso listo para que un operador lo tome por WhatsApp, llamada o videollamada. "
        "Para la llamada realtime el backend debe usar la integracion server-side, sin exponer claves en frontend."
    )
    return {
        "message_body": message,
        "respuesta": message,
        "fuente": DEMO_MUNICIPIO_SOURCE,
        "accion_backend": "demo_handoff_humano",
        "actions": [
            {
                "label": "Derivacion humana preparada",
                "status": "ready",
                "detail": "El panel conserva contexto, adjuntos y canal pedido.",
                "creates": "handoff_request",
                "fields": [{"label": "Canales", "value": "WhatsApp, llamada o videollamada"}],
            }
        ],
        "botones": [
            {"texto": "Dejar telefono", "action_id": "open_demo_form"},
            {"texto": "Seguir por chat", "action_id": "continue_chat"},
        ],
    }


def _response_for_survey() -> dict[str, Any]:
    message = (
        "En una demo de municipio puedo abrir una encuesta o votacion y guardar respuestas trazables. "
        "Decime el tema y las opciones para publicarla en modo prueba."
    )
    return {
        "message_body": message,
        "respuesta": message,
        "fuente": DEMO_MUNICIPIO_SOURCE,
        "accion_backend": "demo_encuesta_votacion",
        "actions": [
            {
                "label": "Encuesta preparada",
                "status": "needs_topic",
                "detail": "Falta tema y opciones para crear una votacion real.",
                "creates": "survey_draft",
                "fields": [{"label": "Dato requerido", "value": "tema y opciones"}],
            }
        ],
    }


def _tool_summary(chat_db_context: ChatSessionContext | None) -> dict[str, Any]:
    if not chat_db_context or not isinstance(chat_db_context.context_data, dict):
        return {}
    data = chat_db_context.context_data
    summary = data.get("rubro_tool_summary")
    if isinstance(summary, dict):
        return summary
    metadata = data.get("demo_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("tool_summary"), dict):
        return metadata["tool_summary"]
    rubro_tools = data.get("rubro_tools")
    if isinstance(rubro_tools, dict) and isinstance(rubro_tools.get("llm_context"), dict):
        return rubro_tools["llm_context"]
    return {}


def _response_for_tool_lookup(question: str, chat_db_context: ChatSessionContext | None) -> dict[str, Any] | None:
    summary = _tool_summary(chat_db_context)
    if not summary:
        return None

    text = _fold(question)
    wants_location = any(token in text for token in ("ubicacion", "direccion", "mapa", "maps"))
    wants_contact = any(token in text for token in ("telefono", "whatsapp", "contacto", "web"))
    wants_hours = any(token in text for token in ("horario", "horarios"))
    wants_prices = any(token in text for token in ("precio", "precios", "lista"))
    wants_catalog = any(token in text for token in ("catalogo", "catalogos", "tramite", "tramites"))

    cards: list[dict[str, Any]] = []
    lines: list[str] = []

    locations = summary.get("locations") if isinstance(summary.get("locations"), list) else []
    if wants_location and locations:
        first = locations[0]
        label = first.get("label") or "Ubicacion"
        address = first.get("address") or "Direccion publicada"
        maps_url = first.get("maps_url")
        lines.append(f"{label}: {address}.")
        cards.append(
            {
                "label": "Ubicacion",
                "status": "ready",
                "detail": address,
                "creates": "tool_result",
                "fields": [{"label": "Direccion", "value": address}],
                "metadata": {"maps_url": maps_url, "kind": "location"},
            }
        )

    contact = summary.get("contact") if isinstance(summary.get("contact"), dict) else {}
    if wants_contact and contact:
        fields = []
        for key, label in (("phone", "Telefono"), ("whatsapp", "WhatsApp"), ("email", "Email"), ("website", "Web")):
            if contact.get(key):
                fields.append({"label": label, "value": contact.get(key)})
        if fields:
            lines.append("Contacto: " + " | ".join(f"{item['label']}: {item['value']}" for item in fields) + ".")
            cards.append(
                {
                    "label": "Contacto",
                    "status": "ready",
                    "detail": "Canales publicados para este tenant.",
                    "creates": "tool_result",
                    "fields": fields,
                    "metadata": {"kind": "contact"},
                }
            )

    hours = summary.get("hours")
    if wants_hours and hours:
        if isinstance(hours, dict):
            hours_text = " | ".join(f"{key}: {value}" for key, value in hours.items())
        else:
            hours_text = str(hours)
        lines.append(f"Horarios: {hours_text}.")
        cards.append(
            {
                "label": "Horarios",
                "status": "ready",
                "detail": hours_text,
                "creates": "tool_result",
                "fields": [{"label": "Horario", "value": hours_text}],
                "metadata": {"kind": "hours"},
            }
        )

    price_resources = summary.get("price_resources") if isinstance(summary.get("price_resources"), list) else []
    if wants_prices and price_resources:
        resource = price_resources[0]
        lines.append(f"Lista de precios: {resource.get('label')}.")
        cards.append(
            {
                "label": "Lista de precios",
                "status": "ready",
                "detail": resource.get("description") or resource.get("label"),
                "creates": "tool_result",
                "fields": [{"label": "Recurso", "value": resource.get("label")}],
                "metadata": {"kind": "price_list", "url": resource.get("url")},
            }
        )

    resources = summary.get("resources") if isinstance(summary.get("resources"), list) else []
    if wants_catalog and resources:
        resource = resources[0]
        lines.append(f"Catalogo/recurso: {resource.get('label')}.")
        cards.append(
            {
                "label": "Catalogo",
                "status": "ready",
                "detail": resource.get("description") or resource.get("label"),
                "creates": "tool_result",
                "fields": [{"label": "Recurso", "value": resource.get("label")}],
                "metadata": {"kind": "catalog", "url": resource.get("url")},
            }
        )

    if not cards:
        return None

    message = " ".join(lines) or "Tengo herramientas publicadas para este rubro."
    return {
        "message_body": message,
        "respuesta": message,
        "fuente": DEMO_MUNICIPIO_SOURCE,
        "accion_backend": "demo_tool_lookup",
        "rubro_tools_result": {
            "contract_version": "demo.rubro_tools_result.v1",
            "matched": [card.get("metadata", {}).get("kind") for card in cards],
            "source": "chat_session_context.rubro_tool_summary",
        },
        "actions": cards,
    }


def _response_for_ticket(ticket: MunicipioTicket, created: bool, details: dict[str, Any], media: dict[str, Any] | None) -> dict[str, Any]:
    has_location = bool(ticket.latitud is not None and ticket.longitud is not None)
    media_labels = [item.get("kind") for item in details.get("media") or [] if isinstance(item, dict) and item.get("kind")]
    next_hint = []
    if not has_location:
        next_hint.append("ubicacion")
    if not media_labels:
        next_hint.append("foto, audio o archivo")
    if next_hint:
        extra = " Ahora puedo sumar " + " y ".join(next_hint) + " para completar evidencia."
    else:
        extra = " Ya quedo con ubicacion y evidencia para que el equipo lo opere."

    verb = "cree" if created else "actualice"
    message = (
        f"Listo: {verb} el reclamo demo de {ticket.categoria}. "
        f"Ticket #{ticket.nro_ticket}, PIN {ticket.consulta_pin}.{extra}"
    )
    fields = [
        {"label": "Categoria", "value": ticket.categoria or "Reclamo ciudadano"},
        {"label": "Estado", "value": ticket.estado},
    ]
    if has_location:
        fields.append({"label": "Ubicacion", "value": ticket.direccion or f"{ticket.latitud}, {ticket.longitud}"})
    if media_labels:
        fields.append({"label": "Evidencia", "value": ", ".join(sorted(set(media_labels)))})
    priority = details.get("priority")
    if priority:
        fields.append({"label": "Prioridad sugerida", "value": priority})

    return {
        "message_body": message,
        "respuesta": message,
        "fuente": DEMO_MUNICIPIO_SOURCE,
        "accion_backend": "demo_crear_reclamo",
        "ticket_id": ticket.id,
        "ticket": _ticket_payload(ticket, details),
        "lead": {"created": True, "ticket_id": ticket.id, "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}"},
        "result": {"kind": "ticket", "traceable": True, "target": "inbox", "id": ticket.id},
        "actions": [
            {
                "label": "Reclamo creado" if created else "Reclamo actualizado",
                "status": "ready" if has_location or media_labels else "needs_evidence",
                "detail": "Ticket con categoria, evidencia, ubicacion opcional y seguimiento por codigo.",
                "creates": "ticket",
                "fields": fields,
                "metadata": {"traceable_target": "ticket", "ticket_id": ticket.id},
            }
        ],
        "media_understanding": {
            "contract_version": "demo.media_understanding.v1",
            "supports": ["text", "image", "audio", "video", "file", "location", "emoji", "voice_call", "video_call"],
            "received": sorted(set(media_labels + (["location"] if has_location else []) + ["text"])),
        },
        "botones": [
            {"texto": "Enviar ubicacion", "action_id": "share_location"},
            {"texto": "Adjuntar foto", "action_id": "attach_image"},
            {"texto": "Consultar estado", "action_id": "consultar_estado"},
            {"texto": "Hablar con una persona", "action_id": "human_handoff"},
        ],
    }


def handle_demo_municipio_message(
    *,
    question: str,
    action_id: str | None = None,
    attachment_info: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
    chat_db_context: ChatSessionContext | None = None,
    owner_user: User | None = None,
    anon_id: str | None = None,
    chat_session_id: str | None = None,
    demo_session_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Handle public municipality demo turns with real, traceable backend state."""

    intent = _classify_demo_intent(question, action_id, attachment_info, location)
    if not intent:
        return None

    kind = intent.get("kind")
    if kind == "info":
        return _response_for_info(question)
    if kind == "status_lookup":
        return _response_for_status(chat_db_context)
    if kind == "tool_lookup":
        return _response_for_tool_lookup(question, chat_db_context)
    if kind == "human_handoff":
        return _response_for_handoff()
    if kind == "survey":
        return _response_for_survey()
    if kind != "claim":
        return None

    ticket, created, details, media = _upsert_ticket(
        question=question,
        intent=intent,
        owner_user=owner_user,
        anon_id=anon_id,
        chat_session_id=chat_session_id,
        chat_db_context=chat_db_context,
        attachment_info=attachment_info,
        location=location,
        demo_session_payload=demo_session_payload,
    )
    return _response_for_ticket(ticket, created, details, media)

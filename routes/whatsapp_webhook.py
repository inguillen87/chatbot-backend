from flask import Blueprint, request, jsonify, abort, current_app, g, has_app_context  # Basic Flask components
from twilio.request_validator import RequestValidator  # For validating Twilio requests
from twilio.rest import Client  # For sending messages via Twilio
import logging
import os  # For accessing environment variables
import requests
import io
import json
import threading
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit
from werkzeug.datastructures import FileStorage
from models import (
    WhatsappNumero,
    User,
    ChatSessionContext,
    ArchivoAdjunto,
    MunicipioTicket,
    PymeTicket,
    TicketComentario,
    TenantProfile,
    Notification,
)  # Import necessary models
from models_memory import Contact
from extensions import db  # Import db instance for database operations
import uuid
from sqlalchemy import or_, func
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import joinedload  # To potentially eager load User.rubro
from utils.db_utils import ensure_chat_session_context_schema, safe_flag_modified
from services.gcs_service import upload_to_gcs
from services.attachment_service import create_attachment_with_thumbnail
from services.llm_utils import extract_multiple_contact_details_llm
from services.contact_intake import missing_contact_fields, resolve_contact_snapshot
from services.logic import responder_chatboc
from services.user_service import update_user_profile
from services.media_classifier import clasificar_adjunto_whatsapp
from utils.maps_utils import extraer_coordenadas_de_url_google_maps
from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import CONTEXTO_MUNICIPIO
from services.config_loader import cargar_configuracion_pyme
from services.response_formatter import render_audio_text
from services.tts_orchestrator import generar_audio
from utils.response_utils import normalize_response_payload
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback
from services.contact_service import resolve_contact, sanitize_profile_name
from services.ticket_service import servicio_tickets
from services.crm_intelligence import record_contact_interaction, resolve_or_create_contact
from services.demo_surveys import build_demo_survey_chat_menu
from services.education_contracts import (
    build_education_case_ack_payload,
    build_education_pending_case,
    build_education_profile,
    build_education_whatsapp_menu_payload,
    build_education_whatsapp_playbook,
    education_intents,
    education_menu_item_for_intent,
    education_prompt_for_intent,
    is_education_tenant,
)
from services.education_case_service import (
    create_school_case_alias_for_ticket,
    school_case_alias_payload,
)

# Define the blueprint for WhatsApp webhooks
webhook_bp = Blueprint('whatsapp_webhook', __name__)
logger = logging.getLogger(__name__)

# Twilio imposes a 1600 character limit on message bodies. When the bot
# generates very long responses (e.g. large contact lists) the request can
# fail with `HTTP 400: The concatenated message body exceeds the 1600 character
# limit`.  To prevent this we define a helper that splits long texts into
# chunks that comply with Twilio's limits and send them sequentially.

MAX_TWILIO_BODY_LENGTH = 1600
LIVE_CHAT_STATES = {"esperando_agente_en_vivo", "en_proceso", "en_vivo"}
ALLOWED_MEDIA_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
}
SENSITIVE_MENU_ACTIONS = {
    "enviar_sugerencia",
    "iniciar_sugerencia",
    "crear_sugerencia",
}
SENSITIVE_ACTION_CONFIRM_ACCEPT = {"1", "si", "sí", "confirmar", "ok", "dale"}
SENSITIVE_ACTION_CONFIRM_REJECT = {"2", "no", "cancelar", "menu", "menú"}
GENERIC_CONTACT_NAMES = {"vecino", "vecina", "vecino/a", "usuario", "anonimo", "anonimo/a"}
CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER = "+18564858589"
CHATBOC_DEMO_TENANT_SLUG = "chatboc-demo"
CHATBOC_DEMO_OWNER_EMAIL = "marcelo@chatboc.ar"


def _configured_chatboc_demo_numbers() -> Set[str]:
    raw_numbers = (
        current_app.config.get("CHATBOC_DEMO_WHATSAPP_NUMBERS")
        or os.getenv("CHATBOC_DEMO_WHATSAPP_NUMBERS")
        or CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER
    )
    if isinstance(raw_numbers, str):
        candidates = re.split(r"[,;\s]+", raw_numbers)
    else:
        candidates = list(raw_numbers or [])
    candidates.append(CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER)
    normalized = {_normalize_whatsapp_address(candidate) for candidate in candidates}
    return {candidate for candidate in normalized if candidate}


def _chatboc_demo_max_messages() -> int:
    raw_value = (
        current_app.config.get("CHATBOC_DEMO_MAX_MESSAGES")
        or os.getenv("CHATBOC_DEMO_MAX_MESSAGES")
        or 10
    )
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 10


def _is_chatboc_demo_destination(to_number: Optional[str]) -> bool:
    normalized = _normalize_whatsapp_address(to_number)
    return bool(normalized and normalized in _configured_chatboc_demo_numbers())


def _chatboc_demo_owner_email() -> str:
    return (
        current_app.config.get("CHATBOC_SUPERADMIN_EMAIL")
        or current_app.config.get("SUPERADMIN_LEAD_EMAIL")
        or os.getenv("CHATBOC_SUPERADMIN_EMAIL")
        or os.getenv("SUPERADMIN_LEAD_EMAIL")
        or CHATBOC_DEMO_OWNER_EMAIL
    ).strip().lower()


def _find_or_create_chatboc_demo_owner() -> User:
    email = _chatboc_demo_owner_email()
    owner = User.query.filter_by(email=email).first()
    if not owner:
        owner = (
            User.query.filter(User.rol.in_(["super_admin", "platform_admin"]))
            .order_by(User.id.asc())
            .first()
        )
    if not owner:
        owner = User(
            name="Chatboc Superadmin",
            email=email,
            rol="super_admin",
            tipo_chat="pyme",
            nombre_empresa="Chatboc.ar",
            plan="enterprise",
            tenant_slug=CHATBOC_DEMO_TENANT_SLUG,
            acepto_terminos=True,
        )
        owner.set_password(str(uuid.uuid4()))
        db.session.add(owner)
        db.session.flush()

    changed = False
    if owner.rol not in {"super_admin", "platform_admin"}:
        owner.rol = "super_admin"
        changed = True
    if not owner.tipo_chat:
        owner.tipo_chat = "pyme"
        changed = True
    if not owner.nombre_empresa:
        owner.nombre_empresa = "Chatboc.ar"
        changed = True
    if owner.tenant_slug != CHATBOC_DEMO_TENANT_SLUG:
        owner.tenant_slug = CHATBOC_DEMO_TENANT_SLUG
        changed = True
    if changed:
        db.session.add(owner)
        db.session.flush()
    return owner


def _find_or_create_chatboc_demo_tenant(owner: User) -> TenantProfile:
    tenant = TenantProfile.query.filter_by(slug=CHATBOC_DEMO_TENANT_SLUG).first()
    config = {
        "assistant_name": "Chatboc",
        "nombre": "Chatboc.ar Demo Hub",
        "whatsapp_demo_hub": True,
        "whatsapp_numbers": sorted(_configured_chatboc_demo_numbers()),
        "lead_capture": {
            "owner_email": getattr(owner, "email", None),
            "source": "whatsapp_demo_hub",
        },
    }
    if not tenant:
        tenant = TenantProfile(
            slug=CHATBOC_DEMO_TENANT_SLUG,
            nombre="Chatboc.ar Demo Hub",
            tipo="pyme",
            pyme_id=owner.id,
            vertical="platform",
            plan="enterprise",
            is_active=True,
            configuracion=config,
            dispatch_phone=current_app.config.get("SUPERADMIN_LEAD_PHONE"),
            dispatch_email=current_app.config.get("SUPERADMIN_LEAD_EMAIL"),
            send_dispatch_whatsapp=True,
        )
        db.session.add(tenant)
        db.session.flush()
    else:
        changed = False
        if not tenant.pyme_id and not tenant.municipio_id:
            tenant.pyme_id = owner.id
            changed = True
        if tenant.tipo != "pyme":
            tenant.tipo = "pyme"
            changed = True
        merged_config = dict(tenant.configuracion or {})
        merged_config.update({k: v for k, v in config.items() if k not in merged_config})
        merged_config["whatsapp_demo_hub"] = True
        merged_config["whatsapp_numbers"] = sorted(_configured_chatboc_demo_numbers())
        if merged_config != (tenant.configuracion or {}):
            tenant.configuracion = merged_config
            changed = True
        if not tenant.is_active:
            tenant.is_active = True
            changed = True
        if changed:
            db.session.add(tenant)
            db.session.flush()

    if owner.tenant_id != tenant.id:
        owner.tenant_id = tenant.id
        db.session.add(owner)
        db.session.flush()
    return tenant


def _ensure_chatboc_demo_whatsapp_mapping(to_number: Optional[str]) -> Optional[WhatsappNumero]:
    normalized = _normalize_whatsapp_address(to_number)
    if not normalized or not _is_chatboc_demo_destination(normalized):
        return None

    owner = _find_or_create_chatboc_demo_owner()
    _find_or_create_chatboc_demo_tenant(owner)

    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=normalized).first()
    if not mapping:
        mapping = WhatsappNumero(
            numero_whatsapp=normalized,
            user_id=owner.id,
            is_active=True,
        )
        db.session.add(mapping)
    else:
        mapping.user_id = owner.id
        mapping.is_active = True
        db.session.add(mapping)

    db.session.commit()
    return mapping


def _append_tag(existing: Optional[str], *tags: str) -> str:
    current = [part.strip() for part in str(existing or "").split(",") if part.strip()]
    seen = {part.lower() for part in current}
    for tag in tags:
        clean = str(tag or "").strip()
        if clean and clean.lower() not in seen:
            current.append(clean)
            seen.add(clean.lower())
    return ",".join(current)


def _next_chatboc_demo_ticket_number() -> int:
    current = db.session.query(func.max(PymeTicket.nro_ticket)).scalar()
    return int(current or 0) + 1


def _upsert_chatboc_demo_contact(
    *,
    owner_user: User,
    tenant: Optional[TenantProfile],
    phone: str,
    profile_name: Optional[str],
) -> Optional[User]:
    normalized_phone = _normalize_whatsapp_address(phone) or phone
    if not owner_user or not normalized_phone:
        return None

    contact = User.query.filter_by(telefono=normalized_phone, empresa_id=owner_user.id).first()
    if not contact:
        digits = re.sub(r"\D", "", normalized_phone) or uuid.uuid4().hex[:12]
        email = f"wa_{owner_user.id}_{digits}@whatsapp.chatboc.com"
        contact = User.query.filter_by(email=email).first()
    if not contact:
        contact = User(
            name=_clean_contact_name(profile_name) or "Prospecto WhatsApp",
            email=email,
            telefono=normalized_phone,
            empresa_id=owner_user.id,
            tenant_id=getattr(tenant, "id", None),
            rol="usuario",
            tipo_chat="pyme",
            plan="gratis",
            acepta_marketing=False,
            acepto_terminos=True,
            fecha_aceptacion_terminos=datetime.utcnow(),
            tags="chatboc_demo,whatsapp,prospecto",
        )
        contact.set_password(str(uuid.uuid4()))
        db.session.add(contact)
        db.session.flush()
    else:
        changed = False
        clean_name = _clean_contact_name(profile_name)
        if clean_name and (not contact.name or contact.name in {"Vecino/a", "Prospecto WhatsApp"}):
            contact.name = clean_name
            changed = True
        if not contact.telefono:
            contact.telefono = normalized_phone
            changed = True
        if not contact.empresa_id:
            contact.empresa_id = owner_user.id
            changed = True
        if tenant and not contact.tenant_id:
            contact.tenant_id = tenant.id
            changed = True
        new_tags = _append_tag(contact.tags, "chatboc_demo", "whatsapp", "prospecto")
        if new_tags != (contact.tags or ""):
            contact.tags = new_tags
            changed = True
        if changed:
            db.session.add(contact)
            db.session.flush()
    return contact


def _create_chatboc_demo_ticket(
    *,
    owner_user: User,
    tenant: Optional[TenantProfile],
    contact_user: Optional[User],
    anon_id: str,
    message_body: str,
    action_id: Optional[str],
) -> PymeTicket:
    ticket = PymeTicket(
        tenant_id=getattr(tenant, "id", None),
        pregunta=(message_body or "Nuevo acercamiento desde WhatsApp demo Chatboc"),
        asunto="Lead Chatboc WhatsApp demo",
        categoria="chatboc_demo_lead",
        user_id=getattr(contact_user, "id", None) or getattr(owner_user, "id", None),
        estado="nuevo",
        anon_id=anon_id,
        nro_ticket=_next_chatboc_demo_ticket_number(),
        rubro_id=getattr(owner_user, "rubro_id", None),
        telefono=_normalize_whatsapp_address(anon_id) or anon_id,
        email=getattr(contact_user, "email", None),
        estado_cliente="nuevo",
    )
    db.session.add(ticket)
    db.session.flush()

    comment = TicketComentario(
        pyme_ticket_id=ticket.id,
        comentario=(
            f"[whatsapp_demo_hub] action={action_id or 'message'} | "
            f"{(message_body or '').strip() or 'Primer contacto'}"
        ),
        anon_id=anon_id,
        origen="whatsapp",
        estado_ticket=ticket.estado,
    )
    db.session.add(comment)
    return ticket


def _upsert_chatboc_demo_crm_contact(
    *,
    tenant: Optional[TenantProfile],
    contact_user: Optional[User],
    phone: str,
    profile_name: Optional[str],
    message_body: str,
    action_id: Optional[str],
    ticket: Optional[PymeTicket],
    input_context: Optional[Dict[str, Any]] = None,
) -> Optional[Contact]:
    if not tenant or not getattr(tenant, "id", None):
        return None
    normalized_phone = _normalize_whatsapp_address(phone) or phone
    contact = None
    if normalized_phone:
        contact = Contact.query.filter_by(tenant_id=tenant.id, phone=normalized_phone).first()
    if contact is None and contact_user and getattr(contact_user, "email", None):
        contact = Contact.query.filter_by(tenant_id=tenant.id, email=contact_user.email).first()

    clean_name = _clean_contact_name(profile_name) or getattr(contact_user, "name", None) or "Prospecto WhatsApp"
    inquiry_type = _classify_chatboc_demo_inquiry(message_body, action_id)
    input_context = input_context or _build_chatboc_demo_input_context(
        message_body=message_body,
        uploaded_file_info=None,
        location_info=None,
    )
    content_type = input_context.get("content_type") or "text"
    media_context = input_context.get("media") if isinstance(input_context.get("media"), dict) else None
    location_context = input_context.get("location") if isinstance(input_context.get("location"), dict) else None
    tags = ["chatboc_demo", "whatsapp", "prospecto"]
    if inquiry_type:
        tags.append(inquiry_type)
    if content_type and content_type not in {"text", "event"}:
        tags.append(f"entrada_{content_type}")
    preferences = {
        "preferred_channel": "whatsapp",
        "source": "chatboc_demo_whatsapp_hub",
        "legacy_user_id": getattr(contact_user, "id", None),
        "marketing_consent_status": "unknown",
        "service_window_source": "inbound_whatsapp",
        "last_demo_input_type": content_type,
        "last_demo_input_summary": input_context.get("summary"),
        "last_demo_media": media_context,
        "last_demo_location": location_context,
        "whatsapp_service_window_until": (
            datetime.utcnow() + timedelta(hours=24)
        ).isoformat(),
    }

    if contact is None:
        contact = Contact(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            name=clean_name,
            phone=normalized_phone,
            email=getattr(contact_user, "email", None),
            whatsapp_id=normalized_phone,
            type="lead",
            tags=tags,
            preferences=preferences,
            last_interaction_at=datetime.utcnow(),
        )
        db.session.add(contact)
        db.session.flush()
    else:
        current_tags = contact.tags if isinstance(contact.tags, list) else []
        merged_tags = list(dict.fromkeys([*current_tags, *tags]))
        current_prefs = contact.preferences if isinstance(contact.preferences, dict) else {}
        current_prefs.update({k: v for k, v in preferences.items() if v is not None})
        contact.name = contact.name or clean_name
        contact.phone = contact.phone or normalized_phone
        contact.email = contact.email or getattr(contact_user, "email", None)
        contact.whatsapp_id = contact.whatsapp_id or normalized_phone
        contact.type = contact.type if contact.type not in {None, "unknown"} else "lead"
        contact.tags = merged_tags
        contact.preferences = current_prefs
        contact.last_interaction_at = datetime.utcnow()
        db.session.add(contact)

    record_contact_interaction(
        tenant=tenant,
        contact=contact,
        message_body=message_body or "",
        channel="whatsapp",
        direction="inbound",
        source="chatboc_demo_whatsapp_hub",
        metadata={
            "event_type": "chatboc_demo_inbound",
            "action_id": action_id,
            "inquiry_type": inquiry_type,
            "content_type": content_type,
            "input_summary": input_context.get("summary"),
            "media": media_context,
            "location": location_context,
            "ticket_id": getattr(ticket, "id", None),
            "ticket_number": getattr(ticket, "nro_ticket", None),
        },
        content_type=content_type,
        media_url=(media_context or {}).get("url"),
        emit=True,
    )
    return contact


def _classify_chatboc_demo_inquiry(message_body: str, action_id: Optional[str]) -> str:
    action = _normalize_chatboc_demo_text(action_id)
    text = _normalize_chatboc_demo_text(message_body)
    if any(word in text for word in ("familia", "inasistencia", "admisiones", "cuota", "comunicado")):
        return "interes_educacion"
    if any(
        word in text
        for word in ("producto", "envio", "promo", "promocion", "presupuesto", "comprar", "carrito")
    ):
        return "interes_empresa"
    if "gobierno" in action or any(word in text for word in ("municipio", "reclamo", "tramite", "trámite")):
        return "interes_municipio"
    if "educacion" in action or any(word in text for word in ("colegio", "escuela", "alumno", "secretaria")):
        return "interes_educacion"
    if "empresa" in action or any(word in text for word in ("empresa", "pyme", "pedido", "stock", "catalogo", "catálogo")):
        return "interes_empresa"
    if "survey" in action or any(word in text for word in ("encuesta", "votacion", "votación", "sondeo")):
        return "interes_encuestas"
    if "sales" in action or any(word in text for word in ("precio", "contratar", "ventas", "asesor", "llamen")):
        return "interes_comercial"
    return "interes_general"


def _chatboc_demo_media_kind(uploaded_file_info: Optional[Dict[str, Any]]) -> Optional[str]:
    if not uploaded_file_info:
        return None
    mime_type = str(uploaded_file_info.get("mime_type") or "").lower()
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type:
        return "file"
    return "file"


def _build_chatboc_demo_input_context(
    *,
    message_body: str,
    uploaded_file_info: Optional[Dict[str, Any]],
    location_info: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Normalize WhatsApp demo input so CRM, tickets and replies share one truth."""

    clean_body = (message_body or "").strip()
    context: Dict[str, Any] = {
        "raw_text": clean_body,
        "content_type": "text" if clean_body else "event",
        "effective_message": clean_body,
        "summary": clean_body,
        "metadata": {},
    }

    if location_info:
        lat = location_info.get("latitude")
        lng = location_info.get("longitude")
        address = location_info.get("address")
        label = location_info.get("label")
        location_label = address or label or (
            f"{lat},{lng}" if lat is not None and lng is not None else "ubicacion compartida"
        )
        summary = f"Ubicacion compartida por WhatsApp: {location_label}"
        context.update(
            {
                "content_type": "location",
                "effective_message": clean_body or summary,
                "summary": summary,
                "location": {
                    "latitude": lat,
                    "longitude": lng,
                    "address": address,
                    "label": label,
                },
            }
        )
        context["metadata"]["location"] = context["location"]

    media_kind = _chatboc_demo_media_kind(uploaded_file_info)
    if uploaded_file_info and media_kind:
        transcript = (uploaded_file_info.get("transcribed_text") or "").strip()
        media_summary_by_kind = {
            "audio": "Nota de voz recibida por WhatsApp",
            "image": "Imagen recibida por WhatsApp",
            "video": "Video recibido por WhatsApp",
            "file": "Archivo recibido por WhatsApp",
        }
        media_summary = media_summary_by_kind.get(media_kind, "Archivo recibido por WhatsApp")
        if transcript:
            media_summary = f"Nota de voz transcripta: {transcript}"
        elif clean_body:
            media_summary = f"{media_summary}: {clean_body}"

        media_context = {
            "kind": media_kind,
            "attachment_id": uploaded_file_info.get("id"),
            "url": uploaded_file_info.get("url"),
            "thumbnail_url": uploaded_file_info.get("thumbnail_url"),
            "mime_type": uploaded_file_info.get("mime_type"),
            "name": uploaded_file_info.get("name"),
            "transcribed_text": transcript or None,
        }
        context.update(
            {
                "content_type": media_kind,
                "effective_message": transcript or clean_body or media_summary,
                "summary": media_summary,
                "media": media_context,
                "media_url": uploaded_file_info.get("url"),
            }
        )
        context["metadata"]["media"] = media_context

    if context["content_type"] == "event" and not context["summary"]:
        context["summary"] = "Interaccion recibida desde WhatsApp demo Chatboc"
        context["effective_message"] = context["summary"]

    return context


def _build_chatboc_demo_limit_payload(used: int, limit: int, ticket: Optional[PymeTicket]) -> Dict[str, Any]:
    ticket_line = f"\nTu contacto quedo registrado como ticket #{ticket.nro_ticket}." if ticket else ""
    return {
        "success": True,
        "message_body": (
            "Llegaste al limite de mensajes de esta demo de WhatsApp. "
            "Guarde tu consulta para que el equipo de Chatboc la revise."
            f"{ticket_line}\n\n"
            "Podes seguir desde la demo web o dejar tus datos para una prueba guiada."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Abrir demo web", url="https://www.chatboc.ar/demo"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
        ],
        "fuente": "chatboc_demo_whatsapp_limit",
        "data": {
            "trial_usage": {
                "channel": "whatsapp",
                "limit": limit,
                "used": used,
                "remaining": max(limit - used, 0),
            }
        },
        "skip_audio_generation": True,
    }


def _increment_chatboc_demo_usage(session_context: ChatSessionContext) -> Tuple[int, int]:
    limit = _chatboc_demo_max_messages()
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    usage = session_context.context_data.get("chatboc_demo_usage")
    if not isinstance(usage, dict):
        usage = {"channel": "whatsapp", "message_count": 0}
    usage["channel"] = "whatsapp"
    usage["message_count"] = int(usage.get("message_count") or 0) + 1
    usage["limit"] = limit
    usage["remaining"] = max(limit - usage["message_count"], 0)
    usage["last_message_at"] = datetime.utcnow().isoformat()
    session_context.context_data["chatboc_demo_usage"] = usage
    safe_flag_modified(session_context, "context_data")
    db.session.add(session_context)
    return usage["message_count"], limit


def _record_chatboc_demo_engagement(
    *,
    owner_user: User,
    tenant: Optional[TenantProfile],
    session_context: ChatSessionContext,
    from_number: str,
    profile_name: Optional[str],
    message_body: str,
    action_id: Optional[str],
    message_sid: Optional[str],
    input_context: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[User], Optional[PymeTicket]]:
    contact_user = _upsert_chatboc_demo_contact(
        owner_user=owner_user,
        tenant=tenant,
        phone=from_number,
        profile_name=profile_name,
    )

    context_data = session_context.context_data if isinstance(session_context.context_data, dict) else {}
    input_context = input_context or _build_chatboc_demo_input_context(
        message_body=message_body,
        uploaded_file_info=None,
        location_info=None,
    )
    effective_message = input_context.get("effective_message") or message_body
    input_summary = input_context.get("summary") or effective_message
    content_type = input_context.get("content_type") or "text"
    ticket_id = context_data.get("chatboc_demo_lead_ticket_id")
    ticket = db.session.get(PymeTicket, ticket_id) if ticket_id else None
    should_create_ticket = ticket is None
    if action_id in {"chatboc_sales_lead", "capturar_lead_comercial"}:
        should_create_ticket = True if ticket is None else False

    if should_create_ticket:
        ticket = _create_chatboc_demo_ticket(
            owner_user=owner_user,
            tenant=tenant,
            contact_user=contact_user,
            anon_id=from_number,
            message_body=effective_message,
            action_id=action_id,
        )
        context_data["chatboc_demo_lead_ticket_id"] = ticket.id
        context_data["chatboc_demo_lead_nro"] = ticket.nro_ticket
    elif ticket and effective_message:
        media_suffix = ""
        media = input_context.get("media") if isinstance(input_context.get("media"), dict) else {}
        location = input_context.get("location") if isinstance(input_context.get("location"), dict) else {}
        if media:
            media_suffix = f" | adjunto={media.get('attachment_id') or '-'} | tipo={media.get('mime_type') or content_type}"
        elif location:
            media_suffix = (
                f" | lat={location.get('latitude') or '-'} | lng={location.get('longitude') or '-'}"
            )
        db.session.add(
            TicketComentario(
                pyme_ticket_id=ticket.id,
                comentario=f"[whatsapp_demo_hub] {content_type} | {(input_summary or effective_message or '').strip()}{media_suffix}",
                anon_id=from_number,
                origen="whatsapp",
                estado_ticket=ticket.estado,
            )
        )

    _upsert_chatboc_demo_crm_contact(
        tenant=tenant,
        contact_user=contact_user,
        phone=from_number,
        profile_name=profile_name,
        message_body=effective_message,
        action_id=action_id,
        ticket=ticket,
        input_context=input_context,
    )

    context_data["chatboc_demo_hub"] = True
    context_data["chatboc_demo_last_input"] = {
        "content_type": content_type,
        "summary": input_summary,
        "media": input_context.get("media"),
        "location": input_context.get("location"),
        "message_sid": message_sid,
    }
    if action_id:
        context_data["chatboc_demo_last_action"] = action_id
    session_context.context_data = context_data
    safe_flag_modified(session_context, "context_data")
    db.session.add(session_context)

    if tenant:
        notification_key = message_sid or f"{from_number}:{uuid.uuid4().hex}"
        notification = Notification(
            tenant_id=tenant.id,
            user_id=getattr(owner_user, "id", None),
            channel="in_app",
            recipient=getattr(owner_user, "email", None) or "superadmin",
            subject="Nuevo contacto WhatsApp demo Chatboc",
            body=(
                f"{getattr(contact_user, 'name', None) or 'Prospecto'} escribio desde "
                f"{from_number}. Ticket interno: {getattr(ticket, 'nro_ticket', None) or '-'}."
            ),
            status="queued",
            idempotency_key=f"chatboc_demo_whatsapp:{notification_key}",
            metadata_json={
                "source": "whatsapp_demo_hub",
                "phone": from_number,
                "profile_name": profile_name,
                "action_id": action_id,
                "content_type": content_type,
                "input_summary": input_summary,
                "media": input_context.get("media"),
                "location": input_context.get("location"),
                "ticket_id": getattr(ticket, "id", None),
                "ticket_number": getattr(ticket, "nro_ticket", None),
            },
        )
        db.session.add(notification)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("[CHATBOC_DEMO_HUB] Failed to record engagement")
        return contact_user, ticket
    return contact_user, ticket


def _chatboc_demo_option(text: str, action_id: Optional[str] = None, url: Optional[str] = None) -> Dict[str, Any]:
    option: Dict[str, Any] = {"texto": text}
    if action_id:
        option["action_id"] = action_id
    if url:
        option["url"] = url
        option["type"] = "url"
    return option


def _normalize_chatboc_demo_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text)


def _chatboc_demo_has_any(text: str, *keywords: str) -> bool:
    normalized = _normalize_chatboc_demo_text(text)
    return any(keyword in normalized for keyword in keywords)


def _chatboc_demo_active_sector(session_context: ChatSessionContext) -> str:
    context_data = session_context.context_data if isinstance(session_context.context_data, dict) else {}
    candidates: list[str] = [
        str(context_data.get("chatboc_demo_active_sector") or ""),
        str(context_data.get("chatboc_demo_survey_sector") or ""),
        str(context_data.get("chatboc_demo_last_action") or ""),
    ]
    for option in context_data.get("last_options_sent") or []:
        if isinstance(option, dict):
            candidates.append(str(option.get("action_id") or option.get("id") or ""))
            candidates.append(str(option.get("texto") or ""))
    haystack = _normalize_chatboc_demo_text(" ".join(candidates))
    if "educacion" in haystack or "coleg" in haystack or "escuela" in haystack:
        return "educacion"
    if "gobierno" in haystack or "municip" in haystack:
        return "gobierno"
    if "empresas" in haystack or "empresa" in haystack or "pyme" in haystack or "pedido" in haystack:
        return "empresas"
    return "empresas"


def _remember_chatboc_demo_sector(session_context: ChatSessionContext, sector: str) -> None:
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    session_context.context_data["chatboc_demo_active_sector"] = sector
    safe_flag_modified(session_context, "context_data")
    db.session.add(session_context)


def _chatboc_demo_survey_url(slug: str, *, source: str = "whatsapp_demo") -> str:
    clean_slug = str(slug or "").strip().strip("/")
    if not clean_slug:
        return "https://www.chatboc.ar/encuestas"
    return f"https://www.chatboc.ar/e/{clean_slug}?source={source}&demo_participation=1"


def _chatboc_demo_survey_share_url(slug: str) -> str:
    from urllib.parse import quote_plus

    return f"https://wa.me/?text={quote_plus(_chatboc_demo_survey_url(slug))}"


def _build_chatboc_demo_root_payload(contact_name: Optional[str], ticket: Optional[PymeTicket]) -> Dict[str, Any]:
    lead_line = f"\nTicket interno CRM: #{ticket.nro_ticket}" if ticket else ""
    greeting = f"Hola {contact_name}, soy Chatboc.ar." if contact_name else "Hola, soy Chatboc.ar."
    body = (
        f"🤖 {greeting}\n"
        "Este es el hub demo oficial. Probá experiencias reales por WhatsApp: reclamos, colegios, "
        "pedidos, encuestas y CRM de leads.\n"
        "Elegí una demo y respondé como si fueras vecino, familia o cliente. Lo que escribas queda "
        "registrado como oportunidad comercial en Chatboc."
        f"{lead_line}"
    )
    return {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("🏛️ Municipio inteligente", "chatboc_demo:gobierno"),
            _chatboc_demo_option("🎓 Colegio y familias", "chatboc_demo:educacion"),
            _chatboc_demo_option("🛍️ Empresa y pedidos", "chatboc_demo:empresas"),
            _chatboc_demo_option("🗳️ Encuestas en vivo", "chatboc_surveys"),
            _chatboc_demo_option("💬 Hablar con ventas", "chatboc_sales_lead"),
        ],
        "fuente": "chatboc_demo_whatsapp_hub",
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_sector_payload(sector: str) -> Dict[str, Any]:
    labels = {
        "gobierno": (
            "🏛️ Municipio inteligente",
            "Abrí reclamos con ubicación y evidencia, mirá mapa de calor, votaciones y participación ciudadana.",
            "https://www.chatboc.ar/demo?sector=gobierno",
        ),
        "educacion": (
            "🎓 Colegio conectado",
            "Probá consultas de familias, inasistencias, secretaría, comunicados y seguimiento de casos.",
            "https://www.chatboc.ar/demo?sector=educacion",
        ),
        "empresas": (
            "🛍️ Empresa y pedidos",
            "Pedí productos, consultá promociones, cargá pedidos, generá leads y mirá catálogo pro.",
            "https://www.chatboc.ar/demo?sector=empresas",
        ),
    }
    title, detail, url = labels.get(sector, labels["empresas"])
    primary_options = {
        "gobierno": [_chatboc_demo_option("Crear reclamo demo", "chatboc_demo_claim_start")],
        "educacion": [_chatboc_demo_option("Consulta colegio demo", "chatboc_demo_school_start")],
        "empresas": [_chatboc_demo_option("Crear pedido demo", "chatboc_demo_order_start")],
    }.get(sector, [])
    body = (
        f"*{title}*\n"
        f"{detail}\n\n"
        "Podés abrir la demo web o probar una encuesta primero. En las encuestas votás antes de ver "
        "los 100 resultados demo, para que la experiencia sea real."
    )
    return {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": [
            *primary_options,
            _chatboc_demo_option("🗳️ Encuestas y votaciones", f"chatboc_surveys:{sector}:1"),
            _chatboc_demo_option("🌐 Abrir demo web", url=url),
            _chatboc_demo_option("💬 Hablar con ventas", "chatboc_sales_lead"),
            _chatboc_demo_option("↩️ Volver", "menu_principal"),
        ],
        "fuente": f"chatboc_demo_{sector}_menu",
        "skip_audio_generation": True,
    }


def _rewrite_chatboc_survey_options(menu_payload: Dict[str, Any], sector: str) -> Dict[str, Any]:
    rewritten: list[dict[str, Any]] = []
    for option in menu_payload.get("options_list") or []:
        if not isinstance(option, dict):
            continue
        clone = dict(option)
        action = str(clone.get("action_id") or clone.get("id") or "")
        if action.startswith("mostrar_menu_encuestas::"):
            page = action.rsplit("::", 1)[-1]
            clone["action_id"] = f"chatboc_surveys:{sector}:{page}"
        elif action in {"menu_principal", "menu_colegio", "main_menu"}:
            clone["action_id"] = "chatboc_surveys"
        rewritten.append(clone)
    menu_payload["options_list"] = rewritten
    menu_payload["botones"] = rewritten
    return menu_payload


def _build_chatboc_surveys_payload(action_id: str, session_context: ChatSessionContext) -> Dict[str, Any]:
    action = str(action_id or "").strip()
    parts = action.split(":")
    sector = "gobierno"
    page = 1
    if len(parts) >= 2 and parts[1] in {"gobierno", "educacion", "empresas"}:
        sector = parts[1]
    elif isinstance(session_context.context_data, dict):
        sector = session_context.context_data.get("chatboc_demo_survey_sector") or sector
    if len(parts) >= 3:
        try:
            page = max(1, int(parts[2]))
        except (TypeError, ValueError):
            page = 1

    if action == "chatboc_surveys":
        return {
            "success": True,
            "message_body": (
                "🗳️ Elegí una experiencia de encuestas.\n"
                "Primero votás como usuario anónimo. Recién después ves los 100 resultados demo "
                "sumados a tu participación."
            ),
            "message_type": "text",
            "options_list": [
                _chatboc_demo_option("🏛️ Municipios", "chatboc_surveys:gobierno:1"),
                _chatboc_demo_option("🎓 Colegios", "chatboc_surveys:educacion:1"),
                _chatboc_demo_option("🛍️ Empresas", "chatboc_surveys:empresas:1"),
                _chatboc_demo_option("↩️ Volver", "menu_principal"),
            ],
            "fuente": "chatboc_demo_surveys_selector",
            "skip_audio_generation": True,
        }

    if isinstance(session_context.context_data, dict):
        session_context.context_data["chatboc_demo_survey_sector"] = sector
        safe_flag_modified(session_context, "context_data")

    payload = build_demo_survey_chat_menu(
        sector=sector,
        tenant_slug=CHATBOC_DEMO_TENANT_SLUG,
        rubro=sector,
        channel="whatsapp",
        page=page,
        page_size=3,
    )
    payload = _rewrite_chatboc_survey_options(payload, sector)
    payload["fuente"] = "chatboc_demo_surveys_whatsapp"
    payload["skip_audio_generation"] = True
    return payload


def _build_chatboc_survey_link_payload(action_id: str, session_context: ChatSessionContext) -> Dict[str, Any]:
    action = str(action_id or "").strip()
    mode, _, slug = action.partition("::")
    if not slug:
        return _build_chatboc_surveys_payload("chatboc_surveys", session_context)

    sector = "gobierno"
    if isinstance(session_context.context_data, dict):
        sector = session_context.context_data.get("chatboc_demo_survey_sector") or sector

    public_url = _chatboc_demo_survey_url(slug)
    share_url = _chatboc_demo_survey_share_url(slug)
    if mode == "chatboc_survey_share":
        body = (
            "📤 Compartí esta encuesta por WhatsApp:\n"
            f"{share_url}\n\n"
            "El enlace invita a votar primero y después muestra los resultados demo."
        )
        options = [
            _chatboc_demo_option("🗳️ Votar ahora", f"chatboc_survey_open::{slug}"),
            _chatboc_demo_option("↩️ Volver a encuestas", f"chatboc_surveys:{sector}:1"),
        ]
    else:
        body = (
            "🗳️ Abrí la encuesta y votá como usuario anónimo:\n"
            f"{public_url}\n\n"
            "Después de votar vas a ver los 100 resultados demo más tu participación."
        )
        options = [
            _chatboc_demo_option("📤 Compartir encuesta", f"chatboc_survey_share::{slug}"),
            _chatboc_demo_option("↩️ Volver a encuestas", f"chatboc_surveys:{sector}:1"),
        ]

    return {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": options,
        "fuente": "chatboc_demo_survey_link",
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_business_order_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    name = getattr(contact_user, "name", None) or "prospecto"
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    body = (
        f"Pedido demo empresas para {name}\n"
        f"CRM interno: {ticket_ref}\n\n"
        "Simulamos un pedido completo de una empresa:\n"
        "- Cliente consulta producto, stock, promo o envio.\n"
        "- Chatboc arma el carrito y pide los datos que faltan.\n"
        "- El panel recibe pedido, lead, historial y proxima accion.\n\n"
        "Pedido sugerido: 2 Malbec Reserva + 1 caja degustacion. "
        "Podes confirmarlo o abrir la demo web para probar catalogo, carrito y pedidos."
    )
    return {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Confirmar pedido demo", "chatboc_demo_order_confirm"),
            _chatboc_demo_option(
                "Abrir demo pedidos",
                url="https://www.chatboc.ar/demo?sector=empresas&tenant_slug=bodega&intent=crear_pedido",
            ),
            _chatboc_demo_option("Encuestas empresas", "chatboc_surveys:empresas:1"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
        ],
        "fuente": "chatboc_demo_business_order",
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_order_confirm_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    ticket_ref = getattr(ticket, "nro_ticket", None) or "demo"
    name = getattr(contact_user, "name", None) or "cliente"
    return {
        "success": True,
        "message_body": (
            f"Pedido demo generado para {name}.\n"
            f"Orden demo: D-{ticket_ref}\n"
            "Estado: pendiente de validacion comercial.\n\n"
            "En producto real esto queda en CRM, pedidos, historial del contacto y notificaciones del equipo."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Abrir demo pedidos", url="https://www.chatboc.ar/demo?sector=empresas&tenant_slug=bodega"),
            _chatboc_demo_option("Probar municipio", "chatboc_demo:gobierno"),
            _chatboc_demo_option("Probar colegio", "chatboc_demo:educacion"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
        ],
        "fuente": "chatboc_demo_order_confirm",
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_claim_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    body = (
        f"Reclamo demo municipio\nCRM interno: {ticket_ref}\n\n"
        "Simulamos un reclamo completo: categoria, direccion o ubicacion, foto, confirmacion, "
        "ticket publico y seguimiento para el vecino.\n\n"
        "Ejemplo: luminaria apagada en una esquina. Si escribis una direccion, el flujo te guia "
        "sin volver al menu."
    )
    return {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Abrir demo reclamos", url="https://www.chatboc.ar/demo?sector=gobierno&intent=reclamo"),
            _chatboc_demo_option("Encuestas ciudadanas", "chatboc_surveys:gobierno:1"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
            _chatboc_demo_option("Volver municipio", "chatboc_demo:gobierno"),
        ],
        "fuente": "chatboc_demo_claim_start",
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_school_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    body = (
        f"Consulta colegio demo\nCRM interno: {ticket_ref}\n\n"
        "Simulamos atencion para familias: admisiones, cuotas, inasistencias, comunicados "
        "y derivacion a secretaria o preceptor.\n\n"
        "Ejemplo: una familia pregunta por admision o avisa una inasistencia. Chatboc guarda "
        "el motivo, el contacto y la accion pendiente."
    )
    return {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Abrir demo colegios", url="https://www.chatboc.ar/demo?sector=educacion&intent=consulta_escolar"),
            _chatboc_demo_option("Encuestas colegio", "chatboc_surveys:educacion:1"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
            _chatboc_demo_option("Volver colegios", "chatboc_demo:educacion"),
        ],
        "fuente": "chatboc_demo_school_start",
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_voice_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    return {
        "success": True,
        "message_body": (
            f"Llamada demo Chatboc\nCRM interno: {ticket_ref}\n\n"
            "La experiencia de voz puede guiar reclamos, pedidos o consultas escolares. "
            "En esta demo de WhatsApp te muestro el camino equivalente por texto y dejo el lead registrado."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Voz municipio", "chatboc_demo_claim_start"),
            _chatboc_demo_option("Voz colegio", "chatboc_demo_school_start"),
            _chatboc_demo_option("Voz empresa", "chatboc_demo_order_start"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
        ],
        "fuente": "chatboc_demo_voice_start",
        "skip_audio_generation": True,
    }


def _demo_sector_label(sector: str) -> str:
    labels = {
        "gobierno": "municipio",
        "educacion": "colegio",
        "empresas": "empresa",
    }
    return labels.get(sector, "demo")


def _chatboc_demo_contextual_options(sector: str) -> List[Dict[str, Any]]:
    if sector == "empresas":
        return [
            _chatboc_demo_option("Crear pedido demo", "chatboc_demo_order_start"),
            _chatboc_demo_option("Abrir demo empresas", url="https://www.chatboc.ar/demo?sector=empresas&tenant_slug=bodega"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
        ]
    if sector == "educacion":
        return [
            _chatboc_demo_option("Consulta colegio demo", "chatboc_demo_school_start"),
            _chatboc_demo_option("Abrir demo colegios", url="https://www.chatboc.ar/demo?sector=educacion"),
            _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
        ]
    return [
        _chatboc_demo_option("Crear reclamo demo", "chatboc_demo_claim_start"),
        _chatboc_demo_option("Abrir demo reclamos", url="https://www.chatboc.ar/demo?sector=gobierno&intent=reclamo"),
        _chatboc_demo_option("Hablar con ventas", "chatboc_sales_lead"),
    ]


def _build_chatboc_demo_location_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
    input_context: Dict[str, Any],
    sector: str,
) -> Dict[str, Any]:
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    location = input_context.get("location") if isinstance(input_context.get("location"), dict) else {}
    address = location.get("address") or location.get("label")
    coords = ", ".join(
        str(value)
        for value in (location.get("latitude"), location.get("longitude"))
        if value not in {None, ""}
    )
    location_line = address or coords or "ubicacion compartida"
    sector_label = _demo_sector_label(sector)
    if sector == "empresas":
        use_case = (
            "En una pyme esto dispara cotizacion de envio, zona de cobertura, pedido "
            "y proxima accion comercial."
        )
    elif sector == "educacion":
        use_case = (
            "En un colegio esto puede guardar sede, zona de familia, retiro o referencia "
            "para secretaria/preceptoria."
        )
    else:
        use_case = (
            "En municipio esto alimenta el reclamo con direccion, mapa, trazabilidad "
            "y evidencia territorial."
        )

    return {
        "success": True,
        "message_body": (
            f"Ubicacion recibida para demo {sector_label}.\n"
            f"CRM interno: {ticket_ref}\n"
            f"Referencia: {location_line}\n\n"
            f"{use_case}\n\n"
            "Quedo guardada en el CRM y en el historial del contacto."
        ),
        "message_type": "text",
        "options_list": _chatboc_demo_contextual_options(sector),
        "fuente": "chatboc_demo_location_received",
        "data": {"chatboc_demo_input": input_context},
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_media_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
    input_context: Dict[str, Any],
    sector: str,
) -> Dict[str, Any]:
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    media = input_context.get("media") if isinstance(input_context.get("media"), dict) else {}
    media_kind = media.get("kind") or input_context.get("content_type") or "archivo"
    sector_label = _demo_sector_label(sector)
    if media_kind == "audio":
        transcript = media.get("transcribed_text")
        detail = (
            f"Transcripcion detectada: {transcript}"
            if transcript
            else "La nota de voz quedo guardada para seguimiento y se puede revisar desde el CRM."
        )
        title = "Nota de voz procesada"
    elif sector == "empresas":
        title = "Adjunto recibido para demo empresa"
        detail = "Puede ser foto de producto, etiqueta, comprobante o referencia de pedido."
    elif sector == "educacion":
        title = "Adjunto recibido para demo colegio"
        detail = "Puede ser comprobante, autorizacion, certificado o consulta de una familia."
    else:
        title = "Evidencia recibida para demo municipio"
        detail = "Puede ser foto o archivo asociado a reclamo, inspeccion o seguimiento."

    return {
        "success": True,
        "message_body": (
            f"{title}.\n"
            f"CRM interno: {ticket_ref}\n"
            f"Tipo: {media.get('mime_type') or media_kind}\n\n"
            f"{detail}\n\n"
            "Quedo guardado en el contacto, el ticket interno y la trazabilidad del demo."
        ),
        "message_type": "text",
        "options_list": _chatboc_demo_contextual_options(sector),
        "fuente": "chatboc_demo_media_received",
        "data": {"chatboc_demo_input": input_context},
        "skip_audio_generation": True,
    }


def _build_chatboc_sales_payload(contact_user: Optional[User], ticket: Optional[PymeTicket]) -> Dict[str, Any]:
    name = getattr(contact_user, "name", None) or "prospecto"
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    return {
        "success": True,
        "message_body": (
            f"Listo, {name}. Te deje registrado en el CRM de Chatboc como {ticket_ref}.\n"
            "Respondeme con nombre, empresa/rubro y que queres probar, o elegi una demo."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Demo municipios", "chatboc_demo:gobierno"),
            _chatboc_demo_option("Demo colegios", "chatboc_demo:educacion"),
            _chatboc_demo_option("Demo empresas", "chatboc_demo:empresas"),
            _chatboc_demo_option("Encuestas demo", "chatboc_surveys"),
        ],
        "fuente": "chatboc_demo_sales_lead",
        "data": {
            "lead": {
                "ticket_id": getattr(ticket, "id", None),
                "ticket_number": getattr(ticket, "nro_ticket", None),
                "status": getattr(ticket, "estado", None),
            }
        },
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_whatsapp_payload(
    *,
    action_id: Optional[str],
    message_body: str,
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
    session_context: ChatSessionContext,
    input_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    action = str(action_id or "").strip()
    normalized_message = _normalize_chatboc_demo_text(message_body)
    input_context = input_context or _build_chatboc_demo_input_context(
        message_body=message_body,
        uploaded_file_info=None,
        location_info=None,
    )
    content_type = input_context.get("content_type")
    active_sector = _chatboc_demo_active_sector(session_context)
    if content_type == "location":
        if active_sector not in {"gobierno", "educacion", "empresas"}:
            active_sector = "gobierno"
            _remember_chatboc_demo_sector(session_context, active_sector)
        return _build_chatboc_demo_location_payload(contact_user, ticket, input_context, active_sector)
    if content_type in {"image", "video", "file"}:
        if active_sector not in {"gobierno", "educacion", "empresas"}:
            active_sector = "gobierno"
            _remember_chatboc_demo_sector(session_context, active_sector)
        return _build_chatboc_demo_media_payload(contact_user, ticket, input_context, active_sector)
    if action in {"", "menu", "menu_principal", "main_menu", "cancelar"} and normalized_message in {
        "",
        "hola",
        "buenas",
        "menu",
        "menú",
        "volver",
        "inicio",
    }:
        return _build_chatboc_demo_root_payload(getattr(contact_user, "name", None), ticket)
    if action.startswith("chatboc_demo:"):
        sector = action.split(":", 1)[1].strip().lower()
        _remember_chatboc_demo_sector(session_context, sector)
        return _build_chatboc_demo_sector_payload(sector)
    if action == "chatboc_demo_order_start":
        _remember_chatboc_demo_sector(session_context, "empresas")
        return _build_chatboc_demo_business_order_payload(contact_user, ticket)
    if action == "chatboc_demo_order_confirm":
        _remember_chatboc_demo_sector(session_context, "empresas")
        return _build_chatboc_demo_order_confirm_payload(contact_user, ticket)
    if action == "chatboc_demo_claim_start":
        _remember_chatboc_demo_sector(session_context, "gobierno")
        return _build_chatboc_demo_claim_payload(contact_user, ticket)
    if action == "chatboc_demo_school_start":
        _remember_chatboc_demo_sector(session_context, "educacion")
        return _build_chatboc_demo_school_payload(contact_user, ticket)
    if action == "chatboc_demo_voice_start":
        return _build_chatboc_demo_voice_payload(contact_user, ticket)
    if action.startswith("chatboc_survey_open::") or action.startswith("chatboc_survey_share::"):
        return _build_chatboc_survey_link_payload(action, session_context)
    if action.startswith("chatboc_surveys"):
        return _build_chatboc_surveys_payload(action, session_context)
    if action in {"chatboc_sales_lead", "capturar_lead_comercial"}:
        return _build_chatboc_sales_payload(contact_user, ticket)
    if normalized_message in {"encuestas", "votaciones", "sondeos"}:
        return _build_chatboc_surveys_payload("chatboc_surveys", session_context)
    if normalized_message in {"municipio", "municipios", "gobierno"}:
        _remember_chatboc_demo_sector(session_context, "gobierno")
        return _build_chatboc_demo_sector_payload("gobierno")
    if normalized_message in {"colegio", "colegios", "educacion", "escuela"}:
        _remember_chatboc_demo_sector(session_context, "educacion")
        return _build_chatboc_demo_sector_payload("educacion")
    if normalized_message in {"empresa", "empresas", "pyme", "pymes", "bodega"}:
        _remember_chatboc_demo_sector(session_context, "empresas")
        return _build_chatboc_demo_sector_payload("empresas")
    if any(word in normalized_message for word in ("precio", "contratar", "ventas", "asesor", "llamen")):
        return _build_chatboc_sales_payload(contact_user, ticket)
    if _chatboc_demo_has_any(
        normalized_message,
        "pedido",
        "producto",
        "catalogo",
        "stock",
        "envio",
        "comprar",
        "carrito",
        "presupuesto",
    ):
        _remember_chatboc_demo_sector(session_context, "empresas")
        return _build_chatboc_demo_business_order_payload(contact_user, ticket)
    if _chatboc_demo_has_any(
        normalized_message,
        "reclamo",
        "tramite",
        "bache",
        "luminaria",
        "alumbrado",
        "municipal",
    ):
        _remember_chatboc_demo_sector(session_context, "gobierno")
        return _build_chatboc_demo_claim_payload(contact_user, ticket)
    if _chatboc_demo_has_any(
        normalized_message,
        "colegio",
        "escuela",
        "alumno",
        "familia",
        "inasistencia",
        "admision",
        "admisiones",
        "cuota",
        "comunicado",
        "preceptor",
        "secretaria",
    ):
        _remember_chatboc_demo_sector(session_context, "educacion")
        return _build_chatboc_demo_school_payload(contact_user, ticket)
    if _chatboc_demo_has_any(normalized_message, "llamada", "llamar", "voz", "telefono"):
        return _build_chatboc_demo_voice_payload(contact_user, ticket)
    if content_type == "audio":
        if active_sector not in {"gobierno", "educacion", "empresas"}:
            active_sector = "empresas"
            _remember_chatboc_demo_sector(session_context, active_sector)
        return _build_chatboc_demo_media_payload(contact_user, ticket, input_context, active_sector)
    return _build_chatboc_demo_root_payload(getattr(contact_user, "name", None), ticket)


def _safe_log_value(value: Any) -> str:
    """Return ASCII-safe text so WhatsApp logs never break local consoles."""

    try:
        text = str(value)
    except Exception:
        text = repr(value)
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _log(level: str, message: str, *args: Any, exc_info: bool = False) -> None:
    target_logger = current_app.logger if has_app_context() else logger
    safe_message = _safe_log_value(message)
    safe_args = tuple(_safe_log_value(arg) for arg in args)
    getattr(target_logger, level)(safe_message, *safe_args, exc_info=exc_info)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "si", "sí"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _build_sensitive_action_confirmation_text(selected_option: dict) -> str:
    label = (selected_option or {}).get("texto") or "esta acción"
    return (
        f"Antes de continuar con *{label}*, confirmame por favor:\n"
        "1) Sí, iniciar desde cero\n"
        "2) No, volver al menú"
    )


def _selected_option_text_for_bot(selected_option: Optional[dict[str, Any]]) -> Optional[str]:
    """Return the human-facing label that should be sent to the bot layer."""

    if not selected_option:
        return None
    value = selected_option.get("category_name") or selected_option.get("texto")
    if not value:
        return None
    cleaned = re.sub(r"[*_`~]", "", str(value)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _clean_contact_name(value: Optional[Any]) -> Optional[str]:
    cleaned = sanitize_profile_name(str(value).strip() if value is not None else None)
    if not cleaned:
        return None
    if cleaned.strip().lower() in GENERIC_CONTACT_NAMES:
        return None
    return cleaned.strip()


def _context_contact_name(context_data: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(context_data, dict):
        return None

    candidates: list[Any] = [context_data.get("profile_name")]
    contact_cache = context_data.get("contact_cache")
    if isinstance(contact_cache, dict):
        candidates.append(contact_cache.get("nombre"))

    municipio_ctx = context_data.get(CONTEXTO_MUNICIPIO)
    if isinstance(municipio_ctx, dict):
        candidates.append(municipio_ctx.get("profile_name"))
        contacto_usuario = municipio_ctx.get("contacto_usuario")
        if isinstance(contacto_usuario, dict):
            candidates.append(contacto_usuario.get("nombre"))

    for candidate in candidates:
        cleaned = _clean_contact_name(candidate)
        if cleaned:
            return cleaned
    return None


def _remember_contact_name(session_context: ChatSessionContext, name: Optional[str]) -> Optional[str]:
    cleaned = _clean_contact_name(name)
    if not cleaned or not session_context:
        return None
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}

    context_data = session_context.context_data
    context_data["profile_name"] = cleaned
    contact_cache = context_data.setdefault("contact_cache", {})
    if isinstance(contact_cache, dict):
        contact_cache["nombre"] = cleaned

    municipio_ctx = context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    if isinstance(municipio_ctx, dict):
        municipio_ctx["profile_name"] = cleaned
        contacto_usuario = municipio_ctx.setdefault("contacto_usuario", {})
        if isinstance(contacto_usuario, dict):
            contacto_usuario["nombre"] = cleaned

    safe_flag_modified(session_context, "context_data")
    return cleaned


def _resolve_welcome_user_name(
    *,
    end_user: Optional[User],
    session_context: ChatSessionContext,
    profile_name: Optional[str],
    resolved_contact: Optional[dict[str, Any]],
) -> Optional[str]:
    candidates = [
        _context_contact_name(session_context.context_data if session_context else None),
        getattr(end_user, "name", None) if end_user else None,
        (resolved_contact or {}).get("nombre"),
        profile_name,
    ]
    for candidate in candidates:
        cleaned = _clean_contact_name(candidate)
        if cleaned:
            return cleaned
    return None


def _extract_requested_contact_name(raw_text: str, extracted: Optional[dict[str, Any]] = None) -> Optional[str]:
    candidates: list[Any] = []
    if isinstance(extracted, dict):
        candidates.append(extracted.get("nombre"))
    candidates.append(raw_text)

    for candidate in candidates:
        if not candidate:
            continue
        cleaned = str(candidate).strip()
        cleaned = re.sub(
            r"^(soy|me llamo|mi nombre es|nombre|nombre:)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!;:¡!¿?")
        cleaned = _clean_contact_name(cleaned)
        if not cleaned:
            continue
        if any(char.isdigit() for char in cleaned):
            continue
        if len(cleaned) > 80 or len(cleaned.split()) > 4:
            continue
        return cleaned
    return None


def _send_welcome_sticker(
    *,
    to_number_raw: str,
    from_number_raw: str,
    resolved_sticker_url: Optional[str],
    session_context: ChatSessionContext,
    state_key: str,
) -> bool:
    if not twilio_client or not resolved_sticker_url:
        return False
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    welcome_state = session_context.context_data.setdefault("_welcome_state", {})
    sticker_state = welcome_state.setdefault("sticker", {})
    if sticker_state.get("disabled") or sticker_state.get(state_key):
        return False
    try:
        twilio_client.messages.create(
            from_=to_number_raw,
            to=from_number_raw,
            media_url=[resolved_sticker_url],
        )
        sent_ts = time.time()
        sticker_state[state_key] = sent_ts
        sticker_state["last_sent_ts"] = sent_ts
        safe_flag_modified(session_context, "context_data")
        current_app.logger.info("[WELCOME] Sticker sent for %s.", state_key)
        return True
    except Exception as exc:
        sticker_state["disabled"] = True
        safe_flag_modified(session_context, "context_data")
        current_app.logger.warning("[WELCOME] Failed to send welcome sticker for %s: %s", state_key, exc)
        return False


def _sync_education_whatsapp_context(
    session_context: ChatSessionContext,
    tenant_profile,
) -> dict[str, Any] | None:
    if not session_context or not is_education_tenant(tenant_profile):
        return None

    profile = build_education_profile(tenant_profile)
    playbook = build_education_whatsapp_playbook(tenant_profile)
    education_context = {
        "vertical": "educacion",
        "profile": profile,
        "whatsapp_playbook": playbook,
        "quick_menu": playbook.get("quick_menu") or [],
        "media_intelligence": playbook.get("media_intelligence") or {},
    }
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    session_context.context_data["education_context"] = education_context
    session_context.context_data["vertical"] = "educacion"
    safe_flag_modified(session_context, "context_data")
    return education_context


def _education_prompt_payload(intent: str, tenant_profile) -> dict[str, Any]:
    prompt = education_prompt_for_intent(intent, tenant_profile)
    return {
        "message_body": prompt.get("message_body") or "Contame el detalle y lo dejo encaminado.",
        "message_type": "interactive_buttons",
        "options_list": prompt.get("options_list") or [],
        "fuente": "education_whatsapp_intent_prompt",
        "generar_audio": True,
        "audio_text": prompt.get("message_body") or "Contame el detalle y lo dejo encaminado.",
    }


def _education_case_description(
    *,
    message_body: str,
    uploaded_file_info: dict[str, Any] | None,
    location_info: dict[str, Any] | None,
    pending_case: dict[str, Any],
) -> str:
    parts = [
        f"Intent: {pending_case.get('intent') or 'consulta_escolar'}",
        f"Categoria: {pending_case.get('category') or 'secretaria'}",
    ]
    if message_body:
        parts.append(f"Mensaje: {message_body.strip()}")
    if uploaded_file_info:
        parts.append(
            "Adjunto: "
            + str(uploaded_file_info.get("name") or uploaded_file_info.get("url") or uploaded_file_info.get("id") or "archivo")
        )
        transcript = uploaded_file_info.get("transcribed_text")
        if transcript:
            parts.append(f"Transcripcion audio: {transcript}")
    if location_info:
        label = location_info.get("address") or location_info.get("label") or ""
        coords = ",".join(
            str(value)
            for value in [
                location_info.get("latitude") or location_info.get("lat"),
                location_info.get("longitude") or location_info.get("lng") or location_info.get("lon"),
            ]
            if value not in (None, "")
        )
        parts.append(f"Ubicacion: {label or coords or 'compartida'}")
    return "\n".join(parts)


def _create_education_whatsapp_ticket(
    *,
    owner_user: User,
    tenant_profile,
    end_user: User | None,
    anon_id: str,
    pending_case: dict[str, Any],
    message_body: str,
    uploaded_file_info: dict[str, Any] | None,
    location_info: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not owner_user:
        return None

    tipo_chat = (getattr(owner_user, "tipo_chat", None) or getattr(tenant_profile, "tipo", None) or "pyme").lower()
    tipo_ticket = "municipio" if tipo_chat == "municipio" else "pyme"
    description = _education_case_description(
        message_body=message_body,
        uploaded_file_info=uploaded_file_info,
        location_info=location_info,
        pending_case=pending_case,
    )
    category = pending_case.get("category") or "secretaria"
    label = pending_case.get("label") or "Consulta escolar"

    ticket_data = {
        "tenant_id": getattr(tenant_profile, "id", None),
        "user_id": getattr(end_user, "id", None),
        "anon_id": anon_id,
        "asunto": f"Colegio - {label}",
        "categoria": category,
        "pregunta": description,
        "comentario": description,
        "canal_ingreso": "whatsapp",
        "estado": "nuevo",
        "direccion": (location_info or {}).get("address") or (location_info or {}).get("label"),
        "latitud": (location_info or {}).get("latitude") or (location_info or {}).get("lat"),
        "longitud": (location_info or {}).get("longitude") or (location_info or {}).get("lng") or (location_info or {}).get("lon"),
        "telefono_cliente": anon_id,
    }
    if tipo_ticket == "municipio":
        ticket_data["municipio_id"] = getattr(owner_user, "municipio_id", None) or getattr(owner_user, "id", None)
    else:
        ticket_data["pyme_id"] = getattr(owner_user, "id", None)

    ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket, ticket_data)
    if not ticket:
        return None

    attachment_id = (uploaded_file_info or {}).get("id")
    ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
    alias = create_school_case_alias_for_ticket(
        tenant_profile=tenant_profile,
        ticket_type=tipo_ticket,
        ticket_id=ticket_id,
        case_type=category,
        channel="whatsapp",
        end_user=end_user,
        phone=anon_id,
        sensitivity_level=pending_case.get("sensitivity_level"),
    )
    alias_payload = school_case_alias_payload(alias)
    if isinstance(ticket, dict) and alias_payload:
        ticket["school_case_id"] = alias_payload.get("school_case_id")
        ticket["school_case"] = alias_payload

    if attachment_id and ticket_id:
        try:
            adjunto = db.session.get(ArchivoAdjunto, attachment_id)
            if adjunto:
                if tipo_ticket == "municipio":
                    adjunto.municipio_ticket_id = ticket_id
                else:
                    adjunto.pyme_ticket_id = ticket_id
                db.session.add(adjunto)
                db.session.commit()
        except Exception:
            current_app.logger.exception("[EDUCATION_WHATSAPP] No se pudo asociar adjunto al ticket escolar")
            db.session.rollback()
    return ticket


def _handle_education_whatsapp_turn(
    *,
    session_context: ChatSessionContext,
    tenant_profile,
    owner_user: User,
    end_user: User | None,
    anon_id: str,
    selected_action_id: str | None,
    selected_option: dict[str, Any] | None,
    message_body: str,
    uploaded_file_info: dict[str, Any] | None,
    location_info: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not is_education_tenant(tenant_profile):
        return None

    _sync_education_whatsapp_context(session_context, tenant_profile)
    context_data = session_context.context_data if isinstance(session_context.context_data, dict) else {}
    normalized_action = str(selected_action_id or "").strip().lower()

    if normalized_action in {"menu_principal", "menu_colegio"}:
        payload = build_education_whatsapp_menu_payload(tenant_profile)
        context_data["last_options_sent"] = payload.get("options_list") or []
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        return payload

    if normalized_action in education_intents(tenant_profile):
        item = education_menu_item_for_intent(normalized_action, tenant_profile) or selected_option or {}
        pending_case = build_education_pending_case(normalized_action, item)
        context_data["education_pending_case"] = pending_case
        payload = _education_prompt_payload(normalized_action, tenant_profile)
        context_data["last_options_sent"] = payload.get("options_list") or []
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        return payload

    pending_case = context_data.get("education_pending_case")
    has_new_detail = bool((message_body or "").strip() or uploaded_file_info or location_info) and normalized_action not in {"education_send_detail"}
    if isinstance(pending_case, dict) and has_new_detail:
        created_at = pending_case.get("created_at")
        if isinstance(created_at, (int, float)) and time.time() - created_at > 1800:
            context_data.pop("education_pending_case", None)
            safe_flag_modified(session_context, "context_data")
            db.session.add(session_context)
            db.session.commit()
            return {
                "message_body": "La consulta escolar anterior vencio. Elegi una opcion del menu y la retomamos.",
                "message_type": "interactive_buttons",
                "options_list": [{"texto": "Menu colegio", "action_id": "menu_colegio"}],
                "fuente": "education_whatsapp_pending_expired",
                "_context_keys_to_delete": ["education_pending_case"],
            }

        ticket = _create_education_whatsapp_ticket(
            owner_user=owner_user,
            tenant_profile=tenant_profile,
            end_user=end_user,
            anon_id=anon_id,
            pending_case=pending_case,
            message_body=message_body,
            uploaded_file_info=uploaded_file_info,
            location_info=location_info,
        )
        context_data.pop("education_pending_case", None)
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        if ticket:
            payload = build_education_case_ack_payload(ticket, intent=pending_case.get("intent"))
            payload["_context_keys_to_delete"] = ["education_pending_case"]
            return payload
        return {
            "message_body": "Recibi el detalle, pero no pude crear el ticket escolar en este momento. Te derivo con secretaria.",
            "message_type": "interactive_buttons",
            "options_list": [{"texto": "Hablar con secretaria", "action_id": "derivar_humano"}],
            "fuente": "education_whatsapp_case_error",
            "_context_keys_to_delete": ["education_pending_case"],
        }

    return None


def _is_valid_media_url(url: Optional[str]) -> bool:
    """Return True when the URL uses HTTPS and has an allowed extension."""

    if not url:
        return False

    parsed = urlsplit(str(url))
    if parsed.scheme.lower() != "https":
        return False

    path = (parsed.path or "").lower()
    if "." not in path:
        return False

    extension = path.rsplit(".", 1)[-1]
    return f".{extension}" in ALLOWED_MEDIA_EXTENSIONS


def _find_live_chat_ticket(
    owner_user: Optional[User],
    end_user: Optional[User],
    anon_id: Optional[str],
) -> Tuple[Optional[str], Optional[MunicipioTicket | PymeTicket]]:
    if not owner_user:
        return None, None

    tipo_chat = getattr(owner_user, "tipo_chat", None)
    if tipo_chat == "municipio":
        municipio_id = getattr(owner_user, "municipio_id", None) or getattr(owner_user, "id", None)
        query = MunicipioTicket.query.filter(MunicipioTicket.estado.in_(LIVE_CHAT_STATES))
        if end_user:
            query = query.filter(or_(MunicipioTicket.user_id == end_user.id, MunicipioTicket.anon_id == anon_id))
        elif anon_id:
            query = query.filter(MunicipioTicket.anon_id == anon_id)
        if municipio_id:
            query = query.filter(MunicipioTicket.municipio_id == municipio_id)
        return "municipio", query.order_by(MunicipioTicket.fecha.desc()).first()

    if tipo_chat == "pyme":
        rubro_id = getattr(owner_user, "rubro_id", None)
        query = PymeTicket.query.filter(PymeTicket.estado.in_(LIVE_CHAT_STATES))
        if end_user:
            query = query.filter(or_(PymeTicket.user_id == end_user.id, PymeTicket.anon_id == anon_id))
        elif anon_id:
            query = query.filter(PymeTicket.anon_id == anon_id)
        if rubro_id:
            query = query.filter(PymeTicket.rubro_id == rubro_id)
        return "pyme", query.order_by(PymeTicket.fecha.desc()).first()

    return None, None


def _normalize_ticket_reference(ticket_ref: Optional[str]) -> List[str]:
    if not ticket_ref:
        return []

    cleaned = str(ticket_ref).strip().upper().replace("#", "")
    if not cleaned:
        return []

    candidates: List[str] = []
    digit_match = re.search(r"\d+", cleaned)
    if cleaned.isdigit():
        candidates.append(cleaned)
        candidates.append(cleaned.lstrip("0") or cleaned)
        padded = cleaned.zfill(6)
        candidates.append(padded)
        for prefix in ("M-", "S-", "P-"):
            candidates.append(f"{prefix}{padded}")
    else:
        candidates.append(cleaned)
        if digit_match:
            number = digit_match.group(0)
            padded = number.zfill(6)
            candidates.extend(
                [
                    padded,
                    number,
                    f"M-{padded}",
                    f"S-{padded}",
                    f"P-{padded}",
                ]
            )
        match = re.match(r"([A-Z]+)-?(\d+)", cleaned)
        if match:
            prefix, number = match.groups()
            padded = number.zfill(6)
            candidates.extend([f"{prefix}-{padded}", padded, number])

    seen: Set[str] = set()
    unique_candidates = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique_candidates.append(candidate)
    return unique_candidates


def _find_municipio_ticket_for_reference(
    ticket_ref: Optional[str],
    tenant_id: Optional[int] = None,
    municipio_id: Optional[int] = None,
    anon_id: Optional[str] = None,
) -> Optional[MunicipioTicket]:
    candidates = _normalize_ticket_reference(ticket_ref)
    if not candidates:
        return None

    base_query = MunicipioTicket.query
    if municipio_id:
        base_query = base_query.filter(MunicipioTicket.municipio_id == municipio_id)
    elif tenant_id:
        base_query = base_query.filter(MunicipioTicket.tenant_id == tenant_id)

    ticket = base_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
    if ticket:
        return ticket

    if anon_id:
        anon_query = MunicipioTicket.query.filter(MunicipioTicket.anon_id == anon_id)
        if municipio_id:
            anon_query = anon_query.filter(MunicipioTicket.municipio_id == municipio_id)
        elif tenant_id:
            anon_query = anon_query.filter(MunicipioTicket.tenant_id == tenant_id)
        ticket = anon_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
        if ticket:
            return ticket

    return None


def _attach_whatsapp_adjunto_to_ticket(
    adjunto: ArchivoAdjunto,
    ticket: MunicipioTicket,
    end_user: Optional[User],
    comentario_text: str,
) -> None:
    adjunto.municipio_ticket_id = ticket.id
    if hasattr(ticket, "foto_principal"):
        if not ticket.foto_principal:
            ticket.foto_principal = adjunto.url
    elif hasattr(ticket, "foto_url_directa") and not ticket.foto_url_directa:
        ticket.foto_url_directa = adjunto.url

    db.session.add(adjunto)
    db.session.add(ticket)

    comentario = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=comentario_text,
        user_id=end_user.id if end_user else None,
        es_admin=False,
        origen="chat",
        estado_ticket=ticket.estado,
        archivo_adjunto_id=adjunto.id,
    )
    db.session.add(comentario)
    db.session.commit()


def _looks_like_ticket_reference(text: str) -> bool:
    if not text:
        return False
    normalized = text.strip().upper()
    return bool(re.search(r"\d{4,}", normalized))


def _normalize_media_base(url: str) -> str:
    base_url = None
    if has_app_context():
        base_url = current_app.config.get("BASE_URL") or current_app.config.get("PUBLIC_BASE_URL")
    if not base_url:
        return url
    base = str(base_url).rstrip("/")
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(f"{base}/", url.lstrip("/"))


def _prepare_media_param(params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize Twilio media parameters.

    Twilio rejects unsupported media types. This helper filters out
    unsafe URLs (non-HTTPS or disallowed extensions) and expands relative
    URLs using the configured ``BASE_URL`` so outbound webhooks can reach
    static assets reliably.
    """

    prepared = dict(params or {})
    media_urls = prepared.get("media_url") or []
    normalized_urls: list[str] = []

    for candidate in media_urls:
        normalized = _normalize_media_base(str(candidate))
        if _is_valid_media_url(normalized):
            normalized_urls.append(normalized)

    if normalized_urls:
        prepared["media_url"] = normalized_urls
    else:
        prepared.pop("media_url", None)

    return prepared


def _split_message(text: str, limit: int = MAX_TWILIO_BODY_LENGTH) -> list[str]:
    """Split ``text`` into chunks whose UTF-8 encoded length stays below ``limit``.

    Twilio enforces the limit using the number of *bytes* in the request body
    rather than Python's notion of characters. Emojis and accented letters can
    therefore push the request over the threshold even if ``len(text)`` is
    below ``limit``.  This helper keeps chunks within the byte budget while
    still preferring to break on newlines or spaces so the response remains
    readable.
    """

    if not text:
        return [text]

    parts: list[str] = []
    remaining = text

    while remaining:
        if len(remaining.encode("utf-8")) <= limit:
            parts.append(remaining)
            break

        # Start with the largest substring that fits the byte limit.
        end = min(len(remaining), limit)
        while end > 0 and len(remaining[:end].encode("utf-8")) > limit:
            end -= 1
        if end <= 0:
            end = 1

        candidate = remaining[:end]
        split_idx = -1
        for delimiter in ("\n\n", "\n🎭", "\n🗞", "\n📰", "\n*", "\n", " "):
            idx = candidate.rfind(delimiter)
            if idx == -1:
                continue
            # Include the delimiter when splitting on blank lines so the next
            # chunk keeps the natural spacing between posts.
            if delimiter == "\n\n":
                proposed_end = idx + len(delimiter)
            elif delimiter in {"\n🎭", "\n🗞", "\n📰", "\n*"}:
                proposed_end = idx
            else:
                proposed_end = idx
            if proposed_end <= 0:
                continue
            if len(remaining[:proposed_end].encode("utf-8")) <= limit:
                split_idx = proposed_end
                break

        if split_idx == -1:
            split_idx = end

        chunk = remaining[:split_idx]
        if not chunk:
            chunk = remaining[:end]
            split_idx = end

        parts.append(chunk)
        remaining = remaining[split_idx:].lstrip()

    return parts


def _resolve_public_url(url: Optional[str], base_url: str) -> Optional[str]:
    """Return an absolute URL for ``url`` using ``base_url`` when relative."""

    if not url:
        return None

    url = str(url).strip()
    if not url:
        return None

    if url.startswith(("http://", "https://")):
        return url

    base = (base_url or "").rstrip("/")
    if not base:
        return url

    if url.startswith("/"):
        return f"{base}{url}"

    return f"{base}/{url}"


def _normalize_media_url(url: Optional[str], base_url: Optional[str] = None) -> Optional[str]:
    """Return a canonical HTTPS URL for comparison purposes."""

    if not url:
        return None

    candidate = str(url).strip()
    if not candidate:
        return None

    resolved = _resolve_public_url(candidate, (base_url or ""))
    candidate = str(resolved or candidate).strip()
    if not candidate:
        return None

    if candidate.startswith("data:"):
        return candidate

    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    normalized = urlunsplit((scheme, netloc, path, "", ""))

    if normalized.startswith("http://"):
        normalized = "https://" + normalized.split("://", 1)[1]

    return normalized


def _media_signature_tokens(url: Optional[str], base_url: Optional[str]) -> Set[str]:
    """Return a set of identifiers that represent the referenced media."""

    tokens: Set[str] = set()
    normalized = _normalize_media_url(url, base_url)
    if not normalized:
        return tokens

    tokens.add(f"url::{normalized}")

    parsed = urlsplit(normalized)
    path = parsed.path.rstrip("/") or "/"
    tokens.add(f"path::{path}")

    basename = path.rsplit("/", 1)[-1]
    if basename:
        tokens.add(f"file::{basename.lower()}")

    return tokens


def _collect_signature_set(urls: Iterable[Optional[str]], base_url: Optional[str]) -> Set[str]:
    """Build a signature set for the provided media URLs."""

    signature_set: Set[str] = set()
    for candidate in urls:
        signature_set.update(_media_signature_tokens(candidate, base_url))
    return signature_set


def _matches_signature(url: Optional[str], base_url: Optional[str], signatures: Set[str]) -> bool:
    """True if the URL matches any of the known media signatures."""

    if not url or not signatures:
        return False

    return bool(_media_signature_tokens(url, base_url) & signatures)


def _strip_duplicate_welcome_media(
    payload: Dict[str, Any],
    sticker_urls: Iterable[Optional[str]],
    base_url: Optional[str],
) -> None:
    """Remove media fields that duplicate the configured welcome sticker."""

    if not isinstance(payload, dict):
        return

    normalized_targets = _collect_signature_set(sticker_urls, base_url)

    if not normalized_targets:
        return

    def _matches(url: Optional[str]) -> bool:
        return _matches_signature(url, base_url, normalized_targets)

    image_url = payload.get("image_url")
    if _matches(image_url):
        payload.pop("image_url", None)

    media_url = payload.get("media_url")

    def _filter_media(values: Iterable[Optional[str]]) -> list[str]:
        return [value for value in values if value and not _matches(value)]

    if isinstance(media_url, str):
        filtered = _filter_media([media_url])
        if filtered:
            payload["media_url"] = filtered[0] if len(filtered) == 1 else filtered
        else:
            payload.pop("media_url", None)
    elif isinstance(media_url, (list, tuple, set)):
        filtered = _filter_media(media_url)
        if filtered:
            payload["media_url"] = list(filtered)
        else:
            payload.pop("media_url", None)

    header = payload.get("header")
    if isinstance(header, dict) and header.get("type") == "image":
        header_image = header.get("image", {})
        header_link = header_image.get("link") if isinstance(header_image, dict) else None
        if _matches(header_link):
            payload.pop("header", None)

    interactive = payload.get("interactive")
    preserve_header = bool(payload.get("_preserve_welcome_header"))
    if isinstance(interactive, dict):
        interactive_header = interactive.get("header")
        if (
            isinstance(interactive_header, dict)
            and interactive_header.get("type") == "image"
            and not preserve_header
        ):
            image_data = interactive_header.get("image", {})
            image_link = image_data.get("link") if isinstance(image_data, dict) else None
            if _matches(image_link):
                interactive.pop("header", None)

    image_field = payload.get("image")
    if isinstance(image_field, dict):
        image_link = image_field.get("link") or image_field.get("url")
        if _matches(image_link):
            payload.pop("image", None)

    media_field = payload.get("media")
    if isinstance(media_field, dict):
        media_link = media_field.get("link") or media_field.get("url")
        if _matches(media_link):
            payload.pop("media", None)



def _slugify_rubro(value: Optional[str]) -> str:
    """Normalize rubro names/keys to filesystem-friendly slugs."""

    if not value:
        return "default"

    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug or "default"


def _load_pyme_welcome_settings(owner: Optional[User]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return welcome overrides and context for a PYME owner."""

    if not owner:
        return {}, {}

    rubro_value = None
    rubro_obj = getattr(owner, "rubro", None)
    if rubro_obj:
        rubro_value = getattr(rubro_obj, "clave", None) or getattr(rubro_obj, "nombre", None)

    rubro_slug = _slugify_rubro(rubro_value)
    config_data = cargar_configuracion_pyme(rubro_slug, "config.json") or {}
    base_config = dict(config_data)

    welcome_config = base_config.get("welcome") if isinstance(base_config.get("welcome"), dict) else {}
    if not welcome_config and rubro_slug != "default":
        default_config = cargar_configuracion_pyme("default", "config.json") or {}
        default_welcome = default_config.get("welcome")
        if isinstance(default_welcome, dict):
            welcome_config = default_welcome
            if not base_config:
                base_config = dict(default_config)

    context: Dict[str, Any] = {
        "config": base_config,
        "nombre_pyme": base_config.get("nombre_pyme"),
        "whatsapp_numero": (base_config.get("whatsapp") or {}).get("numero"),
    }

    return welcome_config or {}, context


def _render_template_variables(
    variables: Dict[str, Any], *, user_name: str, context: Dict[str, Any]
) -> Dict[str, str]:
    """Resolve template variables replacing known placeholders."""

    resolved: Dict[str, str] = {}
    replacements = {
        "{{user_name}}": user_name or "",
        "{{customer_name}}": user_name or "",
        "{{pyme_nombre}}": context.get("nombre_pyme") or "",
        "{{pyme_whatsapp}}": context.get("whatsapp_numero") or "",
    }

    for key, raw_value in (variables or {}).items():
        key_str = str(key)
        value = ""
        if raw_value is not None:
            value = str(raw_value)
            for placeholder, actual in replacements.items():
                if placeholder in value:
                    value = value.replace(placeholder, actual)
        resolved[key_str] = value

    return resolved


def _dispatch_twilio_pre_messages(
    client,
    to_number: str,
    from_number: str,
    payload: Optional[dict],
    resolve_media_link,
    *,
    channel: str = "whatsapp",
) -> None:
    """Send auxiliary Twilio messages declared in the payload metadata."""

    if not client or not isinstance(payload, dict):
        return

    entries = payload.get("_twilio_pre_messages")
    if not entries:
        return

    normalized_channel = (channel or "whatsapp").strip().lower() or "whatsapp"

    for entry in entries if isinstance(entries, (list, tuple)) else [entries]:
        if not isinstance(entry, dict):
            continue

        channels = entry.get("channels")
        if channels:
            normalized_channels = {
                str(ch).strip().lower()
                for ch in channels
                if isinstance(ch, str) and ch.strip()
            }
            if normalized_channel not in normalized_channels:
                continue

        params: Dict[str, Any] = {"from_": to_number, "to": from_number}
        content_sid = entry.get("content_sid")

        if content_sid:
            params["content_sid"] = content_sid
            content_variables = entry.get("content_variables")
            if content_variables is not None:
                if isinstance(content_variables, str):
                    params["content_variables"] = content_variables
                else:
                    try:
                        params["content_variables"] = json.dumps(content_variables or {})
                    except TypeError:
                        params["content_variables"] = json.dumps({})
        else:
            body = entry.get("body")
            if body is not None:
                params["body"] = str(body)

            media_urls: List[str] = []
            for candidate in entry.get("media_urls") or []:
                resolved = resolve_media_link(candidate)
                if not resolved:
                    continue
                if isinstance(resolved, (list, tuple, set)):
                    for item in resolved:
                        if item and item not in media_urls:
                            media_urls.append(item)
                else:
                    if resolved not in media_urls:
                        media_urls.append(resolved)

            if media_urls:
                params["media_url"] = media_urls

            if "body" not in params and "media_url" not in params:
                continue

            if "body" not in params:
                params["body"] = ""

        try:
            client.messages.create(**params)
        except Exception as exc:
            current_app.logger.warning(
                "[whatsapp] Failed to send pre-message via Twilio: %s", exc,
            )


def _normalize_whatsapp_address(value: Optional[str]) -> Optional[str]:
    """Normalize WhatsApp numbers to ``+<digits>`` for consistent lookups."""

    if not value:
        return None

    candidate = str(value).strip()
    if not candidate:
        return None

    if candidate.lower().startswith("whatsapp:"):
        candidate = candidate.split(":", 1)[1].strip()

    if not candidate:
        return None

    digits = re.sub(r"\D", "", candidate)
    if not digits:
        return None

    if digits.startswith("00"):
        digits = digits[2:]

    return f"+{digits}"


def _lookup_whatsapp_mapping(to_number_raw: str) -> Tuple[Optional[WhatsappNumero], str, Optional[str]]:
    """Return the ``WhatsappNumero`` for the destination number, with fallbacks.

    Parameters
    ----------
    to_number_raw:
        Raw ``To`` header received from Twilio (e.g. ``whatsapp:+549...``).

    Returns
    -------
    tuple
        (mapping, cleaned_number, normalized_number)
    """

    cleaned = (to_number_raw or "").replace("whatsapp:", "").strip()
    normalized = _normalize_whatsapp_address(to_number_raw)

    # HACK: If the number is a 13-digit argentine mobile number (+549...),
    # create a variant without the '9' as it's sometimes omitted in databases.
    normalized_arg_variant = None
    if normalized and normalized.startswith("+549") and len(normalized) == 13:
        normalized_arg_variant = "+54" + normalized[4:]

    lookup_options = dict(is_active=True)

    for candidate in filter(None, {cleaned, normalized, normalized_arg_variant}):
        mapping = WhatsappNumero.query.options(
            joinedload(WhatsappNumero.user).joinedload(User.rubro)
        ).filter_by(**lookup_options, numero_whatsapp=candidate).first()
        if mapping:
            return mapping, cleaned, normalized

    if normalized:
        base_query = WhatsappNumero.query.options(
            joinedload(WhatsappNumero.user).joinedload(User.rubro)
        ).filter_by(**lookup_options)
        for mapping in base_query.all():
            existing_normalized = _normalize_whatsapp_address(mapping.numero_whatsapp)
            if existing_normalized == normalized:
                return mapping, cleaned, normalized

    return None, cleaned, normalized


def _ensure_welcome_audio_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        return

    if payload.get("audio_url") or payload.get("skip_audio_generation"):
        return

    has_menu_content = bool(payload.get("options_list") or payload.get("categorias") or payload.get("botones"))
    if not payload.get("generar_audio") and not payload.get("audio_text") and not has_menu_content:
        return

    if has_menu_content and not payload.get("generar_audio"):
        payload["generar_audio"] = True

    text_to_speak = payload.get("audio_text")
    if not text_to_speak:
        categorias_for_audio = payload.get("categorias")
        options_for_audio = payload.get("options_list") or payload.get("botones") or []
        text_to_speak = render_audio_text(
            message=payload.get("message_body", ""),
            options=options_for_audio if not categorias_for_audio else None,
            categorias=categorias_for_audio,
            datos=payload.get("data"),
            accion=payload.get("accion_backend"),
        )

    if text_to_speak:
        tts_speed = payload.get("tts_speed")
        try:
            tts_speed = float(tts_speed) if tts_speed is not None else None
        except (TypeError, ValueError):
            tts_speed = None

        audio_url = generar_audio(
            text_to_speak,
            voice=payload.get("tts_voice"),
            model=payload.get("tts_model"),
            style=payload.get("tts_style"),
            speed=tts_speed,
            cache_namespace=payload.get("tts_cache_namespace"),
        )
        if audio_url:
            payload["audio_url"] = audio_url


def _reset_municipio_context_for_menu(session_context: ChatSessionContext) -> None:
    if not session_context or not isinstance(session_context.context_data, dict):
        return
    municipio_ctx = session_context.context_data.get(CONTEXTO_MUNICIPIO)
    if not isinstance(municipio_ctx, dict):
        municipio_ctx = {}
        session_context.context_data[CONTEXTO_MUNICIPIO] = municipio_ctx

    municipio_ctx["estado_conversacion"] = "ESPERANDO_SELECCION_MENU_PRINCIPAL"
    # Clear potentially stale drafts to avoid accidental auto-confirm/create when
    # the user selects a fresh numeric menu option (e.g., "1. Iniciar reclamo").
    municipio_ctx.pop("reclamo_flow_v2", None)
    municipio_ctx.pop("datos_reclamo", None)
    municipio_ctx.pop("datos_parciales_llm_reclamo", None)
    municipio_ctx.pop("reclamo_confirmacion_pendiente", None)
    municipio_ctx.pop("confirmation_required", None)
    municipio_ctx.pop("ubicacion_contextual", None)
    municipio_ctx.pop("ultima_consulta_poi", None)
    municipio_ctx.pop("consulta_pendiente_ubicacion", None)
    municipio_ctx.pop("menu_opciones", None)
    session_context.context_data.pop("last_options_sent", None)
    session_context.context_data.pop("pending_sensitive_action", None)
    safe_flag_modified(session_context, "context_data")


def _send_delayed_payload(client, to_number: str, from_number: str, payload: dict, delay: int, app):
    """Send a payload via WhatsApp after a delay using a background thread."""

    def _send():
        with app.app_context():
            from services.response_formatter import build_interactive_response

            _ensure_welcome_audio_payload(payload)

            audio_url = payload.get("audio_url")

            formatted = build_interactive_response(
                options=payload.get("options_list", []),
                body_text=payload.get("message_body", ""),
                channel="whatsapp",
                message_type=payload.get("message_type", "text"),
                original_bot_response=payload,
                audio_url=audio_url,
            )

            params = {"from_": to_number, "to": from_number}

            base_for_normalization = ""
            sticker_signatures: Set[str] = set()
            preserve_welcome_header = False
            if isinstance(payload, dict):
                base_for_normalization = (
                    payload.get("_base_url")
                    or payload.get("_request_url_root")
                    or app.config.get("APP_BASE_URL")
                    or ""
                )
                sticker_signatures = _collect_signature_set(
                    payload.get("_welcome_sticker_urls") or [],
                    base_for_normalization,
                )
                preserve_welcome_header = bool(
                    payload.get("_preserve_welcome_header")
                )

            def _is_welcome_sticker(url: Optional[str]) -> bool:
                return _matches_signature(url, base_for_normalization, sticker_signatures)

            if formatted.get("type") == "interactive":
                interactive = formatted.get("interactive") or {}
                header_candidate = interactive.get("header")
                if (
                    isinstance(header_candidate, dict)
                    and header_candidate.get("type") == "image"
                    and not preserve_welcome_header
                ):
                    image_payload = header_candidate.get("image")
                    header_link = None
                    if isinstance(image_payload, dict):
                        header_link = image_payload.get("link") or image_payload.get("url")
                    if _is_welcome_sticker(header_link):
                        interactive.pop("header", None)
                if preserve_welcome_header and not interactive.get("header"):
                    fallback_url = None
                    for candidate in payload.get("_welcome_sticker_urls") or []:
                        resolved_candidate = _resolve_public_url(
                            candidate, base_for_normalization
                        )
                        if resolved_candidate:
                            fallback_url = resolved_candidate
                            break
                    if fallback_url:
                        interactive["header"] = {
                            "type": "image",
                            "image": {"link": fallback_url},
                        }
                formatted["interactive"] = interactive
                params["body"] = interactive.get("body", {}).get("text", "")
                params["persistent_action"] = [f"whatsapp:{json.dumps(interactive)}"]
            else:
                params["body"] = formatted.get("text", {}).get("body", "")

            sticker_signatures = sticker_signatures or set()

            base_candidates = [
                (payload.get("_base_url") or "").rstrip("/"),
                (payload.get("_request_url_root") or "").rstrip("/"),
                (app.config.get("APP_BASE_URL") or "").rstrip("/"),
            ]

            def _resolve_media_link(raw: Optional[str]) -> Optional[str]:
                if not raw:
                    return None
                resolved = str(raw).strip()
                if not resolved:
                    return None
                if resolved.startswith("/"):
                    for base in base_candidates:
                        if base:
                            https_base = (
                                f"https://{base.split('://', 1)[1]}"
                                if base.startswith("http://")
                                else base
                            )
                            resolved = f"{https_base}{resolved}"
                            break
                elif resolved.startswith("http://"):
                    resolved = resolved.replace("http://", "https://", 1)

                if not resolved or _is_welcome_sticker(resolved):
                    return None
                return resolved

            _dispatch_twilio_pre_messages(
                client,
                to_number,
                from_number,
                payload,
                _resolve_media_link,
            )

            raw_media_urls = payload.get("media_urls") or payload.get("media_url")
            resolved_media_urls: List[str] = []
            if isinstance(raw_media_urls, (list, tuple, set)):
                candidates = raw_media_urls
            elif raw_media_urls:
                candidates = [raw_media_urls]
            else:
                candidates = []

            for candidate in candidates:
                resolved_candidate = _resolve_media_link(candidate)
                if resolved_candidate:
                    resolved_media_urls.append(resolved_candidate)

            if resolved_media_urls:
                params["media_url"] = resolved_media_urls

            image_url = formatted.get("image_url")
            if image_url and "persistent_action" not in params:
                resolved_image_url = _resolve_media_link(image_url)
                if resolved_image_url:
                    existing_media = params.get("media_url")
                    if isinstance(existing_media, list):
                        if resolved_image_url not in existing_media:
                            existing_media.append(resolved_image_url)
                        params["media_url"] = existing_media
                    else:
                        params["media_url"] = [resolved_image_url]

            try:
                message = client.messages.create(**params)

                if audio_url:
                    absolute_audio_url = audio_url
                    if absolute_audio_url.startswith('/'):
                        base_url = (app.config.get("APP_BASE_URL") or "").rstrip('/')
                        if base_url:
                            absolute_audio_url = f"{base_url}{audio_url}"
                        else:
                            app.logger.warning(
                                "[DELAYED_AUDIO] APP_BASE_URL no configurada; no se puede enviar audio con URL relativa %s",
                                audio_url,
                            )
                            absolute_audio_url = None

                    if absolute_audio_url:
                        if _is_welcome_sticker(absolute_audio_url):
                            absolute_audio_url = None

                    if absolute_audio_url:
                        audio_params = {
                            'from_': to_number,
                            'to': from_number,
                            'media_url': [absolute_audio_url]
                        }
                        app.logger.debug(
                            "[DELAYED_AUDIO] Enviando audio adicional para mensaje diferido SID %s", getattr(message, 'sid', 'N/A')
                        )
                        client.messages.create(**audio_params)
            except Exception as e:
                app.logger.error(f"Error sending delayed message: {e}")

    if client:
        timer = threading.Timer(delay, _send)
        timer.daemon = True
        timer.start()


def _esperando_info_libre(municipio_ctx: dict) -> bool:
    """True if any LLM prompt awaits free-form user input.

    Both generic conversation fields and claim-specific flows use different
    context keys when asking the user for additional information. This helper
    centralizes the check so numeric shortcuts and other automated handlers
    can pause while the bot waits for a free-form response.
    """

    return (
        municipio_ctx.get("esperando_info_llm")
        or municipio_ctx.get("esperando_info_llm_reclamo")
    )

# Load environment variables for Twilio credentials
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Initialize Twilio client and request validator
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
else:
    _log(
        "warning",
        "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN environment variables not set. Twilio client and validator will not be initialized.",
    )
    twilio_client = None
    validator = None

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    _log("info", "Whatsapp webhook called.")
    _log("debug", "Request form: %s", request.form)
    if not validator:
        _log("error", "Twilio RequestValidator not initialized. Ensure TWILIO_AUTH_TOKEN is set.")
        abort(500, "Twilio validator not configured")

    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    post_vars = request.form.to_dict()

    if not validator.validate(url, post_vars, signature):
        abort(403, "Invalid Twilio signature")

    to_number_raw = post_vars.get("To", "")
    from_number_raw = post_vars.get("From", "")

    whatsapp_mapping, to_number_cleaned, to_number_normalized = _lookup_whatsapp_mapping(to_number_raw)
    is_chatboc_demo_destination = _is_chatboc_demo_destination(to_number_normalized or to_number_cleaned)
    if is_chatboc_demo_destination:
        whatsapp_mapping = _ensure_chatboc_demo_whatsapp_mapping(to_number_normalized or to_number_cleaned)
    from_number_cleaned = from_number_raw.replace("whatsapp:", "")

    current_app.logger.info(
        "[WHATSAPP_WEBHOOK] Incoming message AccountSid=%s ServiceSid=%s To=%s (normalized=%s) From=%s",
        post_vars.get("AccountSid"),
        post_vars.get("MessagingServiceSid"),
        to_number_cleaned,
        to_number_normalized,
        from_number_cleaned,
    )

    if not whatsapp_mapping:
        looked_up = to_number_normalized or to_number_cleaned
        current_app.logger.error(
            "[WHATSAPP_WEBHOOK] No active mapping for destination number %s (raw=%s)",
            looked_up,
            to_number_raw,
        )
        return "WhatsApp number not configured for any client.", 404

    client_user = whatsapp_mapping.user
    if not client_user:
        _log(
            "error",
            "No user associated with WhatsappNumero id %s for number %s.",
            whatsapp_mapping.id,
            to_number_cleaned,
        )
        return "Internal configuration error: WhatsApp number mapped to non-existent user.", 500

    # Store the owner entity on ``flask.g`` so downstream helpers (like the
    # storage fallback) know which empresa/municipio owns this conversation.
    g.owner_user = client_user

    empresa_id = client_user.id
    tenant_profile = (
        getattr(client_user, "tenant", None)
        or getattr(client_user, "tenant_profile", None)
        or getattr(client_user, "tenant_profile_municipio", None)
        or getattr(client_user, "tenant_profile_pyme", None)
    )
    if is_chatboc_demo_destination:
        tenant_profile = TenantProfile.query.filter_by(slug=CHATBOC_DEMO_TENANT_SLUG).first() or tenant_profile
        current_app.logger.info(
            "[CHATBOC_DEMO_HUB] Forced demo routing for To=%s owner_user_id=%s tenant_id=%s",
            to_number_normalized or to_number_cleaned,
            getattr(client_user, "id", None),
            getattr(tenant_profile, "id", None),
        )
    tenant_id = None
    if tenant_profile:
        tenant_id = getattr(tenant_profile, "id", None) or getattr(tenant_profile, "tenant_id", None)
    from services.pymes import get_or_create_user_by_phone
    end_user = get_or_create_user_by_phone(from_number_cleaned, client_user)

    chat_session_id_internal = f"whatsapp_{empresa_id}_{from_number_cleaned}"

    ensure_chat_session_context_schema(db.session)
    try:
        session_context_db_entry = ChatSessionContext.query.filter_by(
            chat_session_id=chat_session_id_internal
        ).first()
    except ProgrammingError as exc:
        current_app.logger.warning(
            "[WHATSAPP_WEBHOOK] tenant_id missing when querying chat_session_context; retrying after safeguard",
            exc_info=exc,
        )
        db.session.rollback()
        ensure_chat_session_context_schema(db.session)
        try:
            session_context_db_entry = ChatSessionContext.query.filter_by(
                chat_session_id=chat_session_id_internal
            ).first()
        except ProgrammingError as exc_retry:
            current_app.logger.exception(
                "[WHATSAPP_WEBHOOK] Error accediendo a chat_session_context (schema mismatch)",
                exc_info=exc_retry,
            )
            db.session.rollback()
            return (
                "Recibimos tu mensaje pero estamos ajustando el servicio. Intentalo nuevamente en unos minutos.",
                200,
            )
    except SQLAlchemyError as exc:
        current_app.logger.exception(
            "[WHATSAPP_WEBHOOK] Error de base de datos obteniendo el contexto de sesión",
            exc_info=exc,
        )
        db.session.rollback()
        return (
            "Estamos teniendo un problema momentáneo al procesar tu mensaje. Probá de nuevo en breve.",
            200,
        )

    if not session_context_db_entry:
        initial_session_data = {
            "historial_chat": [],
            "estado_conversacion": "inicio",
            "user_id_empresa": empresa_id,
            "telefono_usuario": from_number_cleaned,
            "canal_origen": "whatsapp",
            "mensajes_previos_llm_formato": []
        }
        session_context_db_entry = ChatSessionContext(
            chat_session_id=chat_session_id_internal,
            user_id=empresa_id,
            tenant_id=tenant_id,
            anon_id=from_number_cleaned,
            context_data=initial_session_data,
        )
        db.session.add(session_context_db_entry)
        db.session.commit()
    elif tenant_id and not session_context_db_entry.tenant_id:
        session_context_db_entry.tenant_id = tenant_id
        db.session.add(session_context_db_entry)
        db.session.commit()

    # Ensure context_data is a dict
    if not isinstance(session_context_db_entry.context_data, dict):
        session_context_db_entry.context_data = {}
    if is_chatboc_demo_destination and not session_context_db_entry.context_data.get("chatboc_demo_context_ready"):
        for legacy_key in (
            CONTEXTO_MUNICIPIO,
            "contexto_municipio",
            "contexto_pyme",
            "contexto_pyme_v2",
            "pending_sensitive_action",
            "awaiting_user_name",
            "human_chat_in_progress",
            "room",
            "pending_chunks",
            "last_options_sent",
        ):
            session_context_db_entry.context_data.pop(legacy_key, None)
        session_context_db_entry.context_data["chatboc_demo_context_ready"] = True
        session_context_db_entry.context_data["estado_conversacion"] = "chatboc_demo_hub"
        session_context_db_entry.context_data["canal_origen"] = "whatsapp"
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
    if not is_chatboc_demo_destination:
        _sync_education_whatsapp_context(session_context_db_entry, tenant_profile)

    message_sid = post_vars.get("MessageSid") or post_vars.get("SmsMessageSid")
    media_message_sid = post_vars.get("MediaMessageSid") or post_vars.get("MediaSid0")
    processed_message_sids = session_context_db_entry.context_data.setdefault("processed_message_sids", [])
    processed_media_sids = session_context_db_entry.context_data.setdefault("processed_media_sids", [])

    if message_sid and message_sid in processed_message_sids:
        current_app.logger.info(f"[WHATSAPP_WEBHOOK] Duplicate MessageSid ignored: {message_sid}")
        return "OK", 200
    if media_message_sid and media_message_sid in processed_media_sids:
        current_app.logger.info(f"[WHATSAPP_WEBHOOK] Duplicate MediaMessageSid ignored: {media_message_sid}")
        return "OK", 200

    if message_sid:
        processed_message_sids.append(message_sid)
        session_context_db_entry.context_data["processed_message_sids"] = processed_message_sids[-50:]
    if media_message_sid:
        processed_media_sids.append(media_message_sid)
        session_context_db_entry.context_data["processed_media_sids"] = processed_media_sids[-50:]
    safe_flag_modified(session_context_db_entry, "context_data")

    # --- Boti-style Welcome Message Branch ---
    from services.municipio_responder import normalizar_texto
    from services.config_loader import cargar_configuracion_municipio
    from services.common_utils import _get_main_menu_payload
    from datetime import datetime

    button_payload = post_vars.get("ButtonPayload")
    list_id = post_vars.get("ListId")
    incoming_text = button_payload or list_id or post_vars.get("Body", "")
    normalized_input = normalizar_texto(incoming_text.strip())

    GREETING_KEYWORDS = {"hola", "buenas", "buenos dias", "buenas tardes", "buenas noches"}
    OVERRIDE_KEYWORDS = {"menu", "menu principal", "reiniciar", "resetear", "volver", "cancelar", "terminar"}

    is_greeting = normalized_input in GREETING_KEYWORDS
    is_override = normalized_input in OVERRIDE_KEYWORDS

    municipio_ctx = session_context_db_entry.context_data.get(CONTEXTO_MUNICIPIO, {})
    is_waiting_for_info = _esperando_info_libre(municipio_ctx)

    # Cooldown logic
    now = datetime.now().timestamp()
    last_welcome_ts = session_context_db_entry.context_data.get("last_welcome_ts", 0)
    is_rate_limited = (now - last_welcome_ts) < 15

    welcome_state = session_context_db_entry.context_data.setdefault("_welcome_state", {})
    template_state = welcome_state.setdefault("template", {})
    sticker_state = welcome_state.setdefault("sticker", {})

    safe_flag_modified(session_context_db_entry, "context_data")

    # Universal greeting logic: both Pymes and Municipios now use the Boti-style welcome block.
    # _get_main_menu_payload handles generating the correct menu structure for each type.
    should_trigger_welcome = is_greeting and not is_waiting_for_info and not is_chatboc_demo_destination

    request_root = request.url_root or ""
    request_root_stripped = request_root.rstrip("/")
    configured_base_url = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    effective_base_url = configured_base_url or request_root_stripped
    configured_sticker_url = current_app.config.get("WELCOME_MEDIA_URL")
    configured_audio_url = current_app.config.get("WELCOME_AUDIO_URL")
    resolved_sticker_url = _resolve_public_url(configured_sticker_url, effective_base_url)
    resolved_audio_url = _resolve_public_url(configured_audio_url, effective_base_url)

    pyme_welcome_overrides: Dict[str, Any] = {}
    pyme_welcome_context: Dict[str, Any] = {}
    if client_user and getattr(client_user, "tipo_chat", "") == "pyme":
        pyme_welcome_overrides, pyme_welcome_context = _load_pyme_welcome_settings(client_user)
        if "sticker_url" in pyme_welcome_overrides:
            resolved_sticker_url = _resolve_public_url(
                pyme_welcome_overrides.get("sticker_url"), effective_base_url
            )
        if "audio_url" in pyme_welcome_overrides:
            resolved_audio_url = _resolve_public_url(
                pyme_welcome_overrides.get("audio_url"), effective_base_url
            )

    tenant_config: Dict[str, Any] = {}
    assistant_name = None

    if should_trigger_welcome and not is_rate_limited:
        current_app.logger.info(f"[WELCOME] Triggering Boti-style welcome for user {from_number_cleaned}. Reason: '{normalized_input}'.")

        session_context_db_entry.context_data["last_welcome_ts"] = now
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.commit()

        if twilio_client:
            try:
                template_sid = current_app.config.get("WELCOME_TEMPLATE_SID")
                sticker_cooldown = current_app.config.get("WELCOME_STICKER_COOLDOWN_SECONDS", 300)
                profile_name_from_request = _clean_contact_name(post_vars.get("ProfileName"))
                resolved_contact_for_welcome = resolve_contact(
                    from_number_cleaned,
                    profile_name_from_request,
                )
                user_name = _resolve_welcome_user_name(
                    end_user=end_user,
                    session_context=session_context_db_entry,
                    profile_name=profile_name_from_request,
                    resolved_contact=resolved_contact_for_welcome,
                )
                if user_name:
                    _remember_contact_name(session_context_db_entry, user_name)
                else:
                    user_name = ""

                municipio_config = {}
                municipio_name = None
                if client_user and getattr(client_user, "tipo_chat", None) == "municipio":
                    municipio_id = getattr(client_user, "municipio_id", None)
                    if municipio_id is not None:
                        municipio_config = cargar_configuracion_municipio(str(municipio_id), "config.json") or {}
                    if isinstance(municipio_config, dict):
                        municipio_name = municipio_config.get("nombre") or None

                should_send_template = bool(template_sid) and not template_state.get("disabled", False)
                should_send_sticker = bool(resolved_sticker_url) and not sticker_state.get("disabled", False)
                sticker_metadata_allowed = True
                template_variables_payload: Dict[str, str] = {"1": user_name or ""}

                if tenant_profile and isinstance(getattr(tenant_profile, "configuracion", None), dict):
                    tenant_config = tenant_profile.configuracion or {}
                    assistant_name = tenant_config.get("assistant_name") or tenant_config.get("bot_name")

                if assistant_name and getattr(client_user, "tipo_chat", "") == "municipio":
                    should_send_template = False

                if should_send_template:
                    # Permitir template + sticker cuando el canal lo soporte.
                    # Antes se forzaba False en ambos branches, deshabilitando
                    # el sticker de bienvenida para municipios.
                    sticker_metadata_allowed = True

                if client_user and getattr(client_user, "tipo_chat", None) == "pyme":
                    if "sticker_cooldown_seconds" in pyme_welcome_overrides:
                        try:
                            sticker_cooldown = int(pyme_welcome_overrides.get("sticker_cooldown_seconds") or sticker_cooldown)
                            if sticker_cooldown < 0:
                                sticker_cooldown = 0
                        except (TypeError, ValueError):
                            current_app.logger.warning(
                                "[WELCOME] Invalid sticker cooldown override '%s' for PYME owner %s.",
                                pyme_welcome_overrides.get("sticker_cooldown_seconds"),
                                getattr(client_user, "id", "<unknown>"),
                            )

                    if "template_sid" in pyme_welcome_overrides:
                        template_sid = pyme_welcome_overrides.get("template_sid")
                        should_send_template = bool(template_sid) and not template_state.get("disabled", False)
                    else:
                        should_send_template = False

                    if "sticker_url" in pyme_welcome_overrides:
                        should_send_sticker = bool(resolved_sticker_url) and not sticker_state.get("disabled", False)
                    else:
                        should_send_sticker = False

                    template_vars_override = pyme_welcome_overrides.get("template_variables")
                    if template_vars_override and should_send_template:
                        template_variables_payload = _render_template_variables(
                            template_vars_override,
                            user_name=user_name,
                            context=pyme_welcome_context,
                        )
                    elif not should_send_template:
                        template_variables_payload = {}

                if is_override:
                    if should_send_sticker:
                        current_app.logger.info(
                            "[WELCOME] Sticker suppressed for %s due to override keyword.",
                            from_number_cleaned,
                        )
                    should_send_sticker = False
                    sticker_metadata_allowed = False

                last_sticker_ts = sticker_state.get("last_sent_ts")
                if should_send_sticker and last_sticker_ts:
                    if (now - last_sticker_ts) < max(0, sticker_cooldown):
                        should_send_sticker = False
                        current_app.logger.info(
                            "[WELCOME] Sticker skipped for %s due to cooldown (last_sent_ts=%s)",
                            from_number_cleaned,
                            last_sticker_ts,
                        )

                template_sent = False
                sticker_sent = False

                if should_send_template and template_sid:
                    params = {
                        "from_": to_number_raw,
                        "to": from_number_raw,
                        "content_sid": template_sid,
                        # Always supply the template variables. WhatsApp requires
                        # all placeholders to be populated, so an empty string is
                        # safer than omitting the field and triggering a 400.
                        "content_variables": json.dumps(template_variables_payload or {}),
                    }
                    try:
                        twilio_client.messages.create(**params)
                        template_state["last_sent_ts"] = now
                        safe_flag_modified(session_context_db_entry, "context_data")
                        template_sent = True
                        current_app.logger.info(
                            "[WELCOME] Template %s sent to %s with variables: %s",
                            template_sid,
                            from_number_cleaned,
                            template_variables_payload,
                        )
                    except Exception as e:
                        current_app.logger.warning(
                            f"[WELCOME] Failed to send welcome template {template_sid} to {from_number_cleaned}: {e}"
                        )
                        template_state["disabled"] = True
                        safe_flag_modified(session_context_db_entry, "context_data")

                if should_send_sticker and not is_override:
                    try:
                        twilio_client.messages.create(
                            from_=to_number_raw,
                            to=from_number_raw,
                            media_url=[resolved_sticker_url],
                        )
                        sticker_state["last_sent_ts"] = now
                        safe_flag_modified(session_context_db_entry, "context_data")
                        sticker_sent = True
                        current_app.logger.info(
                            f"[WELCOME] Sticker sent to {from_number_cleaned} using {resolved_sticker_url}."
                        )
                        # Avoid re-attaching the same sticker through the delayed payload.
                        sticker_metadata_allowed = False
                    except Exception as e:
                        current_app.logger.warning(
                            f"[WELCOME] Failed to send welcome sticker to {from_number_cleaned}: {e}"
                        )
                        sticker_state["disabled"] = True
                        safe_flag_modified(session_context_db_entry, "context_data")

                greeting_sent = False

                tenant_name = "Tu Municipio"
                assistant_name = assistant_name or None
                tenant_config = tenant_config or {}
                if tenant_profile and isinstance(getattr(tenant_profile, "configuracion", None), dict):
                    tenant_config = tenant_profile.configuracion or tenant_config
                    assistant_name = assistant_name or tenant_config.get("assistant_name") or tenant_config.get("bot_name")
                    tenant_name = (
                        tenant_config.get("nombre_municipio")
                        or tenant_config.get("nombre")
                        or tenant_profile.nombre
                        or tenant_name
                    )
                elif client_user:
                    tenant_name = (
                        getattr(client_user, "nombre_empresa", None)
                        or getattr(client_user, "name", None)
                        or tenant_name
                    )

                greeting_name = f"{assistant_name} de {tenant_name}" if assistant_name else tenant_name

                if user_name is not None:
                    greeting = (
                        f"*¡Hola, {user_name}!* Acá *{greeting_name}* \U0001F44B"
                        if user_name
                        else f"*¡Hola!* Soy *{greeting_name}* \U0001F44B ¿Cómo te llamás?"
                    )
                    try:
                        twilio_client.messages.create(
                            from_=to_number_raw, to=from_number_raw, body=greeting
                        )
                        greeting_sent = True
                    except Exception as e:
                        greeting_sent = False
                        current_app.logger.error(
                            f"[WELCOME] Failed to send welcome greeting to {from_number_cleaned}: {e}"
                        )

                if not user_name and greeting_sent:
                    session_context_db_entry.context_data["awaiting_user_name"] = True
                    safe_flag_modified(session_context_db_entry, "context_data")
                    db.session.commit()
                    return "OK", 200
            except Exception as e:
                current_app.logger.error(f"[WELCOME] Failed to send welcome template or sticker: {e}")

            try:
                municipio_config = {}
                if client_user and getattr(client_user, "tipo_chat", "") == "municipio":
                    municipio_id = getattr(client_user, "municipio_id", None) or getattr(client_user, "id", None)
                    if municipio_id:
                        loaded_config = cargar_configuracion_municipio(str(municipio_id), "config.json")
                        if isinstance(loaded_config, dict):
                            municipio_config.update(loaded_config)
                if tenant_config:
                    municipio_config.update(tenant_config)

                profile_name = (
                    user_name
                    or _context_contact_name(session_context_db_entry.context_data)
                    or _clean_contact_name(post_vars.get("ProfileName"))
                )
                resolved_contact = resolve_contact(from_number_cleaned, profile_name or None)
                if resolved_contact and not profile_name:
                    profile_name = _clean_contact_name(resolved_contact.get("nombre"))
                if profile_name:
                    _remember_contact_name(session_context_db_entry, profile_name)

                menu_context = {
                    "user_obj": client_user,
                    "viewer_user_obj": end_user,
                    "chat_db_context_data": session_context_db_entry.context_data,
                    "channel": "whatsapp",
                    "municipio_config_actual": municipio_config,
                    "profile_name": profile_name or None,
                    "resolved_contact": resolved_contact or None,
                }
                reduced_menu = template_sent or greeting_sent or sticker_sent
                welcome_message_override = None
                if is_education_tenant(tenant_profile):
                    welcome_response_payload = build_education_whatsapp_menu_payload(
                        tenant_profile,
                        profile_name=profile_name or None,
                        reduced=reduced_menu,
                    )
                else:
                    welcome_response_payload = _get_main_menu_payload(
                        menu_context,
                        welcome_message_override=welcome_message_override,
                        reduced=reduced_menu,
                    )
                if isinstance(welcome_response_payload, dict):
                    if effective_base_url:
                        welcome_response_payload.setdefault("_base_url", effective_base_url)
                    if request_root:
                        welcome_response_payload.setdefault("_request_url_root", request_root)

                    welcome_response_payload["_preserve_welcome_header"] = True

                    _strip_duplicate_welcome_media(
                        welcome_response_payload,
                        sticker_urls=[resolved_sticker_url, configured_sticker_url],
                        base_url=effective_base_url,
                    )

                    sticker_payload = [
                        url
                        for url in [resolved_sticker_url, configured_sticker_url]
                        if url
                    ]
                    if sticker_payload and sticker_metadata_allowed:
                        welcome_response_payload["_welcome_sticker_urls"] = sticker_payload
                        welcome_response_payload["_preserve_welcome_header"] = True
                    else:
                        welcome_response_payload.pop("_welcome_sticker_urls", None)
                        if not sticker_metadata_allowed:
                            welcome_response_payload.pop("_preserve_welcome_header", None)

                    remaining_image_url = welcome_response_payload.get("image_url")
                    resolved_existing_image = _resolve_public_url(
                        remaining_image_url, effective_base_url
                    )
                    if resolved_existing_image:
                        welcome_response_payload["image_url"] = resolved_existing_image
                    elif "image_url" in welcome_response_payload:
                        welcome_response_payload.pop("image_url", None)

                    existing_audio_url = welcome_response_payload.get("audio_url")
                    resolved_existing_audio = _resolve_public_url(existing_audio_url, effective_base_url)
                    if resolved_existing_audio:
                        welcome_response_payload["audio_url"] = resolved_existing_audio
                    elif resolved_audio_url:
                        welcome_response_payload.setdefault("audio_url", resolved_audio_url)

                    _ensure_welcome_audio_payload(welcome_response_payload)

                _reset_municipio_context_for_menu(session_context_db_entry)
                if isinstance(welcome_response_payload, dict):
                    options_list = welcome_response_payload.get("options_list")
                    if isinstance(options_list, list):
                        session_context_db_entry.context_data["last_options_sent"] = options_list
                        safe_flag_modified(session_context_db_entry, "context_data")

                delay = current_app.config.get("WELCOME_MESSAGE_DELAY_SECONDS", 5)
                _send_delayed_payload(
                    client=twilio_client, to_number=to_number_raw, from_number=from_number_raw,
                    payload=welcome_response_payload, delay=delay, app=current_app._get_current_object()
                )
                # Persist any context modifications made during the welcome call
                safe_flag_modified(session_context_db_entry, "context_data")
                db.session.add(session_context_db_entry)
                db.session.commit()
                current_app.logger.info(f"[WELCOME] Scheduled delayed menu for {from_number_cleaned}.")
            except Exception as e:
                current_app.logger.error(f"[WELCOME] Failed to schedule delayed menu: {e}")

        return "OK", 200
    elif should_trigger_welcome and is_rate_limited:
        current_app.logger.info(f"[WELCOME] Welcome skipped for {from_number_cleaned} due to rate-limit.")
    elif is_greeting and is_waiting_for_info:
        current_app.logger.info(f"[WELCOME] Welcome skipped for {from_number_cleaned} because bot is waiting for info.")

    # Determine incoming text before any special handling (re-declaration to ensure it's available for the rest of the code)
    list_id = post_vars.get("ListId")
    incoming_text = button_payload or list_id or post_vars.get("Body", "")

    if session_context_db_entry.context_data.get("awaiting_user_name"):
        name_candidate = incoming_text.strip()
        if name_candidate:
            try:
                extracted = extract_multiple_contact_details_llm(name_candidate, ["nombre"])
            except Exception as e:
                current_app.logger.error(f"[WELCOME] Name extraction failed: {e}")
                extracted = {}
            new_name = _extract_requested_contact_name(name_candidate, extracted)
            if not new_name:
                if twilio_client:
                    twilio_client.messages.create(
                        from_=to_number_raw,
                        to=from_number_raw,
                        body="No pude tomar tu nombre. Decime por favor como te llamas.",
                    )
                return "OK", 200

            if end_user:
                update_user_profile(end_user, {"name": new_name})
            _remember_contact_name(session_context_db_entry, new_name)
            session_context_db_entry.context_data.pop("awaiting_user_name", None)
            personalized_sticker_sent = _send_welcome_sticker(
                to_number_raw=to_number_raw,
                from_number_raw=from_number_raw,
                resolved_sticker_url=resolved_sticker_url,
                session_context=session_context_db_entry,
                state_key="personalized_name_sent_ts",
            )
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.add(session_context_db_entry)
            db.session.commit()
            if twilio_client:
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body=f"¡Encantado, {new_name}! ¿En qué puedo ayudarte?",
                )
            try:
                if is_education_tenant(tenant_profile):
                    welcome_response_payload = build_education_whatsapp_menu_payload(
                        tenant_profile,
                        profile_name=new_name,
                    )
                else:
                    welcome_response_payload = responder_chatboc(
                        pregunta="hola", owner_user=client_user, current_user=end_user,
                        rubro_obj=client_user.rubro, chat_db_context=session_context_db_entry,
                        tipo_chat=client_user.tipo_chat, anon_id=from_number_cleaned,
                        chat_session_uuid=chat_session_id_internal, channel="whatsapp",
                    )
                if isinstance(welcome_response_payload, dict):
                    if effective_base_url:
                        welcome_response_payload.setdefault("_base_url", effective_base_url)
                    if request_root:
                        welcome_response_payload.setdefault("_request_url_root", request_root)

                    welcome_response_payload["_preserve_welcome_header"] = True

                    _strip_duplicate_welcome_media(
                        welcome_response_payload,
                        sticker_urls=[resolved_sticker_url, configured_sticker_url],
                        base_url=effective_base_url,
                    )

                    sticker_payload = [
                        url
                        for url in [resolved_sticker_url, configured_sticker_url]
                        if url
                    ]
                    if (
                        sticker_payload
                        and not personalized_sticker_sent
                        and not sticker_state.get("disabled", False)
                    ):
                        welcome_response_payload["_welcome_sticker_urls"] = sticker_payload
                        welcome_response_payload["_preserve_welcome_header"] = True
                    else:
                        welcome_response_payload.pop("_welcome_sticker_urls", None)
                        welcome_response_payload.pop("_preserve_welcome_header", None)

                    remaining_image_url = welcome_response_payload.get("image_url")
                    resolved_existing_image = _resolve_public_url(
                        remaining_image_url, effective_base_url
                    )
                    if resolved_existing_image:
                        welcome_response_payload["image_url"] = resolved_existing_image
                    elif "image_url" in welcome_response_payload:
                        welcome_response_payload.pop("image_url", None)

                    existing_audio_url = welcome_response_payload.get("audio_url")
                    resolved_existing_audio = _resolve_public_url(existing_audio_url, effective_base_url)
                    if resolved_existing_audio:
                        welcome_response_payload["audio_url"] = resolved_existing_audio
                    elif resolved_audio_url:
                        welcome_response_payload.setdefault("audio_url", resolved_audio_url)

                    _ensure_welcome_audio_payload(welcome_response_payload)

                _reset_municipio_context_for_menu(session_context_db_entry)
                if isinstance(welcome_response_payload, dict):
                    options_list = welcome_response_payload.get("options_list")
                    if isinstance(options_list, list):
                        session_context_db_entry.context_data["last_options_sent"] = options_list
                        safe_flag_modified(session_context_db_entry, "context_data")

                delay = current_app.config.get("WELCOME_MESSAGE_DELAY_SECONDS", 5)
                _send_delayed_payload(
                    client=twilio_client,
                    to_number=to_number_raw,
                    from_number=from_number_raw,
                    payload=welcome_response_payload,
                    delay=delay,
                    app=current_app._get_current_object(),
                )
                if isinstance(welcome_response_payload, dict):
                    options_list = welcome_response_payload.get("options_list")
                    if isinstance(options_list, list):
                        session_context_db_entry.context_data["last_options_sent"] = options_list
                        safe_flag_modified(session_context_db_entry, "context_data")
                # Persist any context updates from responder_chatboc
                safe_flag_modified(session_context_db_entry, "context_data")
                db.session.add(session_context_db_entry)
                db.session.commit()
                current_app.logger.info(
                    f"[WELCOME] Scheduled delayed menu for {from_number_cleaned}."
                )
            except Exception as e:
                current_app.logger.error(
                    f"[WELCOME] Failed to schedule delayed menu after name: {e}"
                )
            return "OK", 200

    # --- Handle pending paginated messages ---
    pending_chunks = session_context_db_entry.context_data.get("pending_chunks", [])
    if pending_chunks and incoming_text.strip().lower() in ["mas", "más", "mostrar mas", "mostrar más", "show_more"]:
        next_chunk = pending_chunks.pop(0)
        session_context_db_entry.context_data["pending_chunks"] = pending_chunks
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
        if twilio_client:
            twilio_client.messages.create(
                from_=to_number_raw,
                to=from_number_raw,
                body=next_chunk,
            )
            if pending_chunks:
                more_payload = {
                    "type": "button",
                    "body": {"text": "¿Mostrar más resultados?"},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": "show_more", "title": "Mostrar más"}},
                            {"type": "reply", "reply": {"id": "menu_principal", "title": "Menú"}},
                        ]
                    },
                }
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body="Seleccioná una opción",
                    persistent_action=[f"whatsapp:{json.dumps(more_payload)}"],
                )
        return "OK", 200

    # --- Profile confirmation flow ---
    if not session_context_db_entry.context_data.get("perfil_confirmado"):
        session_context_db_entry.context_data["perfil_confirmado"] = True
        session_context_db_entry.context_data.setdefault("estado_conversacion", "activo")
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()

    # --- Message and Media Handling SECOND ---
    media_url = post_vars.get("MediaUrl0")
    media_content_type = post_vars.get("MediaContentType0")
    uploaded_file_info = None
    skip_media_analysis = False
    message_body = incoming_text

    if media_url and media_content_type:
        try:
            # Download the file from Twilio's URL first
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            r = requests.get(media_url, auth=auth)
            r.raise_for_status()
            media_content = r.content

            # Create a FileStorage object to be compatible with our services
            file_stream = io.BytesIO(media_content)
            file_name = f"whatsapp_media_{uuid.uuid4().hex[:12]}"
            file_storage = FileStorage(
                stream=file_stream,
                filename=file_name,
                content_type=media_content_type
            )

            # Use the attachment service to save the file and create a thumbnail if applicable
            adjunto = create_attachment_with_thumbnail(
                file_storage=file_storage,
                user_id=end_user.id if end_user else None,
                session_id=chat_session_id_internal
            )

            if adjunto:
                thumb_url = None
                if getattr(adjunto, "analisis", None) and isinstance(adjunto.analisis.datos_estructurados, dict):
                    thumb_url = adjunto.analisis.datos_estructurados.get("url")
                # Prepare the info for the chatbot logic, which will be used for all media types
                uploaded_file_info = {
                    "id": adjunto.id,
                    "url": adjunto.url,
                    "mime_type": adjunto.mime,
                    "name": adjunto.nombre_original,
                    "source": "whatsapp"
                }
                if thumb_url:
                    uploaded_file_info["thumbnail_url"] = thumb_url
                current_app.logger.info(f"WhatsApp media processed and saved as ArchivoAdjunto ID: {adjunto.id}")
            else:
                current_app.logger.error("create_attachment_with_thumbnail failed to process the WhatsApp media")

            if media_content_type.startswith("audio/"):
                session_context_db_entry.context_data['source_is_audio'] = True
                from services.audio_transcription_service import transcribe_audio_from_url
                # We pass the direct URL to the transcription service
                transcribed_text = transcribe_audio_from_url(media_url, media_content_type, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                if transcribed_text:
                    message_body = transcribed_text
                    uploaded_file_info['transcribed_text'] = transcribed_text
                else:
                    current_app.logger.warning("Audio transcription failed or returned empty.")

            # --- Voice Bot / WhatsApp Media Bridge ---
            # If we were waiting for media for a specific ticket, link it now.
            awaiting_ticket_nro = session_context_db_entry.context_data.get("awaiting_photo_for_ticket")
            awaiting_ticket_photo = session_context_db_entry.context_data.get("awaiting_ticket_photo")
            awaiting_ticket_photo_until = session_context_db_entry.context_data.get(
                "awaiting_ticket_photo_until"
            )
            last_ticket_code = session_context_db_entry.context_data.get("last_ticket_code")
            is_ticket_media = bool(media_content_type)

            if not media_content_type.startswith("audio/"):
                session_context_db_entry.context_data.pop('source_is_audio', None)

            if adjunto and is_ticket_media:
                now_ts = time.time()
                within_photo_window = (
                    awaiting_ticket_photo
                    and isinstance(awaiting_ticket_photo_until, (int, float))
                    and now_ts <= awaiting_ticket_photo_until
                )
                if (
                    awaiting_ticket_photo_until
                    and isinstance(awaiting_ticket_photo_until, (int, float))
                    and now_ts > awaiting_ticket_photo_until
                ):
                    session_context_db_entry.context_data.pop("awaiting_photo_for_ticket", None)
                    session_context_db_entry.context_data.pop("awaiting_ticket_photo", None)
                    session_context_db_entry.context_data.pop("awaiting_ticket_photo_until", None)
                    safe_flag_modified(session_context_db_entry, "context_data")
                    db.session.add(session_context_db_entry)
                    db.session.commit()
                    within_photo_window = False

                target_ticket_ref = None
                if within_photo_window:
                    target_ticket_ref = last_ticket_code or awaiting_ticket_nro

                if target_ticket_ref:
                    try:
                        municipio_owner_id = None
                        if client_user and getattr(client_user, "tipo_chat", "") == "municipio":
                            municipio_owner_id = (
                                getattr(client_user, "municipio_id", None)
                                or getattr(client_user, "id", None)
                            )
                        tenant_id = getattr(client_user, "tenant_id", None)
                        ticket = _find_municipio_ticket_for_reference(
                            target_ticket_ref,
                            tenant_id=tenant_id,
                            municipio_id=municipio_owner_id,
                            anon_id=from_number_cleaned,
                        )
                        current_app.logger.info(
                            "[VOICE_BRIDGE] Received media for ticket %s (resolved=%s)",
                            target_ticket_ref,
                            getattr(ticket, "nro_ticket", None),
                        )
                        if ticket:
                            _attach_whatsapp_adjunto_to_ticket(
                                adjunto=adjunto,
                                ticket=ticket,
                                end_user=end_user,
                                comentario_text="[SISTEMA] Vecino adjuntó archivo solicitado por llamada o WhatsApp.",
                            )
                            if twilio_client:
                                twilio_client.messages.create(
                                    from_=to_number_raw,
                                    to=from_number_raw,
                                    body=(
                                        "✅ Archivo recibido y adjuntado al reclamo "
                                        f"*{ticket.nro_ticket}*. ¡Muchas gracias!"
                                    ),
                                )
                            session_context_db_entry.context_data.pop("awaiting_photo_for_ticket", None)
                            session_context_db_entry.context_data.pop("awaiting_ticket_photo", None)
                            session_context_db_entry.context_data.pop("awaiting_ticket_photo_until", None)
                            session_context_db_entry.context_data.pop("pending_attachment_id", None)
                            session_context_db_entry.context_data.pop("pending_ticket_code", None)
                            session_context_db_entry.context_data.pop("pending_attachment_until", None)
                            safe_flag_modified(session_context_db_entry, "context_data")
                            db.session.commit()
                            return "OK", 200

                        session_context_db_entry.context_data["pending_attachment_id"] = adjunto.id
                        session_context_db_entry.context_data["pending_ticket_code"] = target_ticket_ref
                        session_context_db_entry.context_data["pending_attachment_until"] = now_ts + 600
                        safe_flag_modified(session_context_db_entry, "context_data")
                        db.session.commit()
                        if twilio_client:
                            twilio_client.messages.create(
                                from_=to_number_raw,
                                to=from_number_raw,
                                body=(
                                    "⚠️ Todavía no encuentro el ticket en sistema. "
                                    "Respondé con el número del ticket para adjuntar la foto."
                                ),
                            )
                        return "OK", 200
                    except Exception as e_bridge:
                        current_app.logger.error(f"[VOICE_BRIDGE] Error attaching photo: {e_bridge}")

        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Error downloading media from Twilio URL {media_url}: {e}")
        except Exception as e:
            current_app.logger.error(f"Error processing WhatsApp media file: {e}", exc_info=True)
            # Reset uploaded_file_info if processing fails
            uploaded_file_info = None
    else:
        # If no media, ensure the flag is not present
        session_context_db_entry.context_data.pop('source_is_audio', None)

    # --- Location Handling ---
    latitud = post_vars.get("Latitude")
    longitud = post_vars.get("Longitude")
    location_info = None
    if latitud and longitud:
        location_info = {"latitude": latitud, "longitude": longitud}
        address = post_vars.get("Address")
        label = post_vars.get("Label")
        if address:
            location_info["address"] = address
        else:
            try:
                addr = geocodificar_inversa_llm(latitud, longitud)
                if addr and addr.get("formatted_address"):
                    location_info["address"] = addr["formatted_address"]
            except Exception as e:
                current_app.logger.error(f"Error al geocodificar inversamente {latitud, longitud}: {e}")
        if label:
            location_info["label"] = label
        _log("info", "Received location data: %s", location_info)
    else:
        coordenadas = extraer_coordenadas_de_url_google_maps(incoming_text)
        if coordenadas:
            latitud, longitud = coordenadas
            location_info = {"latitude": str(latitud), "longitude": str(longitud)}
            try:
                addr = geocodificar_inversa_llm(latitud, longitud)
                if addr and addr.get("formatted_address"):
                    location_info["address"] = addr["formatted_address"]
            except Exception as e:
                current_app.logger.error(
                    f"Error al geocodificar inversamente {coordenadas}: {e}"
                )
            # treat message as location input only
            incoming_text = ""
            message_body = ""

    # --- Pending attachment resolution ---
    pending_attachment_id = session_context_db_entry.context_data.get("pending_attachment_id")
    pending_attachment_until = session_context_db_entry.context_data.get("pending_attachment_until")
    if pending_attachment_id and not uploaded_file_info and message_body:
        now_ts = time.time()
        if isinstance(pending_attachment_until, (int, float)) and now_ts > pending_attachment_until:
            session_context_db_entry.context_data.pop("pending_attachment_id", None)
            session_context_db_entry.context_data.pop("pending_ticket_code", None)
            session_context_db_entry.context_data.pop("pending_attachment_until", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
        elif _looks_like_ticket_reference(message_body):
            municipio_owner_id = None
            if client_user and getattr(client_user, "tipo_chat", "") == "municipio":
                municipio_owner_id = (
                    getattr(client_user, "municipio_id", None)
                    or getattr(client_user, "id", None)
                )
            tenant_id = getattr(client_user, "tenant_id", None)
            ticket = _find_municipio_ticket_for_reference(
                message_body,
                tenant_id=tenant_id,
                municipio_id=municipio_owner_id,
                anon_id=from_number_cleaned,
            )
            if ticket:
                adjunto = db.session.get(ArchivoAdjunto, pending_attachment_id)
                if adjunto:
                    _attach_whatsapp_adjunto_to_ticket(
                        adjunto=adjunto,
                        ticket=ticket,
                        end_user=end_user,
                        comentario_text="[SISTEMA] Vecino adjuntó foto pendiente por WhatsApp.",
                    )
                session_context_db_entry.context_data.pop("pending_attachment_id", None)
                session_context_db_entry.context_data.pop("pending_ticket_code", None)
                session_context_db_entry.context_data.pop("pending_attachment_until", None)
                session_context_db_entry.context_data.pop("awaiting_ticket_photo", None)
                session_context_db_entry.context_data.pop("awaiting_ticket_photo_until", None)
                safe_flag_modified(session_context_db_entry, "context_data")
                db.session.commit()
                if twilio_client:
                    twilio_client.messages.create(
                        from_=to_number_raw,
                        to=from_number_raw,
                        body=f"✅ Listo. Adjunté la foto al ticket *{ticket.nro_ticket}*.",
                    )
                return "OK", 200

    # --- Numeric Menu Handling ---
    last_options = session_context_db_entry.context_data.get("last_options_sent")
    municipio_ctx = (
        session_context_db_entry.context_data.get(CONTEXTO_MUNICIPIO)
        or session_context_db_entry.context_data.get("contexto_municipio", {})
    )
    if isinstance(municipio_ctx, dict):
        flow_state = (municipio_ctx.get("reclamo_flow_v2") or {}).get("state")
        if flow_state:
            skip_media_analysis = True
    esperando_info = _esperando_info_libre(municipio_ctx)

    # Solo traducir números a acciones cuando no estamos esperando información libre.
    selected_option = None
    selected_action_id = None
    if message_body.isdigit() and last_options and not esperando_info:
        idx = int(message_body) - 1
        if 0 <= idx < len(last_options):
            selected_option = last_options[idx]
            selected_action_id = (
                selected_option.get("action_id")
                or selected_option.get("id")
                or selected_option.get("id_accion")
                or selected_option.get("category_name")
                or selected_option.get("texto")
            )
    elif last_options and not esperando_info:
        normalized_body = (message_body or "").strip().lower()
        for option in last_options:
            option_text = (option.get("texto") or "").strip().lower()
            option_action = (option.get("action_id") or option.get("id") or "").strip().lower()
            if normalized_body and normalized_body in {option_text, option_action}:
                selected_option = option
                selected_action_id = (
                    option.get("action_id")
                    or option.get("id")
                    or option.get("id_accion")
                    or option.get("category_name")
                    or option.get("texto")
                )
                break

    pending_sensitive_action = session_context_db_entry.context_data.get("pending_sensitive_action")
    normalized_message = (message_body or "").strip().lower()

    if isinstance(pending_sensitive_action, dict) and not selected_option:
        if normalized_message in SENSITIVE_ACTION_CONFIRM_ACCEPT:
            selected_option = pending_sensitive_action.get("selected_option") or selected_option
            selected_action_id = pending_sensitive_action.get("action_id") or selected_action_id
            session_context_db_entry.context_data.pop("pending_sensitive_action", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
        elif normalized_message in SENSITIVE_ACTION_CONFIRM_REJECT:
            session_context_db_entry.context_data.pop("pending_sensitive_action", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
            selected_action_id = "menu_principal"

    if selected_option and (selected_action_id or "").strip().lower() in SENSITIVE_MENU_ACTIONS:
        if not (
            isinstance(pending_sensitive_action, dict)
            and normalized_message in SENSITIVE_ACTION_CONFIRM_ACCEPT
        ):
            session_context_db_entry.context_data["pending_sensitive_action"] = {
                "action_id": selected_action_id,
                "selected_option": selected_option,
            }
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()

            confirmation_text = _build_sensitive_action_confirmation_text(selected_option)
            if twilio_client:
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body=confirmation_text,
                )
            return "OK", 200

        # User explicitly confirmed. Start from a clean draft.
        _reset_municipio_context_for_menu(session_context_db_entry)

    selected_option_label = None
    if selected_option and str(post_vars.get("Body", "")).strip().isdigit():
        selected_option_label = _selected_option_text_for_bot(selected_option)
        if selected_option_label:
            message_body = selected_option_label

    # --- Live Chat Routing (WhatsApp -> Admin panel) ---
    human_chat_active = bool(
        session_context_db_entry.context_data.get("human_chat_in_progress")
        or session_context_db_entry.context_data.get("room")
    )
    if human_chat_active and (message_body or uploaded_file_info or location_info):
        should_route_live_chat = not bool(selected_option)
        if should_route_live_chat:
            tipo_ticket, live_ticket = _find_live_chat_ticket(
                client_user,
                end_user,
                from_number_cleaned,
            )
            if live_ticket:
                comentario_text = (message_body or "").strip()
                if location_info and not comentario_text:
                    label = location_info.get("label") or location_info.get("address")
                    if label:
                        comentario_text = f"[Ubicación compartida: {label}]"
                    else:
                        comentario_text = "[Ubicación compartida]"

                if uploaded_file_info:
                    attachment_name = uploaded_file_info.get("name") or "archivo"
                    if comentario_text:
                        comentario_text = f"{comentario_text} [Archivo: {attachment_name}]"
                    else:
                        comentario_text = f"[Archivo adjunto: {attachment_name}]"

                comentario_data = {
                    "comentario": comentario_text or "[Mensaje sin texto]",
                    "user_id": getattr(end_user, "id", None),
                    "anon_id": None if end_user else from_number_cleaned,
                    "es_admin": False,
                    "origen": "whatsapp",
                    "archivo_adjunto_id": uploaded_file_info.get("id") if uploaded_file_info else None,
                    "emit_socket": False,
                }

                nuevo_comentario = servicio_tickets.crear_comentario(
                    ticket_id=live_ticket.id,
                    tipo_ticket=tipo_ticket,
                    comentario_data=comentario_data,
                )

                if uploaded_file_info:
                    adjunto = db.session.get(ArchivoAdjunto, uploaded_file_info.get("id"))
                    if adjunto:
                        if tipo_ticket == "municipio":
                            adjunto.municipio_ticket_id = live_ticket.id
                        else:
                            adjunto.pyme_ticket_id = live_ticket.id
                        db.session.add(adjunto)

                try:
                    db.session.commit()
                except Exception as exc:
                    current_app.logger.error(
                        "[WHATSAPP_WEBHOOK] Error guardando mensaje de chat en vivo: %s",
                        exc,
                        exc_info=True,
                    )
                    db.session.rollback()
                else:
                    if nuevo_comentario:
                        try:
                            from socket_service import socketio

                            room_name = f"ticket_{tipo_ticket}_{live_ticket.id}"
                            socketio.emit(
                                "new_chat_message",
                                {
                                    "ticket_id": live_ticket.id,
                                    "message": nuevo_comentario.to_dict(),
                                },
                                room=room_name,
                            )
                        except Exception as socket_exc:
                            current_app.logger.error(
                                "[WHATSAPP_WEBHOOK] Error emitiendo mensaje en vivo: %s",
                                socket_exc,
                                exc_info=True,
                            )

                return "OK", 200

    # --- Human Chat Check ---
    if session_context_db_entry.context_data.get("human_chat_in_progress"):
        room = session_context_db_entry.context_data.get("room")
        if room:
            from socket_service import socketio
            socketio.emit('message', {'msg': message_body}, room=room)
            return "OK", 200


    # --- Call Real Chatbot Logic: responder_chatboc ---
    # Initialize with a default error response
    bot_response_dict = {
        'message_body': "Lo siento, no pude procesar tu solicitud en este momento.",
        'options_list': [],
        'message_type': 'text',
        'fuente': 'error_handler_whatsapp'
    }

    chatboc_demo_direct_payload = None
    education_direct_payload = None

    if is_chatboc_demo_destination:
        profile_name_from_request = _clean_contact_name(post_vars.get("ProfileName"))
        chatboc_demo_input_context = _build_chatboc_demo_input_context(
            message_body=message_body,
            uploaded_file_info=uploaded_file_info,
            location_info=location_info,
        )
        message_body_for_demo = (
            chatboc_demo_input_context.get("effective_message")
            or message_body
            or chatboc_demo_input_context.get("summary")
            or ""
        )
        used_messages, message_limit = _increment_chatboc_demo_usage(session_context_db_entry)
        contact_user, demo_ticket = _record_chatboc_demo_engagement(
            owner_user=client_user,
            tenant=tenant_profile,
            session_context=session_context_db_entry,
            from_number=from_number_cleaned,
            profile_name=profile_name_from_request,
            message_body=message_body_for_demo,
            action_id=selected_action_id,
            message_sid=message_sid,
            input_context=chatboc_demo_input_context,
        )
        if contact_user and not end_user:
            end_user = contact_user
        if used_messages > message_limit:
            chatboc_demo_direct_payload = _build_chatboc_demo_limit_payload(
                used_messages,
                message_limit,
                demo_ticket,
            )
        else:
            chatboc_demo_direct_payload = _build_chatboc_demo_whatsapp_payload(
                action_id=selected_action_id,
                message_body=message_body_for_demo,
                contact_user=contact_user or end_user,
                ticket=demo_ticket,
                session_context=session_context_db_entry,
                input_context=chatboc_demo_input_context,
            )
        bot_response_dict = chatboc_demo_direct_payload
    else:
        education_direct_payload = _handle_education_whatsapp_turn(
            session_context=session_context_db_entry,
            tenant_profile=tenant_profile,
            owner_user=client_user,
            end_user=end_user,
            anon_id=from_number_cleaned,
            selected_action_id=selected_action_id,
            selected_option=selected_option,
            message_body=message_body,
            uploaded_file_info=uploaded_file_info,
            location_info=location_info,
        )
    if education_direct_payload:
        bot_response_dict = education_direct_payload

    # Check if we should bypass the bot logic because the user selected a URL option
    bypass_bot_logic = bool(education_direct_payload or chatboc_demo_direct_payload)
    if selected_option and selected_option.get("url"):
        # If the option has a URL, we simply echo it back to the user
        bypass_bot_logic = True
        url_text = selected_option.get("texto", "enlace")
        url_link = selected_option.get("url")
        bot_response_dict = {
            "message_body": f"Podés acceder a *{url_text}* ingresando aquí:\n{url_link}",
            "message_type": "interactive_buttons",
            "options_list": [
                {"texto": "Menú", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"}
            ],
            "fuente": "webhook_url_selection_fallback",
            "generar_audio": True # Ensure audio is generated for this fallback
        }
        current_app.logger.info(f"Intercepted numeric selection for URL option: {url_text}")

    respuesta_del_bot_text = bot_response_dict['message_body']

    # The context_data from session_context_db_entry will be passed to responder_chatboc
    # and it's expected that responder_chatboc might modify it directly or return a new context.

    try:
        if not bypass_bot_logic:
            _log(
                "info",
                "Calling responder_chatboc for session_id: %s, owner_user: %s",
                chat_session_id_internal,
                client_user.name,
            )

            interpretacion_media_data = None
            if uploaded_file_info:
                mime_type = uploaded_file_info.get("mime_type", "")
                if not skip_media_analysis and not mime_type.startswith("audio/"):
                    interpretacion_media_data = clasificar_adjunto_whatsapp(uploaded_file_info, client_user)
            # Location info should not be treated as interpreted media.
            # It should be passed directly as location data.

            kwargs_for_bot = {"source_channel": "whatsapp"}
            education_context = (
                session_context_db_entry.context_data.get("education_context")
                if isinstance(session_context_db_entry.context_data, dict)
                else None
            )
            if education_context:
                kwargs_for_bot["education_context"] = education_context
                kwargs_for_bot["vertical"] = "educacion"
            if uploaded_file_info:
                kwargs_for_bot["uploaded_file_info"] = uploaded_file_info
                kwargs_for_bot["archivo_id_para_asociar"] = uploaded_file_info.get("id")
                mime_type = uploaded_file_info.get("mime_type", "")
                if mime_type.startswith("image/"):
                    # Also add the specific keys the old flow handler expects
                    kwargs_for_bot["es_foto"] = True
                    kwargs_for_bot["foto_url"] = uploaded_file_info.get("url")
                if skip_media_analysis:
                    kwargs_for_bot["skip_media_analysis"] = True
            if location_info:
                # Pass location_info and mark it explicitly as a location payload
                kwargs_for_bot["location"] = location_info
                kwargs_for_bot["es_ubicacion"] = True
                kwargs_for_bot["ubicacion_usuario"] = location_info
            if interpretacion_media_data and not interpretacion_media_data.get("error"):
                # This will now only contain data from actual images/files, not locations.
                kwargs_for_bot["datos_interpretados_archivo"] = interpretacion_media_data
            if selected_action_id:
                kwargs_for_bot["action"] = selected_action_id

            if selected_option:
                kwargs_for_bot["selected_option_data"] = selected_option

            profile_name = post_vars.get("ProfileName")
            resolved_contact = resolve_contact(from_number_cleaned, profile_name)
            if resolved_contact:
                kwargs_for_bot["resolved_contact"] = resolved_contact
                if isinstance(session_context_db_entry.context_data, dict):
                    session_context_db_entry.context_data["resolved_contact"] = resolved_contact
                    session_context_db_entry.context_data.setdefault("contact_cache", {}).update(
                        {k: v for k, v in resolved_contact.items() if v}
                    )
                if not profile_name and resolved_contact.get("nombre"):
                    profile_name = resolved_contact.get("nombre")
            if profile_name:
                kwargs_for_bot["profile_name"] = profile_name

            # The actual call that might raise an exception
            bot_response_dict = responder_chatboc(
                pregunta=message_body,
                owner_user=client_user,
                current_user=end_user,
                rubro_obj=client_user.rubro,
                chat_db_context=session_context_db_entry,
                rubro_nombre_frontend=None,
                tipo_chat=client_user.tipo_chat,
                anon_id=from_number_cleaned,
                chat_session_uuid=chat_session_id_internal,
                channel="whatsapp",
                **kwargs_for_bot
            )

            normalize_response_payload(bot_response_dict)

            # Si el usuario es anónimo y la acción requiere datos personales, pedir solo los faltantes.
            if not end_user and bot_response_dict.get("accion_backend") in ["crear_reclamo", "iniciar_reclamo"]:
                contexto_actual = session_context_db_entry.context_data.get("contexto_municipio", {})
                datos_reclamo = contexto_actual.get("datos_parciales_llm_reclamo", {})

                potential_fields = ["nombre_cliente", "telefono_cliente", "email_cliente"]
                current_app.logger.debug(f"[CONTACT_EXTRACTION] Extracting {potential_fields} from: {message_body}")
                extracted_data = extract_multiple_contact_details_llm(message_body, potential_fields)
                current_app.logger.debug(f"[CONTACT_EXTRACTION] Extracted: {extracted_data}")

                if extracted_data.get("nombre_cliente"):
                    datos_reclamo["nombre_usuario_detectado"] = extracted_data["nombre_cliente"]
                if extracted_data.get("telefono_cliente"):
                    datos_reclamo["telefono_detectado"] = extracted_data["telefono_cliente"]
                if extracted_data.get("email_cliente"):
                    datos_reclamo["email_detectado"] = extracted_data["email_cliente"]

                contexto_actual["datos_parciales_llm_reclamo"] = datos_reclamo
                session_context_db_entry.context_data["contexto_municipio"] = contexto_actual

                contacto = resolve_contact_snapshot(
                    datos=datos_reclamo,
                    profile_name=contexto_actual.get("profile_name") or session_context_db_entry.context_data.get("profile_name"),
                    anon_id=from_number_cleaned,
                )
                faltan_contactos = missing_contact_fields(contacto)
                if faltan_contactos:
                    bot_response_dict = {
                        "message_body": (
                            "Para poder registrar tu reclamo, necesito estos datos: "
                            + ", ".join(faltan_contactos)
                            + "."
                        ),
                        "pedir_info": faltan_contactos,
                    }

            _log("debug", "Raw response from responder_chatboc: %s", bot_response_dict)

            # Validate the response from the bot logic
            if not isinstance(bot_response_dict, dict):
                _log(
                    "warning",
                    "responder_chatboc did not return a dictionary. Response: %s",
                    bot_response_dict,
                )
                # Keep the default error response initialized earlier
                bot_response_dict = {
                    'message_body': "Lo siento, hubo un error interno al procesar tu mensaje.",
                    'options_list': [], 'message_type': 'text', 'fuente': 'error_handler_non_dict_response'
                }

            # Ensure context_data is a dict for saving
            if not isinstance(session_context_db_entry.context_data, dict):
                _log(
                    "warning",
                    "context_data in session_context_db_entry is not a dict. Resetting. Data: %s",
                    session_context_db_entry.context_data,
                )
                session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format."}],
                    "estado_conversacion": "error_context"
                }

    except Exception as e:
        _log("error", "Error calling real chatbot logic (responder_chatboc): %s", e, exc_info=True)
        # bot_response_dict is already set to a default error message, so we just log and continue

    # Update respuesta_del_bot_text for logging from the final bot_response_dict
    respuesta_del_bot_text = bot_response_dict.get("message_body", "")
    _log(
        "debug",
        "Bot response text for logging: %s. Session context to save: %s",
        respuesta_del_bot_text,
        session_context_db_entry.context_data,
    )

    # --- Format Response and Save Session ---
    formatted_whatsapp_payload = {}
    try:
        from services.response_formatter import build_interactive_response

        receipt_payload = bot_response_dict.get("whatsapp_receipt")
        if receipt_payload:
            bot_response_dict["message_body"] = receipt_payload.get("body_text")
            bot_response_dict["options_list"] = []
            bot_response_dict["message_type"] = "text"
            if receipt_payload.get("media_url"):
                bot_response_dict["image_url"] = receipt_payload.get("media_url")

        body_text = bot_response_dict.get("message_body", "")

        # This call will modify bot_response_dict to include context for the numeric menu
        formatted_whatsapp_payload = build_interactive_response(
            options=bot_response_dict.get('options_list', []),
            body_text=body_text,
            channel='whatsapp',
            message_type=bot_response_dict.get('message_type', 'text'),
            original_bot_response=bot_response_dict,
            header_text=bot_response_dict.get('header_text'),
            footer_text=bot_response_dict.get('footer_text'),
            audio_url=bot_response_dict.get('audio_url')
        )

        # After formatting, the context might be updated (e.g., with last_options_sent).
        # We need to merge this updated context back into our main session object before saving.
        updated_context = formatted_whatsapp_payload.get('contexto_actualizado')

        # The existing context from the database
        db_context = session_context_db_entry.context_data or {}
        _log("info", "[CONTEXT_WHATSAPP] Contexto de la base de datos: %s", db_context)
        _log("info", "[CONTEXT_WHATSAPP] Contexto actualizado del turno actual: %s", updated_context)


        # Merge the contexts
        if updated_context:
            merged_context = {**db_context, **updated_context}
        else:
            merged_context = db_context

        for key_to_delete in bot_response_dict.get("_context_keys_to_delete") or []:
            merged_context.pop(key_to_delete, None)

        _log("info", "[CONTEXT_WHATSAPP] Contexto fusionado para guardar: %s", merged_context)


        # Save the merged context
        session_context_db_entry.context_data = merged_context
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        if not is_chatboc_demo_destination and tenant_profile and getattr(tenant_profile, "id", None):
            try:
                contact = resolve_or_create_contact(
                    tenant_profile,
                    phone=from_number_cleaned,
                    whatsapp_id=from_number_cleaned,
                    name=locals().get("profile_name") or post_vars.get("ProfileName"),
                    legacy_user=end_user,
                    contact_type="neighbor" if client_user.tipo_chat == "municipio" else "customer",
                    source="whatsapp_inbound",
                )
                record_contact_interaction(
                    tenant=tenant_profile,
                    contact=contact,
                    message_body=message_body or selected_option_label or "",
                    channel="whatsapp",
                    direction="inbound",
                    source="whatsapp_webhook",
                    metadata={
                        "event_type": "whatsapp_inbound",
                        "message_sid": message_sid,
                        "selected_action_id": selected_action_id,
                        "selected_option": selected_option_label,
                        "owner_user_id": getattr(client_user, "id", None),
                        "end_user_id": getattr(end_user, "id", None),
                        "bot_fuente": bot_response_dict.get("fuente"),
                    },
                    content_type="text" if message_body else "event",
                    emit=True,
                )
            except Exception as crm_err:
                current_app.logger.warning(
                    "[CRM] WhatsApp contact enrichment skipped for %s: %s",
                    from_number_cleaned,
                    crm_err,
                    exc_info=True,
                )
        db.session.commit()
        _log(
            "info",
            "Session saved for %s. Context: %s",
            chat_session_id_internal,
            session_context_db_entry.context_data,
        )

    except Exception as e:
        db.session.rollback()
        _log(
            "error",
            "Error formatting response or saving session for %s: %s",
            chat_session_id_internal,
            e,
            exc_info=True,
        )

    # --- Send Response via Twilio ---
    if twilio_client:
        try:
            # El formateador ahora devuelve un diccionario con 'type' y los datos.
            # Si es de tipo 'text', usamos el cuerpo directamente.
            message_params = {
                'from_': to_number_raw,
                'to': from_number_raw,
            }

            if not isinstance(session_context_db_entry.context_data, dict):
                session_context_db_entry.context_data = {}

            def _resolve_pre_media_link(raw: Optional[str]) -> Optional[str]:
                if not raw:
                    return None
                resolved = str(raw).strip()
                if not resolved:
                    return None
                if resolved.startswith('/'):
                    base_url = request.url_root.rstrip('/')
                    resolved = f"{base_url}{resolved}"
                elif resolved.startswith('http://'):
                    resolved = resolved.replace('http://', 'https://', 1)
                return resolved

            _dispatch_twilio_pre_messages(
                twilio_client,
                to_number_raw,
                from_number_raw,
                bot_response_dict,
                _resolve_pre_media_link,
            )

            interactive_payload = None
            interactive_body_dict = None
            if formatted_whatsapp_payload.get("type") == "interactive":
                interactive_payload = formatted_whatsapp_payload.get("interactive") or {}
                # The body is required, it's the fallback for notifications and older clients
                interactive_body_dict = interactive_payload.get("body") or {}
                message_params['body'] = interactive_body_dict.get("text", "Por favor, mirá las opciones.")
            else: # Text message
                message_params['body'] = formatted_whatsapp_payload.get("text", {}).get("body", "No se pudo generar una respuesta.")

            image_url = formatted_whatsapp_payload.get("image_url")
            if image_url and 'persistent_action' not in message_params:
                if image_url.startswith('/'):
                    base_url = request.url_root.rstrip('/')
                    image_url = f"{base_url}{image_url}"
                message_params['media_url'] = [image_url]

            current_app.logger.debug(f"Sending WhatsApp message params: {message_params}")

            def _apply_persistent_action(params: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> bool:
                """Attach the interactive payload to Twilio params ensuring it respects length limits."""
                if not payload:
                    params.pop('persistent_action', None)
                    return True

                encoded_payload = f"whatsapp:{json.dumps(payload, ensure_ascii=False)}"
                body_len = len(params.get('body') or "")
                if len(encoded_payload) > MAX_TWILIO_BODY_LENGTH or (body_len + len(encoded_payload)) > MAX_TWILIO_BODY_LENGTH:
                    current_app.logger.warning(
                        "Interactive payload exceeds Twilio character limit; falling back to plain text delivery."
                    )
                    params.pop('persistent_action', None)
                    return False

                params['persistent_action'] = [encoded_payload]
                return True

            # Send the main message. If the body exceeds Twilio's 1600 character
            # limit we now split it into chunks. For interactive payloads we
            # update the fallback text and deliver the remaining chunks as
            # separate plain messages. For regular text payloads we keep the
            # "Mostrar más" flow so the user can request the remaining chunks.
            body_text = message_params.get('body', '') or ''
            if len(body_text) > MAX_TWILIO_BODY_LENGTH:
                chunks = _split_message(body_text)
                first_chunk = chunks[0]
                remaining_chunks = chunks[1:]

                message_params['body'] = first_chunk

                sent_interactive_chunk = False
                if interactive_payload is not None:
                    # Update the interactive payload fallback text as well so the
                    # JSON payload we send through Twilio respects the character
                    # limit.
                    interactive_body = interactive_body_dict if interactive_body_dict is not None else interactive_payload.setdefault("body", {})
                    interactive_body["text"] = first_chunk

                    if _apply_persistent_action(message_params, interactive_payload):
                        session_context_db_entry.context_data.pop('pending_chunks', None)
                        safe_flag_modified(session_context_db_entry, 'context_data')
                        db.session.add(session_context_db_entry)
                        db.session.commit()

                        main_message = twilio_client.messages.create(**message_params)
                        _log(
                            "info",
                            "Mensaje principal (interactivo) enviado a %s, SID: %s",
                            from_number_raw,
                            main_message.sid,
                        )

                        for idx, chunk in enumerate(remaining_chunks, start=2):
                            followup_params = {
                                'from_': to_number_raw,
                                'to': from_number_raw,
                                'body': chunk,
                            }
                            followup_message = twilio_client.messages.create(**followup_params)
                            _log(
                                "info",
                                "Mensaje adicional %s/%s enviado a %s, SID: %s",
                                idx,
                                len(chunks),
                                from_number_raw,
                                followup_message.sid,
                            )
                        sent_interactive_chunk = True
                    else:
                        interactive_payload = None

                if not sent_interactive_chunk:
                    session_context_db_entry.context_data['pending_chunks'] = remaining_chunks
                    safe_flag_modified(session_context_db_entry, 'context_data')
                    db.session.add(session_context_db_entry)
                    db.session.commit()

                    first_chunk_params = {
                        'from_': to_number_raw,
                        'to': from_number_raw,
                        'body': first_chunk,
                    }
                    main_message = twilio_client.messages.create(**first_chunk_params)
                    _log(
                        "info",
                        "Mensaje parte 1/%s enviado a %s, SID: %s",
                        len(chunks),
                        from_number_raw,
                        main_message.sid,
                    )

                    if session_context_db_entry.context_data['pending_chunks']:
                        more_payload = {
                            "type": "button",
                            "body": {"text": "¿Mostrar más resultados?"},
                            "action": {
                                "buttons": [
                                    {"type": "reply", "reply": {"id": "show_more", "title": "Mostrar más"}},
                                    {"type": "reply", "reply": {"id": "menu_principal", "title": "Menú"}},
                                ]
                            },
                        }
                        twilio_client.messages.create(
                            from_=to_number_raw,
                            to=from_number_raw,
                            body="Seleccioná una opción",
                            persistent_action=[f"whatsapp:{json.dumps(more_payload)}"],
                        )
            else:
                if interactive_payload is not None:
                    if not _apply_persistent_action(message_params, interactive_payload):
                        interactive_payload = None

                session_context_db_entry.context_data.pop('pending_chunks', None)
                safe_flag_modified(session_context_db_entry, 'context_data')
                db.session.add(session_context_db_entry)
                db.session.commit()
                main_message = twilio_client.messages.create(**message_params)
                _log(
                    "info",
                    "Mensaje principal enviado a %s, SID: %s",
                    from_number_raw,
                    main_message.sid,
                )

            audio_enabled = bool(
                _as_bool(
                    current_app.config.get(
                        "WHATSAPP_AUDIO_ENABLED",
                        os.getenv("WHATSAPP_AUDIO_ENABLED"),
                    ),
                    default=True,
                )
                or _as_bool(bot_response_dict.get("force_audio_whatsapp"))
            )
            if audio_enabled:
                _ensure_welcome_audio_payload(bot_response_dict)

                # Second, if there is an audio URL, send it as a separate media message.
                audio_url = bot_response_dict.get('audio_url')
                if audio_url:
                    # Ensure the URL is absolute
                    if audio_url.startswith('/'):
                        base_url = request.url_root.rstrip('/')
                        absolute_audio_url = f"{base_url}{audio_url}"
                    else:
                        absolute_audio_url = audio_url

                    audio_message_params = {
                        'from_': to_number_raw,
                        'to': from_number_raw,
                        'media_url': [absolute_audio_url]
                    }
                    current_app.logger.debug(f"Sending WhatsApp audio params: {audio_message_params}")
                    audio_message = twilio_client.messages.create(**audio_message_params)
                    _log(
                        "info",
                        "Mensaje de audio enviado a %s, SID: %s",
                        from_number_raw,
                        audio_message.sid,
                    )

        except Exception as e:
            _log("error", "Error al enviar mensaje de Twilio: %s", e, exc_info=True)
    else:
        _log("warning", "Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    # Schedule delayed menu or follow-up payload if requested
    if (
        twilio_client
        and bot_response_dict.get("delayed_payload")
        and bot_response_dict.get("delay_seconds")
    ):
        _send_delayed_payload(
            twilio_client,
            to_number_raw,
            from_number_raw,
            bot_response_dict["delayed_payload"],
            bot_response_dict["delay_seconds"],
            current_app._get_current_object()
        )

    return "OK", 200


@webhook_bp.route("/twilio/whatsapp/status", methods=["POST"])
def twilio_whatsapp_status():
    """Log WhatsApp delivery status callbacks from Twilio."""
    if TWILIO_AUTH_TOKEN:
        status_validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not status_validator.validate(
            request.url, request.form, request.headers.get("X-Twilio-Signature", "")
        ):
            return "Forbidden", 403

    message_sid = request.form.get("MessageSid")
    message_status = request.form.get("MessageStatus")
    error_code = request.form.get("ErrorCode")
    error_message = request.form.get("ErrorMessage")
    to_number = request.form.get("To")
    from_number = request.form.get("From")

    current_app.logger.info(
        "[TWILIO_WHATSAPP_STATUS] MessageSid=%s Status=%s ErrorCode=%s ErrorMessage=%s To=%s From=%s",
        message_sid,
        message_status,
        error_code,
        error_message,
        to_number,
        from_number,
    )
    return "OK", 200

from flask import Blueprint, request, jsonify, abort, current_app, g, has_app_context, has_request_context  # Basic Flask components
from twilio.request_validator import RequestValidator  # For validating Twilio requests
from twilio.rest import Client  # For sending messages via Twilio
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import logging
import os  # For accessing environment variables
import requests
import io
import json
import threading
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
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
    ProviderSender,
    Notification,
    MessageTemplateRegistry,
    MessagingEventLedger,
    WhatsAppFlowInteraction,
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
from services.whatsapp_assisted_intake import create_whatsapp_assisted_intake
from services.bounded_media import MediaDownloadTooLarge, read_bounded_response_body
from services.whatsapp_inbound_content import (
    classify_twilio_whatsapp_payload,
    honest_unprocessable_reply,
    is_audio_media_type,
)
from utils.maps_utils import extraer_coordenadas_de_url_google_maps
from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import CONTEXTO_MUNICIPIO
from services.config_loader import cargar_configuracion_pyme
from services.response_formatter import repair_common_mojibake, render_audio_text
from services.tts_orchestrator import generar_audio
from utils.response_utils import normalize_response_payload
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback
from utils.roles import is_authorized_superadmin_email
from services.contact_service import resolve_contact, sanitize_profile_name
from services.ticket_service import build_whatsapp_ticket_effect_key, servicio_tickets
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
    tenant_owner_ids,
)
from services.reclamo_turn_semantics import (
    ReclamoTurnIntent,
    classify_reclamo_confirmation_turn,
)
from services.whatsapp_receipts import CLAIM_FOLLOWUP_WINDOW_SECONDS
from services.crm_intelligence import record_contact_interaction, resolve_or_create_contact
from services.demo_surveys import build_demo_survey_chat_menu
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from services.whatsapp_flow_submissions import (
    FlowSubmissionValidationError,
    has_whatsapp_flow_submission,
    parse_whatsapp_flow_submission,
    persistence_safe_flow_submission,
    safe_twilio_form_metadata,
)
from services.whatsapp_flow_security import (
    WhatsAppFlowTokenError,
    consume_whatsapp_flow_interaction,
    verify_whatsapp_flow_token,
)
from services.meta_flow_data_exchange import MetaFlowActionError
from services.meta_flow_runtime import (
    CLAIM_EVIDENCE_FLOW_ID,
    CLAIM_FLOW_ID,
    ORDER_FLOW_ID,
    SURVEY_FLOW_ID,
    apply_whatsapp_flow_completion,
    record_whatsapp_flow_completion_rejection,
    replay_whatsapp_flow_completion,
)
from services.twilio_tech_provider import (
    TwilioRuntimeCredentials,
    resolve_twilio_runtime_credentials,
)
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
TWILIO_STATUS_CALLBACK_CONNECTION_OVERRIDES = "rc=2&rp=5xx,ct,rt"
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
ACTIVE_NATIVE_FLOW_STATUSES = {"approved", "active"}
ACTIVE_META_FLOW_STATUSES = {"approved", "active", "published"}
MUNICIPAL_TICKET_CLOSED_STATES = {
    "anulado",
    "archivado",
    "archived",
    "canceled",
    "cancelada",
    "cancelado",
    "cancelled",
    "cerrada",
    "cerrado",
    "closed",
    "completada",
    "completado",
    "completed",
    "finalizada",
    "finalizado",
    "resolved",
    "resuelta",
    "resuelto",
}


class WhatsAppOutboundPolicyError(RuntimeError):
    whatsapp_outbound_permanent = True

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WhatsAppDurableReplayError(RuntimeError):
    """A swallowed legacy failure that must retry in durable replay mode."""


def _durable_replay_failure_or_retry(
    durable_replay: bool,
    code: str,
    exc: Exception,
) -> Tuple[str, int]:
    """Raise inside the queue worker; ask the provider to retry direct mode."""

    if durable_replay:
        raise WhatsAppDurableReplayError(code) from exc
    return "RETRY", 503


def _safe_session_context_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"valid": False, "key_count": 0}
    history = value.get("historial_chat")
    return {
        "valid": True,
        "key_count": len(value),
        "history_items": len(history) if isinstance(history, list) else 0,
        "has_flow_submission": isinstance(value.get("last_whatsapp_flow_submission"), dict),
        "has_municipio_context": isinstance(value.get(CONTEXTO_MUNICIPIO), dict),
    }
SENSITIVE_ACTION_CONFIRM_ACCEPT = {"1", "si", "sí", "confirmar", "ok", "dale"}
SENSITIVE_ACTION_CONFIRM_REJECT = {"2", "no", "cancelar", "menu", "menú"}
GENERIC_CONTACT_NAMES = {"vecino", "vecina", "vecino/a", "usuario", "anonimo", "anonimo/a"}
CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER = "+18564858589"
CHATBOC_DEMO_DEFAULT_RESET_WHATSAPP_NUMBER = "+5492613168608"
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


def _configured_chatboc_demo_reset_numbers() -> Set[str]:
    configured_numbers = current_app.config.get("CHATBOC_DEMO_RESET_WHATSAPP_NUMBERS")
    if configured_numbers is None:
        configured_numbers = os.getenv("CHATBOC_DEMO_RESET_WHATSAPP_NUMBERS")
    raw_numbers = (
        configured_numbers
        if configured_numbers not in (None, "")
        else CHATBOC_DEMO_DEFAULT_RESET_WHATSAPP_NUMBER
    )
    if isinstance(raw_numbers, str):
        candidates = re.split(r"[,;\s]+", raw_numbers)
    else:
        candidates = list(raw_numbers or [])
    normalized = {_normalize_whatsapp_address(candidate) for candidate in candidates}
    return {candidate for candidate in normalized if candidate}


def _configured_chatboc_demo_unlimited_numbers() -> Set[str]:
    configured_numbers = current_app.config.get("CHATBOC_DEMO_UNLIMITED_WHATSAPP_NUMBERS")
    if configured_numbers is None:
        configured_numbers = os.getenv("CHATBOC_DEMO_UNLIMITED_WHATSAPP_NUMBERS")

    raw_numbers: Any = (
        configured_numbers
        if configured_numbers not in (None, "")
        else CHATBOC_DEMO_DEFAULT_RESET_WHATSAPP_NUMBER
    )

    if isinstance(raw_numbers, str):
        candidates = re.split(r"[,;\s]+", raw_numbers)
    else:
        candidates = list(raw_numbers or [])
    candidates.append(CHATBOC_DEMO_DEFAULT_RESET_WHATSAPP_NUMBER)
    normalized = {_normalize_whatsapp_address(candidate) for candidate in candidates}
    return {candidate for candidate in normalized if candidate}


def _can_reset_chatboc_demo_usage(from_number: Optional[str]) -> bool:
    normalized = _normalize_whatsapp_address(from_number)
    return bool(normalized and normalized in _configured_chatboc_demo_reset_numbers())


def _can_use_chatboc_demo_unlimited(from_number: Optional[str]) -> bool:
    normalized = _normalize_whatsapp_address(from_number)
    return bool(normalized and normalized in _configured_chatboc_demo_unlimited_numbers())


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
    desired_role = "super_admin" if is_authorized_superadmin_email(email) else "admin"
    if not owner:
        owner = User(
            name="Chatboc Demo Owner",
            email=email,
            rol=desired_role,
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
    if owner.rol != desired_role:
        owner.rol = desired_role
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
            fecha_aceptacion_terminos=datetime.now(timezone.utc),
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
    wants_contact = _is_chatboc_demo_limit_contact_yes(action_id)
    if wants_contact:
        tags.extend(["lead_caliente", "quiere_contacto"])
    if inquiry_type:
        tags.append(inquiry_type)
    if content_type and content_type not in {"text", "event"}:
        tags.append(f"entrada_{content_type}")
    preferences = {
        "preferred_channel": "whatsapp",
        "source": "chatboc_demo_whatsapp_hub",
        "legacy_user_id": getattr(contact_user, "id", None),
        "marketing_consent_status": "opted_in" if wants_contact else "unknown",
        "commercial_contact_requested": wants_contact,
        "lead_temperature": "hot" if wants_contact else "warm",
        "service_window_source": "inbound_whatsapp",
        "last_demo_input_type": content_type,
        "last_demo_input_summary": input_context.get("summary"),
        "last_demo_media": media_context,
        "last_demo_location": location_context,
        "whatsapp_service_window_until": (
            datetime.now(timezone.utc) + timedelta(hours=24)
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
            last_interaction_at=datetime.now(timezone.utc),
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
        contact.last_interaction_at = datetime.now(timezone.utc)
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
    if _is_chatboc_demo_limit_contact_yes(action):
        return "interes_comercial"
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
    if is_audio_media_type(mime_type):
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
            f"Llegaste al limite de {limit} mensajes de prueba por WhatsApp para nuestros rubros demo. "
            "Guarde tu consulta para que el equipo de Chatboc la revise."
            f"{ticket_line}\n\n"
            "Te interesa implementar Chatboc en tu empresa, colegio o municipio? "
            "Podemos contactarte y dejarte una prueba guiada."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Si, quiero que me contacten", "chatboc_demo_limit_contact_yes"),
            _chatboc_demo_option("No por ahora", "chatboc_demo_limit_contact_no"),
            _chatboc_demo_option("Abrir demo web", url="https://www.chatboc.ar/demo"),
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


def _build_chatboc_demo_limit_contact_yes_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    name = getattr(contact_user, "name", None) or "prospecto"
    ticket_ref = f"#{ticket.nro_ticket}" if ticket else "registrado"
    return {
        "success": True,
        "message_body": (
            f"Perfecto, {name}. Te marque como lead prioritario en Chatboc.\n"
            f"Ticket interno CRM: {ticket_ref}\n\n"
            "El equipo comercial puede contactarte por WhatsApp para armar una prueba guiada, "
            "ver rubro, volumen de mensajes y caso de uso."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Abrir demo web", url="https://www.chatboc.ar/demo"),
        ],
        "fuente": "chatboc_demo_limit_contact_yes",
        "data": {
            "lead": {
                "ticket_id": getattr(ticket, "id", None),
                "ticket_number": getattr(ticket, "nro_ticket", None),
                "status": getattr(ticket, "estado", None),
                "estado_cliente": getattr(ticket, "estado_cliente", None),
                "commercial_contact_requested": True,
            }
        },
        "skip_audio_generation": True,
    }


def _build_chatboc_demo_limit_contact_no_payload(ticket: Optional[PymeTicket]) -> Dict[str, Any]:
    ticket_ref = f"\nTicket interno CRM: #{ticket.nro_ticket}" if ticket else ""
    return {
        "success": True,
        "message_body": (
            "Listo, no te marcamos como contacto comercial prioritario."
            f"{ticket_ref}\n\n"
            "Podes seguir probando desde la demo web sin consumir mas mensajes de WhatsApp."
        ),
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("Abrir demo web", url="https://www.chatboc.ar/demo"),
        ],
        "fuente": "chatboc_demo_limit_contact_no",
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
    usage["last_message_at"] = datetime.now(timezone.utc).isoformat()
    session_context.context_data["chatboc_demo_usage"] = usage
    safe_flag_modified(session_context, "context_data")
    db.session.add(session_context)
    return usage["message_count"], limit


def _chatboc_demo_usage_snapshot(session_context: ChatSessionContext) -> Tuple[int, int]:
    limit = _chatboc_demo_max_messages()
    if not isinstance(session_context.context_data, dict):
        return 0, limit
    usage = session_context.context_data.get("chatboc_demo_usage")
    if not isinstance(usage, dict):
        return 0, limit
    try:
        used = int(usage.get("message_count") or 0)
    except (TypeError, ValueError):
        used = 0
    return max(used, 0), limit


def _is_chatboc_demo_reset_turn(
    *,
    selected_action_id: Optional[str],
    selected_option: Optional[Dict[str, Any]],
    message_body: str,
    over_limit: bool,
) -> bool:
    action = _normalize_chatboc_demo_text(selected_action_id)
    text = _normalize_chatboc_demo_text(message_body)
    option_text = _normalize_chatboc_demo_text((selected_option or {}).get("texto"))
    reset_actions = {"menu", "menu_principal", "main_menu", "cancelar"}
    reset_texts = {"menu", "volver", "inicio", "cancelar", "salir", "reset", "reiniciar"}
    greeting_texts = {"hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches"}
    if action in reset_actions:
        return True
    if text in reset_texts or option_text in reset_texts:
        return True
    return over_limit and text in greeting_texts


def _reset_chatboc_demo_usage_for_navigation(
    session_context: ChatSessionContext,
    *,
    reason: str,
) -> None:
    limit = _chatboc_demo_max_messages()
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    usage = session_context.context_data.get("chatboc_demo_usage")
    if not isinstance(usage, dict):
        usage = {"channel": "whatsapp"}
    usage.update(
        {
            "channel": "whatsapp",
            "message_count": 0,
            "limit": limit,
            "remaining": limit,
            "last_reset_at": datetime.now(timezone.utc).isoformat(),
            "last_reset_reason": reason,
        }
    )
    session_context.context_data["chatboc_demo_usage"] = usage
    session_context.context_data.pop("chatboc_demo_active_sector", None)
    session_context.context_data.pop("chatboc_demo_last_action", None)
    session_context.context_data.pop("pending_sensitive_action", None)
    safe_flag_modified(session_context, "context_data")
    db.session.add(session_context)


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

    wants_contact = _is_chatboc_demo_limit_contact_yes(action_id)
    if wants_contact:
        if ticket:
            ticket.estado_cliente = "quiere_contacto"
            ticket.estado = ticket.estado or "nuevo"
            db.session.add(ticket)
            db.session.add(
                TicketComentario(
                    pyme_ticket_id=ticket.id,
                    comentario=(
                        "[whatsapp_demo_hub] lead_limit_contact=yes | "
                        "El prospecto llego al limite de la demo y pidio contacto comercial."
                    ),
                    anon_id=from_number,
                    origen="whatsapp",
                    estado_ticket=ticket.estado,
                )
            )
        if contact_user:
            contact_user.acepta_marketing = True
            contact_user.tags = _append_tag(contact_user.tags, "lead_caliente", "quiere_contacto")
            db.session.add(contact_user)

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
        notification_subject = (
            "Lead caliente WhatsApp demo Chatboc"
            if wants_contact
            else "Nuevo contacto WhatsApp demo Chatboc"
        )
        notification_body = (
            f"{getattr(contact_user, 'name', None) or 'Prospecto'} pidio contacto comercial "
            f"desde {from_number}. Ticket interno: {getattr(ticket, 'nro_ticket', None) or '-'}."
            if wants_contact
            else (
                f"{getattr(contact_user, 'name', None) or 'Prospecto'} escribio desde "
                f"{from_number}. Ticket interno: {getattr(ticket, 'nro_ticket', None) or '-'}."
            )
        )
        notification = Notification(
            tenant_id=tenant.id,
            user_id=getattr(owner_user, "id", None),
            channel="in_app",
            recipient=getattr(owner_user, "email", None) or "superadmin",
            subject=notification_subject,
            body=notification_body,
            status="queued",
            idempotency_key=f"chatboc_demo_whatsapp:{notification_key}",
            metadata_json={
                "source": "whatsapp_demo_hub",
                "phone": from_number,
                "profile_name": profile_name,
                "action_id": action_id,
                "content_type": content_type,
                "input_summary": input_summary,
                "commercial_contact_requested": wants_contact,
                "lead_temperature": "hot" if wants_contact else "warm",
                "media": input_context.get("media"),
                "location": input_context.get("location"),
                "ticket_id": getattr(ticket, "id", None),
                "ticket_number": getattr(ticket, "nro_ticket", None),
            },
        )
        db.session.add(notification)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log(
            "error",
            "[CHATBOC_DEMO_HUB] Failed to record engagement error_type=%s",
            type(exc).__name__,
        )
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


def _apply_chatboc_demo_interactive_list(
    payload: Dict[str, Any],
    *,
    title: str = "Opciones Chatboc",
    button_text: str = "Ver opciones",
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    rows: list[dict[str, str]] = []
    for option in payload.get("options_list") or []:
        if not isinstance(option, dict):
            continue
        action_id = option.get("action_id") or option.get("id")
        if not action_id:
            continue
        label = str(option.get("texto") or option.get("label") or "").strip()
        if not label:
            continue
        rows.append(
            {
                "id": str(action_id),
                "title": label,
                "description": str(option.get("description") or "").strip(),
            }
        )

    if not rows:
        return payload

    payload["interactive_list_sections"] = [{"title": title, "rows": rows[:10]}]
    payload["interactive_list_button_text"] = button_text
    payload["_force_whatsapp_interactive"] = True
    payload["message_type"] = "interactive_list"
    return payload


def _normalize_chatboc_demo_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text)


def _chatboc_demo_has_any(text: str, *keywords: str) -> bool:
    normalized = _normalize_chatboc_demo_text(text)
    return any(keyword in normalized for keyword in keywords)


def _is_chatboc_demo_limit_contact_yes(action_id: Optional[str]) -> bool:
    return _normalize_chatboc_demo_text(action_id) in {
        "chatboc_demo_limit_contact_yes",
        "chatboc_sales_lead",
        "capturar_lead_comercial",
    }


def _resolve_chatboc_demo_limit_decision_action(message_body: str) -> Optional[str]:
    text = _normalize_chatboc_demo_text(message_body)
    if text in {"1", "si", "s", "yes", "ok", "dale", "contactame", "contactenme"}:
        return "chatboc_demo_limit_contact_yes"
    if text in {"0", "2", "no", "n", "cancelar", "salir"}:
        return "chatboc_demo_limit_contact_no"
    if _chatboc_demo_has_any(text, "interesa", "contratar", "ventas", "asesor", "contacto"):
        return "chatboc_demo_limit_contact_yes"
    return None


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


def _chatboc_demo_tracking_ref(prefix: str, ticket: Optional[PymeTicket]) -> str:
    number = getattr(ticket, "nro_ticket", None) or getattr(ticket, "id", None) or "demo"
    return f"{prefix}-{number}"


def _chatboc_demo_template_pre_message(template_name: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        str(key): "" if value is None else str(value)
        for key, value in (variables or {}).items()
    }
    return {
        "template_name": template_name,
        "content_variables": normalized,
        "channels": ["whatsapp"],
    }


def _attach_chatboc_demo_template(
    payload: Dict[str, Any],
    template_name: str,
    variables: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    current_entries = payload.get("_twilio_pre_messages")
    entries = list(current_entries) if isinstance(current_entries, list) else []
    entries.append(_chatboc_demo_template_pre_message(template_name, variables))
    payload["_twilio_pre_messages"] = entries
    return payload


def _chatboc_demo_template_action_alias(action: str, active_sector: str) -> str:
    normalized = str(action or "").strip()
    sector = active_sector if active_sector in {"gobierno", "educacion", "empresas"} else ""

    if normalized == "open_case":
        return {
            "gobierno": "chatboc_demo_claim_start",
            "educacion": "chatboc_demo_school_start",
            "empresas": "chatboc_demo_order_start",
        }.get(sector, "chatboc_demo_claim_start")
    if normalized == "commerce":
        return "chatboc_demo_order_start"
    if normalized == "human_handoff":
        return "chatboc_sales_lead"
    if normalized == "view_case":
        return {
            "gobierno": "chatboc_demo_claim_start",
            "educacion": "chatboc_demo_school_start",
            "empresas": "chatboc_demo_order_confirm",
        }.get(sector, "chatboc_demo_claim_start")
    if normalized == "attach_info":
        return {
            "gobierno": "chatboc_demo_claim_start",
            "educacion": "chatboc_demo_school_start",
            "empresas": "chatboc_demo_order_start",
        }.get(sector, "chatboc_demo_claim_start")
    if normalized == "cancel":
        return "cancelar"

    return normalized


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
    payload = {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": [
            _chatboc_demo_option("🏛️ Gobiernos y municipios", "chatboc_demo:gobierno"),
            _chatboc_demo_option("🎓 Colegio y familias", "chatboc_demo:educacion"),
            _chatboc_demo_option("🛍️ Empresa y pedidos", "chatboc_demo:empresas"),
            _chatboc_demo_option("🗳️ Encuestas en vivo", "chatboc_surveys"),
            _chatboc_demo_option("💬 Hablar con ventas", "chatboc_sales_lead"),
        ],
        "fuente": "chatboc_demo_whatsapp_hub",
        "skip_audio_generation": True,
    }
    _apply_chatboc_demo_interactive_list(payload, title="Demos Chatboc", button_text="Elegir demo")
    return _attach_chatboc_demo_template(
        payload,
        "chatboc_welcome_menu_v2",
        {"1": contact_name or "ahi", "2": "Chatboc.ar"},
    )


def _build_chatboc_demo_sector_payload(sector: str) -> Dict[str, Any]:
    labels = {
        "gobierno": (
            "🏛️ Gobiernos y municipios",
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
    payload = {
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
    _apply_chatboc_demo_interactive_list(payload, title="Acciones demo", button_text="Elegir accion")
    if sector == "empresas":
        return _attach_chatboc_demo_template(
            payload,
            "chatboc_pyme_catalog_invite_v1",
            {"1": "Chatboc Empresas", "2": "bodega"},
        )
    return payload


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

    payload = {
        "success": True,
        "message_body": body,
        "message_type": "text",
        "options_list": options,
        "fuente": "chatboc_demo_survey_link",
        "skip_audio_generation": True,
    }
    if mode != "chatboc_survey_share":
        return _attach_chatboc_demo_template(
            payload,
            "chatboc_survey_invite_v2",
            {"1": _demo_sector_label(sector), "2": slug},
        )
    return payload


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
    payload = {
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
    _apply_chatboc_demo_interactive_list(payload, title="Pedido demo", button_text="Pedido demo")
    return _attach_chatboc_demo_template(
        payload,
        "chatboc_pyme_catalog_invite_v1",
        {"1": "Chatboc Empresas", "2": "bodega"},
    )


def _build_chatboc_demo_order_confirm_payload(
    contact_user: Optional[User],
    ticket: Optional[PymeTicket],
) -> Dict[str, Any]:
    ticket_ref = getattr(ticket, "nro_ticket", None) or "demo"
    name = getattr(contact_user, "name", None) or "cliente"
    payload = {
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
    _apply_chatboc_demo_interactive_list(payload, title="Despues del pedido", button_text="Ver acciones")
    return _attach_chatboc_demo_template(
        payload,
        "chatboc_order_checkout_v1",
        {"1": f"D-{ticket_ref}", "2": "$25.000", "3": "demo"},
    )


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
    payload = {
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
    _apply_chatboc_demo_interactive_list(payload, title="Reclamo demo", button_text="Ver acciones")
    claim_ref = _chatboc_demo_tracking_ref("REC", ticket)
    return _attach_chatboc_demo_template(
        payload,
        "chatboc_gov_claim_created_v2",
        {"1": claim_ref, "2": claim_ref},
    )


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
    payload = {
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
    _apply_chatboc_demo_interactive_list(payload, title="Colegio demo", button_text="Ver acciones")
    return _attach_chatboc_demo_template(
        payload,
        "chatboc_school_family_case_created_v1",
        {
            "1": _chatboc_demo_tracking_ref("ESC", ticket),
            "2": getattr(contact_user, "name", None) or "familia demo",
            "3": "recibido",
        },
    )


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
    payload = {
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
    return _attach_chatboc_demo_template(
        payload,
        "chatboc_handoff_v1",
        {"1": ticket_ref},
    )


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
    action = _chatboc_demo_template_action_alias(action, active_sector)
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
        "cancelar",
        "salir",
        "reset",
        "reiniciar",
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
    if action == "chatboc_demo_limit_contact_yes":
        return _build_chatboc_demo_limit_contact_yes_payload(contact_user, ticket)
    if action == "chatboc_demo_limit_contact_no":
        return _build_chatboc_demo_limit_contact_no_payload(ticket)
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


_LOG_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>'\"]+")
_LOG_EMAIL_RE = re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b")
_LOG_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_LOG_PROVIDER_ID_RE = re.compile(
    r"\b(?:AC|AP|CH|FW|HX|MG|MM|PN|SM|VA|WT|ZS)[A-Za-z0-9_-]{12,}\b"
)
_LOG_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|auth[_-]?token|access[_-]?token|flow[_-]?token|token|secret|signature|password)"
    r"\s*[:=]\s*[^\s,;]+"
)
_LOG_CITIZEN_SECRET_RE = re.compile(
    r"(?i)\b(pin|dni)\s*[:=#-]?\s*[A-Za-z0-9-]{4,}\b"
)
_LOG_COORDINATE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(lat|latitude|lon|lng|longitude)\s*[:=]\s*[-+]?\d{1,3}(?:\.\d+)?"
)
_LOG_COORDINATE_PAIR_RE = re.compile(
    r"(?<!\d)[+-]?(?:\d{1,2}(?:\.\d{3,})|1[0-7]\d(?:\.\d{3,})?)"
    r"\s*[,;/]\s*"
    r"[+-]?(?:\d{1,2}(?:\.\d{3,})|1[0-7]\d(?:\.\d{3,})?)(?!\d)"
)
_LOG_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")


def _safe_provider_reference(value: Any) -> Optional[str]:
    """Return a stable, non-reversible hint for a provider identifier."""

    text = str(value or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"sha256:{digest}"


def _safe_log_value(value: Any) -> str:
    """Return bounded diagnostics without user content, credentials or identifiers.

    This is the final guard for ``_log``.  Call sites must still avoid passing
    arbitrary message bodies; mappings and sequences are summarized instead of
    serialized so a future context field cannot silently become a PII leak.
    """

    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, dict):
        return f"dict(count={len(value)})"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"{type(value).__name__}(count={len(value)})"
    try:
        text = str(value)
    except Exception:
        return type(value).__name__
    text = text[:1000]
    text = _LOG_URL_RE.sub("[redacted-url]", text)
    text = _LOG_EMAIL_RE.sub("[redacted-email]", text)
    text = _LOG_BEARER_RE.sub("Bearer [redacted]", text)
    text = _LOG_SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _LOG_CITIZEN_SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _LOG_COORDINATE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    text = _LOG_PROVIDER_ID_RE.sub("[redacted-provider-id]", text)
    text = _LOG_COORDINATE_PAIR_RE.sub("[redacted-coordinates]", text)
    text = _LOG_PHONE_RE.sub("[redacted-number]", text)
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _safe_outbound_log_metadata(params: Any) -> dict[str, Any]:
    """Describe an outbound payload without bodies, recipients or media URLs."""

    if not isinstance(params, dict):
        return {"valid": False}
    body = params.get("body")
    media = params.get("media_url")
    persistent = params.get("persistent_action")
    return {
        "valid": True,
        "body_length": len(body) if isinstance(body, str) else 0,
        "media_count": len(media) if isinstance(media, (list, tuple)) else int(bool(media)),
        "interactive_count": (
            len(persistent)
            if isinstance(persistent, (list, tuple))
            else int(bool(persistent))
        ),
        "has_template": bool(params.get("content_sid")),
        "has_status_callback": bool(params.get("status_callback")),
    }


def _log(level: str, message: str, *args: Any, exc_info: bool = False) -> None:
    target_logger = current_app.logger if has_app_context() else logger
    safe_message = _safe_log_value(message)
    safe_args = tuple(_safe_log_value(arg) for arg in args)
    # Exception messages routinely include requested URLs and provider response
    # bodies.  Log the sanitized exception type above, never the raw traceback.
    getattr(target_logger, level)(safe_message, *safe_args, exc_info=False)


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


def _mark_welcome_outbound_submission(
    state: dict[str, Any],
    provider_message: Any,
    *,
    timestamp: float,
) -> str:
    """Record staging/acceptance without claiming final provider delivery."""

    state["last_attempt_ts"] = timestamp
    state["last_provider_message_ref"] = _safe_provider_reference(
        getattr(provider_message, "sid", None)
    )
    provider_status = str(getattr(provider_message, "status", "") or "").lower()
    if provider_status == "queued":
        state["last_durably_staged_ts"] = timestamp
        return "durably_staged"
    state["last_provider_accepted_ts"] = timestamp
    return "provider_accepted"


def _send_welcome_sticker(
    *,
    client,
    to_number_raw: str,
    from_number_raw: str,
    resolved_sticker_url: Optional[str],
    session_context: ChatSessionContext,
    state_key: str,
) -> bool:
    if not client or not resolved_sticker_url:
        return False
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    welcome_state = session_context.context_data.setdefault("_welcome_state", {})
    sticker_state = welcome_state.setdefault("sticker", {})
    if sticker_state.get("disabled") or sticker_state.get(state_key):
        return False
    try:
        provider_message = _send_twilio_message(
            client,
            from_=to_number_raw,
            to=from_number_raw,
            media_url=[resolved_sticker_url],
        )
        accepted_ts = time.time()
        sticker_state[state_key] = accepted_ts
        submission_state = _mark_welcome_outbound_submission(
            sticker_state,
            provider_message,
            timestamp=accepted_ts,
        )
        safe_flag_modified(session_context, "context_data")
        _log(
            "info",
            "[WELCOME] Sticker submitted state=%s outcome=%s",
            state_key,
            submission_state,
        )
        return True
    except Exception as exc:
        sticker_state["disabled"] = True
        safe_flag_modified(session_context, "context_data")
        _log(
            "warning",
            "[WELCOME] Failed to send welcome sticker state=%s error_type=%s",
            state_key,
            type(exc).__name__,
        )
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
    durable_claim: Any = None,
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

    ticket = servicio_tickets.crear_nuevo_ticket(
        tipo_ticket,
        ticket_data,
        **_durable_whatsapp_ticket_effect_kwargs(
            durable_claim,
            getattr(tenant_profile, "id", None),
            "education_case_create",
        ),
    )
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
        except Exception as exc:
            _log(
                "error",
                "[EDUCATION_WHATSAPP] No se pudo asociar adjunto al ticket escolar error_type=%s",
                type(exc).__name__,
            )
            if has_app_context() and getattr(
                g,
                "whatsapp_outbound_collector",
                None,
            ) is not None:
                raise
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
    durable_claim: Any = None,
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
            durable_claim=durable_claim,
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
    tenant_profile: Optional[TenantProfile] = None,
) -> Tuple[Optional[str], Optional[MunicipioTicket | PymeTicket]]:
    if not owner_user:
        return None, None

    tipo_chat = getattr(owner_user, "tipo_chat", None)
    if tipo_chat == "municipio":
        resolved_tenant = tenant_profile
        if resolved_tenant is None:
            owner_id = getattr(owner_user, "municipio_id", None) or getattr(owner_user, "id", None)
            try:
                resolution = resolve_unique_tenant_for_owner(owner_id)
            except TicketTenantScopeError:
                resolution = None
            if resolution is not None and resolution.status == "unique":
                resolved_tenant = resolution.tenant
        if resolved_tenant is None:
            current_app.logger.warning(
                "[WHATSAPP_WEBHOOK] Live chat municipal lookup rejected without exact tenant owner=%s",
                getattr(owner_user, "id", None),
            )
            return "municipio", None
        query = scoped_municipio_ticket_query(resolved_tenant).filter(
            MunicipioTicket.estado.in_(LIVE_CHAT_STATES)
        )
        if end_user:
            query = query.filter(or_(MunicipioTicket.user_id == end_user.id, MunicipioTicket.anon_id == anon_id))
        elif anon_id:
            query = query.filter(MunicipioTicket.anon_id == anon_id)
        return "municipio", query.order_by(MunicipioTicket.fecha.desc()).first()

    if tipo_chat == "pyme":
        resolved_tenant = tenant_profile or _tenant_profile_for_user(owner_user)
        tenant_id = getattr(resolved_tenant, "id", None)
        if not tenant_id:
            current_app.logger.warning(
                "[WHATSAPP_WEBHOOK] Live chat PyME ticket lookup rejected without exact tenant owner=%s",
                getattr(owner_user, "id", None),
            )
            return "pyme", None
        query = PymeTicket.query.filter(PymeTicket.estado.in_(LIVE_CHAT_STATES))
        if end_user:
            query = query.filter(or_(PymeTicket.user_id == end_user.id, PymeTicket.anon_id == anon_id))
        elif anon_id:
            query = query.filter(PymeTicket.anon_id == anon_id)
        query = query.filter(PymeTicket.tenant_id == tenant_id)
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

    tenant = None
    if tenant_id:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            return None
    elif municipio_id:
        try:
            resolution = resolve_unique_tenant_for_owner(municipio_id)
        except TicketTenantScopeError:
            return None
        tenant = resolution.tenant if resolution.status == "unique" else None
    if tenant is None:
        return None
    if municipio_id:
        try:
            normalized_owner_id = int(municipio_id)
        except (TypeError, ValueError):
            return None
        if normalized_owner_id not in tenant_owner_ids(tenant):
            return None

    base_query = scoped_municipio_ticket_query(tenant)

    ticket = base_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
    if ticket:
        return ticket

    if anon_id:
        anon_query = base_query.filter(MunicipioTicket.anon_id == anon_id)
        ticket = anon_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
        if ticket:
            return ticket

    return None


def _active_municipal_ticket_followup(
    context_data: Any,
    *,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return a bounded, ticket-specific follow-up receipt from session state."""

    if not isinstance(context_data, dict):
        return None
    now_value = time.time() if now_ts is None else float(now_ts)
    raw_followup = context_data.get("active_ticket_followup")
    followup = dict(raw_followup) if isinstance(raw_followup, dict) else {}

    # Backwards compatibility for receipts created before the structured
    # follow-up context existed.  The old photo window is already bounded and
    # therefore safe to reuse; ``last_ticket_code`` alone is never enough.
    if not followup:
        legacy_until = context_data.get("awaiting_ticket_photo_until")
        legacy_active = bool(context_data.get("awaiting_ticket_photo"))
        if legacy_active and isinstance(legacy_until, (int, float)):
            followup = {
                "ticket_nro": (
                    context_data.get("last_ticket_code")
                    or context_data.get("awaiting_photo_for_ticket")
                ),
                "consulta_pin": context_data.get("latest_ticket_pin"),
                "tracking_url": context_data.get("latest_tracking_url"),
                "until": legacy_until,
            }

    ticket_nro = str(followup.get("ticket_nro") or "").strip()
    until = followup.get("until")
    if not ticket_nro or not isinstance(until, (int, float)) or now_value > float(until):
        return None
    followup["ticket_nro"] = ticket_nro
    return followup


def _clear_municipal_ticket_followup(context_data: Any) -> None:
    if not isinstance(context_data, dict):
        return
    for key in (
        "active_ticket_followup",
        "awaiting_photo_for_ticket",
        "awaiting_ticket_photo",
        "awaiting_ticket_photo_until",
        "pending_attachment_id",
        "pending_ticket_code",
        "pending_attachment_until",
    ):
        context_data.pop(key, None)


def _municipal_ticket_is_open(ticket: Optional[MunicipioTicket]) -> bool:
    if ticket is None:
        return False
    state = str(getattr(ticket, "estado", "") or "").strip().lower()
    return state not in MUNICIPAL_TICKET_CLOSED_STATES


def _build_municipal_followup_tracking_text(
    followup: Dict[str, Any],
    ticket: Optional[MunicipioTicket],
) -> str:
    ticket_nro = str(
        followup.get("ticket_nro") or getattr(ticket, "nro_ticket", "") or ""
    ).strip()
    pin = str(
        followup.get("consulta_pin") or getattr(ticket, "consulta_pin", "") or ""
    ).strip()
    tracking_url = str(followup.get("tracking_url") or "").strip()
    lines = [f"🔎 *Reclamo {ticket_nro}*"]
    if pin:
        lines.append(f"*PIN:* {pin}")
    if tracking_url:
        lines.append(f"*Seguimiento:* {tracking_url}")
    lines.append("Podés seguir respondiendo acá para sumar información al mismo reclamo.")
    return "\n".join(lines)


def _handle_recent_municipal_ticket_followup(
    *,
    session_context: ChatSessionContext,
    client_user: Optional[User],
    tenant_profile: Optional[TenantProfile],
    end_user: Optional[User],
    from_number_cleaned: str,
    message_body: str,
    action: Optional[str],
    twilio_message_client: Any,
    to_number_raw: str,
    from_number_raw: str,
    durable_claim: Any = None,
    now_ts: Optional[float] = None,
) -> Optional[Tuple[str, int]]:
    """Handle one safe text turn against the newly-created open ticket.

    The structured, expiring receipt is the only implicit binding.  A clear
    new-claim/menu intent exits this mode and continues through the normal LLM
    orchestrator.  Everything executed here is tenant-scoped and validated
    against the persisted ticket before a comment is created.
    """

    if not session_context or not isinstance(session_context.context_data, dict):
        return None
    if not client_user or getattr(client_user, "tipo_chat", None) != "municipio":
        return None

    context_data = session_context.context_data
    municipio_ctx = context_data.get(CONTEXTO_MUNICIPIO)
    if isinstance(municipio_ctx, dict) and (
        (municipio_ctx.get("reclamo_flow_v2") or {}).get("state")
    ):
        return None

    followup = _active_municipal_ticket_followup(context_data, now_ts=now_ts)
    if not followup:
        # Expired structured windows must not linger and later bind evidence to
        # an unrelated ticket.
        if isinstance(context_data.get("active_ticket_followup"), dict):
            _clear_municipal_ticket_followup(context_data)
            safe_flag_modified(session_context, "context_data")
            db.session.add(session_context)
            db.session.commit()
        return None

    decision = classify_reclamo_confirmation_turn(message_body, action=action)
    if decision.intent in {ReclamoTurnIntent.NEW_CLAIM, ReclamoTurnIntent.CANCEL}:
        _clear_municipal_ticket_followup(context_data)
        context_data.pop("last_options_sent", None)
        context_data.pop("pending_sensitive_action", None)
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        return None

    ticket_tenant_id = getattr(tenant_profile, "id", None) or session_context.tenant_id
    municipio_owner_id = (
        getattr(client_user, "municipio_id", None) or getattr(client_user, "id", None)
    )
    ticket = _find_municipio_ticket_for_reference(
        followup["ticket_nro"],
        tenant_id=ticket_tenant_id,
        municipio_id=municipio_owner_id,
        anon_id=from_number_cleaned,
    )
    if not _municipal_ticket_is_open(ticket):
        _clear_municipal_ticket_followup(context_data)
        context_data.pop("last_options_sent", None)
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        return None

    context_data.pop("last_options_sent", None)
    context_data.pop("pending_sensitive_action", None)
    safe_flag_modified(session_context, "context_data")

    if decision.intent is ReclamoTurnIntent.FOLLOW_UP_QUESTION:
        db.session.add(session_context)
        db.session.commit()
        if twilio_message_client:
            _send_twilio_message(
                twilio_message_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=_build_municipal_followup_tracking_text(followup, ticket),
            )
        return "OK", 200

    if decision.intent is ReclamoTurnIntent.ADD_PHOTO:
        db.session.add(session_context)
        db.session.commit()
        if twilio_message_client:
            _send_twilio_message(
                twilio_message_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=(
                    f"Enviá la foto o el audio y lo voy a sumar al reclamo "
                    f"*{followup['ticket_nro']}*."
                ),
            )
        return "OK", 200

    if decision.intent is ReclamoTurnIntent.EDIT and not decision.corrections:
        db.session.add(session_context)
        db.session.commit()
        if twilio_message_client:
            _send_twilio_message(
                twilio_message_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=(
                    "Decime qué dato querés corregir. Lo voy a registrar como "
                    f"actualización del reclamo *{followup['ticket_nro']}*."
                ),
            )
        return "OK", 200

    clean_message = re.sub(r"\s+", " ", str(message_body or "")).strip()
    if not clean_message:
        return None

    if decision.reason == "isolated_emoji" or decision.intent is ReclamoTurnIntent.CONFIRM:
        db.session.add(session_context)
        db.session.commit()
        if twilio_message_client:
            _send_twilio_message(
                twilio_message_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=(
                    f"Recibí tu mensaje. El reclamo *{followup['ticket_nro']}* "
                    "sigue abierto."
                ),
            )
        return "OK", 200

    comment = servicio_tickets.crear_comentario(
        ticket_id=ticket.id,
        tipo_ticket="municipio",
        comentario_data={
            "comentario": clean_message[:4000],
            "user_id": getattr(end_user, "id", None),
            "anon_id": None if end_user else from_number_cleaned,
            "es_admin": False,
            "origen": "whatsapp",
            "estado_ticket": ticket.estado,
            "emit_notifications": False,
            "emit_socket": False,
        },
        **_durable_whatsapp_ticket_effect_kwargs(
            durable_claim,
            ticket_tenant_id,
            f"ticket_followup_comment:{ticket.id}",
        ),
    )
    if comment is None:
        raise RuntimeError("municipal_ticket_followup_comment_not_persisted")
    db.session.add(session_context)
    db.session.commit()
    if twilio_message_client:
        _send_twilio_message(
            twilio_message_client,
            from_=to_number_raw,
            to=from_number_raw,
            body=f"✅ Sumé tu mensaje al reclamo *{followup['ticket_nro']}*.",
        )
    return "OK", 200


def _durable_whatsapp_ticket_effect_kwargs(
    durable_claim: Any,
    tenant_id: Any,
    effect: str,
) -> dict[str, Any]:
    if durable_claim is None:
        return {}
    durable_turn_id = getattr(durable_claim, "turn_id", None)
    key = build_whatsapp_ticket_effect_key(tenant_id, durable_turn_id, effect)
    return {
        "idempotency_key": key,
        "idempotency_tenant_id": int(tenant_id),
    }


def _attach_whatsapp_adjunto_to_ticket(
    adjunto: ArchivoAdjunto,
    ticket: MunicipioTicket,
    end_user: Optional[User],
    comentario_text: str,
    *,
    idempotency_key: Optional[str] = None,
    idempotency_tenant_id: Optional[int] = None,
) -> Optional[TicketComentario]:
    adjunto.municipio_ticket_id = ticket.id
    if hasattr(ticket, "foto_principal"):
        if not ticket.foto_principal:
            ticket.foto_principal = adjunto.url
    elif hasattr(ticket, "foto_url_directa") and not ticket.foto_url_directa:
        ticket.foto_url_directa = adjunto.url

    db.session.add(adjunto)
    db.session.add(ticket)

    return servicio_tickets.crear_comentario(
        ticket_id=ticket.id,
        tipo_ticket="municipio",
        comentario_data={
            "comentario": comentario_text,
            "user_id": end_user.id if end_user else None,
            "es_admin": False,
            "origen": "whatsapp",
            "estado_ticket": ticket.estado,
            "archivo_adjunto_id": adjunto.id,
            "emit_notifications": False,
            "emit_socket": False,
        },
        idempotency_key=idempotency_key,
        idempotency_tenant_id=idempotency_tenant_id,
    )


def _looks_like_ticket_reference(text: str) -> bool:
    if not text:
        return False
    normalized = text.strip().upper()
    return bool(re.search(r"\d{4,}", normalized))


def _is_local_base_url(value: str) -> bool:
    return value.startswith(("http://localhost", "http://127.0.0.1", "http://0.0.0.0"))


def _public_media_base_url(default_base_url: Optional[str] = None) -> str:
    """Return the public backend base URL used by Twilio to download media."""

    fallback = (default_base_url or "").strip().rstrip("/")
    configured_candidates: list[str] = []
    if has_app_context():
        for key in (
            "PUBLIC_API_BASE_URL",
            "BACKEND_URL",
            "API_BASE_URL",
            "BASE_URL",
            "PUBLIC_BASE_URL",
        ):
            value = current_app.config.get(key)
            if not value:
                continue
            candidate = str(value).strip().rstrip("/")
            if not candidate or candidate in configured_candidates:
                continue
            configured_candidates.append(candidate)

    # Twilio must be able to fetch media from the public internet. A stale
    # localhost value must not shadow a later public backend URL.
    for candidate in configured_candidates:
        if not _is_local_base_url(candidate):
            return candidate
    if fallback and not _is_local_base_url(fallback):
        return fallback
    if configured_candidates:
        return configured_candidates[0]
    return fallback


def _normalize_media_base(url: str) -> str:
    base_url = _public_media_base_url()
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


WHATSAPP_FLOW_CONTRACT_VERSION = "whatsapp.flow_contract.v1"

WHATSAPP_FLOW_CONFIG: Dict[str, Dict[str, Any]] = {
    "claim_status": {
        "label": "Ver estado",
        "kind": "claim_status_webview",
        "url_keys": ("ticket_status_url", "tracking_url", "ticket_url", "reclamo_url", "public_status_url"),
        "reply_options": (
            {"texto": "Actualizar reclamo", "action_id": "consultar_estado_reclamo"},
            {"texto": "Menú", "action_id": "menu_principal"},
        ),
    },
    "order_catalog": {
        "label": "Abrir catálogo",
        "kind": "catalog_order_webview",
        "url_keys": (
            "catalog_url",
            "catalogo_url",
            "public_catalog_url",
            "checkout_url",
            "order_url",
            "pedido_url",
            "tracking_url",
        ),
        "reply_options": (
            {"texto": "Buscar producto", "action_id": "ver_catalogo_pyme_buscar_otra"},
            {"texto": "Estado pedido", "action_id": "pyme_estado_pedido"},
        ),
    },
    "survey": {
        "label": "Responder encuesta",
        "kind": "survey_vote_webview",
        "url_keys": ("survey_url", "encuesta_url", "public_survey_url", "share_url", "vote_url"),
        "reply_options": (
            {"texto": "Ver encuestas", "action_id": "mostrar_menu_encuestas"},
            {"texto": "Menú", "action_id": "menu_principal"},
        ),
    },
}


def _safe_whatsapp_webview_url(raw_url: Optional[Any], base_url: str) -> Optional[str]:
    """Resolve a payload webview URL and keep only HTTPS links.

    WhatsApp CTAs are user-visible and may come from tenant configuration or
    LLM-adjacent payloads, so this function intentionally rejects javascript,
    data, protocol-relative and plain HTTP URLs. Relative paths are allowed only
    when the current backend base URL is HTTPS.
    """

    if raw_url is None:
        return None
    candidate = str(raw_url).strip()
    if not candidate or candidate.startswith("//"):
        return None

    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme.lower() != "https":
        return None

    base = (base_url or "").strip().rstrip("/")
    if not parsed.scheme:
        base_parts = urlsplit(base)
        if base_parts.scheme.lower() != "https" or not base_parts.netloc:
            return None

    resolved = _resolve_public_url(candidate, base)
    if not resolved:
        return None

    resolved_parts = urlsplit(resolved)
    if resolved_parts.scheme.lower() != "https" or not resolved_parts.netloc:
        return None
    return urlunsplit(
        (
            "https",
            resolved_parts.netloc,
            resolved_parts.path or "/",
            resolved_parts.query,
            "",
        )
    )


def _payload_value(payload: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        if key in payload and payload.get(key):
            return payload.get(key)

    nested_containers = (
        payload.get("data"),
        payload.get("datos"),
        payload.get("metadata"),
        payload.get("whatsapp_receipt"),
    )
    for container in nested_containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            if container.get(key):
                return container.get(key)
    return None


def _normalize_whatsapp_options(raw_options: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_options, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in raw_options:
        if isinstance(item, dict):
            normalized.append(dict(item))
    return normalized


def _option_identity(option: Dict[str, Any]) -> str:
    for key in ("action_id", "id", "url", "texto", "label", "title"):
        value = option.get(key)
        if value:
            return str(value).strip().lower()
    return ""


def _detect_whatsapp_flow_kind(payload: Dict[str, Any]) -> Optional[str]:
    explicit = (
        payload.get("whatsapp_flow")
        or payload.get("flow_kind")
        or payload.get("template_kind")
    )
    if isinstance(payload.get("webview"), dict):
        explicit = explicit or payload["webview"].get("flow_kind") or payload["webview"].get("kind")
    for option in _normalize_whatsapp_options(payload.get("options_list") or payload.get("botones")):
        explicit = explicit or option.get("flow_kind") or option.get("whatsapp_flow")
    explicit_text = str(explicit or "").strip().lower()
    if explicit_text in WHATSAPP_FLOW_CONFIG:
        return explicit_text
    if explicit_text in {"claim", "reclamo", "estado_reclamo", "ticket_status"}:
        return "claim_status"
    if explicit_text in {"pedido", "order", "catalog", "catalogo", "checkout"}:
        return "order_catalog"
    if explicit_text in {"encuesta", "survey", "votacion", "poll"}:
        return "survey"

    for flow_kind, config in WHATSAPP_FLOW_CONFIG.items():
        if _payload_value(payload, *config["url_keys"]):
            return flow_kind

    structured_hints = {
        "claim_status": ("ticket_id", "ticket_nro", "nro_ticket", "consulta_pin", "reclamo_id"),
        "order_catalog": ("pedido_id", "nro_pedido", "cart_id", "catalog_items", "catalogo"),
        "survey": ("encuesta_id", "survey_id", "poll_id"),
    }
    for flow_kind, keys in structured_hints.items():
        if _payload_value(payload, *keys):
            return flow_kind

    has_url_cta = bool(
        payload.get("webview_url")
        or payload.get("cta_url")
        or any(
            option.get("url")
            for option in _normalize_whatsapp_options(payload.get("options_list") or payload.get("botones"))
        )
    )
    if has_url_cta:
        source_text = " ".join(
            str(payload.get(key) or "")
            for key in ("fuente", "accion_backend", "message_type")
        ).lower()
        if any(token in source_text for token in ("encuesta", "survey", "votacion", "poll")):
            return "survey"
        if any(token in source_text for token in ("catalog", "catalogo", "pedido", "checkout", "carrito")):
            return "order_catalog"
        if any(token in source_text for token in ("reclamo", "ticket", "estado")):
            return "claim_status"
    return None


def _first_safe_payload_url(payload: Dict[str, Any], flow_kind: str, base_url: str) -> Optional[str]:
    config = WHATSAPP_FLOW_CONFIG.get(flow_kind) or {}
    candidates: List[Any] = []
    direct_webview = payload.get("webview")
    if isinstance(direct_webview, dict):
        candidates.extend([direct_webview.get("url"), direct_webview.get("href")])
    candidates.extend(
        [
            payload.get("webview_url"),
            payload.get("cta_url"),
            payload.get("url"),
            _payload_value(payload, *config.get("url_keys", ())),
        ]
    )
    for option in _normalize_whatsapp_options(payload.get("options_list") or payload.get("botones")):
        if option.get("url"):
            candidates.append(option.get("url"))

    for candidate in candidates:
        safe = _safe_whatsapp_webview_url(candidate, base_url)
        if safe:
            return safe
    return None


def _normalize_whatsapp_flow_contract(payload: Dict[str, Any], *, base_url: str) -> Dict[str, Any]:
    """Canonicalize local WhatsApp/webview CTA payload metadata.

    This does not infer user intent. It only upgrades already-structured backend
    responses so WhatsApp, webviews and future frontend clients see the same
    CTA contract with a clean text fallback.
    """

    if not isinstance(payload, dict):
        return payload

    flow_kind = _detect_whatsapp_flow_kind(payload)
    if not flow_kind:
        return payload

    config = WHATSAPP_FLOW_CONFIG[flow_kind]
    safe_url = _first_safe_payload_url(payload, flow_kind, base_url)

    options = _normalize_whatsapp_options(payload.get("options_list"))
    if not options:
        options = _normalize_whatsapp_options(payload.get("botones"))

    existing = {_option_identity(option) for option in options}
    ctas: List[Dict[str, Any]] = []

    if safe_url:
        label = str(payload.get("cta_label") or config["label"]).strip() or config["label"]
        webview = {
            "kind": config["kind"],
            "flow_kind": flow_kind,
            "label": label,
            "url": safe_url,
            "source": "whatsapp_webhook_contract",
        }
        payload["webview"] = webview
        ctas.append({"type": "webview", "label": label, "url": safe_url, "flow_kind": flow_kind})

        url_option = {
            "texto": label,
            "type": "url",
            "url": safe_url,
            "webview": True,
            "flow_kind": flow_kind,
        }
        if _option_identity(url_option) not in existing:
            options.insert(0, url_option)
            existing.add(_option_identity(url_option))

    max_actionable_options = 2 if safe_url else 3
    for reply in config["reply_options"]:
        if len([option for option in options if not option.get("url")]) >= max_actionable_options:
            break
        reply_option = dict(reply)
        reply_option.setdefault("id", reply_option.get("action_id"))
        identity = _option_identity(reply_option)
        if identity and identity not in existing:
            options.append(reply_option)
            existing.add(identity)

    if options:
        payload["options_list"] = options
        payload["botones"] = options

    payload["whatsapp_ctas"] = ctas
    payload["whatsapp_contract"] = {
        "contract_version": WHATSAPP_FLOW_CONTRACT_VERSION,
        "flow_kind": flow_kind,
        "surface": "whatsapp",
        "render_as": "interactive_reply_with_safe_webview_cta" if safe_url else "interactive_reply",
        "webview_safe": bool(safe_url),
        "fallback": {
            "mode": "clean_text_with_url",
            "links": [safe_url] if safe_url else [],
        },
    }

    actionable_count = len([option for option in options if not option.get("url")])
    if actionable_count and len(options) <= 10:
        payload["_force_whatsapp_interactive"] = True
        if actionable_count <= 3:
            payload["message_type"] = "interactive_buttons"
        else:
            payload["message_type"] = "interactive_list"

    return payload


def _resolve_public_media_url(url: Optional[str], default_base_url: Optional[str] = None) -> Optional[str]:
    """Return an absolute backend URL for media downloaded by Twilio."""

    return _resolve_public_url(url, _public_media_base_url(default_base_url))


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


def _resolve_approved_whatsapp_template_sid(
    template_name: Optional[str],
    *,
    tenant_profile: Optional[TenantProfile] = None,
    tenant_id: Optional[int] = None,
    language: str = "es",
) -> Optional[str]:
    """Resolve an approved Twilio ContentSid from the tenant template registry."""

    normalized_name = (template_name or "").strip()
    if not normalized_name:
        return None

    resolved_tenant_id = tenant_id
    if resolved_tenant_id is None and tenant_profile is not None:
        resolved_tenant_id = getattr(tenant_profile, "id", None)

    if resolved_tenant_id:
        try:
            query = MessageTemplateRegistry.query.filter_by(
                tenant_id=resolved_tenant_id,
                provider="twilio",
                channel="whatsapp",
                name=normalized_name,
            )
            if language:
                query = query.filter(MessageTemplateRegistry.language == language)

            row = query.first()
            if not row and language:
                row = MessageTemplateRegistry.query.filter_by(
                    tenant_id=resolved_tenant_id,
                    provider="twilio",
                    channel="whatsapp",
                    name=normalized_name,
                ).first()
        except Exception as exc:
            _log(
                "warning",
                "[whatsapp] Failed to resolve template registry name=%s tenant_id=%s error_type=%s",
                normalized_name,
                resolved_tenant_id,
                type(exc).__name__,
            )
            row = None

        if row:
            status = (row.status or "").strip().lower()
            content_sid = (row.content_sid or "").strip()
            if (
                status == "approved"
                and content_sid.startswith("HX")
                and _twilio_template_registry_row_is_fresh(
                    row,
                    template_name=normalized_name,
                )
            ):
                return content_sid
            return None

    return _resolve_approved_whatsapp_template_sid_from_manifest(
        normalized_name,
        language=language,
    )


def _twilio_template_registry_row_is_fresh(
    row: MessageTemplateRegistry,
    *,
    template_name: str,
) -> bool:
    """Require recent provider-sync evidence for an approved DB template."""

    try:
        max_age_hours = float(
            current_app.config.get("TWILIO_TEMPLATE_MANIFEST_MAX_AGE_HOURS", 168)
        )
    except (TypeError, ValueError, OverflowError):
        max_age_hours = 0.0
    if max_age_hours <= 0:
        _log(
            "warning",
            "[whatsapp] Template registry rejected name=%s reason=invalid_max_age",
            template_name,
        )
        return False

    synced_at = getattr(row, "last_sync_at", None)
    if not isinstance(synced_at, datetime):
        _log(
            "warning",
            "[whatsapp] Template registry rejected name=%s reason=missing_sync_timestamp",
            template_name,
        )
        return False
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    else:
        synced_at = synced_at.astimezone(timezone.utc)
    age = datetime.now(timezone.utc) - synced_at
    if age < -timedelta(minutes=5) or age > timedelta(hours=max_age_hours):
        _log(
            "warning",
            "[whatsapp] Template registry rejected name=%s reason=stale_sync",
            template_name,
        )
        return False
    return True


def _resolve_welcome_template_sid(
    *,
    tenant_profile: Optional[TenantProfile],
) -> Optional[str]:
    resolved = _resolve_approved_whatsapp_template_sid(
        "chatboc_welcome_menu_v2",
        tenant_profile=tenant_profile,
    )
    if resolved:
        return resolved

    override = str(current_app.config.get("WELCOME_TEMPLATE_SID") or "").strip()
    if not override:
        return None
    if not _as_bool(
        current_app.config.get("WELCOME_TEMPLATE_OVERRIDE_ENABLED"),
        default=False,
    ):
        _log(
            "warning",
            "[WELCOME] Unattested template override ignored",
        )
        return None
    if not override.startswith("HX") and not current_app.testing:
        _log(
            "warning",
            "[WELCOME] Invalid template override ignored",
        )
        return None
    _log(
        "warning",
        "[WELCOME] Emergency template override enabled template_ref=%s",
        _safe_provider_reference(override),
    )
    return override


@lru_cache(maxsize=1)
def _load_twilio_template_manifest() -> Dict[str, Dict[str, Any]]:
    """Load local Twilio Content manifest entries keyed by friendly name."""

    manifest_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "twilio_content_templates.local.json",
    )
    try:
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            raw_payload = json.load(manifest_file)
    except Exception:
        return {}

    templates = raw_payload.get("templates") if isinstance(raw_payload, dict) else None
    if not isinstance(templates, dict):
        return {}

    normalized: Dict[str, Dict[str, Any]] = {}
    for friendly_name, entry in templates.items():
        if not isinstance(entry, dict):
            continue
        key = str(friendly_name or "").strip().lower()
        if key:
            normalized[key] = entry
    return normalized


def _resolve_approved_whatsapp_template_sid_from_manifest(
    template_name: str,
    *,
    language: str = "es",
) -> Optional[str]:
    """Resolve an approved ContentSid from the local Twilio manifest fallback."""

    if current_app.config.get("DISABLE_TWILIO_TEMPLATE_MANIFEST_FALLBACK"):
        return None

    manifest_entry = _load_twilio_template_manifest().get(
        str(template_name or "").strip().lower()
    )
    if not manifest_entry:
        return None

    manifest_language = str(manifest_entry.get("language") or "").strip().lower()
    requested_language = str(language or "").strip().lower()
    if requested_language and manifest_language and manifest_language != requested_language:
        return None

    approval_status = str(
        manifest_entry.get("approvalStatus")
        or manifest_entry.get("approval_status")
        or manifest_entry.get("status")
        or ""
    ).strip().upper()
    if not (manifest_entry.get("approved") is True or approval_status == "APPROVED"):
        return None

    if not _twilio_template_manifest_entry_is_fresh(
        manifest_entry,
        template_name=template_name,
    ):
        return None

    content_sid = str(manifest_entry.get("sid") or manifest_entry.get("content_sid") or "").strip()
    if not content_sid.startswith("HX"):
        return None

    return content_sid


def _twilio_template_manifest_entry_is_fresh(
    manifest_entry: Dict[str, Any],
    *,
    template_name: str,
) -> bool:
    """Require recent provider status evidence before trusting a local manifest.

    A Content API create response or an old ``APPROVED`` snapshot is not proof
    that the template remains usable today.  When status evidence is missing,
    malformed, from the future, or older than the configured TTL, resolution
    fails closed and the caller can use its policy-checked plain-text fallback
    inside an active WhatsApp service window.
    """

    configured_max_age = current_app.config.get(
        "TWILIO_TEMPLATE_MANIFEST_MAX_AGE_HOURS",
        os.getenv("TWILIO_TEMPLATE_MANIFEST_MAX_AGE_HOURS", "168"),
    )
    try:
        max_age_hours = float(configured_max_age)
    except (TypeError, ValueError):
        max_age_hours = 168.0
    if max_age_hours <= 0:
        _log(
            "warning",
            "[whatsapp] Template manifest rejected name=%s reason=invalid_max_age",
            template_name,
        )
        return False

    raw_timestamp = manifest_entry.get("lastStatusAt") or manifest_entry.get("updatedAt")
    if not raw_timestamp:
        _log(
            "warning",
            "[whatsapp] Template manifest rejected name=%s reason=missing_status_timestamp",
            template_name,
        )
        return False

    try:
        parsed_timestamp = datetime.fromisoformat(
            str(raw_timestamp).strip().replace("Z", "+00:00")
        )
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        status_age = datetime.now(timezone.utc) - parsed_timestamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        _log(
            "warning",
            "[whatsapp] Template manifest rejected name=%s reason=invalid_status_timestamp",
            template_name,
        )
        return False

    # More than five minutes in the future indicates clock or manifest drift.
    if status_age < timedelta(minutes=-5) or status_age > timedelta(hours=max_age_hours):
        _log(
            "warning",
            "[whatsapp] Template manifest rejected name=%s reason=stale_status",
            template_name,
        )
        return False

    return True


def _serialize_template_value(value: Any) -> str:
    """Convert a Twilio template variable value to a string safely."""

    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def _normalize_twilio_content_variables(
    raw_variables: Any,
    *,
    user_name: str = "",
    context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return the JSON string Twilio expects for Content API variables."""

    if raw_variables is None:
        return None

    variables = raw_variables
    if isinstance(raw_variables, str):
        stripped_variables = raw_variables.strip()
        if stripped_variables.startswith("{") or stripped_variables.startswith("["):
            try:
                variables = json.loads(stripped_variables)
            except (TypeError, ValueError):
                variables = raw_variables

    if isinstance(variables, dict) and "variables" in variables and not any(
        str(key).isdigit() for key in variables.keys()
    ):
        variables = variables.get("variables")

    if isinstance(variables, (list, tuple)):
        normalized = {
            str(index): _serialize_template_value(value)
            for index, value in enumerate(variables, start=1)
        }
    elif isinstance(variables, dict):
        rendered = _render_template_variables(
            variables,
            user_name=user_name,
            context=context or {},
        )
        normalized = {
            str(key): _serialize_template_value(value)
            for key, value in rendered.items()
        }
    else:
        normalized = {"1": _serialize_template_value(variables)}

    try:
        return json.dumps(normalized, ensure_ascii=False)
    except TypeError:
        return json.dumps({}, ensure_ascii=False)


def _validate_twilio_content_variables_contract(
    entry: Dict[str, Any],
    normalized_variables: Optional[str],
) -> Tuple[bool, Optional[str]]:
    """Validate declared template placeholders without inspecting user meaning."""

    template_contract = entry.get("template_contract")
    if not isinstance(template_contract, dict):
        return True, None
    declared_variables = template_contract.get("variables")
    if not isinstance(declared_variables, dict) or not declared_variables:
        return True, None

    try:
        parsed_variables = json.loads(normalized_variables or "{}")
    except (TypeError, ValueError):
        return False, "invalid_json"
    if not isinstance(parsed_variables, dict):
        return False, "not_an_object"

    required_keys = {str(key) for key in declared_variables.keys()}
    actual_keys = {str(key) for key in parsed_variables.keys()}
    if actual_keys != required_keys:
        return False, "placeholder_mismatch"
    if any(not str(parsed_variables.get(key) or "").strip() for key in required_keys):
        return False, "blank_required_value"
    return True, None


def _repair_twilio_value(value: Any) -> Any:
    if isinstance(value, str):
        return repair_common_mojibake(value)
    if isinstance(value, list):
        return [_repair_twilio_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_repair_twilio_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _repair_twilio_value(item) for key, item in value.items()}
    return value


def _repair_twilio_json_string(value: str) -> str:
    raw = repair_common_mojibake(value)
    prefix = ""
    candidate = raw
    if raw.startswith("whatsapp:"):
        prefix = "whatsapp:"
        candidate = raw[len(prefix):]
    try:
        parsed = json.loads(candidate)
    except Exception:
        return raw
    repaired = _repair_twilio_value(parsed)
    return f"{prefix}{json.dumps(repaired, ensure_ascii=False)}"


def _sanitize_twilio_message_params(params: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(params or {})
    if "body" in sanitized:
        sanitized["body"] = repair_common_mojibake(sanitized.get("body") or "")
    if "content_variables" in sanitized and sanitized.get("content_variables") is not None:
        sanitized["content_variables"] = _repair_twilio_json_string(str(sanitized["content_variables"]))
    if "persistent_action" in sanitized:
        actions = sanitized.get("persistent_action") or []
        if isinstance(actions, str):
            actions = [actions]
        sanitized["persistent_action"] = [
            _repair_twilio_json_string(str(action))
            for action in actions
            if str(action or "").strip()
        ]
    return sanitized


def _clean_status_callback_url(value: Any) -> Optional[str]:
    cleaned = str(value or "").strip().rstrip("/")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        return None
    if not cleaned.startswith(("http://", "https://")):
        return None
    if cleaned.startswith("http://") and "localhost" not in cleaned and "127.0.0.1" not in cleaned:
        cleaned = "https://" + cleaned.split("://", 1)[1]
    return cleaned


def _with_twilio_status_retry_overrides(value: Any) -> Optional[str]:
    """Opt a callback into bounded retries without changing its signed URL.

    Twilio consumes the URL fragment as connection overrides; browsers and our
    Flask endpoint never receive it. Existing explicit overrides win.
    """

    callback = _clean_status_callback_url(value)
    if not callback:
        return None
    parsed = urlsplit(callback)
    if parsed.fragment:
        return callback
    return urlunsplit(
        parsed._replace(fragment=TWILIO_STATUS_CALLBACK_CONNECTION_OVERRIDES)
    )


def _twilio_whatsapp_status_callback_url(params: Dict[str, Any]) -> Optional[str]:
    if has_app_context():
        configured = (
            current_app.config.get("TWILIO_WHATSAPP_STATUS_CALLBACK_URL")
            or current_app.config.get("WHATSAPP_STATUS_CALLBACK_URL")
        )
        callback = _clean_status_callback_url(configured)
        if callback:
            return callback

        try:
            _, provider_sender = _resolve_status_callback_tenant_and_sender(
                {
                    "From": params.get("from_") or params.get("from"),
                    "MessagingServiceSid": params.get("messaging_service_sid"),
                    "ServiceSid": params.get("service_sid"),
                }
            )
            callback = _clean_status_callback_url(getattr(provider_sender, "status_callback_url", None))
            if callback:
                return callback
        except Exception as exc:
            _log(
                "debug",
                "[TWILIO_WHATSAPP_STATUS] Could not resolve provider callback for outbound send error_type=%s",
                type(exc).__name__,
            )

        for key in (
            "PUBLIC_API_BASE_URL",
            "BACKEND_URL",
            "API_BASE_URL",
            "APP_BASE_URL",
            "BASE_URL",
            "PUBLIC_BASE_URL",
        ):
            base = _clean_status_callback_url(current_app.config.get(key))
            if base:
                return f"{base}/twilio/whatsapp/status"

    for key in (
        "TWILIO_WHATSAPP_STATUS_CALLBACK_URL",
        "WHATSAPP_STATUS_CALLBACK_URL",
        "PUBLIC_API_BASE_URL",
        "BACKEND_URL",
        "API_BASE_URL",
        "APP_BASE_URL",
        "BASE_URL",
        "PUBLIC_BASE_URL",
        "RENDER_EXTERNAL_URL",
    ):
        base = _clean_status_callback_url(os.getenv(key))
        if not base:
            continue
        if key.endswith("STATUS_CALLBACK_URL"):
            return base
        return f"{base}/twilio/whatsapp/status"

    if has_request_context():
        base = _clean_status_callback_url(request.url_root)
        if base:
            return f"{base}/twilio/whatsapp/status"

    return None


def _is_whatsapp_twilio_message(params: Dict[str, Any]) -> bool:
    return any(
        str(params.get(key) or "").strip().lower().startswith("whatsapp:")
        for key in ("to", "from_", "from")
    )


def _can_send_whatsapp_freeform_pre_message(
    *,
    body: str,
    tenant_profile: Optional[TenantProfile],
    recipient: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Validate WhatsApp free-form pre-messages against tenant policy."""

    tenant_id = getattr(tenant_profile, "id", None)
    if not tenant_id:
        return True, None

    evaluation_metadata = dict(metadata or {})
    evaluation_metadata.setdefault("recipient", recipient)
    evaluation_metadata["is_template"] = False

    try:
        return WhatsAppEnterpriseRulesService(int(tenant_id)).evaluate_outbound(
            body=body,
            metadata=evaluation_metadata,
        )
    except Exception as exc:
        _log(
            "warning",
            "[whatsapp] Could not evaluate free-form pre-message policy tenant_id=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )
        return True, None


def _send_twilio_message(client, **params):
    provider_call_hook = params.pop("_chatboc_provider_call_hook", None)
    sanitized = _sanitize_twilio_message_params(params)
    policy_metadata = sanitized.pop("_chatboc_policy_metadata", None)
    policy_metadata = dict(policy_metadata) if isinstance(policy_metadata, dict) else {}
    durable_collector = (
        getattr(g, "whatsapp_outbound_collector", None)
        if has_app_context()
        else None
    )
    if durable_collector is not None:
        # Queue workers render the exact legacy response path, but provider I/O
        # happens only after the inbound transaction stages its ordered outbox.
        # Capture before policy reservation and before ``messages.create`` so a
        # replay cannot consume quota or contact Twilio twice.
        # Never persist a point-in-time policy decision.  A delayed outbox row
        # can cross the customer-service window before dispatch; the worker
        # must re-evaluate the current contact state immediately before send.
        policy_metadata.pop("within_24h_window", None)
        sanitized["_chatboc_policy_metadata"] = policy_metadata
        return durable_collector.capture(sanitized)
    if has_request_context() and request.path == "/webhook/whatsapp":
        policy_metadata.setdefault("within_24h_window", True)
    if _is_whatsapp_twilio_message(sanitized) and not sanitized.get("status_callback"):
        callback = _twilio_whatsapp_status_callback_url(sanitized)
        if callback:
            sanitized["status_callback"] = callback
    if _is_whatsapp_twilio_message(sanitized) and sanitized.get("status_callback"):
        callback = _with_twilio_status_retry_overrides(
            sanitized.get("status_callback")
        )
        if callback:
            sanitized["status_callback"] = callback
    if _is_whatsapp_twilio_message(sanitized) and has_app_context():
        tenant, provider_sender = _resolve_status_callback_tenant_and_sender(
            {
                "From": sanitized.get("from_") or sanitized.get("from"),
                "MessagingServiceSid": sanitized.get("messaging_service_sid"),
                "ServiceSid": sanitized.get("service_sid"),
            }
        )
        if tenant and getattr(tenant, "id", None):
            allowed, reason = WhatsAppEnterpriseRulesService(int(tenant.id)).reserve_outbound(
                body=str(sanitized.get("body") or ""),
                reservation_key=f"send:{uuid.uuid4().hex}",
                metadata={
                    **policy_metadata,
                    "recipient": sanitized.get("to"),
                    "is_template": bool(sanitized.get("content_sid")),
                },
                provider="twilio",
                provider_connection_id=getattr(
                    provider_sender,
                    "provider_connection_id",
                    None,
                ),
                provider_sender_id=getattr(provider_sender, "id", None),
                source="whatsapp_webhook",
            )
            if not allowed:
                raise WhatsAppOutboundPolicyError(reason or "enterprise_policy_blocked")
    if callable(provider_call_hook):
        provider_call_hook()
    return client.messages.create(**sanitized)


def _dispatch_twilio_pre_messages(
    client,
    to_number: str,
    from_number: str,
    payload: Optional[dict],
    resolve_media_link,
    *,
    channel: str = "whatsapp",
    tenant_profile: Optional[TenantProfile] = None,
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
        if not content_sid:
            content_sid = _resolve_approved_whatsapp_template_sid(
                entry.get("template_name") or entry.get("friendly_name"),
                tenant_profile=tenant_profile,
                tenant_id=entry.get("tenant_id"),
                language=str(entry.get("language") or "es"),
            )

        normalized_variables: Optional[str] = None
        if content_sid:
            content_variables = entry.get("content_variables")
            if content_variables is None:
                content_variables = entry.get("variables")
            normalized_variables = _normalize_twilio_content_variables(
                content_variables,
                user_name=str(entry.get("user_name") or ""),
                context=entry.get("context") if isinstance(entry.get("context"), dict) else {},
            )
            variables_valid, validation_reason = _validate_twilio_content_variables_contract(
                entry,
                normalized_variables,
            )
            if not variables_valid:
                _log(
                    "warning",
                    "[whatsapp] Template pre-message rejected name=%s reason=%s",
                    entry.get("template_name") or entry.get("friendly_name") or "explicit_sid",
                    validation_reason,
                )
                content_sid = None

        if content_sid:
            params["content_sid"] = content_sid
            if normalized_variables is not None:
                params["content_variables"] = normalized_variables
        else:
            body = entry.get("body")
            fallback = entry.get("fallback")
            if body is None and isinstance(fallback, dict):
                fallback_mode = str(fallback.get("mode") or "").strip().lower()
                if fallback_mode == "plain_text":
                    body = fallback.get("body")
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

            allowed, reason = _can_send_whatsapp_freeform_pre_message(
                body=str(params.get("body") or ""),
                tenant_profile=tenant_profile,
                recipient=from_number,
                metadata=entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {},
            )
            if not allowed:
                _log(
                    "warning",
                    "[whatsapp] Skipping free-form pre-message because policy rejected it: reason=%s",
                    reason,
                )
                continue

        try:
            params["_chatboc_policy_metadata"] = (
                entry.get("metadata")
                if isinstance(entry.get("metadata"), dict)
                else {}
            )
            provider_message = _send_twilio_message(client, **params)
            _log(
                "info",
                "[whatsapp] Pre-message provider accepted delivery=pending_callback message_ref=%s template=%s",
                _safe_provider_reference(getattr(provider_message, "sid", None)),
                bool(params.get("content_sid")),
            )
        except Exception as exc:
            _log(
                "warning",
                "[whatsapp] Failed to send pre-message via Twilio error_type=%s",
                type(exc).__name__,
            )
            if has_app_context() and getattr(
                g,
                "whatsapp_outbound_collector",
                None,
            ) is not None:
                raise


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


def _tenant_owner_for_sender(tenant: Optional[TenantProfile]) -> Optional[User]:
    if not tenant:
        return None
    return tenant.municipio or tenant.pyme


def _resolve_provider_sender_for_twilio_request(
    *,
    to_number_raw: str,
    normalized_to: Optional[str],
    messaging_service_sid: Optional[str],
) -> Tuple[Optional[ProviderSender], Optional[str]]:
    cleaned = (to_number_raw or "").replace("whatsapp:", "").strip()
    candidates = {value for value in {cleaned, normalized_to} if value}
    if normalized_to and normalized_to.startswith("+549") and len(normalized_to) == 13:
        candidates.add("+54" + normalized_to[4:])

    sender_candidates = set(candidates)
    sender_candidates.update({f"whatsapp:{value}" for value in candidates if value})

    base_query = ProviderSender.query.options(
        joinedload(ProviderSender.tenant),
        joinedload(ProviderSender.provider_connection),
    ).filter(
        ProviderSender.channel == "whatsapp",
    )

    address_matches: List[ProviderSender] = []
    if candidates or sender_candidates:
        address_matches = (
            base_query.filter(
                or_(
                    ProviderSender.phone_number.in_(candidates),
                    ProviderSender.sender_id.in_(sender_candidates),
                )
            )
            .order_by(ProviderSender.id.desc())
            .all()
        )

    service_matches: List[ProviderSender] = []
    service_sid = str(messaging_service_sid or "").strip()
    if service_sid:
        service_matches = (
            base_query.filter(ProviderSender.messaging_service_sid == service_sid)
            .order_by(ProviderSender.id.desc())
            .all()
        )

    def _by_id(senders: Iterable[ProviderSender]) -> Dict[int, ProviderSender]:
        return {
            int(sender.id): sender
            for sender in senders
            if getattr(sender, "id", None) is not None
        }

    address_by_id = _by_id(address_matches)
    service_by_id = _by_id(service_matches)
    if address_by_id and service_by_id:
        shared_ids = set(address_by_id).intersection(service_by_id)
        if len(shared_ids) == 1:
            sender_id = next(iter(shared_ids))
            return address_by_id[sender_id], None
        if not shared_ids:
            return None, "conflicting_sender_hints"
        return None, "ambiguous_sender_hints"

    selected = address_matches or service_matches
    selected_by_id = _by_id(selected)
    if len(selected_by_id) == 1:
        return next(iter(selected_by_id.values())), None
    if len(selected_by_id) > 1:
        return None, "ambiguous_sender_hints"
    return None, None


def _provider_sender_for_inbound(
    *,
    to_number_raw: str,
    normalized_to: Optional[str],
    messaging_service_sid: Optional[str],
) -> Optional[ProviderSender]:
    sender, _ = _resolve_provider_sender_for_twilio_request(
        to_number_raw=to_number_raw,
        normalized_to=normalized_to,
        messaging_service_sid=messaging_service_sid,
    )
    return sender


def _twilio_credentials_for_provider_sender(
    provider_sender: Optional[ProviderSender],
    *,
    tenant: Optional[TenantProfile] = None,
) -> TwilioRuntimeCredentials:
    return resolve_twilio_runtime_credentials(
        tenant=getattr(provider_sender, "tenant", None) or tenant,
        provider_connection=getattr(provider_sender, "provider_connection", None),
        app_config=current_app.config,
    )


def _twilio_validator_for_credentials(
    credentials: TwilioRuntimeCredentials,
) -> Optional[RequestValidator]:
    if credentials.scope == "parent_legacy" and validator:
        return validator
    if not credentials.ready:
        return None
    return RequestValidator(credentials.auth_token)


def _twilio_client_for_credentials(
    credentials: TwilioRuntimeCredentials,
) -> Optional[Client]:
    if credentials.scope == "parent_legacy" and twilio_client:
        return twilio_client
    if not credentials.ready:
        return None
    return Client(credentials.account_sid, credentials.auth_token)


def _twilio_account_sid_matches(
    post_vars: Dict[str, Any],
    credentials: TwilioRuntimeCredentials,
) -> bool:
    request_account_sid = str(post_vars.get("AccountSid") or "").strip()
    if not request_account_sid:
        return True
    return bool(credentials.account_sid and request_account_sid == credentials.account_sid)


def _ensure_whatsapp_mapping_from_provider_sender(
    *,
    provider_sender: ProviderSender,
    to_number_raw: str,
    normalized_to: Optional[str],
) -> Optional[WhatsappNumero]:
    tenant = provider_sender.tenant
    owner = _tenant_owner_for_sender(tenant)
    if not tenant or not owner:
        current_app.logger.error(
            "[WHATSAPP_WEBHOOK] ProviderSender %s has no tenant owner; cannot route inbound WhatsApp",
            getattr(provider_sender, "id", None),
        )
        return None

    number = (
        _normalize_whatsapp_address(provider_sender.phone_number)
        or _normalize_whatsapp_address(provider_sender.sender_id)
        or normalized_to
        or _normalize_whatsapp_address(to_number_raw)
    )
    if not number:
        return None

    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number).first()
    if not mapping:
        mapping = WhatsappNumero(
            numero_whatsapp=number,
            user_id=owner.id,
            is_active=True,
        )
    else:
        mapping.user_id = owner.id
        mapping.is_active = True

    tenant.whatsapp_sender_id = provider_sender.sender_id or provider_sender.phone_number or tenant.whatsapp_sender_id
    cfg = dict(tenant.configuracion or {})
    provider_cfg = dict(cfg.get("twilio_tech_provider") or {})
    if provider_sender.messaging_service_sid:
        provider_cfg["messaging_service_sid"] = provider_sender.messaging_service_sid
    if provider_sender.sender_id:
        provider_cfg["sender_id"] = provider_sender.sender_id
    if provider_sender.phone_number:
        provider_cfg["phone_number"] = provider_sender.phone_number
    provider_cfg["inbound_mapping_status"] = "synced"
    cfg["twilio_tech_provider"] = provider_cfg
    tenant.configuracion = cfg

    db.session.add(mapping)
    db.session.add(tenant)
    safe_flag_modified(tenant, "configuracion")
    db.session.flush()
    return mapping


def _tenant_profile_for_user(user: Optional[User]) -> Optional[TenantProfile]:
    if not user:
        return None
    tenant = (
        getattr(user, "tenant", None)
        or getattr(user, "tenant_profile", None)
        or getattr(user, "tenant_profile_municipio", None)
        or getattr(user, "tenant_profile_pyme", None)
    )
    if tenant:
        return tenant
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id:
        return db.session.get(TenantProfile, tenant_id)
    return None


def _allowed_native_flow_ids_for_tenant(tenant: Optional[TenantProfile]) -> set[str]:
    if not tenant or not getattr(tenant, "id", None):
        return set()
    rows = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
    ).all()
    allowed: set[str] = set()
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if metadata.get("content_family") != "meta_native_flow":
            continue
        registry_status = str(row.status or "").strip().lower()
        flow_status = str(
            metadata.get("meta_flow_status") or metadata.get("approval_status") or ""
        ).strip().lower()
        if registry_status not in ACTIVE_NATIVE_FLOW_STATUSES:
            continue
        if flow_status not in ACTIVE_META_FLOW_STATUSES:
            continue
        for value in (
            metadata.get("flow_id"),
            metadata.get("meta_flow_id"),
            row.external_template_id,
        ):
            normalized = str(value or "").strip().lower()
            if normalized:
                allowed.add(normalized)
    return allowed


def _register_whatsapp_inbound_activity(tenant_id: Optional[int], from_number: Optional[str]) -> None:
    if not tenant_id or not from_number:
        return
    try:
        WhatsAppEnterpriseRulesService(int(tenant_id)).register_inbound_activity(
            recipient=from_number,
        )
    except Exception as exc:
        _log(
            "warning",
            "[WHATSAPP_WEBHOOK] Could not register inbound activity for tenant_id=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )


def _resolve_status_callback_tenant_and_sender(
    post_vars: Dict[str, Any],
) -> Tuple[Optional[TenantProfile], Optional[ProviderSender]]:
    from_number = post_vars.get("From") or ""
    normalized_from = _normalize_whatsapp_address(from_number)
    service_sid = post_vars.get("MessagingServiceSid") or post_vars.get("ServiceSid")

    provider_sender = _provider_sender_for_inbound(
        to_number_raw=from_number,
        normalized_to=normalized_from,
        messaging_service_sid=service_sid,
    )
    if provider_sender and getattr(provider_sender, "tenant", None):
        return provider_sender.tenant, provider_sender

    whatsapp_mapping, _, _ = _lookup_whatsapp_mapping(from_number)
    if whatsapp_mapping and whatsapp_mapping.user:
        return _tenant_profile_for_user(whatsapp_mapping.user), provider_sender

    return None, provider_sender


def _masked_whatsapp_status_address(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    return f"whatsapp:***{digits[-4:]}"


def _safe_twilio_status_error(value: Any) -> Optional[str]:
    text = str(value or "").strip()[:500]
    if not text:
        return None
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    return re.sub(r"\+?\d[\d\s().-]{6,}\d", "[redacted-number]", text)


def _safe_twilio_status_payload(post_vars: Dict[str, Any]) -> Dict[str, Any]:
    allowed = (
        "MessageSid",
        "SmsMessageSid",
        "MessageStatus",
        "SmsStatus",
        "ErrorCode",
        "MessagingServiceSid",
        "ServiceSid",
        "ApiVersion",
        "ChannelPrefix",
    )
    return {key: post_vars.get(key) for key in allowed if post_vars.get(key) not in (None, "")}


_WHATSAPP_DELIVERY_PROGRESS = {
    "unknown": 0,
    "accepted": 10,
    "scheduled": 15,
    "queued": 20,
    "sending": 30,
    "sent": 40,
    "delivered": 50,
    "read": 60,
}
_WHATSAPP_DELIVERY_FAILURES = {"failed", "undelivered", "canceled", "cancelled"}


def _whatsapp_delivery_transition_allowed(current: str, incoming: str) -> bool:
    """Accept progress only; terminal delivery outcomes never regress."""

    if not current:
        return True
    if current in _WHATSAPP_DELIVERY_FAILURES:
        return incoming == current
    if incoming in _WHATSAPP_DELIVERY_FAILURES:
        return _WHATSAPP_DELIVERY_PROGRESS.get(current, 0) < _WHATSAPP_DELIVERY_PROGRESS["delivered"]
    current_rank = _WHATSAPP_DELIVERY_PROGRESS.get(current, 0)
    incoming_rank = _WHATSAPP_DELIVERY_PROGRESS.get(incoming, 0)
    return incoming_rank >= current_rank


def _reconcile_whatsapp_flow_delivery(
    *,
    tenant_id: int,
    provider_sender: Optional[ProviderSender],
    message_sid: Any,
    status: Any,
    error_code: Any,
    flow_interaction_id: Any = None,
    recipient: Any = None,
) -> Optional[WhatsAppFlowInteraction]:
    message_sid_text = str(message_sid or "").strip()
    if not message_sid_text and flow_interaction_id in (None, ""):
        return None
    interaction = None
    if message_sid_text:
        query = WhatsAppFlowInteraction.query.filter_by(
            tenant_id=int(tenant_id),
            external_message_sid=message_sid_text,
        )
        if provider_sender and getattr(provider_sender, "id", None):
            query = query.filter_by(provider_sender_id=provider_sender.id)
        interaction = query.first()
    if not interaction and flow_interaction_id not in (None, ""):
        try:
            interaction_id = int(flow_interaction_id)
        except (TypeError, ValueError):
            interaction_id = 0
        if interaction_id > 0:
            fallback_query = WhatsAppFlowInteraction.query.filter_by(
                id=interaction_id,
                tenant_id=int(tenant_id),
            ).filter(
                WhatsAppFlowInteraction.status.in_(
                    ("claimed", "send_uncertain", "sent", "consumed")
                )
            )
            if provider_sender and getattr(provider_sender, "id", None):
                fallback_query = fallback_query.filter_by(
                    provider_sender_id=provider_sender.id,
                )
            interaction = fallback_query.first()
            if interaction and interaction.external_message_sid not in (
                None,
                "",
                message_sid_text,
            ):
                return None
    if not interaction and provider_sender and getattr(provider_sender, "id", None):
        recipient_digits = re.sub(r"\D", "", str(recipient or ""))
        if recipient_digits:
            recipient_hint = f"***{recipient_digits[-4:]}"
            candidates = (
                WhatsAppFlowInteraction.query.filter_by(
                    tenant_id=int(tenant_id),
                    provider_sender_id=provider_sender.id,
                    status="send_uncertain",
                    recipient_hint=recipient_hint,
                )
                .filter(
                    or_(
                        WhatsAppFlowInteraction.external_message_sid.is_(None),
                        WhatsAppFlowInteraction.external_message_sid == "",
                    )
                )
                .limit(2)
                .all()
            )
            if len(candidates) == 1:
                interaction = candidates[0]
    if not interaction:
        return None

    normalized_status = str(status or "unknown").strip().lower()[:32]
    normalized_error = str(error_code or "").strip()[:80] or None
    metadata = dict(interaction.metadata_json or {})
    current_delivery = metadata.get("delivery") if isinstance(metadata.get("delivery"), dict) else {}
    current_status = str(current_delivery.get("status") or "").strip().lower()
    callback_at = datetime.now(timezone.utc).isoformat()
    if not _whatsapp_delivery_transition_allowed(current_status, normalized_status):
        metadata["last_ignored_delivery_callback"] = {
            "status": normalized_status,
            "error_code": normalized_error,
            "callback_at": callback_at,
            "reason": "non_monotonic_transition",
        }
        interaction.metadata_json = metadata
        if message_sid_text and not interaction.external_message_sid:
            interaction.external_message_sid = message_sid_text[:180]
        db.session.add(interaction)
        return interaction

    metadata["delivery"] = {
        "status": normalized_status,
        "error_code": normalized_error,
        "callback_at": callback_at,
    }
    interaction.metadata_json = metadata
    interaction.updated_at = datetime.now(timezone.utc)
    if message_sid_text and not interaction.external_message_sid:
        interaction.external_message_sid = message_sid_text[:180]
    if normalized_status in _WHATSAPP_DELIVERY_FAILURES:
        if interaction.status != "consumed":
            interaction.status = "failed"
            interaction.error_code = normalized_error or normalized_status
    elif normalized_status in {
        "accepted",
        "scheduled",
        "queued",
        "sending",
        "sent",
        "delivered",
        "read",
    }:
        if interaction.status in {"claimed", "send_uncertain"}:
            interaction.status = "sent"
            interaction.error_code = None
    db.session.add(interaction)
    return interaction


def _persist_twilio_whatsapp_status_event(
    post_vars: Dict[str, Any],
    *,
    tenant: Optional[TenantProfile] = None,
    provider_sender: Optional[ProviderSender] = None,
    flow_interaction_id: Any = None,
) -> Optional[MessagingEventLedger]:
    if tenant is None:
        tenant, resolved_sender = _resolve_status_callback_tenant_and_sender(post_vars)
        provider_sender = provider_sender or resolved_sender
    if not tenant or not getattr(tenant, "id", None):
        _log(
            "warning",
            "[TWILIO_WHATSAPP_STATUS] Could not resolve tenant sender=%s service_ref=%s message_ref=%s",
            _masked_whatsapp_status_address(post_vars.get("From")),
            _safe_provider_reference(
                post_vars.get("MessagingServiceSid") or post_vars.get("ServiceSid")
            ),
            _safe_provider_reference(post_vars.get("MessageSid")),
        )
        return None

    message_sid = post_vars.get("MessageSid") or post_vars.get("SmsMessageSid")
    status = post_vars.get("MessageStatus") or post_vars.get("SmsStatus") or "unknown"
    provider_event_id = f"{message_sid or 'unknown'}:{status}"
    event = MessagingEventLedger.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        provider_event_id=provider_event_id,
    ).first()
    if not event:
        event = MessagingEventLedger(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            direction="outbound",
            event_type="delivery_status",
            provider_event_id=provider_event_id,
            external_message_sid=message_sid,
            provider_connection_id=getattr(provider_sender, "provider_connection_id", None),
            provider_sender_id=getattr(provider_sender, "id", None),
            sender=_masked_whatsapp_status_address(post_vars.get("From")),
            recipient=_masked_whatsapp_status_address(post_vars.get("To")),
            request_id=request.headers.get("I-Twilio-Idempotency-Token"),
        )
        db.session.add(event)

    event.external_status = status
    event.error_code = post_vars.get("ErrorCode")
    event.error_message = _safe_twilio_status_error(post_vars.get("ErrorMessage"))
    event.payload = _safe_twilio_status_payload(post_vars)
    event.metadata_json = {
        "account_scope": _masked_whatsapp_status_address(post_vars.get("AccountSid")),
        "messaging_service_sid": post_vars.get("MessagingServiceSid") or post_vars.get("ServiceSid"),
        "api_version": post_vars.get("ApiVersion"),
    }
    _reconcile_whatsapp_flow_delivery(
        tenant_id=tenant.id,
        provider_sender=provider_sender,
        message_sid=message_sid,
        status=status,
        error_code=post_vars.get("ErrorCode"),
        flow_interaction_id=flow_interaction_id,
        recipient=post_vars.get("To"),
    )
    return event


def _is_truthy_config_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "si", "sí", "on", "enabled"}


def _plain_ascii_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _looks_like_junin_context(*values: Any) -> bool:
    for value in values:
        if not value:
            continue
        key = _plain_ascii_key(value)
        if "junin" in key or key in {"juni", "juni bot", "asistente juni"}:
            return True
    return False


def _is_placeholder_municipio_name(value: Any) -> bool:
    return _plain_ascii_key(value) in {"", "tu municipio", "municipio", "municipio inteligente", "demo municipio"}


def _resolve_public_municipio_identity(
    tenant_name: str,
    assistant_name: Optional[str],
    tenant_profile=None,
    client_user=None,
    tenant_config: Optional[dict] = None,
) -> tuple[str, Optional[str]]:
    tenant_config = tenant_config or {}
    profile_name = getattr(tenant_profile, "nombre", None) if tenant_profile else None
    client_name = None
    if client_user:
        client_name = getattr(client_user, "nombre_empresa", None) or getattr(client_user, "name", None)

    if _looks_like_junin_context(
        tenant_name,
        assistant_name,
        profile_name,
        client_name,
        tenant_config.get("tenant_slug"),
        tenant_config.get("slug"),
        tenant_config.get("nombre_municipio"),
        tenant_config.get("nombre"),
    ):
        tenant_key = _plain_ascii_key(tenant_name)
        if _is_placeholder_municipio_name(tenant_name) or "junin" in tenant_key or tenant_key == "juni":
            tenant_name = "Municipalidad de Junín"
        if not assistant_name or _is_placeholder_municipio_name(assistant_name) or "municipio" in _plain_ascii_key(assistant_name):
            assistant_name = "JUNI"

    return tenant_name, assistant_name


def _ensure_welcome_audio_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        return

    has_menu_content = bool(payload.get("options_list") or payload.get("categorias") or payload.get("botones"))
    audio_policy = payload.get("audio_cache_policy") if isinstance(payload.get("audio_cache_policy"), dict) else {}
    is_fixed_menu_audio = bool(
        audio_policy.get("kind") == "fixed_menu"
        or payload.get("menu_audio_enabled")
        or payload.get("tts_cache_namespace")
    )
    prefers_cached_menu_audio = bool(
        payload.get("audio_url")
        and has_menu_content
        and payload.get("audio_text")
        and is_fixed_menu_audio
    )
    fallback_audio_url = payload.get("audio_url") if prefers_cached_menu_audio else None
    if prefers_cached_menu_audio:
        payload.pop("audio_url", None)

    if payload.get("audio_url") or payload.get("skip_audio_generation"):
        return

    if has_menu_content and not payload.get("audio_text"):
        menu_audio_enabled = any(
            _is_truthy_config_value(value)
            for value in (
                payload.get("force_audio"),
                payload.get("force_audio_whatsapp"),
                payload.get("menu_audio_enabled"),
                current_app.config.get("WHATSAPP_MENU_AUDIO_ENABLED") if has_app_context() else None,
                os.getenv("WHATSAPP_MENU_AUDIO_ENABLED"),
            )
        )
        if not menu_audio_enabled:
            payload["generar_audio"] = False
            payload["skip_audio_generation"] = True
            return
        payload["generar_audio"] = True

    if not payload.get("generar_audio") and not payload.get("audio_text"):
        return

    text_to_speak = payload.get("tts_cache_text") if is_fixed_menu_audio else None
    if not text_to_speak:
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
        elif fallback_audio_url:
            payload["audio_url"] = fallback_audio_url


def _prepare_cached_welcome_audio(payload: dict, *, tenant_profile=None, client_user=None) -> None:
    if not isinstance(payload, dict):
        return

    from services.common_utils import build_menu_tts_cache_namespace

    tenant_slug = (
        getattr(tenant_profile, "slug", None)
        or getattr(client_user, "tenant_slug", None)
        or str(getattr(client_user, "id", "") or "default")
    )
    tenant_slug = str(tenant_slug or "default").strip().lower() or "default"
    payload.setdefault("menu_audio_enabled", True)
    current_namespace = str(payload.get("tts_cache_namespace") or "").strip()
    generic_namespaces = {
        "",
        "menu",
        "main_menu",
        "menu_principal",
        "welcome",
        "welcome_menu",
    }
    next_namespace = build_menu_tts_cache_namespace(
        context={
            "tenant_slug": tenant_slug,
            "tenant": tenant_slug,
            "channel": "whatsapp",
            "user_obj": client_user,
        },
        tenant_name=getattr(tenant_profile, "nombre", None)
        or getattr(client_user, "nombre_empresa", None)
        or getattr(client_user, "name", None),
        menu_key=str(payload.get("menu_key") or "main_menu"),
        channel=str(payload.get("channel") or "whatsapp"),
        reduced=bool(payload.get("reduced") or payload.get("_reduced_menu")),
        version=str(payload.get("menu_version") or "v2"),
    )
    if current_namespace in generic_namespaces or current_namespace.startswith("whatsapp:welcome:"):
        payload["tts_cache_namespace"] = next_namespace
    else:
        payload.setdefault("tts_cache_namespace", next_namespace)
    if payload.get("audio_text") and not payload.get("tts_cache_text"):
        payload["tts_cache_text"] = payload.get("audio_text")
    payload.setdefault(
        "audio_cache_policy",
        {
            "kind": "fixed_menu",
            "scope": "tenant",
            "cache": "tts_audio_cache",
            "inclusive": True,
        },
    )


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


def _apply_completed_reclamo_context(context_data: dict, completion: dict) -> dict:
    """Make a completed claim authoritative over a stale flow snapshot.

    ``crear_nuevo_ticket`` commits internally and expires ORM JSON state.  A
    shallow response-format merge must therefore never be allowed to restore
    the pre-commit ``ESPERANDO_CONFIRMACION`` value.
    """

    if not isinstance(context_data, dict) or not isinstance(completion, dict):
        return context_data
    confirmation_id = str(completion.get("confirmation_id") or "").strip()
    ticket_nro = str(completion.get("ticket_nro") or "").strip()
    if not confirmation_id or not ticket_nro:
        return context_data

    context_update = completion.get("contexto_actualizado")
    if isinstance(context_update, dict):
        for key, value in context_update.items():
            context_data[key] = value

    municipio_ctx = context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    if not isinstance(municipio_ctx, dict):
        municipio_ctx = {}
        context_data[CONTEXTO_MUNICIPIO] = municipio_ctx
    for stale_key in (
        "reclamo_flow_v2",
        "historial_llm_reclamo",
        "datos_parciales_llm_reclamo",
        "expected_fields_llm_reclamo",
        "menu_opciones",
    ):
        municipio_ctx.pop(stale_key, None)
    municipio_ctx["estado_conversacion"] = "CONVERSACION_GENERAL_LLM"
    municipio_ctx["last_created_reclamo"] = {
        key: value
        for key, value in {
            "ts": time.time(),
            "confirmation_id": confirmation_id,
            "ticket_id": completion.get("ticket_id"),
            "ticket_nro": ticket_nro,
            "consulta_pin": completion.get("consulta_pin"),
            "tracking_url": completion.get("tracking_url"),
            "fingerprint": completion.get("fingerprint"),
        }.items()
        if value is not None
    }
    followup_started_at = time.time()
    context_data["active_ticket_followup"] = {
        "ticket_id": completion.get("ticket_id"),
        "ticket_nro": ticket_nro,
        "consulta_pin": completion.get("consulta_pin"),
        "tracking_url": completion.get("tracking_url"),
        "started_at": followup_started_at,
        "until": followup_started_at + CLAIM_FOLLOWUP_WINDOW_SECONDS,
    }
    context_data.pop("last_options_sent", None)
    context_data.pop("pending_sensitive_action", None)
    context_data.pop("foto_url", None)
    context_data.pop("es_foto", None)
    return context_data


def _send_delayed_payload(client, to_number: str, from_number: str, payload: dict, delay: int, app):
    """Send a payload via WhatsApp after a delay using a background thread."""

    durable_collector = (
        getattr(g, "whatsapp_outbound_collector", None)
        if has_app_context()
        else None
    )

    def _send():
        with app.app_context():
            if durable_collector is not None:
                g.whatsapp_outbound_collector = durable_collector
            from services.response_formatter import build_interactive_response

            delayed_base_url = (
                payload.get("_base_url")
                or payload.get("_request_url_root")
                or app.config.get("APP_BASE_URL")
                or ""
            )
            _normalize_whatsapp_flow_contract(payload, base_url=str(delayed_base_url).rstrip("/"))
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
                delay_context = (
                    durable_collector.delay(delay)
                    if durable_collector is not None
                    else nullcontext()
                )
                with delay_context:
                    message = _send_twilio_message(client, **params)

                    if audio_url:
                        absolute_audio_url = audio_url
                        if absolute_audio_url.startswith('/'):
                            base_url = (app.config.get("APP_BASE_URL") or "").rstrip('/')
                            if base_url:
                                absolute_audio_url = f"{base_url}{audio_url}"
                            else:
                                app.logger.warning(
                                    "[DELAYED_AUDIO] APP_BASE_URL no configurada; audio relativo omitido"
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
                                "[DELAYED_AUDIO] Enviando audio adicional message_ref=%s",
                                _safe_provider_reference(getattr(message, 'sid', None)),
                            )
                            _send_twilio_message(client, **audio_params)
            except Exception as exc:
                app.logger.error(
                    "[WHATSAPP_WEBHOOK] Delayed message failed error_type=%s",
                    type(exc).__name__,
                )
                if durable_collector is not None:
                    raise

    if durable_collector is not None:
        # Render synchronously while request/app context and tenant assets are
        # available; the collector persists the requested delay in the outbox.
        # The outer delay also covers auxiliary pre-messages rendered by the
        # delayed payload, not only its main body/audio sends.
        with durable_collector.delay(delay):
            _send()
    elif client:
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


def _whatsapp_inbound_durability_mode() -> str:
    return str(
        current_app.config.get("WHATSAPP_INBOUND_DURABILITY_MODE", "legacy")
        or "legacy"
    ).strip().lower()


def _durable_whatsapp_claim() -> Any:
    if not has_app_context():
        return None
    return getattr(g, "whatsapp_inbound_turn_claim", None)


def _assert_durable_replay_sender_scope(
    claim: Any,
    *,
    provider_sender: Optional[ProviderSender],
) -> None:
    if str(getattr(claim, "provider", "") or "").strip().lower() != "twilio":
        abort(403, "Invalid durable provider scope")

    expected_sender_id = getattr(claim, "provider_sender_id", None)
    actual_sender_id = getattr(provider_sender, "id", None)
    if expected_sender_id != actual_sender_id:
        abort(403, "Invalid durable sender scope")

    expected_connection_id = getattr(claim, "provider_connection_id", None)
    actual_connection_id = getattr(provider_sender, "provider_connection_id", None)
    if expected_connection_id != actual_connection_id:
        abort(403, "Invalid durable connection scope")


def _assert_durable_replay_tenant_scope(claim: Any, tenant_id: Any) -> None:
    try:
        expected_tenant_id = int(getattr(claim, "tenant_id", 0) or 0)
        actual_tenant_id = int(tenant_id or 0)
    except (TypeError, ValueError, OverflowError):
        abort(403, "Invalid durable tenant scope")
    if not expected_tenant_id or expected_tenant_id != actual_tenant_id:
        abort(403, "Invalid durable tenant scope")


def _resolve_whatsapp_session_identity(
    *,
    tenant_id: Any,
    empresa_id: Any,
    from_number_cleaned: str,
    durable_claim: Any = None,
):
    """Resolve or verify the non-PII canonical session for this inbound turn."""

    from services.channel_session_identity import (
        MODE_ENFORCE,
        MODE_LEGACY,
        ChannelSessionIdentityConflict,
        channel_session_identity_mode,
        resolve_channel_session_identity,
        verify_channel_session_identity_binding,
    )

    mode = channel_session_identity_mode(current_app.config)
    if mode == MODE_LEGACY:
        return None
    if durable_claim is not None:
        binding_id = getattr(durable_claim, "session_identity_binding_id", None)
        if binding_id is not None:
            return verify_channel_session_identity_binding(
                tenant_id=tenant_id,
                binding_id=binding_id,
                channel="whatsapp",
                provider=getattr(durable_claim, "provider", None),
                identity_version=getattr(
                    durable_claim,
                    "session_identity_version",
                    None,
                ),
                identity_hmac=getattr(
                    durable_claim,
                    "session_identity_hmac",
                    None,
                ),
                chat_session_id=getattr(durable_claim, "chat_session_id", None),
            )
        if mode == MODE_ENFORCE:
            raise ChannelSessionIdentityConflict("session_identity_replay_incomplete")

    return resolve_channel_session_identity(
        config=current_app.config,
        tenant_id=tenant_id,
        channel="whatsapp",
        provider="twilio",
        provider_identity=from_number_cleaned,
        owner_user_id=empresa_id,
    )


def _persist_validated_whatsapp_inbound_turn(
    *,
    post_vars: Dict[str, Any],
    safe_flow_submission: Optional[Dict[str, Any]],
    tenant_id: Any,
    provider_sender: Optional[ProviderSender],
    from_number_cleaned: str,
    to_number_cleaned: str,
    empresa_id: Any,
    session_identity: Any = None,
) -> Tuple[str, int]:
    """Persist the signed/scoped webhook before acknowledging queue mode."""

    from services.whatsapp_inbound_turns import (
        WhatsAppInboundDigestConflict,
        WhatsAppTurnValidationError,
        derive_whatsapp_stream_key,
        ingest_whatsapp_inbound_turn,
        normalize_whatsapp_inbound_payload,
    )

    provider_message_sid = str(
        post_vars.get("MessageSid") or post_vars.get("SmsMessageSid") or ""
    ).strip()
    if not provider_message_sid:
        return "Missing provider message id", 422

    try:
        resolved_tenant_id = int(tenant_id or 0)
    except (TypeError, ValueError, OverflowError):
        resolved_tenant_id = 0
    if resolved_tenant_id < 1:
        current_app.logger.error(
            "[WHATSAPP_QUEUE] Refused inbound without canonical tenant owner_user_id=%s",
            empresa_id,
        )
        return "WhatsApp tenant scope unavailable", 503

    # Never trust an internal-looking form field. Only the contract produced by
    # the synchronous signed Flow validator may cross the durable boundary.
    payload_source = dict(post_vars)
    payload_source.pop("safe_flow_submission", None)
    if isinstance(safe_flow_submission, dict):
        durable_flow_submission = deepcopy(safe_flow_submission)
        # These fields describe local validation time/state rather than the
        # immutable provider event. Keeping them would turn a byte-identical
        # Twilio retry into a false digest conflict.
        durable_flow_submission.pop("received_at", None)
        durable_correlation = durable_flow_submission.get("correlation")
        if isinstance(durable_correlation, dict):
            durable_correlation.pop("already_consumed", None)
        payload_source["safe_flow_submission"] = durable_flow_submission

    try:
        normalized_payload = normalize_whatsapp_inbound_payload(payload_source)
        stream_key = derive_whatsapp_stream_key(
            secret=current_app.config.get("WHATSAPP_INBOUND_HASH_SECRET") or "",
            tenant_id=resolved_tenant_id,
            sender=from_number_cleaned,
            destination=to_number_cleaned,
        )
        receipt = ingest_whatsapp_inbound_turn(
            tenant_id=resolved_tenant_id,
            provider="twilio",
            provider_message_sid=provider_message_sid,
            stream_key=stream_key,
            payload=normalized_payload,
            provider_connection_id=getattr(
                provider_sender,
                "provider_connection_id",
                None,
            ),
            provider_sender_id=getattr(provider_sender, "id", None),
            chat_session_id=(
                getattr(session_identity, "chat_session_id", None)
                if session_identity is not None
                else f"whatsapp_{empresa_id}_{from_number_cleaned}"
            ),
            session_identity_binding_id=getattr(
                session_identity,
                "binding_id",
                None,
            ),
            session_identity_version=getattr(
                session_identity,
                "identity_version",
                None,
            ),
            session_identity_hmac=getattr(
                session_identity,
                "identity_hmac",
                None,
            ),
            max_attempts=current_app.config.get(
                "WHATSAPP_INBOUND_MAX_ATTEMPTS",
                8,
            ),
        )
    except WhatsAppInboundDigestConflict as exc:
        current_app.logger.error(
            "[WHATSAPP_QUEUE] Replayed provider id changed immutable payload "
            "tenant_id=%s sender_id=%s code=%s",
            resolved_tenant_id,
            getattr(provider_sender, "id", None),
            exc.code,
        )
        return "Conflicting provider replay", 409
    except WhatsAppTurnValidationError as exc:
        current_app.logger.warning(
            "[WHATSAPP_QUEUE] Rejected invalid durable payload tenant_id=%s code=%s",
            resolved_tenant_id,
            exc.code,
        )
        return "Invalid WhatsApp payload", 422
    except Exception as exc:
        current_app.logger.error(
            "[WHATSAPP_QUEUE] Persistence failed before acknowledgement "
            "tenant_id=%s error_type=%s",
            resolved_tenant_id,
            type(exc).__name__,
        )
        return "WhatsApp queue unavailable", 503

    if receipt.created:
        try:
            from services.whatsapp_inbound_worker import (
                enqueue_whatsapp_inbound_stream,
            )

            enqueue_whatsapp_inbound_stream(
                tenant_id=receipt.tenant_id,
                stream_key=receipt.stream_key,
            )
        except Exception as exc:
            # The committed database row remains the source of truth and is
            # recoverable by the poller even when the broker is unavailable.
            current_app.logger.warning(
                "[WHATSAPP_QUEUE] Best-effort wakeup failed turn_id=%s error_type=%s",
                receipt.turn_id,
                type(exc).__name__,
            )

    current_app.logger.info(
        "[WHATSAPP_QUEUE] Inbound persisted outcome=%s turn_id=%s tenant_id=%s",
        receipt.outcome,
        receipt.turn_id,
        receipt.tenant_id,
    )
    return "OK", 200

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    _log("info", "Whatsapp webhook called.")
    durable_claim = _durable_whatsapp_claim()
    durable_replay = durable_claim is not None
    durability_mode = _whatsapp_inbound_durability_mode()
    if durability_mode not in {"legacy", "queue"}:
        current_app.logger.error(
            "[WHATSAPP_QUEUE] Invalid durability mode configured"
        )
        return "WhatsApp durability mode unavailable", 503
    if durable_replay and durability_mode != "queue":
        abort(503, "Durable WhatsApp replay is disabled")
    if durable_replay and getattr(g, "whatsapp_outbound_collector", None) is None:
        # A replay without capture would execute the legacy provider sends
        # inline and reopen the duplicate-send window this mode is designed to
        # close. Only the internal worker is allowed to construct both values.
        abort(503, "Durable WhatsApp outbox capture unavailable")

    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    if durable_replay:
        post_vars = dict(getattr(durable_claim, "payload", None) or {})
    else:
        post_vars = request.form.to_dict()
        # This key is internal-only. A provider form field with the same name
        # must never become a trusted Flow contract, even though the request is
        # otherwise signed.
        post_vars.pop("safe_flow_submission", None)
    inbound_content = classify_twilio_whatsapp_payload(post_vars)
    request_metadata = safe_twilio_form_metadata(post_vars)
    _log("debug", "Request form metadata: %s", request_metadata)
    persisted_flow_submission = post_vars.get("safe_flow_submission")
    flow_submission_present = bool(
        isinstance(persisted_flow_submission, dict)
        or has_whatsapp_flow_submission(post_vars)
    )
    flow_submission = (
        deepcopy(persisted_flow_submission)
        if durable_replay and isinstance(persisted_flow_submission, dict)
        else None
    )
    safe_flow_submission = (
        deepcopy(persisted_flow_submission)
        if durable_replay and isinstance(persisted_flow_submission, dict)
        else None
    )
    flow_completion_payload = None
    to_number_raw = post_vars.get("To", "")
    from_number_raw = post_vars.get("From", "")
    to_number_normalized = _normalize_whatsapp_address(to_number_raw)
    service_sid = post_vars.get("MessagingServiceSid") or post_vars.get("ServiceSid")

    provider_sender, sender_resolution_error = _resolve_provider_sender_for_twilio_request(
        to_number_raw=to_number_raw,
        normalized_to=to_number_normalized,
        messaging_service_sid=service_sid,
    )
    if inbound_content.is_event_only and not provider_sender and not sender_resolution_error:
        # Status callbacks use the business sender in ``From`` rather than
        # ``To``.  Resolve that reverse direction only for a content-free
        # control event; normal inbound messages remain strictly To-bound.
        event_sender_raw = from_number_raw
        event_sender_normalized = _normalize_whatsapp_address(event_sender_raw)
        if event_sender_normalized and event_sender_normalized != to_number_normalized:
            provider_sender, sender_resolution_error = _resolve_provider_sender_for_twilio_request(
                to_number_raw=event_sender_raw,
                normalized_to=event_sender_normalized,
                messaging_service_sid=service_sid,
            )
    if sender_resolution_error:
        _log(
            "warning",
            "[WHATSAPP_WEBHOOK] Rejected ambiguous Twilio sender scope reason=%s service_ref=%s",
            sender_resolution_error,
            _safe_provider_reference(service_sid),
        )
        abort(403, "Invalid Twilio sender scope")

    if durable_replay:
        _assert_durable_replay_sender_scope(
            durable_claim,
            provider_sender=provider_sender,
        )

    whatsapp_mapping = None
    to_number_cleaned = (to_number_raw or "").replace("whatsapp:", "").strip()
    credential_tenant = getattr(provider_sender, "tenant", None)
    if not provider_sender:
        whatsapp_mapping, to_number_cleaned, to_number_normalized = _lookup_whatsapp_mapping(to_number_raw)
        if inbound_content.is_event_only and not whatsapp_mapping:
            event_mapping, _, _ = _lookup_whatsapp_mapping(from_number_raw)
            if event_mapping:
                whatsapp_mapping = event_mapping
        if whatsapp_mapping and whatsapp_mapping.user:
            credential_tenant = _tenant_profile_for_user(whatsapp_mapping.user)

    if provider_sender and not getattr(provider_sender, "tenant", None):
        current_app.logger.error(
            "[WHATSAPP_WEBHOOK] ProviderSender id=%s has no tenant credentials scope",
            getattr(provider_sender, "id", None),
        )
        abort(503, "Twilio sender credentials unavailable")

    credentials = _twilio_credentials_for_provider_sender(
        provider_sender,
        tenant=credential_tenant,
    )
    request_validator = _twilio_validator_for_credentials(credentials)
    if not request_validator:
        current_app.logger.error(
            "[WHATSAPP_WEBHOOK] Twilio credentials unavailable sender_id=%s tenant_id=%s scope=%s",
            getattr(provider_sender, "id", None),
            getattr(provider_sender, "tenant_id", None) or getattr(credential_tenant, "id", None),
            credentials.scope,
        )
        abort(503 if provider_sender or credential_tenant else 500, "Twilio validator not configured")

    if not _twilio_account_sid_matches(post_vars, credentials):
        current_app.logger.warning(
            "[WHATSAPP_WEBHOOK] Rejected Twilio AccountSid mismatch sender_id=%s tenant_id=%s",
            getattr(provider_sender, "id", None),
            getattr(provider_sender, "tenant_id", None) or getattr(credential_tenant, "id", None),
        )
        abort(403, "Invalid Twilio sender scope")

    if not durable_replay and not request_validator.validate(url, request.form, signature):
        abort(403, "Invalid Twilio signature")

    if inbound_content.is_event_only:
        event_tenant_id = getattr(credential_tenant, "id", None)
        if durable_replay:
            _assert_durable_replay_tenant_scope(durable_claim, event_tenant_id)
        if not event_tenant_id:
            current_app.logger.warning(
                "[WHATSAPP_WEBHOOK] Content-free control event rejected without tenant scope kind=%s",
                inbound_content.kind,
            )
            return "WhatsApp tenant scope unavailable", 404
        current_app.logger.info(
            "[WHATSAPP_WEBHOOK] Content-free control event acknowledged before conversation kind=%s tenant_id=%s",
            inbound_content.kind,
            event_tenant_id,
        )
        return "OK", 200

    if flow_submission_present and not durable_replay:
        try:
            if not credential_tenant or not provider_sender:
                raise WhatsAppFlowTokenError("flow_sender_not_registered")
            allowed_flow_ids = _allowed_native_flow_ids_for_tenant(credential_tenant)
            flow_submission = parse_whatsapp_flow_submission(
                post_vars,
                allowed_flow_ids=allowed_flow_ids,
                message_sid=post_vars.get("MessageSid") or post_vars.get("SmsMessageSid"),
                require_correlation_token=True,
                flow_token_validator=lambda raw_token: verify_whatsapp_flow_token(
                    raw_token,
                    secret=current_app.config.get("WHATSAPP_FLOW_TOKEN_KEY_V1"),
                    tenant_id=credential_tenant.id,
                    recipient=from_number_raw,
                    provider_sender_id=provider_sender.id,
                    allowed_flow_ids=allowed_flow_ids,
                    ttl_seconds=current_app.config.get("WHATSAPP_FLOW_TOKEN_TTL_SECONDS"),
                ),
            )
        except (FlowSubmissionValidationError, WhatsAppFlowTokenError) as exc:
            current_app.logger.warning(
                "[WHATSAPP_FLOW] Rejected unsafe Flow submission code=%s source=%s",
                exc.code,
                getattr(exc, "source_field", None) or "combined",
            )
        else:
            safe_flow_submission = persistence_safe_flow_submission(flow_submission or {})
            integrity = (flow_submission or {}).get("integrity") or {}
            current_app.logger.info(
                "[WHATSAPP_FLOW] Accepted Flow submission fields=%s redacted=%s sources=%s",
                integrity.get("field_count", 0),
                integrity.get("redacted_field_count", 0),
                len(integrity.get("source_fields") or []),
            )

    # Keep all dispatch paths in this request on the same validated account.
    twilio_client = _twilio_client_for_credentials(credentials)

    if provider_sender:
        whatsapp_mapping, to_number_cleaned, to_number_normalized = _lookup_whatsapp_mapping(to_number_raw)
    if not whatsapp_mapping:
        if provider_sender:
            whatsapp_mapping = _ensure_whatsapp_mapping_from_provider_sender(
                provider_sender=provider_sender,
                to_number_raw=to_number_raw,
                normalized_to=to_number_normalized,
            )
            if whatsapp_mapping:
                current_app.logger.info(
                    "[WHATSAPP_WEBHOOK] Synced inbound WhatsApp mapping from ProviderSender id=%s tenant_id=%s",
                    getattr(provider_sender, "id", None),
                    getattr(provider_sender, "tenant_id", None),
                )
    is_chatboc_demo_destination = _is_chatboc_demo_destination(to_number_normalized or to_number_cleaned)
    force_chatboc_demo_hub = False
    if is_chatboc_demo_destination and not whatsapp_mapping:
        whatsapp_mapping = _ensure_chatboc_demo_whatsapp_mapping(to_number_normalized or to_number_cleaned)
        force_chatboc_demo_hub = bool(whatsapp_mapping)
    from_number_cleaned = from_number_raw.replace("whatsapp:", "")

    _log(
        "info",
        "[WHATSAPP_WEBHOOK] Incoming message sender_id=%s tenant_id=%s request_metadata=%s",
        getattr(provider_sender, "id", None),
        getattr(provider_sender, "tenant_id", None)
        or getattr(credential_tenant, "id", None),
        request_metadata,
    )

    if not whatsapp_mapping:
        _log(
            "error",
            "[WHATSAPP_WEBHOOK] No active mapping sender_id=%s has_destination=%s",
            getattr(provider_sender, "id", None),
            bool(to_number_raw),
        )
        return "WhatsApp number not configured for any client.", 404

    client_user = whatsapp_mapping.user
    if not client_user:
        _log(
            "error",
            "No user associated with WhatsappNumero id=%s",
            whatsapp_mapping.id,
        )
        return "Internal configuration error: WhatsApp number mapped to non-existent user.", 500

    # Store the owner entity on ``flask.g`` so downstream helpers (like the
    # storage fallback) know which empresa/municipio owns this conversation.
    g.owner_user = client_user

    empresa_id = client_user.id
    tenant_profile = (
        credential_tenant
        or getattr(client_user, "tenant", None)
        or getattr(client_user, "tenant_profile", None)
        or getattr(client_user, "tenant_profile_municipio", None)
        or getattr(client_user, "tenant_profile_pyme", None)
    )
    if is_chatboc_demo_destination and not force_chatboc_demo_hub:
        force_chatboc_demo_hub = getattr(tenant_profile, "slug", None) == CHATBOC_DEMO_TENANT_SLUG
    if force_chatboc_demo_hub:
        tenant_profile = TenantProfile.query.filter_by(slug=CHATBOC_DEMO_TENANT_SLUG).first() or tenant_profile
        _log(
            "info",
            "[CHATBOC_DEMO_HUB] Forced demo routing owner_user_id=%s tenant_id=%s",
            getattr(client_user, "id", None),
            getattr(tenant_profile, "id", None),
        )
    tenant_id = None
    if tenant_profile:
        tenant_id = getattr(tenant_profile, "id", None) or getattr(tenant_profile, "tenant_id", None)
    if flow_submission_present:
        credential_tenant_id = getattr(credential_tenant, "id", None)
        if not tenant_id or not credential_tenant_id or int(tenant_id) != int(credential_tenant_id):
            current_app.logger.error(
                "[WHATSAPP_FLOW] Tenant routing mismatch rejected credential_tenant_id=%s routed_tenant_id=%s",
                credential_tenant_id,
                tenant_id,
            )
            if durability_mode == "queue":
                abort(403, "Invalid WhatsApp Flow tenant scope")
            return "OK", 200

    from services.channel_session_identity import (
        ChannelSessionIdentityConflict,
        ChannelSessionIdentityError,
    )

    session_identity = None
    try:
        session_identity = _resolve_whatsapp_session_identity(
            tenant_id=tenant_id,
            empresa_id=empresa_id,
            from_number_cleaned=from_number_cleaned,
            durable_claim=durable_claim,
        )
    except ChannelSessionIdentityError as exc:
        current_app.logger.error(
            "[WHATSAPP_SESSION_IDENTITY] Resolution refused tenant_id=%s "
            "durable_replay=%s code=%s",
            tenant_id,
            durable_replay,
            exc.code,
        )
        if durable_replay and isinstance(exc, ChannelSessionIdentityConflict):
            abort(403, "Invalid durable session identity scope")
        return "WhatsApp session identity unavailable", 503

    if session_identity is not None:
        current_app.logger.info(
            "[WHATSAPP_SESSION_IDENTITY] tenant_id=%s outcome=%s "
            "continuity_preserved=%s",
            tenant_id,
            session_identity.outcome,
            session_identity.continuity_preserved,
        )

    if durable_replay:
        _assert_durable_replay_tenant_scope(durable_claim, tenant_id)
    elif durability_mode == "queue":
        return _persist_validated_whatsapp_inbound_turn(
            post_vars=post_vars,
            safe_flow_submission=safe_flow_submission,
            tenant_id=tenant_id,
            provider_sender=provider_sender,
            from_number_cleaned=from_number_cleaned,
            to_number_cleaned=to_number_cleaned,
            empresa_id=empresa_id,
            session_identity=session_identity,
        )

    from services.pymes import get_or_create_user_by_phone
    end_user = get_or_create_user_by_phone(from_number_cleaned, client_user)

    chat_session_id_internal = (
        session_identity.chat_session_id
        if session_identity is not None
        else f"whatsapp_{empresa_id}_{from_number_cleaned}"
    )

    ensure_chat_session_context_schema(db.session)
    try:
        session_context_db_entry = ChatSessionContext.query.filter_by(
            chat_session_id=chat_session_id_internal
        ).first()
    except ProgrammingError as exc:
        _log(
            "warning",
            "[WHATSAPP_WEBHOOK] tenant_id missing when querying chat_session_context; retrying after safeguard error_type=%s",
            type(exc).__name__,
        )
        db.session.rollback()
        ensure_chat_session_context_schema(db.session)
        try:
            session_context_db_entry = ChatSessionContext.query.filter_by(
                chat_session_id=chat_session_id_internal
            ).first()
        except ProgrammingError as exc_retry:
            _log(
                "error",
                "[WHATSAPP_WEBHOOK] Error accediendo a chat_session_context schema_mismatch error_type=%s",
                type(exc_retry).__name__,
            )
            db.session.rollback()
            return (
                "Recibimos tu mensaje pero estamos ajustando el servicio. Intentalo nuevamente en unos minutos.",
                200,
            )
    except SQLAlchemyError as exc:
        _log(
            "error",
            "[WHATSAPP_WEBHOOK] Error de base de datos obteniendo el contexto de sesión error_type=%s",
            type(exc).__name__,
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
    elif (
        tenant_id
        and session_context_db_entry.tenant_id
        and int(tenant_id) != int(session_context_db_entry.tenant_id)
    ):
        current_app.logger.error(
            "[WHATSAPP_WEBHOOK] Session tenant mismatch rejected "
            "routed_tenant_id=%s session_tenant_id=%s",
            tenant_id,
            session_context_db_entry.tenant_id,
        )
        return (
            "Recibimos tu mensaje pero no pudimos validar el canal de esta organización. "
            "Intentá nuevamente en unos minutos.",
            200,
        )

    # Ensure context_data is a dict
    if not isinstance(session_context_db_entry.context_data, dict):
        session_context_db_entry.context_data = {}
    base_session_data = {
        "historial_chat": [],
        "estado_conversacion": "inicio",
        "user_id_empresa": empresa_id,
        "telefono_usuario": from_number_cleaned,
        "canal_origen": "whatsapp",
        "mensajes_previos_llm_formato": [],
    }
    context_changed = False
    for key, value in base_session_data.items():
        if key not in session_context_db_entry.context_data:
            session_context_db_entry.context_data[key] = value
            context_changed = True
    if not session_context_db_entry.anon_id:
        session_context_db_entry.anon_id = from_number_cleaned
        context_changed = True
    if context_changed:
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
    if force_chatboc_demo_hub and not session_context_db_entry.context_data.get("chatboc_demo_context_ready"):
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
    if not force_chatboc_demo_hub:
        _sync_education_whatsapp_context(session_context_db_entry, tenant_profile)

    message_sid = post_vars.get("MessageSid") or post_vars.get("SmsMessageSid")
    media_message_sid = post_vars.get("MediaMessageSid") or post_vars.get("MediaSid0")
    processed_message_sids: List[str] = []
    processed_media_sids: List[str] = []
    processed_flow_tokens: List[str] = []
    completed_flow_id = str(
        (((safe_flow_submission or {}).get("flow") or {}).get("id")) or ""
    ).strip()
    replayable_flow_completion = completed_flow_id in {
        CLAIM_EVIDENCE_FLOW_ID,
        CLAIM_FLOW_ID,
        ORDER_FLOW_ID,
        SURVEY_FLOW_ID,
    }
    if not durable_replay:
        processed_message_sids = session_context_db_entry.context_data.setdefault(
            "processed_message_sids",
            [],
        )
        processed_media_sids = session_context_db_entry.context_data.setdefault(
            "processed_media_sids",
            [],
        )
        processed_flow_tokens = session_context_db_entry.context_data.setdefault(
            "processed_whatsapp_flow_token_digests",
            [],
        )

        if (
            message_sid
            and message_sid in processed_message_sids
            and not replayable_flow_completion
        ):
            _log(
                "info",
                "[WHATSAPP_WEBHOOK] Duplicate MessageSid ignored message_ref=%s",
                _safe_provider_reference(message_sid),
            )
            return "OK", 200
        if (
            media_message_sid
            and media_message_sid in processed_media_sids
            and not replayable_flow_completion
        ):
            _log(
                "info",
                "[WHATSAPP_WEBHOOK] Duplicate MediaMessageSid ignored message_ref=%s",
                _safe_provider_reference(media_message_sid),
            )
            return "OK", 200

    flow_correlation = (safe_flow_submission or {}).get("correlation") or {}
    flow_token_digest = str(flow_correlation.get("token_digest") or "").strip()
    flow_interaction_id = flow_correlation.get("interaction_id")
    if safe_flow_submission:
        consumed = bool(
            flow_interaction_id
            and consume_whatsapp_flow_interaction(
                interaction_id=int(flow_interaction_id),
                tenant_id=int(tenant_id),
                inbound_message_sid=message_sid,
                commit=False,
            )
        )
        if not consumed:
            if replayable_flow_completion and flow_interaction_id:
                try:
                    flow_completion_payload = replay_whatsapp_flow_completion(
                        tenant_id=int(tenant_id),
                        interaction_id=int(flow_interaction_id),
                        submission=safe_flow_submission,
                    )
                except MetaFlowActionError as exc:
                    current_app.logger.info(
                        "[WHATSAPP_FLOW] Completion replay ignored code=%s flow_id=%s interaction_id=%s",
                        exc.code,
                        completed_flow_id,
                        flow_interaction_id,
                    )
            if not flow_completion_payload:
                current_app.logger.info(
                    "[WHATSAPP_FLOW] Duplicate, expired or inactive Flow invocation ignored before orchestration"
                )
                if message_sid and not durable_replay:
                    if message_sid not in processed_message_sids:
                        processed_message_sids.append(message_sid)
                    session_context_db_entry.context_data["processed_message_sids"] = processed_message_sids[-50:]
                    safe_flag_modified(session_context_db_entry, "context_data")
                    db.session.add(session_context_db_entry)
                    db.session.commit()
                return "OK", 200
        elif replayable_flow_completion:
            try:
                flow_completion_payload = apply_whatsapp_flow_completion(
                    tenant_id=int(tenant_id),
                    interaction_id=int(flow_interaction_id),
                    submission=safe_flow_submission,
                    actor_user_id=getattr(end_user, "id", None),
                    anon_id=from_number_cleaned,
                )
            except MetaFlowActionError as exc:
                current_app.logger.warning(
                    "[WHATSAPP_FLOW] Completion rejected code=%s flow_id=%s interaction_id=%s",
                    exc.code,
                    completed_flow_id,
                    flow_interaction_id,
                )
                flow_completion_payload = record_whatsapp_flow_completion_rejection(
                    tenant_id=int(tenant_id),
                    interaction_id=int(flow_interaction_id),
                    code=exc.code,
                    actor_user_id=getattr(end_user, "id", None),
                    submission=safe_flow_submission,
                )
    if (
        not durable_replay
        and flow_token_digest
        and flow_token_digest in processed_flow_tokens
        and not flow_completion_payload
    ):
        current_app.logger.info("[WHATSAPP_FLOW] Replayed Flow token ignored before orchestration")
        if message_sid:
            processed_message_sids.append(message_sid)
            session_context_db_entry.context_data["processed_message_sids"] = processed_message_sids[-50:]
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
        return "OK", 200

    if not durable_replay:
        if message_sid and message_sid not in processed_message_sids:
            processed_message_sids.append(message_sid)
            session_context_db_entry.context_data["processed_message_sids"] = processed_message_sids[-50:]
        if media_message_sid and media_message_sid not in processed_media_sids:
            processed_media_sids.append(media_message_sid)
            session_context_db_entry.context_data["processed_media_sids"] = processed_media_sids[-50:]
    if safe_flow_submission:
        if not durable_replay:
            if flow_token_digest not in processed_flow_tokens:
                processed_flow_tokens.append(flow_token_digest)
            session_context_db_entry.context_data["processed_whatsapp_flow_token_digests"] = (
                processed_flow_tokens[-50:]
            )
        session_context_db_entry.context_data["last_whatsapp_flow_submission"] = deepcopy(
            safe_flow_submission
        )
    _register_whatsapp_inbound_activity(tenant_id, from_number_cleaned)
    safe_flag_modified(session_context_db_entry, "context_data")
    db.session.add(session_context_db_entry)
    db.session.commit()

    realtime_event = (
        flow_completion_payload.get("realtime_event")
        if isinstance(flow_completion_payload, dict)
        else None
    )
    if isinstance(realtime_event, dict) and realtime_event.get("comment_id"):
        try:
            from socket_service import emit_new_chat_message

            completed_comment = db.session.get(
                TicketComentario,
                int(realtime_event["comment_id"]),
            )
            if completed_comment is not None:
                ticket_type = str(realtime_event.get("ticket_type") or "municipio")
                ticket_id = int(realtime_event["ticket_id"])
                emit_new_chat_message(
                    {
                        "socket_room": f"ticket_{ticket_type}_{ticket_id}",
                        "tenant_type": ticket_type,
                        "ticket_id": ticket_id,
                        "channel": "whatsapp_flow",
                        "message": completed_comment.to_dict(),
                    }
                )
        except Exception as socket_exc:
            current_app.logger.warning(
                "[WHATSAPP_FLOW] CRM realtime emit failed interaction_id=%s error_type=%s",
                flow_interaction_id,
                type(socket_exc).__name__,
            )
    elif isinstance(realtime_event, dict) and realtime_event.get("kind") == "survey_vote":
        try:
            from services.survey_response_effects import (
                dispatch_survey_response_effects,
            )

            completed_entity = (
                flow_completion_payload.get("entity")
                if isinstance(flow_completion_payload, dict)
                else None
            )
            if not isinstance(completed_entity, dict) or completed_entity.get("kind") != "survey_response":
                raise ValueError("survey_response_entity_missing")
            dispatch_survey_response_effects(
                tenant_id=int(tenant_id),
                response_id=int(completed_entity["id"]),
                limit=3,
            )
        except Exception as effect_exc:
            # Invocation consumption and the canonical response are already
            # committed.  Leave the outbox pending/retryable for Celery or the
            # tenant reconciliation endpoint instead of faking delivery.
            db.session.rollback()
            current_app.logger.warning(
                "[WHATSAPP_FLOW] Survey effects remain pending interaction_id=%s error_type=%s",
                flow_interaction_id,
                type(effect_exc).__name__,
            )

    if flow_submission_present and not flow_submission:
        current_app.logger.info(
            "[WHATSAPP_FLOW] Invalid Flow submission stopped before orchestration"
        )
        if twilio_client:
            _send_twilio_message(
                twilio_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=(
                    "No pudimos validar el formulario de WhatsApp. "
                    "Volvelo a abrir y envialo nuevamente."
                ),
            )
        return "OK", 200

    if inbound_content.kind == "unsupported":
        # A signed provider message can legitimately be blank (for example an
        # unsupported disappearing-message payload).  It is still distinct
        # from a status/call callback, but must not be handed to the LLM as an
        # empty instruction or create a speculative ticket.
        if twilio_client:
            _send_twilio_message(
                twilio_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=honest_unprocessable_reply("unsupported"),
            )
        return "OK", 200

    # --- Boti-style Welcome Message Branch ---
    from services.municipio_responder import normalizar_texto
    from services.config_loader import cargar_configuracion_municipio
    from services.common_utils import _get_main_menu_payload
    from datetime import datetime

    button_payload = post_vars.get("ButtonPayload")
    list_id = post_vars.get("ListId")
    if flow_submission:
        flow_incoming_text = flow_submission.get("synthetic_text") or (
            "El usuario completo un formulario nativo de WhatsApp."
        )
    else:
        flow_incoming_text = None
    incoming_text = flow_incoming_text or button_payload or list_id or post_vars.get("Body", "")
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
    should_trigger_welcome = (
        not flow_submission_present
        and is_greeting
        and not is_waiting_for_info
        and not force_chatboc_demo_hub
    )

    request_root = request.url_root or ""
    request_root_stripped = request_root.rstrip("/")
    configured_base_url = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    effective_base_url = configured_base_url or request_root_stripped
    effective_media_base_url = _public_media_base_url(request_root_stripped)
    configured_sticker_url = current_app.config.get("WELCOME_MEDIA_URL")
    configured_audio_url = current_app.config.get("WELCOME_AUDIO_URL")
    resolved_sticker_url = _resolve_public_media_url(configured_sticker_url, request_root_stripped)
    resolved_audio_url = _resolve_public_media_url(configured_audio_url, request_root_stripped)

    pyme_welcome_overrides: Dict[str, Any] = {}
    pyme_welcome_context: Dict[str, Any] = {}
    if client_user and getattr(client_user, "tipo_chat", "") == "pyme":
        pyme_welcome_overrides, pyme_welcome_context = _load_pyme_welcome_settings(client_user)
        if "sticker_url" in pyme_welcome_overrides:
            resolved_sticker_url = _resolve_public_media_url(
                pyme_welcome_overrides.get("sticker_url"), request_root_stripped
            )
        if "audio_url" in pyme_welcome_overrides:
            resolved_audio_url = _resolve_public_media_url(
                pyme_welcome_overrides.get("audio_url"), request_root_stripped
            )

    tenant_config: Dict[str, Any] = {}
    assistant_name = None

    if should_trigger_welcome and not is_rate_limited:
        _log(
            "info",
            "[WELCOME] Triggering welcome tenant_id=%s greeting=%s",
            getattr(tenant_profile, "id", None),
            bool(is_greeting),
        )

        session_context_db_entry.context_data["last_welcome_ts"] = now
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.commit()

        # SQLAlchemy expires JSON attributes on commit. Rebind the nested state
        # dictionaries so subsequent submission metadata is written into the
        # current persisted context instead of stale pre-commit references.
        welcome_state = session_context_db_entry.context_data.setdefault(
            "_welcome_state", {}
        )
        template_state = welcome_state.setdefault("template", {})
        sticker_state = welcome_state.setdefault("sticker", {})
        safe_flag_modified(session_context_db_entry, "context_data")

        if twilio_client:
            try:
                template_sid = _resolve_welcome_template_sid(
                    tenant_profile=tenant_profile,
                )
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
                is_municipio_client = bool(
                    client_user and getattr(client_user, "tipo_chat", None) == "municipio"
                )
                if is_municipio_client:
                    municipio_id = getattr(client_user, "municipio_id", None) or getattr(client_user, "id", None)
                    if municipio_id is not None:
                        municipio_config = cargar_configuracion_municipio(str(municipio_id), "config.json") or {}
                    if isinstance(municipio_config, dict):
                        municipio_name = municipio_config.get("nombre") or None

                if tenant_profile and isinstance(getattr(tenant_profile, "configuracion", None), dict):
                    tenant_config = tenant_profile.configuracion or {}
                    assistant_name = tenant_config.get("assistant_name") or tenant_config.get("bot_name")

                if is_municipio_client:
                    tenant_name_for_welcome = (
                        municipio_name
                        or tenant_config.get("nombre_municipio")
                        or tenant_config.get("nombre")
                        or (tenant_profile.nombre if tenant_profile else None)
                        or getattr(client_user, "nombre_empresa", None)
                        or getattr(client_user, "name", None)
                        or "Tu Municipio"
                    )
                    municipio_name, assistant_name = _resolve_public_municipio_identity(
                        tenant_name=tenant_name_for_welcome,
                        assistant_name=assistant_name,
                        tenant_profile=tenant_profile,
                        client_user=client_user,
                        tenant_config=tenant_config,
                    )

                should_send_template = bool(template_sid) and not template_state.get("disabled", False)
                should_send_sticker = bool(resolved_sticker_url) and not sticker_state.get("disabled", False)
                sticker_metadata_allowed = True
                public_welcome_identity = (
                    f"{assistant_name} de {municipio_name}"
                    if is_municipio_client and assistant_name
                    else municipio_name
                    or assistant_name
                    or "Chatboc"
                )
                template_variables_payload: Dict[str, str] = {
                    "1": user_name or "",
                    "2": public_welcome_identity,
                }

                if tenant_profile and isinstance(getattr(tenant_profile, "configuracion", None), dict):
                    if assistant_name and not is_municipio_client:
                        template_variables_payload["2"] = assistant_name

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
                            _log(
                                "warning",
                                "[WELCOME] Invalid sticker cooldown override for PYME owner=%s",
                                getattr(client_user, "id", "<unknown>"),
                            )

                    if "template_sid" in pyme_welcome_overrides:
                        template_ref = str(pyme_welcome_overrides.get("template_sid") or "").strip()
                        if template_ref.startswith("HX"):
                            template_sid = template_ref
                        else:
                            template_sid = _resolve_approved_whatsapp_template_sid(
                                template_ref,
                                tenant_profile=tenant_profile,
                                language=str(
                                    pyme_welcome_overrides.get("template_language")
                                    or pyme_welcome_overrides.get("language")
                                    or "es"
                                ),
                            )
                            if template_ref and not template_sid:
                                _log(
                                    "warning",
                                    "[WELCOME] PYME template override is not approved/resolved; using fallback menu reference_kind=%s",
                                    "provider_sid" if template_ref.startswith("HX") else "registry_name",
                                )
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
                        _log("info", "[WELCOME] Sticker suppressed due to override keyword")
                    should_send_sticker = False
                    sticker_metadata_allowed = False

                last_sticker_ts = (
                    sticker_state.get("last_attempt_ts")
                    or sticker_state.get("last_provider_accepted_ts")
                    or sticker_state.get("last_sent_ts")  # legacy state
                )
                if should_send_sticker and last_sticker_ts:
                    if (now - last_sticker_ts) < max(0, sticker_cooldown):
                        should_send_sticker = False
                        _log("info", "[WELCOME] Sticker skipped due to cooldown")

                template_submitted = False
                sticker_submitted = False

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
                        provider_message = _send_twilio_message(twilio_client, **params)
                        submission_state = _mark_welcome_outbound_submission(
                            template_state,
                            provider_message,
                            timestamp=now,
                        )
                        safe_flag_modified(session_context_db_entry, "context_data")
                        template_submitted = True
                        _log(
                            "info",
                            "[WELCOME] Template submitted template_ref=%s variable_count=%s outcome=%s",
                            _safe_provider_reference(template_sid),
                            len(template_variables_payload),
                            submission_state,
                        )
                    except Exception as exc:
                        _log(
                            "warning",
                            "[WELCOME] Failed to send welcome template template_ref=%s error_type=%s",
                            _safe_provider_reference(template_sid),
                            type(exc).__name__,
                        )
                        template_state["disabled"] = True
                        safe_flag_modified(session_context_db_entry, "context_data")

                if should_send_sticker and not is_override:
                    try:
                        provider_message = _send_twilio_message(
                            twilio_client,
                            from_=to_number_raw,
                            to=from_number_raw,
                            media_url=[resolved_sticker_url],
                        )
                        submission_state = _mark_welcome_outbound_submission(
                            sticker_state,
                            provider_message,
                            timestamp=now,
                        )
                        safe_flag_modified(session_context_db_entry, "context_data")
                        sticker_submitted = True
                        _log(
                            "info",
                            "[WELCOME] Sticker submitted outcome=%s",
                            submission_state,
                        )
                        # Avoid re-attaching the same sticker through the delayed payload.
                        sticker_metadata_allowed = False
                    except Exception as exc:
                        _log(
                            "warning",
                            "[WELCOME] Failed to send welcome sticker error_type=%s",
                            type(exc).__name__,
                        )
                        sticker_state["disabled"] = True
                        safe_flag_modified(session_context_db_entry, "context_data")

                greeting_submitted = False

                tenant_name = municipio_name or "Tu Municipio"
                assistant_name = assistant_name or None
                tenant_config = tenant_config or {}
                if tenant_profile and isinstance(getattr(tenant_profile, "configuracion", None), dict):
                    tenant_config = tenant_profile.configuracion or tenant_config
                    assistant_name = assistant_name or tenant_config.get("assistant_name") or tenant_config.get("bot_name")
                    tenant_name = (
                        municipio_name
                        or tenant_config.get("nombre_municipio")
                        or tenant_config.get("nombre")
                        or tenant_profile.nombre
                        or tenant_name
                    )
                elif client_user:
                    tenant_name = (
                        municipio_name
                        or getattr(client_user, "nombre_empresa", None)
                        or getattr(client_user, "name", None)
                        or tenant_name
                    )

                tenant_name, assistant_name = _resolve_public_municipio_identity(
                    tenant_name=tenant_name,
                    assistant_name=assistant_name,
                    tenant_profile=tenant_profile,
                    client_user=client_user,
                    tenant_config=tenant_config,
                )

                greeting_name = f"{assistant_name} de {tenant_name}" if assistant_name else tenant_name

                if user_name is not None:
                    greeting = (
                        f"*¡Hola, {user_name}!* Acá *{greeting_name}* \U0001F44B"
                        if user_name
                        else f"*¡Hola!* Soy *{greeting_name}* \U0001F44B ¿Cómo te llamás?"
                    )
                    try:
                        _send_twilio_message(
                            twilio_client,
                            from_=to_number_raw, to=from_number_raw, body=greeting
                        )
                        greeting_submitted = True
                    except Exception as exc:
                        greeting_submitted = False
                        _log(
                            "error",
                            "[WELCOME] Failed to send welcome greeting error_type=%s",
                            type(exc).__name__,
                        )

                if not user_name and greeting_submitted:
                    session_context_db_entry.context_data["awaiting_user_name"] = True
                    safe_flag_modified(session_context_db_entry, "context_data")
                    db.session.commit()
                    return "OK", 200
            except Exception as exc:
                _log(
                    "error",
                    "[WELCOME] Failed to send welcome template or sticker error_type=%s",
                    type(exc).__name__,
                )

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
                reduced_menu = (
                    template_submitted
                    or greeting_submitted
                    or sticker_submitted
                )
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

                    _prepare_cached_welcome_audio(
                        welcome_response_payload,
                        tenant_profile=tenant_profile,
                        client_user=client_user,
                    )
                    welcome_response_payload["_preserve_welcome_header"] = True

                    _strip_duplicate_welcome_media(
                        welcome_response_payload,
                        sticker_urls=[resolved_sticker_url, configured_sticker_url],
                        base_url=effective_media_base_url,
                    )

                    sticker_payload = list(
                        dict.fromkeys(
                            url
                            for url in [
                                resolved_sticker_url,
                                _resolve_public_media_url(configured_sticker_url, request_root_stripped),
                            ]
                            if url
                        )
                    )
                    if sticker_payload and sticker_metadata_allowed:
                        welcome_response_payload["_welcome_sticker_urls"] = sticker_payload
                        welcome_response_payload["_preserve_welcome_header"] = True
                    else:
                        welcome_response_payload.pop("_welcome_sticker_urls", None)
                        if not sticker_metadata_allowed:
                            welcome_response_payload.pop("_preserve_welcome_header", None)

                    remaining_image_url = welcome_response_payload.get("image_url")
                    resolved_existing_image = _resolve_public_media_url(
                        remaining_image_url, request_root_stripped
                    )
                    if resolved_existing_image:
                        welcome_response_payload["image_url"] = resolved_existing_image
                    elif "image_url" in welcome_response_payload:
                        welcome_response_payload.pop("image_url", None)

                    existing_audio_url = welcome_response_payload.get("audio_url")
                    resolved_existing_audio = _resolve_public_media_url(existing_audio_url, request_root_stripped)
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
                _log("info", "[WELCOME] Scheduled delayed menu")
            except Exception as exc:
                _log(
                    "error",
                    "[WELCOME] Failed to schedule delayed menu error_type=%s",
                    type(exc).__name__,
                )

        return "OK", 200
    elif should_trigger_welcome and is_rate_limited:
        _log("info", "[WELCOME] Welcome skipped due to rate-limit")
    elif is_greeting and is_waiting_for_info:
        _log("info", "[WELCOME] Welcome skipped because bot is waiting for info")

    # Determine incoming text before any special handling (re-declaration to ensure it's available for the rest of the code)
    list_id = post_vars.get("ListId")
    incoming_text = flow_incoming_text or button_payload or list_id or post_vars.get("Body", "")

    if not flow_submission_present and session_context_db_entry.context_data.get("awaiting_user_name"):
        name_candidate = incoming_text.strip()
        if name_candidate:
            try:
                extracted = extract_multiple_contact_details_llm(name_candidate, ["nombre"])
            except Exception as exc:
                _log(
                    "error",
                    "[WELCOME] Name extraction failed error_type=%s",
                    type(exc).__name__,
                )
                extracted = {}
            new_name = _extract_requested_contact_name(name_candidate, extracted)
            if not new_name:
                if twilio_client:
                    _send_twilio_message(
                        twilio_client,
                        from_=to_number_raw,
                        to=from_number_raw,
                        body="No pude tomar tu nombre. Decime por favor como te llamas.",
                    )
                return "OK", 200

            if end_user:
                update_user_profile(end_user, {"name": new_name})
            _remember_contact_name(session_context_db_entry, new_name)
            session_context_db_entry.context_data.pop("awaiting_user_name", None)
            personalized_sticker_submitted = _send_welcome_sticker(
                client=twilio_client,
                to_number_raw=to_number_raw,
                from_number_raw=from_number_raw,
                resolved_sticker_url=resolved_sticker_url,
                session_context=session_context_db_entry,
                state_key="personalized_name_provider_accepted_ts",
            )
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.add(session_context_db_entry)
            db.session.commit()
            if twilio_client:
                _send_twilio_message(
                    twilio_client,
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

                    _prepare_cached_welcome_audio(
                        welcome_response_payload,
                        tenant_profile=tenant_profile,
                        client_user=client_user,
                    )
                    welcome_response_payload["_preserve_welcome_header"] = True

                    _strip_duplicate_welcome_media(
                        welcome_response_payload,
                        sticker_urls=[resolved_sticker_url, configured_sticker_url],
                        base_url=effective_media_base_url,
                    )

                    sticker_payload = list(
                        dict.fromkeys(
                            url
                            for url in [
                                resolved_sticker_url,
                                _resolve_public_media_url(configured_sticker_url, request_root_stripped),
                            ]
                            if url
                        )
                    )
                    if (
                        sticker_payload
                        and not personalized_sticker_submitted
                        and not sticker_state.get("disabled", False)
                    ):
                        welcome_response_payload["_welcome_sticker_urls"] = sticker_payload
                        welcome_response_payload["_preserve_welcome_header"] = True
                    else:
                        welcome_response_payload.pop("_welcome_sticker_urls", None)
                        welcome_response_payload.pop("_preserve_welcome_header", None)

                    remaining_image_url = welcome_response_payload.get("image_url")
                    resolved_existing_image = _resolve_public_media_url(
                        remaining_image_url, request_root_stripped
                    )
                    if resolved_existing_image:
                        welcome_response_payload["image_url"] = resolved_existing_image
                    elif "image_url" in welcome_response_payload:
                        welcome_response_payload.pop("image_url", None)

                    existing_audio_url = welcome_response_payload.get("audio_url")
                    resolved_existing_audio = _resolve_public_media_url(existing_audio_url, request_root_stripped)
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
                _log("info", "[WELCOME] Scheduled delayed menu after name capture")
            except Exception as exc:
                _log(
                    "error",
                    "[WELCOME] Failed to schedule delayed menu after name error_type=%s",
                    type(exc).__name__,
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
            _send_twilio_message(
                twilio_client,
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
                _send_twilio_message(
                    twilio_client,
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
    media_content = None
    media_processing_error_code = None
    media_understanding_error_code = None
    skip_media_analysis = False
    message_body = incoming_text

    if media_url and media_content_type:
        try:
            # Download the file from Twilio's URL first
            # Media belongs to the account that received the message.  A
            # tenant connected through a Twilio subaccount cannot download
            # that media with the platform parent's credentials.
            auth = (credentials.account_sid, credentials.auth_token)
            try:
                media_timeout = float(current_app.config.get("WHATSAPP_MEDIA_DOWNLOAD_TIMEOUT_SECONDS", 12))
            except (TypeError, ValueError):
                media_timeout = 12.0
            try:
                media_max_bytes = int(
                    current_app.config.get("WHATSAPP_MEDIA_MAX_BYTES", 25 * 1024 * 1024)
                )
            except (TypeError, ValueError, OverflowError):
                media_max_bytes = 25 * 1024 * 1024
            media_max_bytes = min(100 * 1024 * 1024, max(1024, media_max_bytes))
            r = requests.get(
                media_url,
                auth=auth,
                stream=True,
                timeout=max(1.0, media_timeout),
            )
            try:
                r.raise_for_status()
                media_content = read_bounded_response_body(
                    r,
                    max_bytes=media_max_bytes,
                )
            finally:
                r.close()

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
                _log(
                    "info",
                    "WhatsApp media processed attachment_id=%s media_type=%s byte_count=%s",
                    adjunto.id,
                    media_content_type.split(";", 1)[0],
                    len(media_content),
                )
            else:
                current_app.logger.error("create_attachment_with_thumbnail failed to process the WhatsApp media")
                media_processing_error_code = "attachment_storage_failed"

            if is_audio_media_type(media_content_type):
                session_context_db_entry.context_data['source_is_audio'] = True
                from services.audio_transcription_service import transcribe_audio_bytes

                transcribed_text = transcribe_audio_bytes(
                    media_content,
                    media_content_type,
                    cache_url=media_url,
                )
                if transcribed_text:
                    message_body = transcribed_text
                    if uploaded_file_info is None:
                        uploaded_file_info = {
                            "url": media_url,
                            "mime_type": media_content_type,
                            "name": file_name,
                            "source": "whatsapp",
                        }
                    uploaded_file_info['transcribed_text'] = transcribed_text
                else:
                    current_app.logger.warning("Audio transcription failed or returned empty.")
                    media_understanding_error_code = "audio_transcription_unavailable"

            # --- Voice Bot / WhatsApp Media Bridge ---
            # If we were waiting for media for a specific ticket, link it now.
            awaiting_ticket_nro = session_context_db_entry.context_data.get("awaiting_photo_for_ticket")
            awaiting_ticket_photo = session_context_db_entry.context_data.get("awaiting_ticket_photo")
            awaiting_ticket_photo_until = session_context_db_entry.context_data.get(
                "awaiting_ticket_photo_until"
            )
            last_ticket_code = session_context_db_entry.context_data.get("last_ticket_code")
            is_ticket_media = bool(media_content_type)

            if not is_audio_media_type(media_content_type):
                session_context_db_entry.context_data.pop('source_is_audio', None)

            if adjunto and is_ticket_media:
                now_ts = time.time()
                active_followup = _active_municipal_ticket_followup(
                    session_context_db_entry.context_data,
                    now_ts=now_ts,
                )
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
                if active_followup:
                    target_ticket_ref = active_followup.get("ticket_nro")
                elif within_photo_window:
                    target_ticket_ref = last_ticket_code or awaiting_ticket_nro

                if target_ticket_ref:
                    try:
                        municipio_owner_id = None
                        if client_user and getattr(client_user, "tipo_chat", "") == "municipio":
                            municipio_owner_id = (
                                getattr(client_user, "municipio_id", None)
                                or getattr(client_user, "id", None)
                            )
                        ticket_tenant_id = (
                            getattr(tenant_profile, "id", None)
                            or session_context_db_entry.tenant_id
                        )
                        ticket = _find_municipio_ticket_for_reference(
                            target_ticket_ref,
                            tenant_id=ticket_tenant_id,
                            municipio_id=municipio_owner_id,
                            anon_id=from_number_cleaned,
                        )
                        current_app.logger.info(
                            "[VOICE_BRIDGE] Received media for ticket %s (resolved=%s)",
                            target_ticket_ref,
                            getattr(ticket, "nro_ticket", None),
                        )
                        if ticket and not _municipal_ticket_is_open(ticket):
                            _clear_municipal_ticket_followup(
                                session_context_db_entry.context_data
                            )
                            session_context_db_entry.context_data.pop(
                                "last_options_sent", None
                            )
                            safe_flag_modified(
                                session_context_db_entry, "context_data"
                            )
                            db.session.add(session_context_db_entry)
                            db.session.commit()
                            if twilio_client:
                                _send_twilio_message(
                                    twilio_client,
                                    from_=to_number_raw,
                                    to=from_number_raw,
                                    body=(
                                        f"El reclamo *{target_ticket_ref}* ya está cerrado. "
                                        "No adjunté el archivo; si es otro problema, "
                                        "iniciá un reclamo nuevo."
                                    ),
                                )
                            return "OK", 200

                        if _municipal_ticket_is_open(ticket):
                            if is_audio_media_type(media_content_type):
                                transcript = str(
                                    (uploaded_file_info or {}).get("transcribed_text") or ""
                                ).strip()
                                evidence_comment = (
                                    f"Nota de voz del vecino: {transcript[:3500]}"
                                    if transcript
                                    else "[SISTEMA] Vecino adjuntó una nota de voz por WhatsApp."
                                )
                            elif media_content_type.startswith("image/"):
                                evidence_comment = (
                                    "[SISTEMA] Vecino adjuntó una imagen como evidencia por WhatsApp."
                                )
                            else:
                                evidence_comment = (
                                    "[SISTEMA] Vecino adjuntó un archivo como evidencia por WhatsApp."
                                )
                            _attach_whatsapp_adjunto_to_ticket(
                                adjunto=adjunto,
                                ticket=ticket,
                                end_user=end_user,
                                comentario_text=evidence_comment,
                                **_durable_whatsapp_ticket_effect_kwargs(
                                    durable_claim,
                                    ticket_tenant_id,
                                    f"ticket_attachment_comment:{ticket.id}:{adjunto.id}",
                                ),
                            )
                            if twilio_client:
                                _send_twilio_message(
                                    twilio_client,
                                    from_=to_number_raw,
                                    to=from_number_raw,
                                    body=(
                                        "✅ Archivo recibido y asociado al reclamo "
                                        f"*{ticket.nro_ticket}*."
                                    ),
                                )
                            # The optional-photo prompt is one-shot, but the
                            # structured ticket follow-up window remains open
                            # so a second photo, audio or comment is not routed
                            # back into a completed claim draft.
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
                            _send_twilio_message(
                                twilio_client,
                                from_=to_number_raw,
                                to=from_number_raw,
                                body=(
                                    "⚠️ Todavía no encuentro el ticket en sistema. "
                                    "Respondé con el número del ticket para adjuntar el archivo."
                                ),
                            )
                        return "OK", 200
                    except Exception as e_bridge:
                        db.session.rollback()
                        _log(
                            "error",
                            "[VOICE_BRIDGE] Error attaching media error_type=%s",
                            type(e_bridge).__name__,
                        )
                        return _durable_replay_failure_or_retry(
                            durable_replay,
                            "municipal_ticket_media_followup_failed",
                            e_bridge,
                        )

        except MediaDownloadTooLarge:
            media_processing_error_code = "media_too_large"
            _log(
                "warning",
                "WhatsApp media rejected before buffering reason=media_too_large media_type=%s",
                inbound_content.kind,
            )
        except requests.exceptions.RequestException as exc:
            media_processing_error_code = "media_download_failed"
            _log(
                "error",
                "Error downloading WhatsApp media error_type=%s",
                type(exc).__name__,
            )
        except Exception as exc:
            media_processing_error_code = "media_processing_failed"
            _log(
                "error",
                "Error processing WhatsApp media file error_type=%s",
                type(exc).__name__,
            )
            # Reset uploaded_file_info if processing fails
            uploaded_file_info = None
    else:
        # If no media, ensure the flag is not present
        session_context_db_entry.context_data.pop('source_is_audio', None)

    if inbound_content.has_media and not uploaded_file_info:
        # Do not let a missing/failed media fetch degrade into an empty LLM turn.
        # The already-recorded provider MessageSid keeps this response
        # idempotent in legacy mode, while queue mode stages it in the outbox.
        if twilio_client:
            _send_twilio_message(
                twilio_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=honest_unprocessable_reply(
                    inbound_content.kind,
                    reason=media_processing_error_code or inbound_content.reason,
                ),
            )
        return "OK", 200

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
            except Exception as exc:
                _log(
                    "error",
                    "Error al geocodificar inversamente source=twilio_location error_type=%s",
                    type(exc).__name__,
                )
        if label:
            location_info["label"] = label
        _log(
            "info",
            "Received location data has_coordinates=%s has_address=%s has_label=%s",
            True,
            bool(location_info.get("address")),
            bool(location_info.get("label")),
        )
    else:
        coordenadas = extraer_coordenadas_de_url_google_maps(incoming_text)
        if coordenadas:
            latitud, longitud = coordenadas
            location_info = {"latitude": str(latitud), "longitude": str(longitud)}
            try:
                addr = geocodificar_inversa_llm(latitud, longitud)
                if addr and addr.get("formatted_address"):
                    location_info["address"] = addr["formatted_address"]
            except Exception as exc:
                _log(
                    "error",
                    "Error al geocodificar inversamente source=message_link error_type=%s",
                    type(exc).__name__,
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
            ticket_tenant_id = (
                getattr(tenant_profile, "id", None)
                or session_context_db_entry.tenant_id
            )
            ticket = _find_municipio_ticket_for_reference(
                message_body,
                tenant_id=ticket_tenant_id,
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
                        **_durable_whatsapp_ticket_effect_kwargs(
                            durable_claim,
                            ticket_tenant_id,
                            f"pending_attachment_comment:{ticket.id}:{adjunto.id}",
                        ),
                    )
                session_context_db_entry.context_data.pop("pending_attachment_id", None)
                session_context_db_entry.context_data.pop("pending_ticket_code", None)
                session_context_db_entry.context_data.pop("pending_attachment_until", None)
                session_context_db_entry.context_data.pop("awaiting_ticket_photo", None)
                session_context_db_entry.context_data.pop("awaiting_ticket_photo_until", None)
                safe_flag_modified(session_context_db_entry, "context_data")
                db.session.commit()
                if twilio_client:
                    _send_twilio_message(
                        twilio_client,
                        from_=to_number_raw,
                        to=from_number_raw,
                        body=f"✅ Listo. Adjunté la foto al ticket *{ticket.nro_ticket}*.",
                    )
                return "OK", 200

    human_chat_pending = bool(
        session_context_db_entry.context_data.get("human_chat_in_progress")
        or session_context_db_entry.context_data.get("room")
    )
    requires_honest_media_fallback = bool(
        media_understanding_error_code == "audio_transcription_unavailable"
        or inbound_content.kind in {"video", "sticker", "contact", "unsupported_media"}
    )
    if uploaded_file_info and requires_honest_media_fallback and not human_chat_pending:
        # An open municipal ticket was handled inside the media bridge above.
        # Outside exact human/ticket routing, preserve the current dialog state
        # and ask for usable context instead of treating a filename, vCard or
        # unanalysed video as understood language.
        if twilio_client:
            _send_twilio_message(
                twilio_client,
                from_=to_number_raw,
                to=from_number_raw,
                body=honest_unprocessable_reply(
                    inbound_content.kind,
                    reason=media_understanding_error_code,
                ),
            )
        return "OK", 200

    if (
        uploaded_file_info
        and media_content
        and tenant_profile
        and client_user
        and not force_chatboc_demo_hub
        and not session_context_db_entry.context_data.get("human_chat_in_progress")
        and not session_context_db_entry.context_data.get("room")
    ):
        try:
            assisted_intake = create_whatsapp_assisted_intake(
                tenant=tenant_profile,
                owner_user=client_user,
                end_user=end_user,
                session_id=chat_session_id_internal,
                from_number=from_number_cleaned,
                message_body=message_body,
                uploaded_file_info=uploaded_file_info,
                media_bytes=media_content,
                location_info=location_info,
                idempotency_key=media_message_sid or message_sid,
            )
        except Exception as exc:  # noqa: BLE001
            assisted_intake = None
            _log(
                "error",
                "[WHATSAPP_ASSISTED_INTAKE] Error creando intake asistido error_type=%s",
                type(exc).__name__,
            )

        if assisted_intake and (assisted_intake.get("created") or assisted_intake.get("idempotent_replay")):
            session_context_db_entry.context_data["last_whatsapp_assisted_intake"] = {
                "ticket_id": assisted_intake.get("ticket_id"),
                "pedido_id": assisted_intake.get("pedido_id"),
                "request_kind": assisted_intake.get("request_kind"),
                "created": bool(assisted_intake.get("created")),
            }
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.add(session_context_db_entry)
            db.session.commit()
            if twilio_client:
                ack_text = assisted_intake.get("customer_message") or (
                    "Recibimos tu archivo y lo dejamos cargado para revision del equipo."
                )
                ticket_id = assisted_intake.get("ticket_id")
                if ticket_id:
                    ack_text = f"{ack_text}\n\nCaso CRM: #{ticket_id}"
                _send_twilio_message(
                    twilio_client,
                    from_=to_number_raw,
                    to=from_number_raw,
                    body=ack_text[:MAX_TWILIO_BODY_LENGTH],
                )
            return "OK", 200

    # --- Recently-created municipal ticket follow-up ---
    # This runs before numeric-menu translation so options from the completed
    # confirmation card can never reinterpret a PIN question, comment or emoji
    # as a second confirmation/cancellation.
    try:
        followup_result = _handle_recent_municipal_ticket_followup(
            session_context=session_context_db_entry,
            client_user=client_user,
            tenant_profile=tenant_profile,
            end_user=end_user,
            from_number_cleaned=from_number_cleaned,
            message_body=message_body,
            action=button_payload or list_id,
            twilio_message_client=twilio_client,
            to_number_raw=to_number_raw,
            from_number_raw=from_number_raw,
            durable_claim=durable_claim,
        )
    except Exception as followup_exc:
        db.session.rollback()
        _log(
            "error",
            "[WHATSAPP_FOLLOWUP] Failed to persist municipal ticket follow-up error_type=%s",
            type(followup_exc).__name__,
        )
        return _durable_replay_failure_or_retry(
            durable_replay,
            "municipal_ticket_followup_failed",
            followup_exc,
        )
    if followup_result is not None:
        return followup_result

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
                _send_twilio_message(
                    twilio_client,
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
    if (
        not flow_submission_present
        and human_chat_active
        and (message_body or uploaded_file_info or location_info)
    ):
        should_route_live_chat = not bool(selected_option)
        if should_route_live_chat:
            tipo_ticket, live_ticket = _find_live_chat_ticket(
                client_user,
                end_user,
                from_number_cleaned,
                tenant_profile,
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

                    candidate_attachment = db.session.get(
                        ArchivoAdjunto,
                        uploaded_file_info.get("id"),
                    )
                    expected_user_id = getattr(end_user, "id", None)
                    identity_matches = bool(
                        candidate_attachment
                        and (
                            expected_user_id is None
                            or candidate_attachment.user_id == expected_user_id
                        )
                        and (
                            candidate_attachment.session_id is None
                            or candidate_attachment.session_id == chat_session_id_internal
                        )
                    )
                    if tipo_ticket == "municipio":
                        target_matches = bool(
                            candidate_attachment
                            and candidate_attachment.pyme_ticket_id is None
                            and candidate_attachment.municipio_ticket_id in {None, live_ticket.id}
                        )
                    else:
                        target_matches = bool(
                            candidate_attachment
                            and candidate_attachment.municipio_ticket_id is None
                            and candidate_attachment.pyme_ticket_id in {None, live_ticket.id}
                        )
                    if not identity_matches or not target_matches:
                        _log(
                            "warning",
                            "Rejected unsafe live-chat attachment binding ticket_type=%s ticket_id=%s",
                            tipo_ticket,
                            live_ticket.id,
                        )
                        uploaded_file_info = None
                    elif tipo_ticket == "municipio":
                        candidate_attachment.municipio_ticket_id = live_ticket.id
                        db.session.add(candidate_attachment)
                    else:
                        candidate_attachment.pyme_ticket_id = live_ticket.id
                        db.session.add(candidate_attachment)

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
                    **_durable_whatsapp_ticket_effect_kwargs(
                        durable_claim,
                        getattr(live_ticket, "tenant_id", None)
                        or getattr(tenant_profile, "id", None)
                        or session_context_db_entry.tenant_id,
                        f"live_chat_comment:{tipo_ticket}:{live_ticket.id}",
                    ),
                )

                try:
                    db.session.commit()
                except Exception as exc:
                    _log(
                        "error",
                        "[WHATSAPP_WEBHOOK] Error guardando mensaje de chat en vivo error_type=%s",
                        type(exc).__name__,
                    )
                    db.session.rollback()
                else:
                    if nuevo_comentario:
                        try:
                            from socket_service import emit_new_chat_message

                            room_name = f"ticket_{tipo_ticket}_{live_ticket.id}"
                            emit_new_chat_message(
                                {
                                    "socket_room": room_name,
                                    "tenant_type": tipo_ticket,
                                    "ticket_id": live_ticket.id,
                                    "channel": "whatsapp",
                                    "message": nuevo_comentario.to_dict(),
                                }
                            )
                        except Exception as socket_exc:
                            _log(
                                "error",
                                "[WHATSAPP_WEBHOOK] Error emitiendo mensaje en vivo error_type=%s",
                                type(socket_exc).__name__,
                            )

                return "OK", 200

    # Legacy room relays are not a valid authorization boundary. If no exact
    # open ticket was resolved above, clear stale state and resume normal bot
    # handling instead of emitting to a client-controlled/stored room.
    if session_context_db_entry.context_data.get("human_chat_in_progress"):
        current_app.logger.warning(
            "[WHATSAPP_WEBHOOK] Clearing stale live-chat state without exact ticket session=%s",
            chat_session_id_internal,
        )
        session_context_db_entry.context_data.pop("human_chat_in_progress", None)
        session_context_db_entry.context_data.pop("room", None)
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()


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

    if flow_completion_payload:
        # Entity and realtime metadata are operational-only. Keep them out of
        # response normalization and every outbound WhatsApp payload.
        bot_response_dict = {
            key: value
            for key, value in flow_completion_payload.items()
            if key not in {"entity", "realtime_event"}
        }

    if force_chatboc_demo_hub and not flow_submission_present:
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
        current_demo_usage, message_limit = _chatboc_demo_usage_snapshot(session_context_db_entry)
        was_over_or_at_limit = current_demo_usage >= message_limit
        can_reset_demo_usage = _can_reset_chatboc_demo_usage(from_number_cleaned)
        can_use_demo_unlimited = _can_use_chatboc_demo_unlimited(from_number_cleaned)
        if was_over_or_at_limit and not selected_action_id:
            selected_action_id = _resolve_chatboc_demo_limit_decision_action(message_body_for_demo)
        normalized_demo_action = _normalize_chatboc_demo_text(selected_action_id)
        normalized_demo_text = _normalize_chatboc_demo_text(message_body_for_demo)
        should_send_chatboc_demo_sticker = (
            is_greeting
            or normalized_demo_action in {"menu_principal", "cancelar"}
            or normalized_demo_text in {"hola", "buenas", "menu", "menú", "inicio"}
        )
        if should_send_chatboc_demo_sticker:
            chatboc_demo_sticker_url = _resolve_public_media_url(
                current_app.config.get("CHATBOC_DEMO_WELCOME_MEDIA_URL"),
                request_root_stripped,
            )
            _send_welcome_sticker(
                client=twilio_client,
                to_number_raw=to_number_raw,
                from_number_raw=from_number_raw,
                resolved_sticker_url=chatboc_demo_sticker_url,
                session_context=session_context_db_entry,
                state_key="chatboc_demo_hub",
            )
        url_demo_turn = bool(selected_option and selected_option.get("url"))
        sales_demo_turn = normalized_demo_action in {"chatboc_sales_lead", "capturar_lead_comercial"}
        limit_decision_turn = normalized_demo_action in {
            "chatboc_demo_limit_contact_yes",
            "chatboc_demo_limit_contact_no",
        }
        reset_demo_turn = _is_chatboc_demo_reset_turn(
            selected_action_id=selected_action_id,
            selected_option=selected_option,
            message_body=message_body_for_demo,
            over_limit=was_over_or_at_limit,
        )
        limit_bypass_turn = (
            can_use_demo_unlimited
            or url_demo_turn
            or sales_demo_turn
            or limit_decision_turn
            or (reset_demo_turn and (not was_over_or_at_limit or can_reset_demo_usage))
        )
        if can_use_demo_unlimited:
            _reset_chatboc_demo_usage_for_navigation(
                session_context_db_entry,
                reason="authorized_unlimited",
            )
            used_messages, message_limit = _chatboc_demo_usage_snapshot(session_context_db_entry)
        elif reset_demo_turn and was_over_or_at_limit and can_reset_demo_usage:
            _reset_chatboc_demo_usage_for_navigation(
                session_context_db_entry,
                reason="limit_navigation",
            )
            used_messages, message_limit = _chatboc_demo_usage_snapshot(session_context_db_entry)
        elif limit_bypass_turn:
            used_messages, message_limit = current_demo_usage, message_limit
        else:
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
        if used_messages > message_limit and not limit_bypass_turn:
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
    elif not flow_submission_present:
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
            durable_claim=durable_claim,
        )
    if education_direct_payload:
        bot_response_dict = education_direct_payload

    # Check if we should bypass the bot logic because the user selected a URL option
    bypass_bot_logic = bool(
        education_direct_payload
        or chatboc_demo_direct_payload
        or flow_completion_payload
    )
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
        _log(
            "info",
            "Intercepted numeric selection for URL option has_url=%s",
            bool(url_link),
        )

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
                if not skip_media_analysis and not is_audio_media_type(mime_type):
                    interpretacion_media_data = clasificar_adjunto_whatsapp(uploaded_file_info, client_user)
            # Location info should not be treated as interpreted media.
            # It should be passed directly as location data.

            kwargs_for_bot = {
                "source_channel": "whatsapp",
                "tenant_profile": tenant_profile,
                "tenant_id": tenant_id,
            }
            if durable_replay:
                kwargs_for_bot.update(
                    {
                        "source_event_id": message_sid,
                        "durable_turn_id": getattr(durable_claim, "turn_id", None),
                        "idempotency_key": (
                            f"whatsapp:{tenant_id}:"
                            f"{getattr(durable_claim, 'turn_id', '')}"
                        ),
                    }
                )
            if safe_flow_submission:
                kwargs_for_bot["whatsapp_flow_submission"] = deepcopy(safe_flow_submission)
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
                _log(
                    "debug",
                    "[CONTACT_EXTRACTION] Extracting contact fields requested_count=%s body_length=%s",
                    len(potential_fields),
                    len(message_body or ""),
                )
                extracted_data = extract_multiple_contact_details_llm(message_body, potential_fields)
                _log(
                    "debug",
                    "[CONTACT_EXTRACTION] Extraction completed extracted_count=%s",
                    sum(bool(extracted_data.get(field)) for field in potential_fields),
                )

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

            _log(
                "debug",
                "responder_chatboc response metadata type=%s keys=%s body_length=%s",
                type(bot_response_dict).__name__,
                sorted(str(key) for key in bot_response_dict.keys())[:24]
                if isinstance(bot_response_dict, dict)
                else [],
                len(str(bot_response_dict.get("message_body") or ""))
                if isinstance(bot_response_dict, dict)
                else 0,
            )

            # Validate the response from the bot logic
            if not isinstance(bot_response_dict, dict):
                _log(
                    "warning",
                    "responder_chatboc returned an invalid response type=%s",
                    type(bot_response_dict).__name__,
                )
                if durable_replay:
                    raise WhatsAppDurableReplayError(
                        "responder_chatboc_invalid_response"
                    )
                # Keep the default error response initialized earlier.
                bot_response_dict = {
                    'message_body': "Lo siento, hubo un error interno al procesar tu mensaje.",
                    'options_list': [], 'message_type': 'text', 'fuente': 'error_handler_non_dict_response'
                }

            # Ensure context_data is a dict for saving
            if not isinstance(session_context_db_entry.context_data, dict):
                _log(
                    "warning",
                    "context_data in session_context_db_entry is not a dict. Resetting.",
                )
                session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format."}],
                    "estado_conversacion": "error_context"
                }

    except Exception as exc:
        _log(
            "error",
            "Error calling real chatbot logic (responder_chatboc) error_type=%s",
            type(exc).__name__,
        )
        if durable_replay:
            raise WhatsAppDurableReplayError("responder_chatboc_failed") from exc
        # Legacy mode keeps its user-facing fallback response.

    # Update respuesta_del_bot_text for logging from the final bot_response_dict
    respuesta_del_bot_text = bot_response_dict.get("message_body", "")
    _log(
        "debug",
        "Bot response prepared. body_length=%s context_metadata=%s",
        len(respuesta_del_bot_text) if isinstance(respuesta_del_bot_text, str) else 0,
        _safe_session_context_metadata(session_context_db_entry.context_data),
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

        response_base_url = (
            (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
            or (request.url_root or "").rstrip("/")
        )
        _normalize_whatsapp_flow_contract(bot_response_dict, base_url=response_base_url)
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
        _log(
            "info",
            "[CONTEXT_WHATSAPP] Context metadata db_key_count=%s update_key_count=%s",
            len(db_context) if isinstance(db_context, dict) else 0,
            len(updated_context) if isinstance(updated_context, dict) else 0,
        )


        # Merge the contexts
        if updated_context:
            merged_context = {**db_context, **updated_context}
        else:
            merged_context = db_context

        for key_to_delete in bot_response_dict.get("_context_keys_to_delete") or []:
            merged_context.pop(key_to_delete, None)

        claim_completion = bot_response_dict.get("_reclamo_completion")
        if isinstance(claim_completion, dict):
            _apply_completed_reclamo_context(merged_context, claim_completion)

        _log(
            "info",
            "[CONTEXT_WHATSAPP] Context merged key_count=%s has_claim_completion=%s",
            len(merged_context) if isinstance(merged_context, dict) else 0,
            isinstance(claim_completion, dict),
        )


        # Save the merged context
        session_context_db_entry.context_data = merged_context
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        if not force_chatboc_demo_hub and tenant_profile and getattr(tenant_profile, "id", None):
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
                _log(
                    "warning",
                    "[CRM] WhatsApp contact enrichment skipped tenant_id=%s error_type=%s",
                    getattr(tenant_profile, "id", None),
                    type(crm_err).__name__,
                )
        db.session.commit()
        _log(
            "info",
            "Session saved. tenant_id=%s user_id=%s context_metadata=%s",
            getattr(session_context_db_entry, "tenant_id", None),
            getattr(session_context_db_entry, "user_id", None),
            _safe_session_context_metadata(session_context_db_entry.context_data),
        )

    except Exception as exc:
        db.session.rollback()
        _log(
            "error",
            "Error formatting response or saving session session_id=%s error_type=%s",
            chat_session_id_internal,
            type(exc).__name__,
        )
        # A 200 here permanently acknowledges an event that we did not store.
        # The queue worker must observe an exception and transition its durable
        # turn to retry_wait; the direct webhook asks Twilio for a bounded 5xx
        # retry instead.
        return _durable_replay_failure_or_retry(
            durable_replay,
            "response_format_or_session_commit_failed",
            exc,
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
                tenant_profile=tenant_profile,
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

            current_app.logger.debug(
                "Sending WhatsApp message metadata=%s",
                _safe_outbound_log_metadata(message_params),
            )

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

                        main_message = _send_twilio_message(twilio_client, **message_params)
                        _log(
                            "info",
                            "Mensaje principal (interactivo) enviado recipient=%s message_ref=%s",
                            _masked_whatsapp_status_address(from_number_raw),
                            _safe_provider_reference(main_message.sid),
                        )

                        for idx, chunk in enumerate(remaining_chunks, start=2):
                            followup_params = {
                                'from_': to_number_raw,
                                'to': from_number_raw,
                                'body': chunk,
                            }
                            followup_message = _send_twilio_message(twilio_client, **followup_params)
                            _log(
                                "info",
                                "Mensaje adicional %s/%s enviado recipient=%s message_ref=%s",
                                idx,
                                len(chunks),
                                _masked_whatsapp_status_address(from_number_raw),
                                _safe_provider_reference(followup_message.sid),
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
                    main_message = _send_twilio_message(twilio_client, **first_chunk_params)
                    _log(
                        "info",
                        "Mensaje parte 1/%s enviado recipient=%s message_ref=%s",
                        len(chunks),
                        _masked_whatsapp_status_address(from_number_raw),
                        _safe_provider_reference(main_message.sid),
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
                        _send_twilio_message(
                            twilio_client,
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
                main_message = _send_twilio_message(twilio_client, **message_params)
                _log(
                    "info",
                    "Mensaje principal enviado recipient=%s message_ref=%s",
                    _masked_whatsapp_status_address(from_number_raw),
                    _safe_provider_reference(main_message.sid),
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
                    current_app.logger.debug(
                        "Sending WhatsApp audio metadata=%s",
                        _safe_outbound_log_metadata(audio_message_params),
                    )
                    audio_message = _send_twilio_message(twilio_client, **audio_message_params)
                    _log(
                        "info",
                        "Mensaje de audio enviado recipient=%s message_ref=%s",
                        _masked_whatsapp_status_address(from_number_raw),
                        _safe_provider_reference(audio_message.sid),
                    )

        except Exception as exc:
            _log(
                "error",
                "Error al enviar mensaje de Twilio error_type=%s",
                type(exc).__name__,
            )
            if durable_replay:
                raise WhatsAppDurableReplayError("outbound_capture_failed") from exc
    else:
        _log("warning", "Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")
        if durable_replay:
            raise WhatsAppDurableReplayError("twilio_client_unavailable")

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
    post_vars = request.form.to_dict()
    from_number = post_vars.get("From") or ""
    provider_sender, sender_resolution_error = _resolve_provider_sender_for_twilio_request(
        to_number_raw=from_number,
        normalized_to=_normalize_whatsapp_address(from_number),
        messaging_service_sid=post_vars.get("MessagingServiceSid") or post_vars.get("ServiceSid"),
    )
    if sender_resolution_error:
        _log(
            "warning",
            "[TWILIO_WHATSAPP_STATUS] Rejected ambiguous sender scope reason=%s service_ref=%s",
            sender_resolution_error,
            _safe_provider_reference(
                post_vars.get("MessagingServiceSid") or post_vars.get("ServiceSid")
            ),
        )
        return "Forbidden", 403

    if provider_sender and not getattr(provider_sender, "tenant", None):
        current_app.logger.error(
            "[TWILIO_WHATSAPP_STATUS] ProviderSender id=%s has no tenant credentials scope",
            getattr(provider_sender, "id", None),
        )
        return "Twilio sender credentials unavailable", 503

    credential_tenant = getattr(provider_sender, "tenant", None)
    if not provider_sender:
        whatsapp_mapping, _, _ = _lookup_whatsapp_mapping(from_number)
        if whatsapp_mapping and whatsapp_mapping.user:
            credential_tenant = _tenant_profile_for_user(whatsapp_mapping.user)

    credentials = _twilio_credentials_for_provider_sender(
        provider_sender,
        tenant=credential_tenant,
    )
    status_validator = _twilio_validator_for_credentials(credentials)
    if not status_validator:
        current_app.logger.error(
            "[TWILIO_WHATSAPP_STATUS] Twilio credentials unavailable sender_id=%s tenant_id=%s scope=%s",
            getattr(provider_sender, "id", None),
            getattr(provider_sender, "tenant_id", None) or getattr(credential_tenant, "id", None),
            credentials.scope,
        )
        return "Twilio validator not configured", 503 if provider_sender or credential_tenant else 500

    if not _twilio_account_sid_matches(post_vars, credentials):
        return "Forbidden", 403

    if not status_validator.validate(
        request.url, request.form, request.headers.get("X-Twilio-Signature", "")
    ):
        return "Forbidden", 403

    tenant, resolved_sender = _resolve_status_callback_tenant_and_sender(post_vars)
    tenant = tenant or credential_tenant
    provider_sender = provider_sender or resolved_sender

    message_sid = request.form.get("MessageSid")
    message_status = request.form.get("MessageStatus")
    error_code = request.form.get("ErrorCode")
    to_number = request.form.get("To")
    from_number = request.form.get("From")

    _log(
        "info",
        "[TWILIO_WHATSAPP_STATUS] message_ref=%s status=%s error_code=%s recipient=%s sender=%s",
        _safe_provider_reference(message_sid),
        message_status,
        error_code,
        _masked_whatsapp_status_address(to_number),
        _masked_whatsapp_status_address(from_number),
    )
    try:
        _persist_twilio_whatsapp_status_event(
            post_vars,
            tenant=tenant,
            provider_sender=provider_sender,
            flow_interaction_id=request.args.get("flow_interaction_id"),
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log(
            "warning",
            "[TWILIO_WHATSAPP_STATUS] Could not persist status callback message_ref=%s status=%s error_type=%s",
            _safe_provider_reference(message_sid),
            message_status,
            type(exc).__name__,
        )
        # The callback is the authoritative asynchronous delivery signal. A
        # successful HTTP response here would acknowledge and permanently lose
        # an event that we failed to persist. Direct and durable callback URLs
        # opt into bounded 5xx retries through Twilio connection overrides.
        return "RETRY", 503

    outbound_attempt_id = str(
        request.args.get("outbound_attempt_id") or ""
    ).strip()
    if outbound_attempt_id and tenant and getattr(tenant, "id", None):
        try:
            from services.whatsapp_inbound_turns import (
                reconcile_whatsapp_outbound_status,
            )

            reconciled = reconcile_whatsapp_outbound_status(
                tenant_id=int(tenant.id),
                attempt_id=outbound_attempt_id,
                provider_message_sid=message_sid,
                provider_status=message_status,
                provider_sender_id=getattr(provider_sender, "id", None),
                error=error_code or message_status,
            )
            if not reconciled:
                current_app.logger.warning(
                    "[TWILIO_WHATSAPP_STATUS] Durable attempt did not match "
                    "signed callback tenant_id=%s sender_id=%s",
                    tenant.id,
                    getattr(provider_sender, "id", None),
                )
        except Exception as exc:
            current_app.logger.warning(
                "[TWILIO_WHATSAPP_STATUS] Durable reconciliation failed "
                "tenant_id=%s sender_id=%s error_type=%s",
                getattr(tenant, "id", None),
                getattr(provider_sender, "id", None),
                type(exc).__name__,
            )
            # Durable status callback URLs explicitly opt into bounded 5xx
            # retries using Twilio connection overrides. Returning 200 here
            # would permanently discard the only automatic reconciliation of
            # a send whose API outcome may be unknown.
            return "RETRY", 503
    return "OK", 200

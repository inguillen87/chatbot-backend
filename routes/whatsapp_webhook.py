from flask import Blueprint, request, jsonify, abort, current_app, g, has_app_context  # Basic Flask components
from twilio.request_validator import RequestValidator  # For validating Twilio requests
from twilio.rest import Client  # For sending messages via Twilio
import os  # For accessing environment variables
import requests
import io
import json
import threading
import re
import time
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
)  # Import necessary models
from extensions import db  # Import db instance for database operations
import uuid
from sqlalchemy import or_
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
from services.contact_service import resolve_contact
from services.ticket_service import servicio_tickets

# Define the blueprint for WhatsApp webhooks
webhook_bp = Blueprint('whatsapp_webhook', __name__)

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
    "iniciar_reclamo",
    "crear_reclamo",
    "enviar_sugerencia",
    "iniciar_sugerencia",
    "crear_sugerencia",
}


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
    print("Warning: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN environment variables not set. Twilio client and validator will not be initialized.")
    twilio_client = None
    validator = None

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    print("Whatsapp webhook called!")
    print(f"Request form: {request.form}")
    if not validator:
        print("Error: Twilio RequestValidator not initialized. Ensure TWILIO_AUTH_TOKEN is set.")
        abort(500, "Twilio validator not configured")

    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    post_vars = request.form.to_dict()

    if not validator.validate(url, post_vars, signature):
        abort(403, "Invalid Twilio signature")

    to_number_raw = post_vars.get("To", "")
    from_number_raw = post_vars.get("From", "")

    whatsapp_mapping, to_number_cleaned, to_number_normalized = _lookup_whatsapp_mapping(to_number_raw)
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
        print(f"Error: No user associated with WhatsappNumero id {whatsapp_mapping.id} for number {to_number_cleaned}.")
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
    should_trigger_welcome = is_greeting and not is_waiting_for_info

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
                # Prioritize DB name, then WhatsApp profile name. Avoid generic
                # "vecino" fallback so the bot either personalizes or greets
                # without a name and lets downstream logic ask for it.
                user_name = getattr(end_user, "name", "")
                if not user_name or user_name.lower() in {"vecino", "vecina", "vecino/a"}:
                    user_name = (post_vars.get("ProfileName") or "").strip()

                if user_name.lower() in {"vecino", "vecina", "vecino/a"}:
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

                if not template_sent and user_name is not None:
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

                profile_name = (post_vars.get("ProfileName") or "").strip()
                if profile_name.lower() in {"vecino", "vecina", "vecino/a"}:
                    profile_name = ""
                resolved_contact = resolve_contact(from_number_cleaned, profile_name or None)
                if resolved_contact and not profile_name:
                    profile_name = resolved_contact.get("nombre") or ""
                if profile_name:
                    session_context_db_entry.context_data["profile_name"] = profile_name
                    contexto_municipio_actual = session_context_db_entry.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
                    contacto_usuario = contexto_municipio_actual.setdefault("contacto_usuario", {})
                    if isinstance(contacto_usuario, dict) and not contacto_usuario.get("nombre"):
                        contacto_usuario["nombre"] = profile_name
                    safe_flag_modified(session_context_db_entry, "context_data")

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
            new_name = extracted.get("nombre") or name_candidate
            update_user_profile(end_user, {"name": new_name})
            session_context_db_entry.context_data.pop("awaiting_user_name", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
            if twilio_client:
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body=f"¡Encantado, {new_name}! ¿En qué puedo ayudarte?",
                )
            try:
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
                    if sticker_payload:
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
        print(f"Received location data: {location_info}")
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

    if (selected_action_id or "").strip().lower() in SENSITIVE_MENU_ACTIONS:
        # Force a clean flow when user selects sensitive actions from a menu.
        # This prevents accidental ticket creation with stale draft/context data.
        _reset_municipio_context_for_menu(session_context_db_entry)

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

    # Check if we should bypass the bot logic because the user selected a URL option
    bypass_bot_logic = False
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
            print(f"Calling responder_chatboc for session_id: {chat_session_id_internal}, owner_user: {client_user.name}")

            interpretacion_media_data = None
            if uploaded_file_info:
                mime_type = uploaded_file_info.get("mime_type", "")
                if not skip_media_analysis and not mime_type.startswith("audio/"):
                    interpretacion_media_data = clasificar_adjunto_whatsapp(uploaded_file_info, client_user)
            # Location info should not be treated as interpreted media.
            # It should be passed directly as location data.

            kwargs_for_bot = {"source_channel": "whatsapp"}
            if uploaded_file_info:
                kwargs_for_bot["uploaded_file_info"] = uploaded_file_info
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

            print(f"Raw response from responder_chatboc: {bot_response_dict}")

            # Validate the response from the bot logic
            if not isinstance(bot_response_dict, dict):
                print(f"Warning: responder_chatboc did not return a dictionary. Response: {bot_response_dict}")
                # Keep the default error response initialized earlier
                bot_response_dict = {
                    'message_body': "Lo siento, hubo un error interno al procesar tu mensaje.",
                    'options_list': [], 'message_type': 'text', 'fuente': 'error_handler_non_dict_response'
                }

            # Ensure context_data is a dict for saving
            if not isinstance(session_context_db_entry.context_data, dict):
                print(f"Warning: context_data in session_context_db_entry is not a dict. Resetting. Data: {session_context_db_entry.context_data}")
                session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format."}],
                    "estado_conversacion": "error_context"
                }

    except Exception as e:
        print(f"Error calling real chatbot logic (responder_chatboc): {e}")
        import traceback
        traceback.print_exc() # Log full traceback for debugging
        # bot_response_dict is already set to a default error message, so we just log and continue

    # Update respuesta_del_bot_text for logging from the final bot_response_dict
    respuesta_del_bot_text = bot_response_dict.get("message_body", "")
    print(f"Bot response text for logging: '{respuesta_del_bot_text}', Session context to save: {session_context_db_entry.context_data}")

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
        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto de la base de datos: {db_context}")
        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto actualizado del turno actual: {updated_context}")


        # Merge the contexts
        if updated_context:
            merged_context = {**db_context, **updated_context}
        else:
            merged_context = db_context

        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto fusionado para guardar: {merged_context}")


        # Save the merged context
        session_context_db_entry.context_data = merged_context
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
        print(f"Session saved for {chat_session_id_internal}. Context: {session_context_db_entry.context_data}")

    except Exception as e:
        db.session.rollback()
        print(f"Error formatting response or saving session for {chat_session_id_internal}: {e}")
        import traceback
        traceback.print_exc()

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
                        print(f"Mensaje principal (interactivo) enviado a {from_number_raw}, SID: {main_message.sid}")

                        for idx, chunk in enumerate(remaining_chunks, start=2):
                            followup_params = {
                                'from_': to_number_raw,
                                'to': from_number_raw,
                                'body': chunk,
                            }
                            followup_message = twilio_client.messages.create(**followup_params)
                            print(f"Mensaje adicional {idx}/{len(chunks)} enviado a {from_number_raw}, SID: {followup_message.sid}")
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
                    print(f"Mensaje parte 1/{len(chunks)} enviado a {from_number_raw}, SID: {main_message.sid}")

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
                print(f"Mensaje principal enviado a {from_number_raw}, SID: {main_message.sid}")

            audio_enabled = bool(
                current_app.config.get("WHATSAPP_AUDIO_ENABLED", True)
                or bot_response_dict.get("force_audio_whatsapp")
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
                    print(f"Mensaje de audio enviado a {from_number_raw}, SID: {audio_message.sid}")

        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

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

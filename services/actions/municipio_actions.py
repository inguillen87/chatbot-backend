# services/actions/municipio_actions.py
import logging
import hashlib
import os
import re
import sys
import time
from urllib.parse import urlparse

from flask import current_app, has_app_context
from sqlalchemy import func, text

from .base_action_handler import BaseActionHandler
from typing import Dict, Any, Optional
import random
from services.ticket_service import servicio_tickets
from services.notifications import enviar_notificacion_whatsapp_con_plantilla, enviar_notificacion_sms
from services import herramientas_municipio
from services.herramientas_municipio import (
    direccion_es_valida,
    normalizar_texto,
    obtener_direccion_de_coordenadas,
)
from services.address_parser import parse_address
from services.categorias_municipio import (
    CATEGORIAS_RECLAMO,
    CATEGORIAS_SINONIMOS,
    normalizar_texto as normalizar_texto_municipio,
)
from services.ticket_utils import build_claim_tracking_url, formatear_ticket_respuesta, remove_buttons_with_urls_in_message
from services.whatsapp_receipts import (
    build_claim_created_template_pre_message,
    build_claim_created_followup_text,
    build_claim_replay_text,
    render_ticket_whatsapp,
)
from services.live_chat_schedule import build_tenant_live_chat_status
from services.live_chat_access import attach_ticket_room_access, build_ticket_room
from utils.ticket_utils import normalize_category
from services.common_utils import validar_telefono, formatear_telefono_e164, validar_email
from services.config_loader import cargar_configuracion_municipio
from models import MunicipioTicket, TenantProfile, CategoriaTicket, User, db
from services.common_utils import _get_main_menu_payload
from services import promo_service
from services.voice_handler import initiate_outbound_call
from services.conversation_summaries import build_claim_confirmation_payload
from utils.db_utils import safe_flag_modified

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"


def parse_direccion(*args, **kwargs):
    """Patch-friendly compatibility proxy for the municipal address parser."""

    return herramientas_municipio.parse_direccion_completa(*args, **kwargs)


def _parse_int_env(var_name: str, default: int) -> int:
    raw_value = os.getenv(var_name, str(default))
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Valor inválido para %s=%r; usando %s.", var_name, raw_value, default)
        return default


def _format_ticket_code(prefix: str, raw_ticket_number: Any) -> str:
    """Return one canonical ticket prefix even when an integration adds it."""

    normalized_prefix = f"{str(prefix or '').strip().upper().rstrip('-')}-"
    raw_value = str(raw_ticket_number or "").strip()
    if raw_value.upper().startswith(normalized_prefix):
        return f"{normalized_prefix}{raw_value[len(normalized_prefix):]}"
    return f"{normalized_prefix}{raw_value}"


def _normalize_voice_e164(
    value: Any,
    *,
    default_country_code: str = "54",
) -> Optional[str]:
    """Return a conservative E.164 destination for PSTN calls.

    WhatsApp identities normally include the international ``+`` prefix and
    are preserved.  A number explicitly typed without a prefix is formatted
    with the tenant's configured calling code (Argentina by default) instead
    of being mistaken for an arbitrary international destination.
    """

    raw_value = str(value or "").strip()
    if raw_value.lower().startswith("whatsapp:"):
        raw_value = raw_value.split(":", 1)[1].strip()
    if not raw_value or re.search(r"[A-Za-z]", raw_value):
        return None

    if raw_value.startswith("+"):
        candidate = f"+{re.sub(r'\D', '', raw_value)}"
    else:
        country_code = re.sub(r"\D", "", str(default_country_code or "54")) or "54"
        candidate = formatear_telefono_e164(raw_value, cod_pais=country_code)

    if not re.fullmatch(r"\+[1-9]\d{7,14}", candidate or ""):
        return None
    return candidate


def _voice_country_code(tenant: Any) -> str:
    tenant_config = getattr(tenant, "configuracion", None)
    if isinstance(tenant_config, dict):
        configured = (
            tenant_config.get("voice_country_calling_code")
            or tenant_config.get("country_calling_code")
        )
        digits = re.sub(r"\D", "", str(configured or ""))
        if 1 <= len(digits) <= 3 and not digits.startswith("0"):
            return digits
    return "54"


def _resolve_callback_destination(
    context: Dict[str, Any],
    action_data: Dict[str, Any],
    viewer_user: Any,
    *,
    country_code: str,
) -> tuple[Optional[str], bool]:
    """Resolve the first valid citizen phone without ever using owner data."""

    candidates = [
        action_data.get("telefono"),
        action_data.get("phone"),
        context.get("anon_id"),
        getattr(viewer_user, "telefono", None) if viewer_user else None,
    ]

    chat_data = context.get("chat_db_context_data")
    if isinstance(chat_data, dict):
        municipal_context = chat_data.get(CONTEXTO_MUNICIPIO)
        if isinstance(municipal_context, dict):
            contact = municipal_context.get("contacto_usuario")
            if isinstance(contact, dict):
                candidates.append(contact.get("telefono"))

    direct_municipal_context = context.get(CONTEXTO_MUNICIPIO)
    if isinstance(direct_municipal_context, dict):
        contact = direct_municipal_context.get("contacto_usuario")
        if isinstance(contact, dict):
            candidates.append(contact.get("telefono"))

    for candidate in candidates:
        normalized = _normalize_voice_e164(
            candidate,
            default_country_code=country_code,
        )
        if normalized:
            return normalized, True
    return None, any(str(candidate or "").strip() for candidate in candidates)


def _resolve_voice_caller_id(tenant: Any) -> Optional[str]:
    """Resolve only an explicitly voice-enabled PSTN caller ID."""

    tenant_config = getattr(tenant, "configuracion", None)
    if isinstance(tenant_config, dict) and tenant_config.get("voice_caller_id"):
        return str(tenant_config["voice_caller_id"]).strip()

    if has_app_context():
        configured = current_app.config.get("TWILIO_VOICE_PHONE_NUMBER")
        if configured:
            return str(configured).strip()
    configured = os.environ.get("TWILIO_VOICE_PHONE_NUMBER")
    return str(configured).strip() if configured else None


def _acquire_municipal_claim_confirmation_lock(
    session,
    *,
    confirmation_id: str,
    tenant_id: Any = None,
    municipio_id: Any = None,
) -> bool:
    """Serialize one logical claim confirmation on PostgreSQL.

    The confirmation id is also stored on the ticket for durable replay.  The
    transaction-scoped advisory lock closes the read-before-create race without
    adding a migration to the already active survey migration chain.  SQLite is
    used only by tests/local development here, so no PostgreSQL-specific
    statement is issued there; durable sequential replay remains active.
    """

    if not confirmation_id:
        return True
    scope_kind = "tenant" if tenant_id else "municipio" if municipio_id else ""
    scope_value = tenant_id or municipio_id
    if not scope_kind or not scope_value:
        return False

    try:
        bind = session.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
            return True
        seed = f"municipal-claim:{scope_kind}:{scope_value}:{confirmation_id}"
        lock_id = int.from_bytes(
            hashlib.sha256(seed.encode("utf-8")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
        return True
    except Exception:
        logger.exception(
            "No se pudo adquirir el bloqueo idempotente del reclamo municipal."
        )
        try:
            session.rollback()
        except Exception:
            logger.exception("No se pudo revertir la sesion tras fallar el bloqueo.")
        return False


def _normalize_url_for_comparison(raw_url: str) -> tuple[str, str]:
    """Return normalized (domain, path) for URL comparison."""

    if not raw_url:
        return "", ""

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return "", ""

    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    path = (parsed.path or "").strip()
    if path:
        if not path.startswith("/"):
            path = f"/{path}"
        path = path.rstrip("/")

    return domain, path


def _resolve_promo_image_url(municipio_config: dict) -> str | None:
    if not isinstance(municipio_config, dict):
        return None
    promo_image_url = municipio_config.get("promo_image_url")
    if promo_image_url:
        return promo_image_url
    promo_section = municipio_config.get("promo_section") or municipio_config.get("promo")
    if isinstance(promo_section, dict):
        return promo_section.get("image_url")
    return None


def _resolve_tenant_for_ai(tenant_id: Any) -> TenantProfile | None:
    if tenant_id in (None, ""):
        return None
    try:
        return db.session.get(TenantProfile, int(tenant_id))
    except (TypeError, ValueError):
        return None


def _persist_municipio_ticket_ai_enrichment(
    ticket_obj: MunicipioTicket,
    *,
    tenant: TenantProfile | None = None,
) -> dict[str, Any] | None:
    """Persist advisory-only AI hints for CRM without mutating operational state."""

    if not ticket_obj or not hasattr(ticket_obj, "datos_extra"):
        return None

    try:
        from sqlalchemy.orm.attributes import flag_modified
        from services.ticket_ai_enrichment import build_ticket_ai_enrichment

        estado_before = getattr(ticket_obj, "estado", None)
        categoria_before = getattr(ticket_obj, "categoria", None)
        enrichment = build_ticket_ai_enrichment(
            ticket_obj,
            scope="municipio",
            tenant=tenant,
        )
        enrichment["persisted"] = True
        enrichment["source_model"] = "MunicipioTicket"
        enrichment["state_mutation"] = {
            **(enrichment.get("state_mutation") or {}),
            "requested": False,
            "applied": False,
            "estado_before": estado_before,
            "estado_after": getattr(ticket_obj, "estado", None),
            "categoria_before": categoria_before,
            "categoria_after": getattr(ticket_obj, "categoria", None),
            "reason": "ai_enrichment_is_advisory_only",
        }

        extra = dict(ticket_obj.datos_extra) if isinstance(ticket_obj.datos_extra, dict) else {}
        extra["ai_enrichment"] = enrichment
        extra["ai_hints"] = enrichment.get("crm_hints") if isinstance(enrichment.get("crm_hints"), dict) else {}
        extra["ai_operator_brief"] = (
            enrichment.get("operator_brief") if isinstance(enrichment.get("operator_brief"), dict) else {}
        )
        extra["ai_enrichment_contract_version"] = enrichment.get("contract_version")
        ticket_obj.datos_extra = extra
        flag_modified(ticket_obj, "datos_extra")
        db.session.add(ticket_obj)
        db.session.commit()
        return enrichment
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "No se pudo persistir enrichment IA advisory para reclamo municipal %s: %s",
            getattr(ticket_obj, "id", None),
            exc,
            exc_info=True,
        )
        return None


def _address_seems_generic(address: str | None) -> bool:
    if not address:
        return True

    normalized = normalizar_texto(address)
    if not normalized:
        return True

    if any(char.isdigit() for char in normalized):
        return False

    generic_tokens = {
        "argentina",
        "provincia",
        "provincia de mendoza",
        "mendoza",
        "ciudad",
        "municipio",
    }

    tokens = set(normalized.split())
    if len(tokens) <= 2 and tokens.issubset(generic_tokens):
        return True

    return False


def _infer_category_from_description(
    description: str | None,
    category_candidates: list[str] | None = None,
) -> str | None:
    if not description:
        return None

    normalized_description = normalizar_texto_municipio(description)
    if not normalized_description:
        return None

    best_match = None
    best_score = 0
    normalized_synonyms_keys = {
        normalizar_texto_municipio(key) for key in CATEGORIAS_SINONIMOS.keys()
    }

    for categoria_key, synonyms in CATEGORIAS_SINONIMOS.items():
        terms = [categoria_key, *synonyms]
        score = 0
        for term in terms:
            normalized_term = normalizar_texto_municipio(term)
            if not normalized_term:
                continue
            pattern = rf"\b{re.escape(normalized_term)}\b"
            if re.search(pattern, normalized_description):
                score += 2
            elif normalized_term in normalized_description:
                score += 1
        if score > best_score:
            best_score = score
            best_match = categoria_key

    for candidate in category_candidates or []:
        normalized_candidate = normalizar_texto_municipio(candidate)
        if not normalized_candidate or normalized_candidate in normalized_synonyms_keys:
            continue
        pattern = rf"\b{re.escape(normalized_candidate)}\b"
        if re.search(pattern, normalized_description):
            score = 2
        elif normalized_candidate in normalized_description:
            score = 1
        else:
            score = 0
        if score > best_score:
            best_score = score
            best_match = candidate

    if not best_match:
        return None

    return normalize_category(best_match)


def _get_categoria_candidates(owner_user: Any, context: Dict[str, Any]) -> list[str]:
    categorias: list[str] = list(CATEGORIAS_RECLAMO)
    tenant_id, _ = _resolve_municipio_tenant_ids(owner_user, context)
    if not tenant_id:
        return categorias

    try:
        tenant_categories = (
            CategoriaTicket.query.filter_by(tenant_id=tenant_id)
            .order_by(CategoriaTicket.nombre.asc())
            .all()
        )
    except Exception as exc:
        logger.warning("No se pudieron cargar categorías del tenant: %s", exc)
        return categorias

    normalized_existing = {normalizar_texto_municipio(cat) for cat in categorias if cat}
    for category in tenant_categories:
        nombre = getattr(category, "nombre", None)
        if not nombre:
            continue
        normalized = normalizar_texto_municipio(nombre)
        if normalized and normalized not in normalized_existing:
            categorias.append(nombre)
            normalized_existing.add(normalized)

    return categorias


def _ubicacion_es_valida(ubicacion: str | None) -> bool:
    if not ubicacion:
        return False
    normalized = normalizar_texto(ubicacion)
    if not normalized:
        return False
    greeting_words = {
        "hola",
        "buenas",
        "buenos",
        "buenas tardes",
        "buenos dias",
        "buenas noches",
    }
    if normalized in greeting_words:
        return False
    if re.search(r"-?\d{1,3}\.\d+", normalized):
        return True
    # Stricter validation: Require a number if it looks like a street, or explicit intersection/barrio keywords
    has_street_keyword = bool(re.search(r"\b(calle|av\.?|avenida|ruta|km)\b", normalized))
    has_number = bool(re.search(r"\d", normalized))

    if has_street_keyword and not has_number:
        return False

    if re.search(r"\b(esquina|interseccion|intersección|entre|altura|barrio|manzana|mz|lote|plaza|parque|monumento)\b", normalized):
        return True
    if re.search(r"\b[a-z]{3,}\s+(y|e)\s+[a-z]{3,}\b", normalized):
        return True
    if re.search(r"\b(rotonda|puente|terminal|hospital|escuela)\b", normalized):
        return True

    if has_number:
        # Check if it's too long (likely a description)
        if len(normalized.split()) > 12:
            return False
        return True

    return direccion_es_valida(ubicacion)


def _normalize_positive_scope_id(value: Any) -> Optional[int]:
    """Return a database-safe positive identifier or ``None``.

    ORM identities are integers.  Reject arbitrary objects (including mocks)
    before they can reach a bound SQL parameter; coercing such objects can
    silently select an unrelated tenant.
    """

    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str):
        candidate = value.strip()
        if re.fullmatch(r"[1-9][0-9]*", candidate):
            return int(candidate)
    return None


def _normalize_tenant_slug(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_municipio_tenant_ids(owner_user, context: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """Resolve tenant and municipality scope without arbitrary cross-tenant fallback."""

    context = context or {}
    municipio_config = (context or {}).get("municipio_config_actual", {}) or {}
    tenant_slug = _normalize_tenant_slug(
        municipio_config.get("tenant_slug")
        or municipio_config.get("slug")
        or getattr(owner_user, "tenant_slug", None)
    )
    owner_user_id = _normalize_positive_scope_id(getattr(owner_user, "id", None))
    legacy_municipio_id = (
        _normalize_positive_scope_id(getattr(owner_user, "municipio_id", None))
        or owner_user_id
    )
    valid_owner_ids = {
        value for value in (owner_user_id, legacy_municipio_id) if value is not None
    }

    explicit_profile = context.get("tenant_profile")
    chat_db_context = context.get("chat_db_context_obj")
    explicit_ids = []
    for value in (
        context.get("tenant_id"),
        getattr(chat_db_context, "tenant_id", None),
        getattr(explicit_profile, "id", None),
    ):
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        normalized = _normalize_positive_scope_id(value)
        if normalized is None:
            logger.error("Invalid explicit tenant scope; refusing municipal fallback.")
            return None, None
        if normalized not in explicit_ids:
            explicit_ids.append(normalized)

    if len(explicit_ids) > 1:
        logger.error(
            "Conflicting explicit tenant scopes detected (tenant_ids=%s); refusing ticket action.",
            explicit_ids,
        )
        return None, None

    def _valid_tenant(candidate) -> bool:
        if candidate is None:
            return False
        candidate_owner_id = _normalize_positive_scope_id(
            getattr(candidate, "municipio_id", None)
        )
        return not valid_owner_ids or candidate_owner_id in valid_owner_ids

    if explicit_ids:
        requested_tenant_id = explicit_ids[0]
        tenant = (
            explicit_profile
            if _normalize_positive_scope_id(getattr(explicit_profile, "id", None))
            == requested_tenant_id
            else TenantProfile.query.filter_by(id=requested_tenant_id).one_or_none()
        )
        if not _valid_tenant(tenant):
            logger.error(
                "Explicit tenant does not belong to municipal owner "
                "(tenant_id=%s owner_id=%s); refusing ticket action.",
                requested_tenant_id,
                owner_user_id,
            )
            return None, None
        return (
            _normalize_positive_scope_id(getattr(tenant, "id", None)),
            _normalize_positive_scope_id(getattr(tenant, "municipio_id", None))
            or legacy_municipio_id,
        )

    if explicit_profile is not None:
        if not _valid_tenant(explicit_profile):
            logger.error("Explicit tenant profile does not belong to municipal owner.")
            return None, None
        return (
            _normalize_positive_scope_id(getattr(explicit_profile, "id", None)),
            _normalize_positive_scope_id(getattr(explicit_profile, "municipio_id", None))
            or legacy_municipio_id,
        )

    tenant = None
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=str(tenant_slug).strip()).one_or_none()
        if tenant is not None and not _valid_tenant(tenant):
            logger.error("Tenant slug does not belong to municipal owner; refusing ticket action.")
            return None, None
    if not tenant and owner_user_id:
        candidates = TenantProfile.query.filter_by(municipio_id=owner_user_id).all()
        active_candidates = [candidate for candidate in candidates if candidate.is_active]
        unambiguous_candidates = active_candidates or candidates
        if len(unambiguous_candidates) == 1:
            tenant = unambiguous_candidates[0]
        elif len(unambiguous_candidates) > 1:
            logger.error(
                "Multiple municipal tenants found for owner_id=%s without explicit scope; "
                "refusing ticket action.",
                owner_user_id,
            )
            return None, None

    tenant_id = _normalize_positive_scope_id(getattr(tenant, "id", None))
    municipio_id = (
        _normalize_positive_scope_id(getattr(tenant, "municipio_id", None))
        or legacy_municipio_id
    )

    if not municipio_id:
        logger.warning(
            "[tickets] municipio_id missing while resolving tenant. tenant_slug=%s owner_id=%s",
            tenant_slug,
            owner_user_id,
        )

    return tenant_id, municipio_id


def _render_closing_caption_template(template: str | None, values: Dict[str, Any]) -> str:
    """Render a caption template using {{placeholder}} or {placeholder} tokens."""

    message_body = str(values.get("message_body") or "").strip()
    if not template:
        return message_body

    rendered = str(template)
    for key, value in values.items():
        token_value = str(value) if value is not None else ""
        rendered = rendered.replace(f"{{{{{key}}}}}", token_value)
        rendered = rendered.replace(f"{{{key}}}", token_value)

    return rendered


def _apply_whatsapp_closing_promo(
    payload: Dict[str, Any],
    *,
    context: Dict[str, Any],
    caption_values: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach WhatsApp hero media for closing flows when enabled."""

    channel = (context.get("channel") or "").strip().lower()
    if not channel.startswith("whatsapp"):
        return payload

    municipio_config = context.get("municipio_config_actual", {}) or {}
    if not municipio_config.get("closing_promo_enabled"):
        return payload

    image_url = (
        municipio_config.get("closing_promo_image_url")
        or municipio_config.get("promo_image_url")
        or payload.get("image_url")
    )
    if not image_url:
        return payload

    caption_template = municipio_config.get("closing_promo_caption_template")
    caption = _render_closing_caption_template(caption_template, caption_values)

    pre_messages = payload.get("_twilio_pre_messages")
    if not isinstance(pre_messages, list):
        pre_messages = [] if pre_messages is None else [pre_messages]

    pre_messages.append(
        {
            "channels": ["whatsapp"],
            "template_name": (
                municipio_config.get("closing_promo_template_name")
                or municipio_config.get("promo_template_name")
                or "promocionar"
            ),
            "content_variables": {},
            "body": caption,
            "media_urls": [image_url],
        }
    )
    payload["_twilio_pre_messages"] = pre_messages

    if payload.get("options_list"):
        payload["message_body"] = municipio_config.get(
            "closing_promo_followup_text",
            "Seleccioná una opción para continuar.",
        )

    payload.pop("image_url", None)
    return payload


class BuscarEstacionamientoActionHandler(BaseActionHandler):
    action_name = "buscar_estacionamiento"

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing BuscarEstacionamientoActionHandler with data: {action_data}")

        # La ubicación puede venir de la acción del LLM o del contexto si se pidió antes
        ubicacion = action_data.get("ubicacion") or self.context.get("ubicacion_usuario")

        if not ubicacion:
            # Si no hay ubicación, la pedimos.
            self.context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = "ESPERANDO_UBICACION_GENERAL"
            self.context[CONTEXTO_MUNICIPIO]["accion_pendiente_tras_ubicacion"] = "buscar_estacionamiento"

            return {
                "success": False,
                "message_to_user": "Para encontrar estacionamiento, por favor compartí tu ubicación o escribí una dirección (ej: San Martín 1200).",
                "pedir_info": "ubicacion"
            }

        # Llamar al servicio de estacionamiento
        from services.estacionamiento_service import consultar_ocupacion
        resultado = consultar_ocupacion(ubicacion) # resultado es un dict {"texto": "..."}

        # Limpiar el estado de espera si existía
        if self.context.get(CONTEXTO_MUNICIPIO, {}).get("accion_pendiente_tras_ubicacion") == "buscar_estacionamiento":
            self.context[CONTEXTO_MUNICIPIO].pop("accion_pendiente_tras_ubicacion")
            if "estado_conversacion" in self.context[CONTEXTO_MUNICIPIO]:
                 self.context[CONTEXTO_MUNICIPIO].pop("estado_conversacion")


        return {
            "success": True,
            "message_to_user": resultado["texto"],
            "data": resultado
        }

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(
            "Executing CrearReclamoActionHandler channel=%s supplied_fields=%s",
            str(self.context.get("channel") or "unknown").lower(),
            sorted(str(key) for key in action_data.keys()),
        )

        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO)
        if not isinstance(contexto_reclamo, dict):
            chat_context_data = self.context.get("chat_db_context_data")
            if isinstance(chat_context_data, dict):
                contexto_reclamo = chat_context_data.setdefault(CONTEXTO_MUNICIPIO, {})
                # Keep both access paths pointing at the same live object.  The
                # guided flow stores its state below ``chat_db_context_data``
                # while action handlers historically read the top-level key.
                self.context[CONTEXTO_MUNICIPIO] = contexto_reclamo
            else:
                contexto_reclamo = {}
        viewer_user = self.context.get("viewer_user_obj")
        datos_parciales_llm = contexto_reclamo.get("datos_parciales_llm_reclamo", {})
        contacto_ctx = contexto_reclamo.get("contacto_usuario", {})
        resolved_contact = self.context.get("resolved_contact") or {}

        if resolved_contact:
            contacto_ctx.setdefault("nombre", resolved_contact.get("nombre"))
            contacto_ctx.setdefault("email", resolved_contact.get("email"))
            contacto_ctx.setdefault("dni", resolved_contact.get("dni"))
            contacto_ctx.setdefault("telefono", resolved_contact.get("telefono"))

        # Fusionar datos: action_data tiene prioridad, luego el contexto del reclamo, luego el perfil del usuario
        datos_parciales = contexto_reclamo.get("datos_parciales_llm_reclamo", {})
        categoria = action_data.get("categoria") or datos_parciales.get("categoria")
        if categoria:
            categoria = re.sub(r"^[^\w]+", "", str(categoria)).strip()
        descripcion = action_data.get("descripcion") or datos_parciales_llm.get("descripcion")
        ubicacion_llm = action_data.get("ubicacion") or datos_parciales_llm.get("ubicacion")
        distrito_llm = action_data.get("distrito") or datos_parciales_llm.get("distrito")
        coordenadas_llm = action_data.get("coordenadas") or datos_parciales_llm.get("coordenadas")
        foto_url_llm = action_data.get("foto_url_adjunta") or datos_parciales_llm.get("foto_url")

        lat_coord = lon_coord = None
        if isinstance(coordenadas_llm, dict):
            lat_raw = (
                coordenadas_llm.get("lat")
                or coordenadas_llm.get("latitude")
                or coordenadas_llm.get("latitud")
            )
            lon_raw = (
                coordenadas_llm.get("lon")
                or coordenadas_llm.get("lng")
                or coordenadas_llm.get("longitude")
                or coordenadas_llm.get("longitud")
            )
            try:
                if lat_raw is not None and lon_raw is not None:
                    lat_coord = float(lat_raw)
                    lon_coord = float(lon_raw)
            except (TypeError, ValueError):
                lat_coord = lon_coord = None

            if lat_coord is not None and lon_coord is not None:
                coordenadas_llm = {"lat": lat_coord, "lng": lon_coord}
            else:
                coordenadas_llm = None

        geocoded_from_coords = None
        enrichment_disabled = (
            os.getenv("CHATBOC_DISABLE_COORD_ENRICHMENT") == "1"
            or bool(os.getenv("PYTEST_CURRENT_TEST"))
            or "pytest" in sys.modules
        )
        if lat_coord is not None and lon_coord is not None and not enrichment_disabled:
            geocoded_from_coords = obtener_direccion_de_coordenadas(lat_coord, lon_coord)
            if geocoded_from_coords and geocoded_from_coords.get("formatted_address"):
                if not ubicacion_llm or _address_seems_generic(ubicacion_llm):
                    ubicacion_llm = geocoded_from_coords.get("formatted_address")

        # Check if the extracted location is actually a description
        if ubicacion_llm:
            lower_ubi = ubicacion_llm.lower()
            # Stricter heuristic: if it's long and has 'descripción' or looks like narrative
            has_valid_coordinates = lat_coord is not None and lon_coord is not None
            if (
                "descripción es" in lower_ubi
                or "problema es" in lower_ubi
                or (len(lower_ubi.split()) > 12 and not has_valid_coordinates)
            ):
                # Likely a description or junk text
                if not descripcion:
                    descripcion = ubicacion_llm # Move to description if empty
                logger.info(
                    "Municipal claim location rejected reason=narrative "
                    "has_coordinates=%s token_count=%s",
                    has_valid_coordinates,
                    len(lower_ubi.split()),
                )
                ubicacion_llm = None
            elif not _ubicacion_es_valida(ubicacion_llm) and not has_valid_coordinates:
                logger.info(
                    "Municipal claim location rejected reason=invalid "
                    "has_coordinates=%s token_count=%s",
                    has_valid_coordinates,
                    len(lower_ubi.split()),
                )
                ubicacion_llm = None

        if descripcion:
            categoria_candidates = _get_categoria_candidates(self.context.get("user_obj"), self.context)
            categoria_inferida = _infer_category_from_description(descripcion, categoria_candidates)
            categoria_actual = normalize_category(categoria) if categoria else None
            categorias_genericas = {
                "Limpieza",
                "Limpieza Y Riego",
                "Reclamo",
                "Reclamo General",
                "Reclamo Generico",
                "Reclamo Genérico",
                "General",
                "Consulta General",
                "Servicios",
                "Otros",
                "Otro",
                "Otro Motivo",
            }
            if categoria_inferida and categoria_inferida != categoria_actual:
                if not categoria_actual or categoria_actual in categorias_genericas:
                    categoria = categoria_inferida

        municipio_config = self.context.get("municipio_config_actual", {})
        if ubicacion_llm:
            parsed_address = parse_address(ubicacion_llm, municipio_config)
            if parsed_address.get("distrito") and not distrito_llm:
                distrito_llm = parsed_address.get("distrito")
            if parsed_address.get("barrio"):
                contexto_reclamo.setdefault("barrio_referencia", parsed_address.get("barrio"))
            if parsed_address.get("referencia"):
                contexto_reclamo.setdefault("referencia_ubicacion", parsed_address.get("referencia"))
        if ubicacion_llm and not distrito_llm and direccion_es_valida(ubicacion_llm):
            try:
                logger.info(
                    "Attempting municipal district parsing location_chars=%s",
                    len(str(ubicacion_llm)),
                )
                parsed_addr = parse_direccion(
                    ubicacion_llm,
                    municipio_config,
                )
                if parsed_addr and parsed_addr.get("localidad"):
                    distrito_llm = parsed_addr.get("localidad")
                else:
                    distrito_llm = municipio_config.get("ciudad") or municipio_config.get("ciudad_default")
            except Exception as e:
                logger.warning(
                    "Municipal district parsing failed error_type=%s",
                    type(e).__name__,
                )
                distrito_llm = municipio_config.get("ciudad") or municipio_config.get("ciudad_default")
        elif geocoded_from_coords and geocoded_from_coords.get("localidad"):
            distrito_llm = geocoded_from_coords.get("localidad")

        # Contact Info - Name
        def _sanitize_nombre(valor: Any, *, preserve_case: bool = False) -> str | None:
            if not isinstance(valor, str):
                return None
            cleaned = valor.strip().strip("\"'")
            cleaned = re.sub(r"\s+", " ", cleaned)
            cleaned = cleaned.strip(".,:;!¡¿?-")
            if not cleaned:
                return None
            cleaned_lower = cleaned.lower()
            if cleaned_lower in {"vecino", "vecina", "vecine", "vecino/a"}:
                return None
            if re.match(
                r"^(quiero|necesito|solicito|pido|por favor|me gustaria|me gustaría)\b",
                cleaned_lower,
            ):
                return None
            forbidden_tokens = {
                "quiero",
                "necesito",
                "solicito",
                "reclamo",
                "problema",
                "pido",
                "favor",
                "hola",
                "buenas",
                "buenos",
                "tengo",
                "hay",
                "soy",
                "mi",
                "nombre",
                "es",
                "me",
                "llamo",
            }

            # Filter filler words while retaining intentional casing in names
            # and short acronyms returned by the user or identity provider.
            words = cleaned.split()
            filtered_words = [w for w in words if w.lower() not in forbidden_tokens]

            if not filtered_words:
                return None

            filtered_value = " ".join(filtered_words)
            has_mixed_case = any(char.isupper() for char in filtered_value) and any(
                char.islower() for char in filtered_value
            )
            if preserve_case and has_mixed_case:
                cleaned_filtered = filtered_value
            else:
                cleaned_filtered = " ".join(
                    word if word.isupper() and len(word) <= 4 else word.title()
                    for word in filtered_words
                )

            if len(cleaned_filtered) < 3: # "Al" ? maybe too short
                return None

            if any(char.isdigit() for char in cleaned_filtered):
                return None

            # Check length again on the filtered result
            if len(cleaned_filtered.split()) > 5:
                return None

            return cleaned_filtered

        trusted_candidates = [
            getattr(viewer_user, "name", None) if viewer_user else None,
            getattr(viewer_user, "nombre", None) if viewer_user else None,
            self.context.get("profile_name"),
            contacto_ctx.get("nombre"),
        ]

        explicit_candidates = [
            action_data.get("usuario"),
            action_data.get("nombre"),
        ]

        inferred_candidates = [
            datos_parciales_llm.get("usuario"),
            datos_parciales_llm.get("nombre"),
            action_data.get("nombre_usuario_detectado"),
            datos_parciales_llm.get("nombre_usuario_detectado"),
            datos_parciales_llm.get("nombre_detectado"),
        ]

        # An explicit valid name from the current turn wins. Trusted identity
        # providers retain intentional casing; inferred LLM values are normalized.
        candidate_names = (
            [(candidate, False) for candidate in explicit_candidates]
            + [(candidate, True) for candidate in trusted_candidates]
            + [(candidate, False) for candidate in inferred_candidates]
        )

        nombre_vecino_final = next(
            (
                clean
                for candidate, preserve_case in candidate_names
                if (clean := _sanitize_nombre(candidate, preserve_case=preserve_case))
            ),
            "Vecino/a",
        )

        telefono_from_llm = (action_data.get("telefono") or datos_parciales.get("telefono") or
                             action_data.get("telefono_detectado") or datos_parciales.get("telefono_detectado"))
        telefono_final = None
        phone_sources = [
            action_data.get("telefono"),
            action_data.get("telefono_detectado"),
            datos_parciales_llm.get("telefono"),
            datos_parciales_llm.get("telefono_detectado"),
            getattr(viewer_user, "telefono", None),
            contacto_ctx.get("telefono")
        ]
        for phone in phone_sources:
            if phone and validar_telefono(str(phone)):
                telefono_final = formatear_telefono_e164(str(phone))
                break

        # Contact Info - Email
        email_final = None
        email_sources = [
            action_data.get("email"),
            action_data.get("email_detectado"),
            datos_parciales_llm.get("email"),
            datos_parciales_llm.get("email_detectado"),
            getattr(viewer_user, "email", None),
            contacto_ctx.get("email")
        ]
        for email in email_sources:
            if email and validar_email(str(email)):
                email_final = str(email).lower()
                break

        # Contact Info - DNI
        dni_final = None
        dni_sources = [
            action_data.get("dni"),
            datos_parciales_llm.get("dni"),
            getattr(viewer_user, "dni", None),
            contacto_ctx.get("dni")
        ]
        for dni in dni_sources:
            dni_str = str(dni).strip() if dni else ""
            if dni_str.isdigit():
                dni_final = dni_str
                break

        # Optional contact address
        direccion_contacto = (
            action_data.get("direccion_contacto")
            or datos_parciales.get("direccion_contacto")
            or action_data.get("direccion")
        )
        if not direccion_contacto and viewer_user:
            direccion_contacto = getattr(viewer_user, "direccion", None)


        # Actualizar el contexto con los datos más recientes para persistencia
        for key, value in [
            ("categoria_reclamo", categoria),
            ("descripcion_reclamo", descripcion),
            ("direccion_reclamo", ubicacion_llm),
            ("coordenadas_reclamo", coordenadas_llm),
            ("nombre_vecino", nombre_vecino_final),
            ("telefono_vecino", telefono_final),
            ("email_vecino", email_final),
            ("dni_vecino", dni_final),
            ("direccion_contacto", direccion_contacto),
            ("foto_url", foto_url_llm),
        ]:
            if value:
                contexto_reclamo[key] = value

        # Validación de datos esenciales para la creación del ticket
        # Default required fields if not specified in config
        campos_requeridos = municipio_config.get(
            "campos_requeridos_reclamo",
            ['descripcion', 'ubicacion', 'nombre', 'telefono', 'email']
        )
        if self.context.get("channel") == "voice":
            campos_requeridos = ["descripcion", "ubicacion", "telefono"]

        datos_finales_reclamo = {
            "categoria": categoria,
            "descripcion": descripcion,
            "ubicacion": ubicacion_llm if _ubicacion_es_valida(ubicacion_llm) else None,
            "nombre": nombre_vecino_final if nombre_vecino_final != "Vecino/a" else None,
            "telefono": telefono_final,
            "email": email_final,
            "dni": dni_final,
        }

        if not datos_finales_reclamo.get("ubicacion") and coordenadas_llm:
            datos_finales_reclamo["ubicacion"] = coordenadas_llm

        campos_faltantes = [campo for campo in campos_requeridos if not datos_finales_reclamo.get(campo)]

        logger.info(
            "Municipal claim validation required_fields=%s missing_fields=%s "
            "present_fields_count=%s",
            sorted(str(field) for field in campos_requeridos),
            sorted(str(field) for field in campos_faltantes),
            sum(bool(value) for value in datos_finales_reclamo.values()),
        )

        # La lógica de confirmación ahora se maneja en 'municipio_responder.py'
        # Este handler ahora solo valida y crea.

        if campos_faltantes:
            # Eliminar duplicados
            campos_faltantes = sorted(list(set(campos_faltantes)))
            self.context[CONTEXTO_MUNICIPIO] = contexto_reclamo

            # Mensaje más amigable y botones de acción
            mensaje = f"Para continuar con tu reclamo, necesito algunos datos más: **{', '.join(campos_faltantes)}**. Por favor, indícamelos."
            botones = [{"texto": f"Ingresar {campo.replace('_', ' ')}", "id_accion": f"ingresar_{campo}"} for campo in campos_faltantes]
            botones.append({"texto": "Cancelar reclamo", "id_accion": "cancelar_reclamo"})

            return {
                "success": False,
                "message_body": mensaje,
                "message_to_user": mensaje,
                "pedir_info": campos_faltantes,
                "options_list": botones,
                "message_type": "interactive_list" if len(botones) > 3 else "interactive_buttons"
            }

        # --- Handle PIN (generate if missing) ---
        pin_llm = (
            action_data.get("pin")
            or datos_parciales.get("pin")
            or datos_parciales.get("consulta_pin")
        )
        pin_str = str(pin_llm).strip() if pin_llm else ""
        if pin_str.isdigit() and len(pin_str) == 6:
            pin_final = pin_str
        else:
            pin_final = f"{random.randint(0, 999999):06d}"

        contexto_reclamo["pin_ticket"] = pin_final

        # Recopilación final de datos y creación del ticket
        owner_user = self.context.get("user_obj")

        # The logic for updating/creating the user is now handled by the ticket_service
        # to centralize user management and correctly handle IntegrityError.
        pregunta_original = self.context.get("pregunta_actual_usuario", "")

        contactos = cargar_configuracion_municipio(
            getattr(owner_user, "municipio_id", "default"),
            "contactos_especializados.json",
        )
        categoria_lookup = None
        if categoria:
            categoria_normalized = re.sub(r"[^\w\s]", "", categoria).strip().lower()
            for key in contactos.keys():
                key_normalized = re.sub(r"[^\w\s]", "", key).strip().lower()
                if (
                    key_normalized == categoria_normalized
                    or key_normalized in categoria_normalized
                    or categoria_normalized in key_normalized
                ):
                    categoria_lookup = key
                    break
        if categoria_lookup:
            categoria = categoria_lookup
        contacto_especializado = dict(contactos.get(categoria_lookup, contactos.get("default", {})))

        tenant_id, municipio_id = _resolve_municipio_tenant_ids(owner_user, self.context)
        if not tenant_id and not municipio_id:
            logger.error("Municipal claim creation refused because tenant scope is unresolved.")
            return {
                "success": False,
                "message_body": (
                    "No pude validar el municipio que debe recibir el reclamo. "
                    "No se creó ningún ticket; intentá nuevamente en unos minutos."
                ),
                "message_to_user": (
                    "No pude validar el municipio que debe recibir el reclamo. "
                    "No se creó ningún ticket; intentá nuevamente en unos minutos."
                ),
                "message_type": "text",
                "fuente": "municipio_tenant_scope_rejected",
            }
        linked_user_id = getattr(viewer_user, "id", None)
        if not linked_user_id and email_final:
            existing_contact_user = (
                User.query.filter(func.lower(User.email) == email_final.lower()).first()
            )
            existing_role = (getattr(existing_contact_user, "rol", "") or "").lower()
            if existing_contact_user and existing_role in {"", "usuario", "cliente", "vecino"}:
                linked_user_id = existing_contact_user.id

        ticket_data = {
            "pregunta": pregunta_original,
            "asunto": f"Reclamo (LLM): {categoria or 'General'}",
            "categoria": categoria or "Reclamo General",
            "detalles": descripcion,
            "direccion": ubicacion_llm,
            "distrito": distrito_llm,
            "nombre_vecino": nombre_vecino_final,
            "telefono_vecino": telefono_final,
            "email_vecino": email_final,
            "dni_vecino": dni_final,
            "direccion_contacto": direccion_contacto,
            "estado": "nuevo",
            "user_id": linked_user_id,
            "anon_id": (
                self.context.get("anon_id")
                if viewer_user or not linked_user_id
                else None
            ),
            "municipio_id": municipio_id,
            "tenant_id": tenant_id,
            "latitud": coordenadas_llm.get("lat") if isinstance(coordenadas_llm, dict) else None,
            "longitud": (
                coordenadas_llm.get("lng") if isinstance(coordenadas_llm, dict) else None
            ),
            "origen_reclamo": "LLM_CHATBOT",
            "foto_url_directa": foto_url_llm,
            "canal_ingreso": self.context.get("channel"),
            "consulta_pin": pin_final,
        }

        confirmation_id_raw = action_data.get("claim_confirmation_id")
        confirmation_id = (
            str(confirmation_id_raw).strip()
            if confirmation_id_raw is not None
            else ""
        )
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", confirmation_id):
            confirmation_id = ""
        if confirmation_id:
            ticket_data["datos_extra"] = {
                "whatsapp_claim_confirmation_id": confirmation_id,
            }

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
        logger.info(
            "Creating municipal claim ticket metadata=%s",
            {
                "tenant_id": tenant_id,
                "municipio_id": municipio_id,
                "linked_user_id": linked_user_id,
                "categoria": ticket_data_cleaned.get("categoria"),
                "canal": ticket_data_cleaned.get("canal_ingreso"),
                "has_photo": bool(ticket_data_cleaned.get("foto_url_directa")),
                "has_coordinates": bool(
                    ticket_data_cleaned.get("latitud") is not None
                    and ticket_data_cleaned.get("longitud") is not None
                ),
                "has_confirmation_id": bool(confirmation_id),
            },
        )

        def _find_confirmation_replay() -> MunicipioTicket | None:
            if not confirmation_id:
                return None
            if not tenant_id and not municipio_id:
                return None
            query = MunicipioTicket.query
            if tenant_id:
                query = query.filter(MunicipioTicket.tenant_id == tenant_id)
            elif municipio_id:
                query = query.filter(MunicipioTicket.municipio_id == municipio_id)
            anon_value = ticket_data_cleaned.get("anon_id")
            if anon_value:
                query = query.filter(MunicipioTicket.anon_id == anon_value)
            return (
                query.filter(
                    MunicipioTicket.datos_extra[
                        "whatsapp_claim_confirmation_id"
                    ].as_string()
                    == confirmation_id
                )
                .order_by(MunicipioTicket.id.desc())
                .first()
            )

        def _confirmation_replay_response(ticket: MunicipioTicket) -> Dict[str, Any]:
            ticket_code = _format_ticket_code("M", ticket.nro_ticket)
            replay_pin = str(ticket.consulta_pin or "").strip() or None
            base_chat_url = municipio_config.get("base_chat_url", "https://www.chatboc.ar/chat")
            tracking_url = build_claim_tracking_url(base_chat_url, ticket_code, replay_pin)
            message = build_claim_replay_text(
                ticket_nro=ticket_code,
                consulta_pin=replay_pin,
                tracking_url=tracking_url,
            )
            replay_payload = {
                "success": True,
                "message_body": message,
                "message_to_user": message,
                "message_type": "text",
                "data": {
                    "ticket_id": ticket.id,
                    "nro_ticket": ticket_code,
                    "consulta_pin": replay_pin,
                    "tracking_url": tracking_url,
                    "deduplicated": True,
                },
                "contexto_actualizado": {
                    "latest_ticket_id": ticket.id,
                    "latest_ticket_nro": ticket_code,
                    "latest_ticket_pin": replay_pin,
                    "latest_tracking_url": tracking_url,
                    "last_ticket_code": ticket_code,
                },
            }
            if str(self.context.get("channel") or "").lower().startswith("whatsapp"):
                replay_payload.update(
                    {
                        "generar_audio": False,
                        "skip_audio_generation": True,
                    }
                )
            return replay_payload

        if confirmation_id and not _acquire_municipal_claim_confirmation_lock(
            db.session,
            confirmation_id=confirmation_id,
            tenant_id=tenant_id,
            municipio_id=municipio_id,
        ):
            logger.error(
                "Se rechaza una confirmacion sin alcance o sin bloqueo idempotente."
            )
            return {
                "success": False,
                "message_body": (
                    "No pude validar de forma segura el municipio del reclamo. "
                    "No se creó ningún ticket; por favor, volvé a intentarlo."
                ),
                "message_to_user": (
                    "No pude validar de forma segura el municipio del reclamo. "
                    "No se creó ningún ticket; por favor, volvé a intentarlo."
                ),
                "message_type": "text",
            }

        confirmation_replay = _find_confirmation_replay()
        if confirmation_replay is not None:
            logger.warning(
                "Confirmacion de reclamo repetida; se reutiliza ticket %s.",
                confirmation_replay.nro_ticket,
            )
            return _confirmation_replay_response(confirmation_replay)

        dedupe_window_seconds = _parse_int_env("CHATBOC_RECLAMO_DEDUP_WINDOW_SECONDS", 600)
        dedupe_fingerprint = {
            "categoria": normalizar_texto_municipio(categoria or ""),
            "descripcion": normalizar_texto_municipio(descripcion or ""),
            "ubicacion": normalizar_texto_municipio(ubicacion_llm or ""),
            "telefono": str(telefono_final or "").strip(),
            "dni": str(dni_final or "").strip(),
            "foto_url": str(foto_url_llm or "").strip(),
        }
        previous_ticket = contexto_reclamo.get("last_created_reclamo")
        if (
            isinstance(previous_ticket, dict)
            and previous_ticket.get("fingerprint") == dedupe_fingerprint
            and (time.time() - float(previous_ticket.get("ts", 0))) < max(0, dedupe_window_seconds)
        ):
            logger.warning(
                "Reclamo duplicado detectado en %ss. Se evita crear nuevo ticket y se reutiliza %s.",
                dedupe_window_seconds,
                previous_ticket.get("ticket_nro"),
            )
            ticket_nro_prev = previous_ticket.get("ticket_nro")
            pin_prev = previous_ticket.get("consulta_pin")
            tracking_url = previous_ticket.get("tracking_url")
            dedupe_message = build_claim_replay_text(
                ticket_nro=ticket_nro_prev,
                consulta_pin=pin_prev,
                tracking_url=tracking_url,
            )
            dedupe_payload = {
                "success": True,
                "message_body": dedupe_message,
                "message_to_user": dedupe_message,
                "message_type": "text",
                "data": {
                    "nro_ticket": ticket_nro_prev,
                    "consulta_pin": pin_prev,
                    "tracking_url": tracking_url,
                    "deduplicated": True,
                },
                "contexto_actualizado": {
                    "latest_ticket_id": previous_ticket.get("ticket_id"),
                    "latest_ticket_nro": ticket_nro_prev,
                    "latest_ticket_pin": pin_prev,
                    "latest_tracking_url": tracking_url,
                    "last_ticket_code": ticket_nro_prev,
                },
            }
            if str(self.context.get("channel") or "").lower().startswith("whatsapp"):
                dedupe_payload.update(
                    {
                        "generar_audio": False,
                        "skip_audio_generation": True,
                    }
                )
            return dedupe_payload

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                # A concurrent confirmation can lose the create race.  The
                # ticket service rolls the failed transaction back, so query
                # the durable confirmation receipt once more before surfacing
                # an error to the citizen.
                confirmation_replay = _find_confirmation_replay()
                if confirmation_replay is not None:
                    return _confirmation_replay_response(confirmation_replay)
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            # 'ticket_creado' is now always a dict.
            ticket_nro = ticket_creado.get('nro_ticket')
            if not ticket_nro:
                raise ValueError("El ticket creado no tiene un 'nro_ticket'.")
            nro_ticket_str = _format_ticket_code("M", ticket_nro)
            logger.info(f"Ticket {nro_ticket_str} creado exitosamente.")

            try:
                from models import ArchivoAdjunto, TicketComentario
                from routes.ticket import serialize_ticket_to_json
                from socket_service import emit_new_ticket

                ticket_obj = db.session.get(MunicipioTicket, ticket_creado.get("id"))
                if ticket_obj:
                    archivo_id_para_asociar = (
                        action_data.get("archivo_id_para_asociar")
                        or datos_parciales_llm.get("archivo_id_para_asociar")
                        or self.context.get("archivo_id_para_asociar")
                    )
                    if archivo_id_para_asociar:
                        try:
                            archivo_id_int = int(archivo_id_para_asociar)
                        except (TypeError, ValueError):
                            archivo_id_int = None
                        if archivo_id_int:
                            adjunto = db.session.get(ArchivoAdjunto, archivo_id_int)
                            if adjunto:
                                adjunto.municipio_ticket_id = ticket_obj.id
                                if not getattr(ticket_obj, "foto_url_directa", None):
                                    ticket_obj.foto_url_directa = adjunto.url
                                db.session.add(adjunto)
                                db.session.add(ticket_obj)
                                db.session.add(
                                    TicketComentario(
                                        municipio_ticket_id=ticket_obj.id,
                                        comentario="[SISTEMA] Vecino adjuntó evidencia al crear el reclamo por WhatsApp.",
                                        user_id=getattr(viewer_user, "id", None),
                                        es_admin=False,
                                        origen="whatsapp",
                                        estado_ticket=ticket_obj.estado,
                                        archivo_adjunto_id=adjunto.id,
                                    )
                                )
                                db.session.commit()
                            else:
                                logger.warning(
                                    "No se encontró ArchivoAdjunto %s para asociar al ticket %s.",
                                    archivo_id_para_asociar,
                                    nro_ticket_str,
                                )
                    _persist_municipio_ticket_ai_enrichment(
                        ticket_obj,
                        tenant=_resolve_tenant_for_ai(tenant_id),
                    )
                    ticket_json = serialize_ticket_to_json(ticket_obj, "municipio")
                    emit_new_ticket(ticket_json)
                else:
                    logger.warning(
                        "No se pudo recuperar el ticket recién creado para emitir socket: id=%s",
                        ticket_creado.get("id"),
                    )
            except Exception as e_notify:
                logger.error(
                    "Municipal claim realtime notification failed ticket_id=%s "
                    "error_type=%s",
                    ticket_creado.get("id"),
                    type(e_notify).__name__,
                )

            # Completar datos desde tramites.json si existen
            tramites_cfg = cargar_configuracion_municipio(
                getattr(owner_user, "municipio_id", "default"),
                "tramites.json",
            )
            tramite_info = tramites_cfg.get(categoria_lookup, {}) if isinstance(tramites_cfg, dict) else {}
            if isinstance(tramite_info, dict):
                if not contacto_especializado.get("telefono") and tramite_info.get("telefono"):
                    contacto_especializado["telefono"] = tramite_info.get("telefono")
                if not contacto_especializado.get("horario") and tramite_info.get("horario"):
                    contacto_especializado["horario"] = tramite_info.get("horario")
                if not contacto_especializado.get("link"):
                    botones = tramite_info.get("botones")
                    if isinstance(botones, list) and botones:
                        contacto_especializado["link"] = botones[0].get("url")

            # Fallback con datos del perfil del municipio y configuración general
            if getattr(owner_user, "link_web", None):
                contacto_especializado.setdefault("link", owner_user.link_web)
            else:
                cfg = cargar_configuracion_municipio(
                    getattr(owner_user, "municipio_id", "default"),
                    "config.json",
                )
                if isinstance(cfg, dict) and cfg.get("web_url"):
                    contacto_especializado.setdefault("link", cfg.get("web_url"))

            if getattr(owner_user, "telefono", None):
                contacto_especializado.setdefault("telefono", owner_user.telefono)
            if getattr(owner_user, "horario", None):
                contacto_especializado.setdefault("horario", owner_user.horario)

            # Limpiar contexto de reclamo después de la creación exitosa
            # Guardamos la info del usuario y de contacto para no perderla.
            user_info = contexto_reclamo.get('user', {})
            contacto_usuario = {
                "nombre": nombre_vecino_final,
                "dni": dni_final,
                "email": email_final,
                "telefono": telefono_final,
                "direccion": direccion_contacto,
            }
            chat_db_context_obj = self.context.get("chat_db_context_obj")
            fresh_chat_data = (
                getattr(chat_db_context_obj, "context_data", None)
                if chat_db_context_obj is not None
                else None
            )
            if isinstance(fresh_chat_data, dict):
                municipio_ctx = fresh_chat_data.setdefault(CONTEXTO_MUNICIPIO, {})
            else:
                municipio_ctx = self.context.get(CONTEXTO_MUNICIPIO)

            if isinstance(municipio_ctx, dict):
                for stale_key in (
                    "reclamo_flow_v2",
                    "historial_llm_reclamo",
                    "datos_parciales_llm_reclamo",
                    "expected_fields_llm_reclamo",
                ):
                    municipio_ctx.pop(stale_key, None)
                if user_info:
                    municipio_ctx["user"] = user_info
                municipio_ctx["contacto_usuario"] = {
                    k: v for k, v in contacto_usuario.items() if v
                }
                from services.municipio_responder import ConversationState
                municipio_ctx["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
                contexto_reclamo = municipio_ctx
                self.context[CONTEXTO_MUNICIPIO] = municipio_ctx
                if isinstance(fresh_chat_data, dict):
                    fresh_chat_data[CONTEXTO_MUNICIPIO] = municipio_ctx
                    self.context["chat_db_context_data"] = fresh_chat_data
                    chat_db_context_obj.context_data = fresh_chat_data
                    safe_flag_modified(chat_db_context_obj, "context_data")
                logger.info(
                    "Contexto de reclamo limpiado. Nuevo estado: %s",
                    municipio_ctx["estado_conversacion"],
                )


            # Notificaciones
            # if ticket_data_cleaned.get("telefono_vecino"):
            #     try:
            #         enviar_notificacion_whatsapp_con_plantilla(
            #             ticket_data_cleaned["telefono_vecino"],
            #             ticket_data_cleaned.get("nombre_vecino", "Vecino"),
            #             str(ticket_nro),
            #             ticket_data_cleaned.get("categoria", "Varios")
            #         )
            #     except Exception as e_whatsapp:
            #         logger.error(f"Error enviando notificación de WhatsApp para {nro_ticket_str}: {e_whatsapp}")

            #     try:
            #         enviar_notificacion_sms(
            #             ticket_data_cleaned["telefono_vecino"],
            #             f"Hola {ticket_data_cleaned.get('nombre_vecino', 'Vecino')}! Tu reclamo M-{ticket_nro} ({ticket_data_cleaned.get('categoria', 'Varios')}) fue generado."
            #         )
            #     except Exception as e_sms:
            #         logger.error(f"Error enviando notificación por SMS para {nro_ticket_str}: {e_sms}")

            # Formatear respuesta y obtener el botón de contacto
            municipio_config = self.context.get('municipio_config_actual', {})
            base_chat_url = municipio_config.get('base_chat_url', 'https://www.chatboc.ar/chat')
            promo_image_url = _resolve_promo_image_url(municipio_config)
            channel_value = (self.context.get("channel") or "").strip().lower()
            is_whatsapp_channel = channel_value.startswith("whatsapp")
            is_web_like_channel = (
                not channel_value
                or channel_value.startswith("web")
                or "widget" in channel_value
            )
            categoria_display = categoria
            mensaje_respuesta, botones_finales = formatear_ticket_respuesta(
                "reclamo",
                ticket_data_cleaned.get("nombre_vecino", "Vecino/a"),
                descripcion,
                categoria_display,
                nro_ticket_str,
                contacto_especializado,
                base_chat_url,
                dni=ticket_data_cleaned.get("dni_vecino"),
                consulta_pin=pin_final,
                include_links_in_message=not is_web_like_channel,
                ubicacion=ubicacion_llm,
            )
            if botones_finales is None:
                botones_finales = []

            logger.info(
                "Municipal claim receipt formatted ticket_id=%s channel=%s "
                "button_count=%s",
                ticket_creado.get("id"),
                channel_value or "unknown",
                len(botones_finales),
            )

            # Append promotional content (image, CTA and button) in a structured way
            promo_section = promo_service.build_ticket_promo_section(
                ticket_number=nro_ticket_str,
                neighbor_name=ticket_data_cleaned.get("nombre_vecino", "Vecino/a"),
                owner_user=owner_user,
                municipio_config=municipio_config,
            )
            promo_text = None
            if promo_section:
                promo_text = promo_section.get("message_body")
                if promo_text:
                    mensaje_respuesta = f"{mensaje_respuesta}\n\n{promo_text}"

                promo_button = promo_section.get("button")
                if promo_button:
                    promo_url = promo_button.get("url")
                    matching_button = None

                    if promo_url:
                        promo_domain, promo_path = _normalize_url_for_comparison(promo_url)
                        for boton in (botones_finales or []):
                            if not isinstance(boton, dict):
                                continue
                            boton_type = boton.get("type")
                            if boton_type and str(boton_type).lower() != "url":
                                continue
                            boton_url = boton.get("url")
                            if not boton_url:
                                continue

                            boton_domain, boton_path = _normalize_url_for_comparison(boton_url)
                            same_domain = bool(promo_domain and boton_domain and promo_domain == boton_domain)
                            same_path = bool(promo_path and boton_path and promo_path == boton_path)

                            if same_domain or same_path:
                                matching_button = boton
                                break

                    if matching_button:
                        promo_cta = promo_button.get("texto")
                        existing_text = (matching_button.get("texto") or "").strip()
                        normalized_existing = existing_text.replace("🌐", "").strip().lower()

                        if promo_cta and (not existing_text or normalized_existing == "más información"):
                            matching_button["texto"] = promo_cta

                        matching_button.setdefault("type", "url")
                    elif promo_url:
                        existing_urls = {
                            boton.get("url")
                            for boton in (botones_finales or [])
                            if isinstance(boton, dict) and boton.get("url")
                        }
                        if promo_url not in existing_urls:
                            botones_finales.append(promo_button)

                if not promo_image_url and promo_section.get("image_url"):
                    promo_image_url = promo_section.get("image_url")

            if not is_web_like_channel:
                botones_finales = remove_buttons_with_urls_in_message(
                    mensaje_respuesta,
                    botones_finales,
                )

            # WhatsApp already has an open ticket conversation.  A delayed
            # main menu is useful on web, but creates an unrelated extra turn
            # (and can trigger menu TTS) immediately after the receipt.
            menu_payload = None if is_whatsapp_channel else _get_main_menu_payload(self.context)

            claim_confirmation = build_claim_confirmation_payload(
                categoria=categoria_display,
                ubicacion=ubicacion_llm,
                descripcion=descripcion,
                nombre=ticket_data_cleaned.get("nombre_vecino"),
                telefono=ticket_data_cleaned.get("telefono_vecino"),
                email=ticket_data_cleaned.get("email_vecino"),
                channel=channel_value or self.context.get("channel"),
            )

            response_payload = {
                "success": True,
                "message_body": mensaje_respuesta,
                "options_list": botones_finales,
                "message_type": "interactive_buttons" if botones_finales else "text",
                "image_url": promo_image_url,
                "data": {
                    "ticket_id": ticket_creado.get('id'),
                    "nro_ticket": nro_ticket_str,
                    "status": "creado",
                    "consulta_pin": pin_final,
                    "nombre_vecino": ticket_data_cleaned.get("nombre_vecino"),
                    "contacto_especializado": contacto_especializado,
                    "promo_text": promo_text,
                    "claim_confirmation": claim_confirmation,
                    "confirmation_card": claim_confirmation,
                }
            }
            if not is_whatsapp_channel:
                response_payload.update(
                    {
                        "delayed_payload": menu_payload,
                        "delay_seconds": 20,
                    }
                )
            tracking_url = build_claim_tracking_url(base_chat_url, nro_ticket_str, pin_final)
            if tracking_url:
                response_payload.update(
                    {
                        "whatsapp_flow": "claim_status",
                        "tracking_url": tracking_url,
                        "ticket_status_url": tracking_url,
                        "webview_url": tracking_url,
                        "cta_label": "Ver estado",
                    }
                )
                response_payload.setdefault("data", {}).update(
                    {
                        "tracking_url": tracking_url,
                        "ticket_status_url": tracking_url,
                        "webview_url": tracking_url,
                    }
                )
            response_payload["contexto_actualizado"] = {
                "latest_ticket_id": ticket_creado.get("id"),
                "latest_ticket_nro": nro_ticket_str,
                "latest_ticket_pin": pin_final,
                "latest_tracking_url": tracking_url,
                "awaiting_photo_for_ticket": nro_ticket_str,
                "last_ticket_code": nro_ticket_str,
                "awaiting_ticket_photo": True,
                "awaiting_ticket_photo_until": time.time() + 600,
            }
            if not is_whatsapp_channel:
                response_payload["whatsapp_receipt"] = render_ticket_whatsapp(
                    kind="reclamo",
                    nombre=ticket_data_cleaned.get("nombre_vecino", "Vecino/a"),
                    ticket_nro=nro_ticket_str,
                    categoria=categoria_display,
                    descripcion=descripcion,
                    direccion=ubicacion_llm,
                    dni=ticket_data_cleaned.get("dni_vecino"),
                    consulta_pin=pin_final,
                    base_chat_url=base_chat_url,
                    promo_image_url=promo_image_url,
                    promo_text=promo_text,
                    contacto_especializado=contacto_especializado,
                    info_url=municipio_config.get("link_web") or municipio_config.get("url_web"),
                    include_menu=True,
                )
            if is_whatsapp_channel:
                pre_messages = list(response_payload.get("_twilio_pre_messages") or [])
                pre_messages.append(
                    build_claim_created_template_pre_message(
                        ticket_nro=nro_ticket_str,
                        categoria=categoria_display,
                        consulta_pin=pin_final,
                        base_chat_url=base_chat_url,
                        within_24h_window=True,
                    )
                )
                response_payload["_twilio_pre_messages"] = pre_messages
                response_payload["message_body"] = build_claim_created_followup_text(pin_final)
                response_payload["options_list"] = []
                response_payload["message_type"] = "text"
                response_payload["generar_audio"] = False
                response_payload["skip_audio_generation"] = True
                response_payload.setdefault("data", {})["receipt_delivery"] = {
                    "primary_surface": "twilio_template_or_plain_text_fallback",
                    "followup_surface": "pin_and_evidence_instructions",
                    "tts_allowed": False,
                }
                # The operational receipt must not become a promotional media
                # card or a third delivery surface.
                response_payload.pop("image_url", None)

            tracking_url = (
                response_payload.get("contexto_actualizado", {}) or {}
            ).get("latest_tracking_url")
            contexto_reclamo["last_created_reclamo"] = {
                "ts": time.time(),
                "fingerprint": dedupe_fingerprint,
                "ticket_id": ticket_creado.get("id"),
                "ticket_nro": nro_ticket_str,
                "consulta_pin": pin_final,
                "tracking_url": tracking_url,
            }
            caption_values = {
                "message_body": mensaje_respuesta,
                "ticket_nro": nro_ticket_str,
                "ticket_id": ticket_creado.get("id"),
                "nombre": ticket_data_cleaned.get("nombre_vecino"),
                "categoria": categoria_display,
                "descripcion": descripcion,
                "consulta_pin": pin_final,
            }
            final_response = (
                response_payload
                if is_whatsapp_channel
                else _apply_whatsapp_closing_promo(
                    response_payload,
                    context=self.context,
                    caption_values=caption_values,
                )
            )
            # Preserve the legacy action-handler contract while channel
            # orchestrators migrate to the unified message_body field.
            final_response.setdefault("message_to_user", final_response.get("message_body"))
            return final_response
        except Exception as e:
            logger.error(
                "Municipal claim creation failed error_type=%s",
                type(e).__name__,
            )
            response = {
                "success": False,
                "message_body": "Hubo un problema al registrar tu reclamo. Por favor, intenta de nuevo más tarde.",
                "error_code": "claim_creation_failed",
            }
            return response

class ConsultarEstadoTicketActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoTicketActionHandler with data: {action_data}")
        ticket_id = action_data.get("id_ticket_mencionado")
        if not ticket_id:
            # Try to parse from raw user question stored in context
            raw_question = self.context.get("pregunta_actual_usuario", "")
            match = re.search(r"\d+", raw_question)
            if match:
                ticket_id = match.group(0)
        if not ticket_id:
            return {
                "success": False,
                "message_to_user": "Para consultar el estado, necesito el número de ticket.",
                "pedir_info": "id_ticket_mencionado"
            }

        ticket_id_str = str(ticket_id).replace("M-", "").strip()

        pin = action_data.get("pin")
        if not pin:
            return {
                "success": False,
                "message_to_user": "Necesito el PIN de 6 dígitos para consultar el ticket.",
                "pedir_info": "pin_ticket",
            }

        owner_user = self.context.get("user_obj")
        tenant_id, municipio_id = _resolve_municipio_tenant_ids(owner_user, self.context)
        if not tenant_id and not municipio_id:
            logger.warning(
                "[tickets] refusing unscoped status lookup for ticket=%s",
                ticket_id_str,
            )
            return {
                "success": False,
                "message_to_user": "No pude validar el municipio asociado a esta consulta. Volvé a iniciar el seguimiento desde el enlace del ticket.",
                "message_type": "text",
            }

        ticket_query = MunicipioTicket.query.filter_by(
            nro_ticket=ticket_id_str,
            consulta_pin=pin,
        )
        if tenant_id:
            ticket_query = ticket_query.filter(MunicipioTicket.tenant_id == tenant_id)
        if municipio_id:
            ticket_query = ticket_query.filter(MunicipioTicket.municipio_id == municipio_id)
        ticket = ticket_query.first()
        if not ticket:
            return {
                "success": False,
                "message_to_user": f"No encontré el ticket M-{ticket_id_str} o el PIN es incorrecto.",
                "options_list": [{"texto": "Ingresar otro número", "id_accion": "consultar_estado_ticket"}],
                "message_type": "interactive_buttons",
            }

        asunto = ticket.asunto or ticket.categoria or "Reclamo"
        user_message = (
            f"El ticket M-{ticket.nro_ticket} sobre '{asunto}' se encuentra actualmente: **{ticket.estado}**."
        )
        botones = [{"texto": "Consultar otro ticket", "id_accion": "consultar_estado_ticket"}]
        municipio_config = self.context.get("municipio_config_actual", {}) if isinstance(self.context, dict) else {}
        base_chat_url = municipio_config.get("base_chat_url", "https://www.chatboc.ar")
        tracking_url = build_claim_tracking_url(base_chat_url, ticket.nro_ticket, ticket.consulta_pin)
        return {
            "success": True,
            "message_to_user": user_message,
            "options_list": botones,
            "message_type": "interactive_buttons",
            "whatsapp_flow": "claim_status",
            "tracking_url": tracking_url,
            "ticket_status_url": tracking_url,
            "webview_url": tracking_url,
            "cta_label": "Ver estado",
            "data": {
                "ticket_id": ticket.nro_ticket,
                "status": ticket.estado,
                "tracking_url": tracking_url,
                "ticket_status_url": tracking_url,
                "webview_url": tracking_url,
            }
        }

class ConsultarInfoTramiteActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarInfoTramiteActionHandler with data: {action_data}")
        tramite_nombre = action_data.get("nombre_tramite") or action_data.get("categoria") # Categoria might be used if specific tramite name isn't clear
        if not tramite_nombre:
            return {
                "success": False,
                "message_to_user": "¿Sobre qué trámite necesitas información?",
                "pedir_info": "nombre_tramite"
            }

        from services.municipio_responder import obtener_info_tramite_web

        info_tramite = obtener_info_tramite_web(tramite_nombre)

        if "error" in info_tramite:
            return {
                "success": False,
                "message_to_user": f"No encontré información sobre el trámite '{tramite_nombre}'.",
                "pedir_info": "nombre_tramite"
            }
        else:
            botones = info_tramite.get("botones", []).copy()
            botones.append({"texto": "Consultar otro trámite", "id_accion": "info_tramite"})
            return {
                "success": True,
                "message_to_user": info_tramite.get("contenido", "No hay información disponible para este trámite."),
                "options_list": botones,
                "message_type": "interactive_buttons",
                "data": {"tramite_nombre": tramite_nombre, "info_recuperada": "json"}
            }

class HacerSugerenciaActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(
            "Executing HacerSugerenciaActionHandler supplied_fields=%s "
            "has_location=%s has_coordinates=%s",
            sorted(str(key) for key in action_data.keys()),
            bool(action_data.get("ubicacion")),
            bool(action_data.get("coordenadas")),
        )
        descripcion_sugerencia = action_data.get("descripcion")
        if not descripcion_sugerencia:
            return {
                "success": False,
                "message_to_user": "Claro, ¿cuál es tu sugerencia?",
                "pedir_info": "descripcion_sugerencia"
            }

        ubicacion_sugerencia = action_data.get("ubicacion")
        coordenadas_sugerencia = action_data.get("coordenadas")
        if not ubicacion_sugerencia:
            return {
                "success": False,
                "message_to_user": "¿En qué lugar aplica tu sugerencia? Podés darme una dirección o ubicación aproximada.",
                "pedir_info": "ubicacion"
            }

        contacto_prev = self.context.get(CONTEXTO_MUNICIPIO, {}).get("contacto_usuario", {})
        viewer_user = self.context.get("viewer_user_obj")
        nombre_vecino = (
            action_data.get("nombre")
            or action_data.get("usuario")
            or action_data.get("nombre_usuario_detectado")
            or contacto_prev.get("nombre")
            or (getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None))
        )
        dni_vecino = action_data.get("dni") or contacto_prev.get("dni") or getattr(viewer_user, "dni", None)
        email_vecino = (
            action_data.get("email")
            or action_data.get("email_detectado")
            or contacto_prev.get("email")
            or getattr(viewer_user, "email", None)
        )
        direccion_contacto = (
            action_data.get("direccion")
            or action_data.get("direccion_contacto")
            or contacto_prev.get("direccion")
            or getattr(viewer_user, "direccion", None)
            or ubicacion_sugerencia
        )
        telefono_vecino = (
            action_data.get("telefono")
            or contacto_prev.get("telefono")
            or getattr(viewer_user, "telefono", None)
        )
        if not all([nombre_vecino, dni_vecino, email_vecino, direccion_contacto]):
            return {
                "success": False,
                "message_to_user": "Para registrar tu sugerencia necesito tu nombre completo, DNI, email y dirección. Podés escribir todo en un solo mensaje.",
                "pedir_info": "datos_contacto_sugerencia"
            }
        pin_llm = action_data.get("pin") or action_data.get("consulta_pin")
        pin_str = str(pin_llm).strip() if pin_llm else ""
        if pin_str.isdigit() and len(pin_str) == 6:
            pin_final = pin_str
        else:
            pin_final = f"{random.randint(0, 999999):06d}"
        # Create a ticket for the suggestion
        owner_user = self.context.get("user_obj")
        user_id_db = getattr(viewer_user, "id", None)
        anon_id_db = self.context.get("anon_id") if not user_id_db else None
        tenant_id, municipio_id = _resolve_municipio_tenant_ids(owner_user, self.context)
        if not tenant_id and not municipio_id:
            logger.error("Municipal suggestion creation refused because tenant scope is unresolved.")
            return {
                "success": False,
                "message_to_user": (
                    "No pude validar el municipio que debe recibir la sugerencia. "
                    "No se creó ningún ticket; intentá nuevamente en unos minutos."
                ),
                "message_body": (
                    "No pude validar el municipio que debe recibir la sugerencia. "
                    "No se creó ningún ticket; intentá nuevamente en unos minutos."
                ),
                "message_type": "text",
                "fuente": "municipio_tenant_scope_rejected",
            }
        nombre_vecino_final = nombre_vecino or getattr(viewer_user, "nombre", "Ciudadano Anónimo")

        ticket_data = {
            "asunto": "Sugerencia de Ciudadano",
            "categoria": "Sugerencia",
            "detalles": descripcion_sugerencia,
            "estado": "nuevo",
            "user_id": user_id_db,
            "anon_id": anon_id_db,
            "origen_reclamo": "LLM_CHATBOT",
            "nombre_vecino": nombre_vecino_final,
            "dni_vecino": dni_vecino,
            "email_vecino": email_vecino,
            "telefono_vecino": telefono_vecino,
            "direccion": ubicacion_sugerencia,
            "direccion_contacto": direccion_contacto,
            "latitud": coordenadas_sugerencia.get("lat") if isinstance(coordenadas_sugerencia, dict) else None,
            "longitud": (
                (
                    coordenadas_sugerencia.get("lng")
                    if coordenadas_sugerencia.get("lng") is not None
                    else coordenadas_sugerencia.get("lon")
                )
                if isinstance(coordenadas_sugerencia, dict)
                else None
            ),
            "municipio_id": municipio_id,
            "tenant_id": tenant_id,
            "consulta_pin": pin_final,
        }
        if self.context.get("foto_url"):
            ticket_data["foto_url_directa"] = self.context.get("foto_url")

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            nro_ticket_str = _format_ticket_code("S", ticket_creado.get('nro_ticket'))
            logger.info(f"Ticket de sugerencia {nro_ticket_str} creado exitosamente.")

            pin_value = ticket_creado.get("consulta_pin") or pin_final
            try:
                from models import MunicipioTicket, db
                from routes.ticket import serialize_ticket_to_json
                from socket_service import emit_new_ticket

                ticket_obj = db.session.get(MunicipioTicket, ticket_creado.get("id"))
                if ticket_obj:
                    if not ticket_obj.consulta_pin:
                        ticket_obj.consulta_pin = pin_value
                        db.session.commit()
                    pin_value = ticket_obj.consulta_pin or pin_value
                    ticket_json = serialize_ticket_to_json(ticket_obj, "municipio")
                    emit_new_ticket(ticket_json)
                else:
                    logger.warning(
                        "No se pudo recuperar la sugerencia recién creada para emitir socket: id=%s",
                        ticket_creado.get("id"),
                    )
            except Exception as e_notify:
                logger.error(
                    "Error enviando notificación en tiempo real para sugerencia %s: %s",
                    nro_ticket_str,
                    e_notify,
                    exc_info=True,
                )

            # Limpiar el contexto para evitar estados pegajosos
            user_info = self.context.get(CONTEXTO_MUNICIPIO, {}).get('user', {})
            contacto_usuario = {
                "nombre": nombre_vecino_final,
                "dni": dni_vecino,
                "email": email_vecino,
                "direccion": direccion_contacto,
                "telefono": telefono_vecino,
            }
            if CONTEXTO_MUNICIPIO in self.context:
                ctx_muni = self.context[CONTEXTO_MUNICIPIO]
                ctx_muni.clear()
                if user_info:
                    ctx_muni['user'] = user_info
                ctx_muni['contacto_usuario'] = {k: v for k, v in contacto_usuario.items() if v}
                from services.municipio_responder import ConversationState
                ctx_muni['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name

            # Obtener la URL base del chat del contexto para el botón "Ver mi Ticket"
            municipio_config = self.context.get('municipio_config_actual', {})
            base_chat_url = municipio_config.get('base_chat_url', 'https://www.chatboc.ar/chat')
            promo_image_url = _resolve_promo_image_url(municipio_config)

            respuesta_formateada, botones_generados = formatear_ticket_respuesta(
                "sugerencia",
                nombre_vecino_final,
                descripcion_sugerencia,
                "Sugerencia",
                nro_ticket_str,
                {}, # No hay contacto especializado para sugerencias
                base_chat_url,
                dni=dni_vecino,
                consulta_pin=pin_value,
            )

            promo_section = promo_service.build_ticket_promo_section(
                ticket_number=nro_ticket_str,
                neighbor_name=nombre_vecino_final,
                owner_user=owner_user,
                municipio_config=municipio_config,
            )
            promo_text = None
            if promo_section:
                promo_text = promo_section.get("message_body")
                if promo_text:
                    respuesta_formateada = f"{respuesta_formateada}\n\n{promo_text}"
                if not promo_image_url and promo_section.get("image_url"):
                    promo_image_url = promo_section.get("image_url")

            # Añadir el botón de acción específico para sugerencias
            botones_finales = botones_generados
            botones_finales.append({"texto": "Hacer otra sugerencia", "id_accion": "hacer_sugerencia"})

            response_payload = {
                "success": True,
                "message_to_user": respuesta_formateada,
                "options_list": botones_finales,
                "message_type": "interactive_buttons",
                "image_url": promo_image_url,
                "consulta_pin": pin_value,
                "data": {
                    "ticket_id": ticket_creado.get('id'),
                    "nro_ticket": nro_ticket_str,
                    "status": "creado",
                    "nombre_vecino": nombre_vecino_final,
                    "promo_text": promo_text,
                    "consulta_pin": pin_value,
                }
            }
            channel_value = (self.context.get("channel") or "").strip().lower()
            response_payload["whatsapp_receipt"] = render_ticket_whatsapp(
                kind="sugerencia",
                nombre=nombre_vecino_final,
                ticket_nro=nro_ticket_str,
                categoria="Sugerencia",
                descripcion=descripcion_sugerencia,
                direccion=ubicacion_sugerencia,
                dni=dni_vecino,
                consulta_pin=pin_value,
                base_chat_url=base_chat_url,
                promo_image_url=promo_image_url,
                promo_text=promo_text,
                info_url=municipio_config.get("link_web") or municipio_config.get("url_web"),
            )
            if channel_value == "whatsapp":
                receipt = response_payload["whatsapp_receipt"]
                response_payload["message_to_user"] = receipt.get("body_text") or respuesta_formateada
                response_payload["options_list"] = []
                response_payload["message_type"] = "text"
                response_payload["image_url"] = receipt.get("media_url") or promo_image_url
            caption_values = {
                "message_body": respuesta_formateada,
                "ticket_nro": nro_ticket_str,
                "ticket_id": ticket_creado.get("id"),
                "nombre": nombre_vecino_final,
                "categoria": "Sugerencia",
                "descripcion": descripcion_sugerencia,
                "consulta_pin": pin_value,
            }
            return _apply_whatsapp_closing_promo(
                response_payload,
                context=self.context,
                caption_values=caption_values,
            )
        except Exception as e:
            logger.error(f"Error en HacerSugerenciaActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al intentar registrar tu sugerencia. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }

class ConsultarPuntosDeInteresActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarPuntosDeInteresActionHandler with data: {action_data}")

        tipo_de_comercio = action_data.get("tipo_comercio")
        if not tipo_de_comercio:
            return {"success": False, "message_to_user": "No especificaste qué tipo de comercio buscar."}

        # La ubicación se obtiene de los datos de la acción (si se proporcionó en el mensaje actual)
        # o del contexto de la conversación como fallback.
        ubicacion = action_data.get("ubicacion") or self.context.get("ubicacion_usuario")
        if not ubicacion:
            # Si no hay ubicación en ningún lado, se la pedimos al usuario.
            self.context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = "ESPERANDO_UBICACION_GENERAL"
            self.context[CONTEXTO_MUNICIPIO]["accion_pendiente_tras_ubicacion"] = "consultar_puntos_de_interes"
            self.context[CONTEXTO_MUNICIPIO]["datos_pendientes"] = {"tipo_comercio": tipo_de_comercio}

            return {
                "success": False,
                "message_to_user": "Para poder ayudarte mejor, necesito tu ubicación. ¿Podrías compartirla?",
                "pedir_info": "ubicacion"
            }

        from services.herramientas_municipio import buscar_comercios_por_rubro_y_ubicacion

        resultado = buscar_comercios_por_rubro_y_ubicacion(tipo_de_comercio, ubicacion)

        return {
            "success": True,
            "message_to_user": resultado,
            "data": {"tipo_comercio_buscado": tipo_de_comercio, "ubicacion_usada": ubicacion}
        }

class ActivarPanicoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.critical(f"Executing ActivarPanicoActionHandler with data: {action_data}")
        # Simulate alerting emergency services
        user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. Hemos notificado a los servicios de emergencia con tu ubicación. Mantené la calma, la ayuda está en camino."
        if not action_data.get("coordenadas") and not action_data.get("ubicacion"):
            user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. No pudimos obtener tu ubicación precisa. Por favor, si es posible, indicala a los servicios de emergencia cuando te contacten. Mantené la calma."

        # servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": "ALERTA PANICO", ...})
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"alerta_status": "enviada"}
        }

from socket_service import socketio, emit_new_ticket
from routes.ticket import serialize_ticket_to_json

class DerivarHumanoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Crea un ticket real de chat en vivo y devuelve su identificador."""
        logger.info(f"Executing DerivarHumanoActionHandler with data: {action_data}")

        try:
            viewer_user = self.context.get("viewer_user_obj")
            owner_user = self.context.get("user_obj")
            pregunta_original = self.context.get("pregunta_actual_usuario", "")

            nombre = (getattr(viewer_user, "name", None) or action_data.get("nombre"))
            telefono = (getattr(viewer_user, "telefono", None) or action_data.get("telefono"))
            email = (getattr(viewer_user, "email", None) or action_data.get("email"))

            ticket_data = {
                "asunto": f"Solicitud de Chat en Vivo por: {nombre or 'Vecino'}",
                "categoria": "Atención en Vivo",
                "pregunta": pregunta_original,
                "detalles": action_data.get("motivo_derivacion", "Solicitud de agente"),
                "user_id": self.context.get("cliente_id"),
                "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                "municipio_id": getattr(owner_user, "municipio_id", None),
                "estado": "esperando_agente_en_vivo",
                "nombre_vecino": nombre,
                "telefono_vecino": telefono,
                "email_vecino": email,
            }
            ticket_type = "municipio"

            ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
            ticket_data_cleaned['tipo_ticket'] = ticket_type
            sala_dict = servicio_tickets.crear_nuevo_ticket(tipo_ticket=ticket_type, ticket_data=ticket_data_cleaned)
            if not sala_dict:
                raise Exception("crear_nuevo_ticket devolvió None")

            # Since downstream functions need the object, fetch it from the DB
            from models import MunicipioTicket
            sala_obj = db.session.get(MunicipioTicket, sala_dict['id'])
            if not sala_obj:
                raise Exception(f"No se pudo recuperar el ticket recién creado con ID {sala_dict['id']}")

            try:
                ticket_json = serialize_ticket_to_json(sala_obj, ticket_type)
                emit_new_ticket(ticket_json)
            except Exception as e_notify:
                logger.error(f"Error enviando notificación en tiempo real para ticket #{sala_dict['nro_ticket']}: {e_notify}", exc_info=True)

            servicio_tickets.crear_comentario(
                ticket_id=sala_dict['id'],
                tipo_ticket=ticket_type,
                comentario_data={
                    "comentario": pregunta_original,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                    "es_admin": False,
                },
            )

            # Emitir evento de socket para notificar al panel de administración
            admin_socket_room = f"municipio_{sala_obj.municipio_id}"
            try:
                ticket_json = serialize_ticket_to_json(sala_obj, ticket_type)
                socketio.emit('live_chat_request', ticket_json, room=admin_socket_room)
                logger.info(f"Socket event 'live_chat_request' emitted to room '{admin_socket_room}' for ticket {sala_obj.id}")
            except Exception as e_socket:
                logger.error(f"Failed to emit socket event for new live chat ticket {sala_obj.id}: {e_socket}", exc_info=True)


            chat_id = f"M-{sala_dict['nro_ticket']}"

            # formatear_ticket_respuesta now returns a tuple (message, buttons)
            user_message, _ = formatear_ticket_respuesta(
                "chat",
                nombre,
                pregunta_original,
                "Atención en Vivo",
                chat_id,
            )
            tenant_profile = self.context.get("tenant_profile") or self.context.get("tenant")
            if not tenant_profile:
                tenant_profile = getattr(owner_user, "tenant", None)
            live_chat_status = attach_ticket_room_access(
                build_tenant_live_chat_status(
                    tenant_profile,
                    socket_room=build_ticket_room(ticket_type, sala_obj.id),
                ),
                ticket_type=ticket_type,
                ticket_id=sala_obj.id,
            )
            socket_room = live_chat_status["socket_room"]
            chat_context_data = self.context.get("chat_db_context_data")
            if isinstance(chat_context_data, dict):
                chat_context_data.update(
                    {
                        "human_chat_in_progress": True,
                        "ticket_id": sala_dict["id"],
                        "tipo_ticket": ticket_type,
                        "room": f"ticket_{ticket_type}_{sala_dict['id']}",
                        "live_chat_socket_room": socket_room,
                        "live_chat_status": live_chat_status.get("mode"),
                    }
                )
            if not live_chat_status.get("available"):
                schedule_text = live_chat_status.get("description")
                if schedule_text:
                    user_message = (
                        f"{user_message}\n\n"
                        f"⏰ Nuestro horario de atención en vivo es {schedule_text}."
                    )
                else:
                    user_message = f"{user_message}\n\n⏰ Ahora mismo no hay agentes disponibles."
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {
                    "ticket_id": sala_dict['id'],
                    "chat_id": chat_id,
                    "status": "esperando_agente_en_vivo",
                    "live_chat": live_chat_status,
                    "live_chat_access_token": live_chat_status["access_token"],
                    "socket_room": socket_room,
                    "channel_mode": live_chat_status.get("mode"),
                },
            }
        except Exception as e:
            logger.error(f"Error en DerivarHumanoActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Ocurrió un problema al crear el chat en vivo. ¿Podés intentar de nuevo más tarde?",
                "error_details": str(e),
            }

class ProcesarAdjuntoReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoReclamoActionHandler with data: {action_data}")
        # This handler would be triggered AFTER an image/file is uploaded and processed by InputProcessor
        # and its analysis (e.g., from Vision API) is available in action_data.

        archivo_url = action_data.get("archivo_url")
        analisis_imagen = action_data.get("analisis_imagen") # e.g., {'es_reclamo': True, 'categoria_sugerida': 'bache', ...}

        if not archivo_url:
            return {"success": False, "message_to_user": "No se detectó ningún archivo adjunto."}

        # Simulate associating the attachment with a claim (either new or existing)
        # This might update a claim in progress or provide data for a new one.
        user_message = f"Recibí el archivo {archivo_url}. "
        if analisis_imagen:
            user_message += f"Parece ser sobre '{analisis_imagen.get('categoria_sugerida', 'algo')}'."
            if analisis_imagen.get('texto_ocr'):
                 user_message += f" Contiene texto: '{analisis_imagen['texto_ocr'][:50]}...'."

        # The result of this action might be to update the context for ReclamoHandler
        # or to directly create/update a claim if enough info is present.
        # For now, just acknowledge.
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"adjunto_procesado": True, "analisis_realizado": bool(analisis_imagen)}
        }

class CorregirDatosReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CorregirDatosReclamoActionHandler with data: {action_data}")

        campo_a_corregir = action_data.get("campo_a_corregir")
        nuevo_valor = action_data.get("nuevo_valor")

        if not campo_a_corregir or nuevo_valor is None:
            return {
                "success": False,
                "message_to_user": "No especificaste qué dato corregir o cuál es el nuevo valor.",
                "pedir_info": "detalle_correccion"
            }

        # Update the context with the new value
        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO, {})
        if campo_a_corregir == "ubicacion":
            contexto_reclamo["direccion_reclamo"] = nuevo_valor
        elif campo_a_corregir == "descripcion":
            contexto_reclamo["descripcion_reclamo"] = nuevo_valor
        elif campo_a_corregir == "categoria":
            contexto_reclamo["categoria_reclamo"] = nuevo_valor
        elif campo_a_corregir == "nombre":
            contexto_reclamo["nombre_vecino"] = nuevo_valor
        elif campo_a_corregir == "telefono":
            contexto_reclamo["telefono_vecino"] = nuevo_valor
        elif campo_a_corregir == "email":
            contexto_reclamo["email_vecino"] = nuevo_valor

        user_message = f"Entendido. He actualizado '{campo_a_corregir}' a '{nuevo_valor}'. ¿Algo más que desees cambiar o confirmamos el reclamo?"

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"campo_corregido": campo_a_corregir, "valor_actualizado": nuevo_valor},
            "pedir_info": "confirmacion_tras_correccion"
        }

class MenuPrincipalActionHandler(BaseActionHandler):
    action_name = "menu_principal"

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": True,
            "message_to_user": "Estas son las cosas que puedo hacer por vos:",
            "options_list": [
                {"texto": "Hacer un Reclamo", "id_accion": "crear_reclamo"},
                {"texto": "Consultas y Turnos", "id_accion": "consultar_tramite"},
                {"texto": "Buscar estacionamiento", "id_accion": "buscar_estacionamiento"},
            ],
            "message_type": "interactive_buttons"
        }

class SolicitarLlamadaActionHandler(BaseActionHandler):
    action_name = "solicitar_llamada"

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Inicia una llamada saliente al usuario para continuar la interacción por voz.
        """
        logger.info("Executing outbound callback action")

        viewer_user = self.context.get("viewer_user_obj")
        owner_user = self.context.get("user_obj")

        # --- Plan Check: Only "full" (or enterprise/premium) plans allow outbound callback ---
        # "pro" allows inbound only.

        # Determine plan from tenant profile or user record
        plan = "free"
        tenant = getattr(owner_user, "tenant", None)
        if tenant:
            plan = str(tenant.plan or "free").lower()
        elif hasattr(owner_user, "plan"):
            plan = str(owner_user.plan or "free").lower()

        # Allow if plan is 'full' (legacy: 'premium', 'enterprise')
        # allowed_plans = {"full"}

        # Map legacy high-tier plans to full
        # if plan in {"premium", "enterprise", "municipio_full"}:
        #    plan = "full"

        # if plan not in allowed_plans:
        #      return {
        #         "success": False,
        #         "message_to_user": "Esta función (Llamada Saliente) está disponible solo en el plan Full. Por favor, llamanos directamente o consultá por upgrade.",
        #         "message_type": "text"
        #     }

        country_code = _voice_country_code(tenant)
        clean_user_phone, had_destination_candidate = _resolve_callback_destination(
            self.context,
            action_data,
            viewer_user,
            country_code=country_code,
        )
        if not clean_user_phone:
            return {
                "success": False,
                "message_to_user": (
                    "Para llamarte necesito un teléfono válido con código de país. "
                    "Escribilo y voy a continuar sin perder el contexto."
                ),
                "message_type": "text",
                "pedir_info": "telefono",
                "data": {
                    "delivery_state": (
                        "rejected_invalid_destination"
                        if had_destination_candidate
                        else "rejected_missing_destination"
                    )
                },
            }

        # A WhatsApp sender is not necessarily voice-enabled. Never fall back
        # to TWILIO_PHONE_NUMBER; callbacks require a dedicated PSTN caller ID.
        bot_phone = _resolve_voice_caller_id(tenant)

        if not bot_phone:
            logger.error("Outbound callback refused reason=voice_caller_id_missing")
            return {
                "success": False,
                "message_to_user": (
                    "La devolución de llamada no está disponible en este momento. "
                    "Ya registré tu pedido para que puedas continuar por chat."
                ),
                "message_type": "text",
                "data": {"delivery_state": "rejected_invalid_caller_id"},
            }

        clean_bot_phone = _normalize_voice_e164(
            bot_phone,
            default_country_code=country_code,
        )
        if not clean_bot_phone:
            logger.error("Outbound callback refused reason=voice_caller_id_invalid")
            return {
                "success": False,
                "message_to_user": (
                    "La devolución de llamada no está disponible en este momento. "
                    "Ya registré tu pedido para que puedas continuar por chat."
                ),
                "message_type": "text",
                "data": {"delivery_state": "rejected_invalid_caller_id"},
            }

        success = initiate_outbound_call(
            to_number=clean_user_phone,
            from_number=clean_bot_phone,
            chat_session_id=self.context.get("chat_session_uuid"),
        )

        if success:
            return {
                "success": True,
                "message_to_user": (
                    "La solicitud de llamada fue aceptada. Deberías recibirla en unos instantes; "
                    "si no conecta, podés seguir por este chat sin perder el contexto."
                ),
                "message_type": "text",
                "data": {"delivery_state": "provider_accepted"},
            }
        else:
            return {
                "success": False,
                "message_to_user": (
                    "No pude confirmar que la llamada haya sido iniciada. Para evitar duplicarla, "
                    "sigamos por este chat o volvé a solicitarla más tarde."
                ),
                "message_type": "text",
                "data": {"delivery_state": "provider_outcome_unknown"},
            }

# Add other handlers as needed
# e.g., CalificarAtencionActionHandler, ConfirmarCierreTicketActionHandler

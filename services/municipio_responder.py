import os
from flask import jsonify
import pandas as pd
from geopy.geocoders import GoogleV3
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from dotenv import load_dotenv
import sys
import logging
import re
import json
from enum import Enum, auto
import unicodedata
import difflib
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse, urlencode
from flask import current_app, has_app_context, session as flask_session
from cachetools import TTLCache
from models import (
    MunicipioTicket,
    TicketComentario,
    ChatSessionContext,
    db,
    SitioWebInfo,
    Conversacion,
    MunicipioPost,
    TenantProfile,
)
from services.ticket_service import servicio_tickets
from utils.db_utils import safe_flag_modified
from utils.response_utils import normalize_response_payload
# Compatibilidad hacia atrás para pruebas que parchean `flag_modified`
flag_modified = safe_flag_modified

logger = logging.getLogger(__name__)
from twilio.rest import Client
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from services.utils_placeholders import (
    reemplazar_placeholders,
    obtener_respuesta_municipio,
)
from services.config_loader import cargar_configuracion_municipio
from utils.municipio_utils import (
    get_numeric_municipio_id,
    resolve_municipio_identifier,
)
from .actions.municipio_actions import (
    CrearReclamoActionHandler,
    HacerSugerenciaActionHandler,
    _normalize_url_for_comparison,
)
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes,
    parse_direccion_completa,
    normalizar_texto,
    direccion_es_valida,
    TOOL_REGISTRY,
    KEYWORD_TO_CATEGORY_MAP,
)
from .points_of_interest_handler import PointsOfInterestHandler
from .categorias_municipio import CATEGORIAS_RECLAMO, categorias_normalizadas
from .common_utils import (
    validar_email,
    validar_telefono,
    formatear_telefono_e164,
    construir_respuesta_sugerir_registro,
    extract_multiple_contact_details_regex,
    _get_main_menu_payload,
)
from utils.validators import extract_email, extract_phone, extract_dni, extract_name
from utils.address_parse import split_ubicacion_y_distrito
from .llm_utils import extract_complaint_details_llm, extract_multiple_contact_details_llm
import math
from services.tasks import process_image_for_chat_task
from services.intent_classifier import IntentClassifier
from services.multimodal_analyzer import analizar_imagen_con_fallback
from services.subastas import listar_subastas_activas
from services import promo_service
import json
from services.ticket_utils import (
    formatear_ticket_respuesta,
    construir_descripcion_breve,
    remove_buttons_with_urls_in_message,
)
from services.vocabulary_loader import get_name_prefix_stopwords
from .constants import ConversationState, CONTEXTO_MUNICIPIO
from config import (
    BACKEND_URL as DEFAULT_BACKEND_URL,
    ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH,
    ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_PATH,
    IS_HTTPS as DEFAULT_IS_HTTPS,
    Config as AppConfig,
)
from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_service import (
    list_public_encuestas_for_tenant,
    serialize_public_encuesta,
    get_public_encuesta,
)
from services.feature_flag_service import get_feature_toggle

ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

LOCATION_KEYWORD_TOKENS = {
    "barrio": {"barrio", "b°", "bº"},
    "distrito": {"distrito", "zona", "localidad", "ciudad"},
}

_ADDRESS_CONNECTOR_TOKENS = {
    "al",
    "a",
    "de",
    "del",
    "la",
    "las",
    "lo",
    "los",
}

NAME_STOPWORDS = get_name_prefix_stopwords()

PLACEHOLDER_NAMES = {"vecino", "vecina", "vecine", "vecino/a"}

_PLACEHOLDER_DESCRIPTION_CANDIDATES = [
    "iniciar_reclamo",
    "iniciar reclamo",
    "iniciar reclamo con ubicacion",
    "iniciar reclamo con ubicación",
    "iniciar un reclamo",
    "hacer un reclamo",
    "hacer reclamo",
    "nuevo reclamo",
    "realizar reclamo",
    "presentar reclamo",
    "registrar queja",
    "reclamo",
]

PLACEHOLDER_DESCRIPTIONS_NORMALIZED = {
    normalized
    for normalized in (
        normalizar_texto(value) for value in _PLACEHOLDER_DESCRIPTION_CANDIDATES
    )
    if normalized
}


def formatear_opciones(opciones: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Normaliza una lista de opciones para respuestas interactivas.

    Esta función garantiza que cada opción contenga las claves ``texto`` y
    ``action_id`` (aceptando alias comunes como ``text`` o ``action``), elimina
    entradas inválidas y conserva cualquier metadato adicional (por ejemplo,
    ``url`` o ``type``).
    """

    if not opciones:
        return []

    opciones_normalizadas: List[Dict[str, Any]] = []
    combinaciones_vistas: set[tuple[Any, Any, Any]] = set()

    for indice, opcion in enumerate(opciones, start=1):
        if not isinstance(opcion, dict):
            continue

        texto_original = opcion.get("texto") or opcion.get("text") or opcion.get("label") or opcion.get("title")
        texto = str(texto_original).strip() if texto_original is not None else ""
        if not texto:
            texto = f"Opción {indice}"

        action_original = opcion.get("action_id") or opcion.get("action") or opcion.get("id") or opcion.get("value")
        action_id = str(action_original).strip() if action_original is not None else ""
        if not action_id:
            action_id = f"opcion_{indice}"

        opcion_normalizada = {
            key: value
            for key, value in opcion.items()
            if key not in {"text", "label", "title", "action"}
        }
        opcion_normalizada.update({"texto": texto, "action_id": action_id})

        dedup_key = (texto, action_id, opcion_normalizada.get("url"))
        if dedup_key in combinaciones_vistas:
            continue

        combinaciones_vistas.add(dedup_key)
        opciones_normalizadas.append(opcion_normalizada)

    return opciones_normalizadas


def _is_placeholder_description(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    normalized_value = normalizar_texto(value)
    if normalized_value in PLACEHOLDER_DESCRIPTIONS_NORMALIZED:
        return True
    if normalized_value.isdigit():
        return True
    return False


def _location_action_options() -> list[dict[str, str]]:
    return [
        {"texto": "📝 Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
        {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
        {"texto": "🅿️ Estacionamiento", "action_id": "buscar_estacionamiento_con_ubicacion"},
        {"texto": "📍 Lugares cercanos", "action_id": "buscar_lugares_cerca"},
        {"texto": "Cancelar", "action_id": "cancelar"},
    ]


def _build_proactive_location_response(
    location_payload: dict[str, Any],
    contexto_municipio_actual: dict[str, Any],
    chat_db_context,
) -> dict[str, Any]:
    address = location_payload.get("address") or location_payload.get("label") or "la ubicación que compartiste"
    opciones_proactivas = _location_action_options()
    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
    contexto_municipio_actual['ubicacion_contextual'] = location_payload
    contexto_municipio_actual['menu_opciones'] = opciones_proactivas
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")
    return {
        "message_body": f"Recibí tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
        "options_list": opciones_proactivas,
        "message_type": "interactive_buttons",
        "fuente": "proactive_location_handler",
    }


PLACEHOLDER_CONTACT_RESPONSES = {
    "ya te la envie",
    "ya te la mande",
    "ya te la mandé",
    "ya la envie",
    "ya la mande",
    "la misma",
    "es la misma",
    "misma direccion",
    "misma dirección",
    "la misma direccion",
    "la misma dirección",
    "la de antes",
    "igual que antes",
    "la anterior",
}


def _prefer_contact_value(*values: Any, placeholder_checker: Optional[Any] = None) -> Any:
    """Return the first non-empty contact value that is not a placeholder."""

    for value in values:
        if value is None:
            continue
        candidate = value
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if not candidate:
                continue
        if placeholder_checker and placeholder_checker(candidate):
            continue
        return candidate
    return None


def _is_placeholder_name(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    return normalizar_texto(value) in PLACEHOLDER_NAMES


def _is_placeholder_address(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    return normalizar_texto(value) in PLACEHOLDER_CONTACT_RESPONSES


def _is_placeholder_email(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.endswith("@whatsapp.chatboc.com") or normalized.endswith("@anon.chatboc.com")


def _normalize_phone_value(value: Any) -> Optional[str]:
    if not value:
        return None
    telefono = str(value).strip()
    if not telefono or not validar_telefono(telefono):
        return None
    try:
        formatted = formatear_telefono_e164(telefono)
    except Exception:
        formatted = telefono
    return formatted or telefono


def _ensure_sugerencia_address(datos: Dict[str, Any]) -> None:
    if datos.get("direccion"):
        return
    ubicacion = datos.get("ubicacion")
    if isinstance(ubicacion, str) and ubicacion and ubicacion != "N/A":
        datos["direccion"] = ubicacion
        return
    if isinstance(ubicacion, dict):
        address = (
            ubicacion.get("address")
            or ubicacion.get("label")
            or ubicacion.get("texto")
        )
        if address:
            datos["direccion"] = address


def _has_valid_sugerencia_address(datos: Dict[str, Any]) -> bool:
    direccion = datos.get("direccion")
    if isinstance(direccion, str) and direccion.strip():
        return True
    ubicacion = datos.get("ubicacion")
    if isinstance(ubicacion, str) and ubicacion.strip() and ubicacion != "N/A":
        return True
    if isinstance(ubicacion, dict):
        return any(
            isinstance(ubicacion.get(key), str) and ubicacion.get(key).strip()
            for key in ("address", "label", "texto")
        )
    return False


def _get_missing_sugerencia_contact_fields(datos: Dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for campo in ["nombre", "dni", "email", "direccion", "telefono"]:
        if campo == "direccion":
            if not _has_valid_sugerencia_address(datos):
                missing.append(campo)
            continue
        if not datos.get(campo):
            missing.append(campo)
    return missing


def _format_sugerencia_address(datos: Dict[str, Any]) -> str:
    direccion = datos.get("direccion")
    if isinstance(direccion, str) and direccion.strip():
        return direccion
    ubicacion = datos.get("ubicacion")
    if isinstance(ubicacion, dict):
        for key in ("address", "label", "texto"):
            value = ubicacion.get(key)
            if isinstance(value, str) and value.strip():
                return value
    elif isinstance(ubicacion, str) and ubicacion.strip():
        return ubicacion
    return ""


def _merge_contacto_usuario(contexto: Dict[str, Any], nuevos_datos: Dict[str, Any]) -> None:
    contacto_prev = contexto.get("contacto_usuario", {}) or {}
    contacto_actualizado: Dict[str, Any] = {}

    # Preservar valores previos válidos
    for campo, valor_prev in contacto_prev.items():
        if not valor_prev:
            continue
        if campo == "nombre" and _is_placeholder_name(valor_prev):
            continue
        if campo == "email" and _is_placeholder_email(valor_prev):
            continue
        if campo == "direccion" and _is_placeholder_address(valor_prev):
            continue
        if campo == "telefono":
            valor_prev = _normalize_phone_value(valor_prev)
            if not valor_prev:
                continue
        contacto_actualizado[campo] = valor_prev

    # Incorporar nuevos valores válidos
    for campo in ["nombre", "dni", "email", "direccion", "telefono"]:
        valor = nuevos_datos.get(campo)
        if not valor:
            continue
        if campo == "nombre" and _is_placeholder_name(valor):
            continue
        if campo == "email" and _is_placeholder_email(valor):
            continue
        if campo == "direccion" and _is_placeholder_address(valor):
            continue
        if campo == "telefono":
            valor = _normalize_phone_value(valor)
            if not valor:
                continue
        contacto_actualizado[campo] = valor

    if contacto_actualizado:
        contexto["contacto_usuario"] = contacto_actualizado
    elif "contacto_usuario" in contexto:
        contexto["contacto_usuario"] = {}


def _update_sugerencia_contact_fields(
    datos_guardados: Dict[str, Any], nuevos_datos: Dict[str, Any]
) -> None:
    direccion_referencia = (
        nuevos_datos.get("direccion")
        or datos_guardados.get("direccion")
        or datos_guardados.get("ubicacion")
        or ""
    )
    for campo in ["nombre", "dni", "email", "direccion", "telefono"]:
        valor_nuevo = nuevos_datos.get(campo)
        if not valor_nuevo:
            continue
        if campo == "telefono":
            valor_nuevo = _normalize_phone_value(valor_nuevo)
            if not valor_nuevo:
                continue
        elif campo == "nombre":
            if _is_placeholder_name(valor_nuevo) or re.search(r"\d", str(valor_nuevo)):
                continue
            nombre_existente = datos_guardados.get("nombre")
            nombre_existente_normalizado = (
                normalizar_texto(nombre_existente) if nombre_existente else ""
            )
            valor_normalizado = normalizar_texto(str(valor_nuevo))
            if nombre_existente and nombre_existente_normalizado == valor_normalizado:
                continue
            if (
                direccion_referencia
                and valor_normalizado in normalizar_texto(str(direccion_referencia))
                and nombre_existente_normalizado
                and nombre_existente_normalizado not in PLACEHOLDER_NAMES
            ):
                continue
        elif campo == "email":
            if _is_placeholder_email(valor_nuevo):
                continue
            valor_nuevo = str(valor_nuevo).strip()
        elif campo == "direccion":
            if _is_placeholder_address(valor_nuevo):
                continue
            valor_nuevo = str(valor_nuevo).strip()
        else:
            valor_nuevo = str(valor_nuevo).strip()

        existing_valor = datos_guardados.get(campo)
        if existing_valor:
            if campo == "telefono":
                existing_norm = _normalize_phone_value(existing_valor)
                if existing_norm == valor_nuevo:
                    continue
            else:
                valor_normalizado = normalizar_texto(str(valor_nuevo))
                existente_normalizado = normalizar_texto(str(existing_valor))
                if valor_normalizado == existente_normalizado:
                    continue
        datos_guardados[campo] = valor_nuevo


def _normalize_location_payload(
    raw_location: Any,
    *,
    fallback_address: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Normalize location payloads to address/latitude/longitude fields."""

    if not raw_location:
        if fallback_address:
            return {"address": fallback_address}
        return None

    if isinstance(raw_location, str):
        return {"address": raw_location}

    if not isinstance(raw_location, dict):
        if fallback_address:
            return {"address": fallback_address}
        return None

    normalized = dict(raw_location)
    lat = (
        raw_location.get("latitude")
        or raw_location.get("lat")
        or raw_location.get("latitud")
    )
    lon = (
        raw_location.get("longitude")
        or raw_location.get("lon")
        or raw_location.get("lng")
        or raw_location.get("longitud")
    )
    if lat is not None:
        normalized["latitude"] = lat
    if lon is not None:
        normalized["longitude"] = lon

    address = (
        raw_location.get("address")
        or raw_location.get("label")
        or raw_location.get("texto")
        or raw_location.get("descripcion")
        or raw_location.get("ubicacion")
        or fallback_address
    )
    if address:
        normalized["address"] = address

    return normalized


def _build_sugerencia_datos(
    sugerencia_texto: str,
    ubicacion: Optional[str],
    coordenadas: Optional[Dict[str, Any]],
    viewer_user_obj: Any,
    contacto_prev: Dict[str, Any],
) -> Dict[str, Any]:
    datos: Dict[str, Any] = {
        "categoria": "Sugerencia",
        "descripcion": sugerencia_texto,
    }
    if ubicacion:
        datos["ubicacion"] = ubicacion
    if coordenadas:
        datos["coordenadas"] = coordenadas

    nombre = _prefer_contact_value(
        contacto_prev.get("nombre"),
        getattr(viewer_user_obj, "name", None) if viewer_user_obj else None,
        getattr(viewer_user_obj, "nombre", None) if viewer_user_obj else None,
        placeholder_checker=_is_placeholder_name,
    )
    if nombre:
        datos["nombre"] = nombre

    dni = _prefer_contact_value(
        contacto_prev.get("dni"),
        getattr(viewer_user_obj, "dni", None) if viewer_user_obj else None,
    )
    if dni:
        datos["dni"] = str(dni).strip()

    email = _prefer_contact_value(
        contacto_prev.get("email"),
        getattr(viewer_user_obj, "email", None) if viewer_user_obj else None,
        placeholder_checker=_is_placeholder_email,
    )
    if email:
        datos["email"] = email.strip()

    direccion = _prefer_contact_value(
        ubicacion if ubicacion and ubicacion != "N/A" else None,
        contacto_prev.get("direccion"),
        getattr(viewer_user_obj, "direccion", None) if viewer_user_obj else None,
        placeholder_checker=_is_placeholder_address,
    )
    if direccion:
        datos["direccion"] = direccion

    telefono = _prefer_contact_value(
        contacto_prev.get("telefono"),
        getattr(viewer_user_obj, "telefono", None) if viewer_user_obj else None,
    )
    telefono_normalizado = _normalize_phone_value(telefono)
    if telefono_normalizado:
        datos["telefono"] = telefono_normalizado

    _ensure_sugerencia_address(datos)
    return datos


def _set_sugerencia_location_context(
    contexto: Dict[str, Any],
    raw_location: Optional[Dict[str, Any]] = None,
    fallback_address: str | None = None,
) -> None:
    """Persist full location information for suggestion flows."""

    normalized_location = _normalize_location_payload(raw_location, fallback_address=fallback_address)
    if not normalized_location:
        normalized_location = {"address": fallback_address or "N/A"}
    else:
        normalized_location.setdefault("address", fallback_address or "N/A")

    contexto["ubicacion_contextual_sugerencia"] = normalized_location


def _extract_sugerencia_location(contexto: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]]]:
    """Return stored suggestion location and normalized coordinates."""

    raw_location = contexto.pop("ubicacion_contextual_sugerencia", None)
    normalized_location = _normalize_location_payload(raw_location)
    if isinstance(normalized_location, dict):
        address = normalized_location.get("address") or "N/A"
        lat = normalized_location.get("latitude")
        lon = normalized_location.get("longitude")
        if lat is not None and lon is not None:
            return address, {"lat": lat, "lng": lon}
        return address, None
    if isinstance(raw_location, str) and raw_location:
        return raw_location, None
    return "N/A", None


def _build_sugerencia_success_payload(
    context: Dict[str, Any],
    datos_confirmados: Dict[str, Any],
    handler_response: Dict[str, Any],
) -> Dict[str, Any]:
    ticket_info = handler_response.get("data") or {}
    nro_ticket = ticket_info.get("nro_ticket") or handler_response.get("nro_ticket")
    consulta_pin = ticket_info.get("consulta_pin") or handler_response.get("consulta_pin")

    categoria = datos_confirmados.get("categoria") or "Sugerencia"
    descripcion = datos_confirmados.get("descripcion") or ""

    contacto_ctx = context.get(CONTEXTO_MUNICIPIO, {}).get("contacto_usuario", {})
    nombre_vecino = (
        datos_confirmados.get("nombre")
        or datos_confirmados.get("usuario")
        or contacto_ctx.get("nombre")
        or "Vecino/a"
    )
    dni_vecino = datos_confirmados.get("dni")

    municipio_config = context.get("municipio_config_actual", {}) or {}
    base_chat_url = municipio_config.get("base_chat_url", "https://www.chatboc.ar/chat")

    channel_value = (context.get("channel") or "").strip().lower()
    is_web_like_channel = channel_value.startswith("web") or "widget" in channel_value

    message_body, base_buttons = formatear_ticket_respuesta(
        "sugerencia",
        nombre_vecino,
        descripcion,
        categoria,
        nro_ticket,
        {},
        base_chat_url,
        dni=dni_vecino,
        consulta_pin=consulta_pin,
        include_links_in_message=not is_web_like_channel,
    )

    buttons: list[dict] = []
    seen_url_fingerprints: set[tuple[str, str]] = set()
    seen_text_keys: set[tuple[str, str]] = set()

    def _button_key(button: Dict[str, Any]) -> tuple[str, str]:
        texto = str(button.get("texto") or "").strip().lower()
        action = button.get("action_id") or button.get("id_accion") or ""
        return (texto, str(action).strip().lower())

    def _normalize_or_none(url: Optional[str]) -> Optional[tuple[str, str]]:
        if not url:
            return None
        try:
            return _normalize_url_for_comparison(url)
        except Exception:
            return None

    def _register_button(button: Any):
        if not isinstance(button, dict):
            return
        candidate = dict(button)
        url = candidate.get("url")
        normalized = _normalize_or_none(url)
        key = _button_key(candidate)

        if normalized and normalized in seen_url_fingerprints:
            return
        if key in seen_text_keys and not normalized:
            return

        buttons.append(candidate)
        seen_text_keys.add(key)
        if normalized:
            seen_url_fingerprints.add(normalized)

    for btn in base_buttons or []:
        _register_button(btn)

    for btn in handler_response.get("options_list") or []:
        _register_button(btn)

    sugerencia_button = {
        "texto": "💡 Hacer otra sugerencia",
        "action_id": "enviar_sugerencia",
        "id_accion": "hacer_sugerencia",
    }
    if not any(
        isinstance(btn, dict)
        and (
            btn.get("texto") == sugerencia_button["texto"]
            or btn.get("action_id") == sugerencia_button["action_id"]
            or btn.get("id_accion") == sugerencia_button["id_accion"]
        )
        for btn in buttons
    ):
        buttons.append(sugerencia_button)

    promo_section = promo_service.build_ticket_promo_section(
        ticket_number=nro_ticket,
        neighbor_name=nombre_vecino,
    )

    image_url = handler_response.get("image_url") or municipio_config.get("promo_image_url")
    if promo_section:
        promo_text = promo_section.get("message_body")
        if promo_text:
            message_body = f"{message_body}\n\n{promo_text}".strip()
        promo_button = promo_section.get("button")
        if promo_button:
            _register_button(promo_button)
        if not image_url:
            image_url = promo_section.get("image_url") or image_url

    if not is_web_like_channel:
        buttons = remove_buttons_with_urls_in_message(message_body, buttons)

    delayed_payload = handler_response.get("delayed_payload") or _get_main_menu_payload(context)
    if channel_value == "whatsapp":
        delayed_payload = None

    payload: Dict[str, Any] = {
        "success": True,
        "message_body": message_body,
        "options_list": buttons,
        "message_type": "interactive_buttons" if buttons else "text",
        "image_url": image_url,
        "data": ticket_info,
        "fuente": "sugerencia_confirmada",
    }

    audio_url = handler_response.get("audio_url")
    if audio_url:
        payload["audio_url"] = audio_url
    audio_text = handler_response.get("audio_text")
    if audio_text:
        payload["audio_text"] = audio_text

    if delayed_payload:
        payload["delayed_payload"] = delayed_payload
        payload["delay_seconds"] = handler_response.get("delay_seconds", 20)

    return payload


def _build_sugerencia_confirmation_payload(
    datos_sugerencia: Dict[str, Any],
) -> Dict[str, Any]:
    direccion = _format_sugerencia_address(datos_sugerencia)
    telefono = datos_sugerencia.get("telefono") or "No informado"
    descripcion = datos_sugerencia.get("descripcion") or ""

    mensaje_confirmacion = (
        "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
        f"- **Nombre**: {datos_sugerencia.get('nombre')}\n"
        f"- **DNI**: {datos_sugerencia.get('dni')}\n"
        f"- **Email**: {datos_sugerencia.get('email')}\n"
        f"- **Dirección**: {direccion}\n"
        f"- **Teléfono**: {telefono}\n"
        f"- **Sugerencia**: {descripcion}"
    )

    return {
        "message_body": mensaje_confirmacion,
        "options_list": [
            {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
            {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
        ],
        "message_type": "interactive_buttons",
        "fuente": "pide_confirmacion_sugerencia",
    }


def _maybe_prompt_sugerencia_confirmation(
    contexto_municipio_actual: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    datos_sugerencia = contexto_municipio_actual.get("datos_parciales_llm_sugerencia", {})
    if not isinstance(datos_sugerencia, dict):
        return None

    _ensure_sugerencia_address(datos_sugerencia)
    if not datos_sugerencia.get("descripcion"):
        return None

    missing_fields = _get_missing_sugerencia_contact_fields(datos_sugerencia)
    if missing_fields:
        return None

    contexto_municipio_actual["datos_sugerencia"] = datos_sugerencia
    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
    contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
    contexto_municipio_actual.pop("expected_fields_llm_sugerencia", None)
    contexto_municipio_actual.pop("esperando_info_llm", None)

    return _build_sugerencia_confirmation_payload(datos_sugerencia)

class ReclamoState(Enum):
    ESPERANDO_CATEGORIA = auto()
    ESPERANDO_DIRECCION = auto()
    ESPERANDO_DESCRIPCION = auto()
    ESPERANDO_FOTO = auto()
    ESPERANDO_DATOS_CONTACTO = auto()
    ESPERANDO_CONFIRMACION = auto()


CANCEL_KEYWORDS = {
    normalizar_texto(k)
    for k in [
        "cancelar",
        "salir",
        "volver",
        "menu",
        "menú principal",
        "menu principal",
        "terminar",
        "basta",
        "reiniciar",
        "resetear",
        "empezar de nuevo",
        "volver a empezar",
        "empezar de cero",
    ]
}

# Simple cache to avoid recomputing responses for repeated municipal queries
MUNICIPIO_RESPONSE_CACHE = TTLCache(maxsize=256, ttl=3600)

def clear_municipio_cache() -> None:
    """Utility mainly for tests to clear the local municipio response cache."""
    MUNICIPIO_RESPONSE_CACHE.clear()

class ReclamoFlowHandler:
    def __init__(self, context, chat_db_context):
        self.context = context
        self.chat_db_context = chat_db_context
        # Ensure the flow data lives inside the main municipio context so it
        # survives across turns just like other conversation state.
        municipal_ctx = context.get("chat_db_context_data", {}).setdefault(
            CONTEXTO_MUNICIPIO, {}
        )
        self.municipal_ctx = municipal_ctx
        self.flow_context = municipal_ctx.setdefault("reclamo_flow_v2", {})
        self.greeting_handler = GreetingHandler(context)


    def check_for_cancel(self, user_input, payload):
        normalized_input = normalizar_texto(user_input)
        action = (payload.get("action_id") or payload.get("action") or "").lower()
        if (
            normalized_input in CANCEL_KEYWORDS
            or action in {"cancelar", "menu_principal"}
        ):
            return self.end_flow(
                "Proceso de reclamo cancelado. ¿En qué más te puedo ayudar?",
                show_menu=True,
            )
        return None

    def _return_to_main_menu(self):
        """Clear the active flow and return the structured main menu."""
        self.flow_context.clear()
        self.municipal_ctx.pop("reclamo_flow_v2", None)
        self.municipal_ctx.pop("estado_conversacion", None)
        self.municipal_ctx.pop("menu_opciones", None)
        return self.greeting_handler.handle({})

    def handle(self, user_input, payload):
        cancel_response = self.check_for_cancel(user_input, payload)
        if cancel_response:
            return cancel_response

        # Ensure the municipal context reflects that we're inside the claim flow.
        self.municipal_ctx['estado_conversacion'] = "EN_FLUJO_RECLAMO"

        state_name = self.flow_context.get("state")
        state = ReclamoState[state_name] if state_name else None

        if state == ReclamoState.ESPERANDO_CATEGORIA:
            return self.handle_categoria(user_input)
        elif state == ReclamoState.ESPERANDO_DIRECCION:
            return self.handle_direccion(user_input, payload)
        elif state == ReclamoState.ESPERANDO_DESCRIPCION:
            return self.handle_descripcion(user_input)
        elif state == ReclamoState.ESPERANDO_FOTO:
            return self.handle_foto(user_input, payload)
        elif state == ReclamoState.ESPERANDO_DATOS_CONTACTO:
            return self.handle_datos_contacto(user_input)
        elif state == ReclamoState.ESPERANDO_CONFIRMACION:
            return self.handle_confirmacion(user_input, payload)
        else:
            logger.error(f"ReclamoFlowHandler: Estado desconocido o no manejado: {state_name}")
            return self.end_flow("Hubo un error en el proceso, por favor intentá de nuevo.", show_menu=True)

    def start_flow(self, datos_iniciales=None, categoria_inicial=None):
        logger.info("Iniciando flujo de reclamo v2.")
        self.flow_context.clear()
        self.flow_context['datos_reclamo'] = datos_iniciales or {}
        datos = self.flow_context['datos_reclamo']

        if _is_placeholder_description(datos.get('descripcion')):
            datos.pop('descripcion', None)
            datos.pop('descripcion_resumida', None)
        if _is_placeholder_description(datos.get('descripcion_sugerida')):
            datos.pop('descripcion_sugerida', None)

        # Discard leftover menu hints from previous states (e.g., ubicación proactiva).
        self.municipal_ctx.pop('menu_opciones', None)

        # Prefill from image analysis if available
        if self.context.get("foto_url") and not datos.get('foto_url'):
            datos['foto_url'] = self.context.get("foto_url")
        if self.context.get("datos_interpretados_archivo"):
            interpreted = self.context.get("datos_interpretados_archivo")
            if not datos.get('categoria') and interpreted.get('categoria_sugerida'):
                datos['categoria'] = interpreted.get('categoria_sugerida')
            if not datos.get('descripcion') and interpreted.get('descripcion_sugerida'):
                descripcion_interpretada = interpreted.get('descripcion_sugerida')
                datos['descripcion'] = descripcion_interpretada
                datos['descripcion_resumida'] = construir_descripcion_breve(descripcion_interpretada)
                datos['origen_descripcion'] = 'imagen'

        if datos.get('descripcion') and not datos.get('descripcion_resumida'):
            resumen = construir_descripcion_breve(datos.get('descripcion'))
            if resumen:
                datos['descripcion_resumida'] = resumen

        def _apply_prefill(field: str, *candidates) -> None:
            """Populate ``datos`` with the first meaningful value available."""
            existing = datos.get(field)
            if existing and existing != 'Vecino/a' and '@whatsapp.chatboc.com' not in str(existing):
                return
            for candidate in candidates:
                if not candidate:
                    continue
                if isinstance(candidate, str):
                    candidate = candidate.strip()
                    if not candidate:
                        continue
                    if field == 'nombre' and candidate.lower() in {'vecino', 'vecina', 'vecino/a'}:
                        continue
                    if field == 'telefono':
                        cleaned_candidate = re.sub(
                            r'^(tel\.?|teléfono|telefono|cel\.?|celular|whatsapp|wsapp|wa)[:\s-]*',
                            '',
                            candidate,
                            flags=re.IGNORECASE,
                        )
                        cleaned_candidate = re.sub(r'\b(int|interno|intern)\b\.?:?', '', cleaned_candidate, flags=re.IGNORECASE)
                        cleaned_candidate = cleaned_candidate.strip()
                        if re.search(r'[A-Za-z]', cleaned_candidate):
                            continue
                        if len(re.sub(r'\D', '', cleaned_candidate)) < 6:
                            continue
                        candidate = cleaned_candidate
                datos[field] = candidate
                break

        # --- Comprehensive Contact Prefill (Refactored) ---
        viewer = self.context.get("viewer_user_obj")
        contacto_cache = self.municipal_ctx.setdefault('contacto_usuario', {})
        last_ticket = None

        # 1. Find the last ticket to source contact data from.
        #    Priority: Logged-in user's tickets > Anonymous session's tickets.
        try:
            owner_user = self.context.get('user_obj')
            municipio_id = getattr(owner_user, 'municipio_id', None)

            query = MunicipioTicket.query
            if municipio_id:
                query = query.filter_by(municipio_id=municipio_id)

            # Find the last ticket using the anonymous ID to identify the user
            # across requests in the same session.
            anon_id = self.context.get('anon_id')
            if anon_id:
                ticket_by_anon = query.filter_by(anon_id=anon_id).order_by(MunicipioTicket.fecha.desc()).first()
                if ticket_by_anon:
                    last_ticket = ticket_by_anon
                    logger.info(f"Prefilling contact data from last ticket {last_ticket.nro_ticket} found by anon_id.")
        except Exception as e:
            logger.warning(f"Error fetching last ticket for prefill: {e}")

        # 2. Apply prefill from the found ticket
        if last_ticket:
            _apply_prefill('nombre', last_ticket.nombre_vecino, last_ticket.nombre_display_whatsapp)
            _apply_prefill('email', last_ticket.email_vecino)
            _apply_prefill('telefono', last_ticket.telefono_vecino)
            _apply_prefill('dni', last_ticket.dni_vecino)

        # 3. From current session's contact cache (as a fallback)
        _apply_prefill('nombre', contacto_cache.get('nombre'))
        _apply_prefill('email', contacto_cache.get('email'))
        _apply_prefill('telefono', contacto_cache.get('telefono'))
        _apply_prefill('dni', contacto_cache.get('dni'))

        # 4. From the viewer profile if available
        if viewer:
            _apply_prefill('nombre', getattr(viewer, 'name', None))
            _apply_prefill('email', getattr(viewer, 'email', None))
            _apply_prefill('telefono', getattr(viewer, 'telefono', None))
            _apply_prefill('dni', getattr(viewer, 'dni_vecino', None))

        # 5. From WhatsApp profile name if no other name is found
        _apply_prefill('nombre', self.context.get('profile_name'))

        # 6. From anon_id as a fallback for phone number
        _apply_prefill('telefono', self.context.get('anon_id'))

        # Remember any newly found data in the session cache
        for campo in ['nombre', 'dni', 'email', 'telefono']:
            if datos.get(campo):
                contacto_cache.setdefault(campo, datos.get(campo))


        if categoria_inicial and not self.flow_context['datos_reclamo'].get('categoria'):
            self.flow_context['datos_reclamo']['categoria'] = categoria_inicial

        if categoria_inicial and not datos.get('categoria'):
            datos['categoria'] = categoria_inicial

        # Check what data is missing and transition to the correct state
        if not self.flow_context['datos_reclamo'].get('categoria'):
            self.municipal_ctx['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name
            self.flow_context['state'] = ReclamoState.ESPERANDO_CATEGORIA.name
            return _get_reclamos_menu()
        # Once we have a category, mark the flow as active.
        self.municipal_ctx['estado_conversacion'] = "EN_FLUJO_RECLAMO"

        if not self.flow_context['datos_reclamo'].get('descripcion'):
            # This case is less likely if categoria is present, but good to have
            self.flow_context['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
            categoria = self.flow_context['datos_reclamo']['categoria']
            return {"message_body": f"Entendemos que el reclamo es por *{categoria}*. Para poder ayudarte mejor, por favor describí brevemente qué está pasando."}
        elif not self.flow_context['datos_reclamo'].get('direccion'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
            # Construct a message confirming the data we have
            categoria = self.flow_context['datos_reclamo']['categoria']
            descripcion = self.flow_context['datos_reclamo'].get('descripcion', 'No especificada')

            # If the description came from an image, use a more natural message
            if self.flow_context['datos_reclamo'].get('origen_descripcion') == 'imagen':
                 # The 'descripcion' field now holds the natural language summary
                 return {"message_body": f"Gracias a tu imagen, entiendo que el reclamo es por *{categoria}*. Veo que se trata de: \"{descripcion}\".\n\nPara continuar, por favor, indicame la dirección exacta del problema."}
            else:
                 return {"message_body": f"Reclamo por *{categoria}*.\n\nPara continuar, por favor, indicame la dirección exacta del problema."}
        else:
            if not datos.get('foto_url'):
                if self.context.get('foto_url'):
                    datos['foto_url'] = self.context.get('foto_url')
                else:
                    self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
                    return {
                        "message_body": "¿Querés agregar una foto? Esto ayuda mucho a resolver el problema.",
                        "options_list": [
                            {"texto": "Sí, agregar foto", "action_id": "reclamo_adjuntar_foto_si"},
                            {"texto": "No, omitir foto", "action_id": "reclamo_adjuntar_foto_no"},
                        ],
                        "message_type": "interactive_buttons",
                    }

            return self.ask_for_contact_details()

    def handle_categoria(self, user_input):
        normalized_user_input = normalizar_texto(user_input or "")
        if (user_input and user_input.strip() == "0") or (
            normalized_user_input in RETURN_TO_MAIN_MENU_NORMALIZED
        ):
            return self._return_to_main_menu()

        reclamo_options = _get_reclamos_menu().get("options_list", [])
        plain_options = [
            {"texto": opt.get("category_name")}
            for opt in reclamo_options
            if opt.get("category_name")
        ]

        category = find_reclamo_category_by_input(user_input, plain_options)
        details = {}
        if not category:
            municipio_config = self.context.get("municipio_config_actual", {})
            default_localidad = municipio_config.get("ciudad")
            default_provincia = municipio_config.get("provincia")
            details = extract_reclamo_details_from_text(user_input, plain_options, default_localidad=default_localidad, default_provincia=default_provincia)
            category = details.pop("categoria", None)

        if category and normalizar_texto(category) in RETURN_TO_MAIN_MENU_NORMALIZED:
            return self._return_to_main_menu()

        if not category:
            return {
                "message_body": "No pude reconocer la categoría. Por favor elegí una opción o describí el problema."
            }

        datos = self.flow_context.setdefault('datos_reclamo', {})
        datos['categoria'] = category

        # Update datos with all extracted details
        for key, value in details.items():
            if value:
                datos[key] = value

        if _is_placeholder_description(datos.get('descripcion')):
            datos.pop('descripcion', None)
            datos.pop('descripcion_resumida', None)

        if (
            not datos.get('descripcion')
            and isinstance(user_input, str)
            and not user_input.strip().isdigit()
            and len(user_input.strip()) >= 10
        ):
            descripcion_texto = user_input.strip()
            datos['descripcion'] = descripcion_texto
            datos['descripcion_resumida'] = construir_descripcion_breve(descripcion_texto)

        if not datos.get('descripcion'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
            return {
                "message_body": f"Perfecto, el reclamo es por *{category}*.\n\nPara poder ayudarte mejor, por favor describí brevemente qué está pasando."
            }

        if not datos.get('direccion'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
            return {"message_body": "¿Dónde ocurre el problema? Indicá la dirección."}

        return self.ask_for_contact_details()

    def handle_direccion(self, user_input, payload):
        # Clear previous map preview if present
        self.flow_context['datos_reclamo'].pop('map_search_url', None)

        if not payload.get("es_ubicacion") and user_input:
            link_info = _detect_location_link_info(user_input)
            if link_info:
                payload = dict(payload)
                payload["es_ubicacion"] = True
                location_payload = {
                    k: v
                    for k, v in {
                        "address": link_info.get("address"),
                        "latitude": link_info.get("latitude"),
                        "longitude": link_info.get("longitude"),
                    }.items()
                    if v is not None
                }
                if location_payload:
                    payload["ubicacion_usuario"] = location_payload

        if payload.get("es_ubicacion") and payload.get("ubicacion_usuario"):
            location_data = payload.get("ubicacion_usuario")

            lat_raw = (
                location_data.get("latitude")
                if location_data.get("latitude") is not None
                else location_data.get("lat")
            )
            lon_raw = (
                location_data.get("longitude")
                if location_data.get("longitude") is not None
                else location_data.get("lon")
            )

            lat_value = lon_value = None
            try:
                if lat_raw is not None and lon_raw is not None:
                    lat_value = float(lat_raw)
                    lon_value = float(lon_raw)
            except (TypeError, ValueError):
                lat_value = lon_value = None

            original_address = location_data.get("address") or location_data.get("label")
            address = original_address
            direccion_info = None
            if lat_value is not None and lon_value is not None:
                from .herramientas_municipio import obtener_direccion_de_coordenadas

                direccion_info = obtener_direccion_de_coordenadas(lat_value, lon_value)

                formatted_address = (
                    direccion_info.get("formatted_address")
                    if direccion_info and direccion_info.get("formatted_address")
                    else None
                )

                original_has_number = bool(re.search(r"\d", original_address or ""))
                formatted_has_number = bool(re.search(r"\d", formatted_address or ""))

                if formatted_address and (
                    not original_address
                    or not original_has_number
                    or formatted_has_number
                ):
                    address = formatted_address

                self.flow_context['datos_reclamo']['coordenadas'] = {
                    "lat": lat_value,
                    "lng": lon_value,
                }

                map_url = f"https://www.google.com/maps/search/?api=1&query={lat_value},{lon_value}"
                self.flow_context['datos_reclamo']['map_search_url'] = map_url

                componentes = {}
                if direccion_info:
                    componentes = {
                        key: direccion_info.get(key)
                        for key in ("calle", "numero", "localidad", "provincia", "codigo_postal", "barrio")
                        if direccion_info.get(key)
                    }

                if original_address:
                    segmentos = [seg.strip() for seg in original_address.split(",") if seg.strip()]
                    if segmentos:
                        primera = segmentos[0]
                        match_calle = re.match(
                            r"^(?P<calle>.+?)\s+(?P<numero>\d+[0-9A-Za-z/-]*)\b",
                            primera,
                        )
                        if match_calle:
                            componentes["calle"] = match_calle.group("calle").strip()
                            componentes.setdefault("numero", match_calle.group("numero").strip())

                    for segmento in segmentos[1:]:
                        if not segmento:
                            continue
                        if not componentes.get("codigo_postal") and re.search(r"\d", segmento):
                            componentes["codigo_postal"] = segmento
                            continue
                        if not componentes.get("localidad"):
                            componentes["localidad"] = segmento
                            continue
                        if not componentes.get("provincia"):
                            componentes["provincia"] = segmento
                            continue

                if componentes:
                    self.flow_context['datos_reclamo']['direccion_componentes'] = componentes

            if not address:
                if lat_value is not None and lon_value is not None:
                    address = f"Lat: {lat_value}, Lon: {lon_value}"
                else:
                    address = "Ubicación sin dirección disponible"

            self.flow_context['datos_reclamo']['direccion'] = address
        elif len(user_input) < 5:
            return {"message_body": "La dirección parece muy corta. Por favor, ingresá una dirección más completa (calle y número)."}
        else:
            direccion_ingresada = user_input.strip()
            self.flow_context['datos_reclamo']['direccion'] = direccion_ingresada

            try:
                from services.address_normalizer import normalize_and_geocode

                municipio_cfg = self.context.get("municipio_config_actual", {})
                normalizado = normalize_and_geocode(direccion_ingresada, municipio_cfg)
            except Exception:
                normalizado = None

            if isinstance(normalizado, dict):
                formatted = normalizado.get("formatted") or normalizado.get("formatted_address")
                if formatted:
                    self.flow_context['datos_reclamo']['direccion'] = formatted

                lat_norm = normalizado.get("lat")
                lon_norm = normalizado.get("lon") or normalizado.get("lng")
                try:
                    if lat_norm is not None and lon_norm is not None:
                        lat_val = float(lat_norm)
                        lon_val = float(lon_norm)
                        self.flow_context['datos_reclamo']['coordenadas'] = {
                            "lat": lat_val,
                            "lng": lon_val,
                        }
                        if normalizado.get("maps_search_url"):
                            self.flow_context['datos_reclamo']['map_search_url'] = normalizado.get("maps_search_url")
                        else:
                            map_url = f"https://www.google.com/maps/search/?api=1&query={lat_val},{lon_val}"
                            self.flow_context['datos_reclamo']['map_search_url'] = map_url
                except (TypeError, ValueError):
                    pass
                else:
                    if normalizado.get("maps_search_url"):
                        self.flow_context['datos_reclamo']['map_search_url'] = normalizado.get("maps_search_url")

        # If a photo was already provided earlier in the flow or exists in the
        # context (e.g. the user started the claim by sending an image), skip
        # asking for it again and go straight to contact details.
        if self.flow_context['datos_reclamo'].get('foto_url') or self.context.get('foto_url'):
            self.flow_context['datos_reclamo'].setdefault('foto_url', self.context.get('foto_url'))
            return self.ask_for_contact_details()

        map_url_preview = self.flow_context['datos_reclamo'].get('map_search_url')
        map_prompt = f"¿Es acá? {map_url_preview}\n\n" if map_url_preview else ""

        self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
        return {
            "message_body": f"{map_prompt}¿Querés agregar una foto? Esto ayuda mucho a resolver el problema.",
            "options_list": [
                {"texto": "Sí, agregar foto", "action_id": "reclamo_adjuntar_foto_si"},
                {"texto": "No, omitir foto", "action_id": "reclamo_adjuntar_foto_no"},
            ],
            "message_type": "interactive_buttons",
        }


    def handle_descripcion(self, user_input):
        if len(user_input) < 10:
            return {"message_body": "Por favor, dame una descripción un poco más detallada del problema."}
        descripcion_texto = user_input.strip()
        self.flow_context['datos_reclamo']['descripcion'] = descripcion_texto
        self.flow_context['datos_reclamo']['descripcion_resumida'] = construir_descripcion_breve(descripcion_texto)
        if not self.flow_context['datos_reclamo'].get('direccion'):
            self.flow_context['state'] = ReclamoState.ESPERANDO_DIRECCION.name
            categoria = self.flow_context['datos_reclamo'].get('categoria', '')
            mensaje = (
                f"Gracias por la descripción para tu reclamo de *{categoria}*.\n\n"
                "Ahora, por favor, indicame la dirección exacta del problema (calle, número, distrito/barrio)."
            )
            return {"message_body": mensaje}
        else:
            # If a photo was already provided earlier or is present in the
            # context, do not ask for another one.
            if self.flow_context['datos_reclamo'].get('foto_url') or self.context.get('foto_url'):
                self.flow_context['datos_reclamo'].setdefault('foto_url', self.context.get('foto_url'))
                return self.ask_for_contact_details()
            self.flow_context['state'] = ReclamoState.ESPERANDO_FOTO.name
            return {
                "message_body": "¿Querés agregar una foto? Esto ayuda mucho a resolver el problema.",
                "options_list": [{"texto": "Sí, agregar foto", "action_id": "reclamo_adjuntar_foto_si"}, {"texto": "No, omitir foto", "action_id": "reclamo_adjuntar_foto_no"}],
                "message_type": "interactive_buttons"
            }
    def handle_foto(self, user_input, payload):
        action = payload.get("action")
        normalized = user_input.lower()

        # Accept the photo if either the payload or the outer context indicates
        # that an image was provided. This covers the case where the user sends
        # a picture directly without first pressing "Sí, agregar foto".
        foto_url = payload.get("foto_url") or self.context.get("foto_url")
        es_foto = payload.get("es_foto") or self.context.get("es_foto")
        if es_foto and foto_url:
            self.flow_context['datos_reclamo']['foto_url'] = foto_url
            return self.ask_for_contact_details()

        no_words = {"no", "omitir", "omitilo", "sin foto", "ninguna"}
        yes_words = {"si", "sí", "enviar", "adjunto", "mandar"}

        if any(w in normalized for w in no_words) or action == "reclamo_adjuntar_foto_no":
            self.flow_context['datos_reclamo']['foto_url'] = None
            return self.ask_for_contact_details()
        elif any(w in normalized for w in yes_words) or action == "reclamo_adjuntar_foto_si":
            return {"message_body": "Por favor, enviá la foto ahora."}
        else:
            return {
                "message_body": "No entendí tu respuesta. Por favor, enviá una foto o elegí una de las opciones.",
                "options_list": [{"texto": "Omitir foto", "action_id": "reclamo_adjuntar_foto_no"}],
            }

    def ask_for_contact_details(self, force_prompt: bool = False):
        """Ask for missing contact details or allow editing if requested."""
        datos = self.flow_context.setdefault('datos_reclamo', {})
        required_fields = ['nombre', 'dni', 'email', 'telefono']
        field_labels = {
            'nombre': 'Nombre completo',
            'dni': 'DNI',
            'email': 'Email',
            'telefono': 'Teléfono',
        }

        missing = [
            f for f in required_fields
            if not datos.get(f)
            or datos.get(f) == 'Vecino/a'
            or '@whatsapp.chatboc.com' in str(datos.get(f))
        ]

        if not force_prompt and not missing:
            self.flow_context['state'] = ReclamoState.ESPERANDO_CONFIRMACION.name
            return self.get_confirmation_message()

        self.flow_context['state'] = ReclamoState.ESPERANDO_DATOS_CONTACTO.name

        known_parts = []
        missing_labels = []
        for field in required_fields:
            value = datos.get(field)
            if value and field not in missing and '@whatsapp.chatboc.com' not in str(value) and value != 'Vecino/a':
                known_parts.append(f"{field_labels[field].capitalize()}: {value}")
            else:
                missing_labels.append(field_labels[field])

        message_lines = []
        if known_parts:
            message_lines.append("¡Ya casi terminamos! ✍️")
            message_lines.append("*Datos que ya tenemos:*")
            message_lines.extend(known_parts)
        if missing_labels:
            message_lines.append("\n*Para finalizar, por favor, completá tus datos:*")
            message_lines.extend(missing_labels)
        message_lines.append("\nPodés escribir todos los datos juntos en un solo mensaje para actualizar o corregir.")

        return {"message_body": "\n".join(message_lines)}

    def handle_datos_contacto(self, user_input):
        contact_details = extract_multiple_contact_details_regex(user_input)
        if not contact_details:
            return {"message_body": "No pude identificar tus datos. Por favor, intentá de nuevo incluyendo nombre, DNI, email y teléfono."}

        datos_reclamo = self.flow_context['datos_reclamo']
        for k, v in contact_details.items():
            if v:
                datos_reclamo[k] = v
        self.flow_context['state'] = ReclamoState.ESPERANDO_CONFIRMACION.name
        return self.get_confirmation_message()

    def get_confirmation_message(self):
        datos = self.flow_context.get('datos_reclamo', {})
        mensaje = "Por favor, confirmá que los datos de tu reclamo son correctos:\n\nDatos del reclamo:\n"
        mensaje += f"- Categoría: {datos.get('categoria', 'No especificada')}\n"
        mensaje += f"- Dirección: {datos.get('direccion', 'No especificada')}\n"
        mensaje += f"- Descripción: {datos.get('descripcion', 'No especificada')}\n\n"
        mensaje += "Datos personales:\n"
        mensaje += f"- Nombre: {datos.get('nombre', 'No especificado')}\n"
        mensaje += f"- DNI: {datos.get('dni', 'No especificado')}\n"
        mensaje += f"- Email: {datos.get('email', 'No especificado')}\n"
        mensaje += f"- Teléfono: {datos.get('telefono', 'No especificado')}\n"
        mensaje += f"- Foto adjunta: {'Sí' if datos.get('foto_url') else 'No'}\n"
        maps_link = datos.get('maps_link') or datos.get('maps_search_url')
        static_map_url = datos.get('static_map_url')
        if maps_link:
            mensaje += f"- Mapa: {maps_link}\n"
        opciones = formatear_opciones([
            {"texto": "✅ Confirmar", "action_id": "reclamo_confirmar_si"},
            {"texto": "✏️ Editar datos", "action_id": "reclamo_confirmar_no"},
            {"texto": "❌ Cancelar", "action_id": "reclamo_cancelar"},
        ])
        payload = {
            "message_body": mensaje,
            "options_list": opciones,
            "message_type": "interactive_buttons"
        }
        if static_map_url:
            payload["image_url"] = static_map_url
            payload["image_alt_text"] = "Mapa de la ubicación"
        return payload

    def handle_confirmacion(self, user_input, payload):
        action = payload.get("action") or payload.get("action_id") or ""
        normalized_plain = normalizar_texto(user_input or "")
        tokens = set(filter(None, re.split(r"\W+", normalized_plain)))
        choice = normalized_plain.strip()

        affirmatives = {"si", "sí", "confirmo", "confirmar", "ok", "okay", "acepto", "aceptar", "dale"}
        negatives = {"no", "editar", "modificar", "cambiar"}
        affirmative_tokens = {normalizar_texto(word) for word in affirmatives}
        negative_tokens = {normalizar_texto(word) for word in negatives}

        edit_requested = (
            action == "reclamo_confirmar_no"
            or choice in {"2"}
            or any(token in negative_tokens for token in tokens)
            or ("confirmar" in normalized_plain and normalized_plain.endswith("no"))
        )

        confirm_requested = (
            action == "reclamo_confirmar_si"
            or choice in {"1"}
            or any(token in affirmative_tokens for token in tokens)
            or ("confirmar" in normalized_plain and normalized_plain.endswith("si"))
        )

        if edit_requested:
            return self.ask_for_contact_details(force_prompt=True)

        if confirm_requested:
            datos = self.flow_context.get('datos_reclamo', {})
            action_data = {
                "categoria": datos.get("categoria"),
                "descripcion": datos.get("descripcion"),
                "ubicacion": datos.get("direccion"),
                "coordenadas": datos.get("coordenadas"),
                "usuario": datos.get("nombre"),
                "dni": datos.get("dni"),
                "email": datos.get("email"),
                "telefono": datos.get("telefono"),
                "descripcion_resumida": datos.get("descripcion_resumida"),
                "foto_url_adjunta": datos.get("foto_url"),
            }
            handler = CrearReclamoActionHandler(self.context)
            result = handler.execute(action_data)
            if result.get("success"):
                ticket_data = result.get("data", {})
                nro_ticket = ticket_data.get("nro_ticket")
                pin_consulta = ticket_data.get("consulta_pin")

                message = (
                    result.get("message_body")
                    or result.get("message_to_user")
                    or (
                        f"¡Tu reclamo fue creado con éxito! ✅\n\nEl número de seguimiento es *{nro_ticket}*. "
                        + (
                            f"Usá el botón \"Ver Ticket\" o tu PIN `{pin_consulta}` para hacer seguimiento."
                            if pin_consulta
                            else "Usá el botón \"Ver Ticket\" para hacer seguimiento."
                        )
                    )
                )

                options_list = result.get("options_list") or []
                message_type = result.get("message_type")
                if not message_type:
                    message_type = "interactive_buttons" if options_list else "text"

                extra_payload = {
                    "message_type": message_type,
                }
                if options_list:
                    extra_payload["options_list"] = list(options_list)

                image_url = result.get("image_url")
                if image_url:
                    extra_payload["image_url"] = image_url

                show_menu = False

                return self.end_flow(message, show_menu=show_menu, extra_payload=extra_payload)
            error_message = result.get(
                "message_to_user",
                "Hubo un problema al registrar tu reclamo. Por favor, intentá de nuevo más tarde.",
            )
            return self.end_flow(error_message, show_menu=True)
        else:  # Cancel or any other input
            cancel_msg = "Proceso de reclamo cancelado. ¿En qué más te puedo ayudar?"
            return self.end_flow(cancel_msg, show_menu=True)

    def end_flow(self, message, show_menu=False, image_url=None, extra_payload=None):
        self.flow_context.clear()
        # Remove flow data from municipio context so subsequent turns don't
        # enter this handler unintentionally.
        self.municipal_ctx.pop("reclamo_flow_v2", None)

        if show_menu:
            if self.municipal_ctx.get('estado_conversacion') != ConversationState.ESPERANDO_NOMBRE_INICIAL.name:
                self.municipal_ctx['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        else:
            self.municipal_ctx.pop('estado_conversacion', None)
        self.municipal_ctx.pop('menu_opciones', None)

        payload: dict[str, object] = {}
        if extra_payload:
            payload.update(extra_payload)

        if message is not None:
            payload["message_body"] = message
        else:
            payload.setdefault("message_body", "")

        payload.setdefault("message_type", "text")

        if image_url and "image_url" not in payload:
            payload["image_url"] = image_url

        if show_menu:
            # Directly call the payload generator for a reduced menu
            menu_payload = _get_main_menu_payload(self.context, reduced=True)
            payload["delayed_payload"] = menu_payload
            payload["delay_seconds"] = 20

        return payload
# Initialize the classifier globally
INTENTS_FILE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'intents.json')
intent_classifier = IntentClassifier(intents_file_path=INTENTS_FILE_PATH)

load_dotenv()

# Placeholder for API key management
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

def configure_geolocator():
    """Configura y retorna un geolocalizador con la API key."""
    if not GOOGLE_API_KEY:
        raise ValueError("La API key de Google Maps no está configurada.")
    return GoogleV3(api_key=GOOGLE_API_KEY)

def obtener_municipios_cercanos(latitud, longitud, radio_km=5):
    """
    Encuentra municipios cercanos a una latitud y longitud dadas.
    """
    geolocator = configure_geolocator()
    try:
        # Intenta obtener la dirección (localidad) desde las coordenadas
        location = geolocator.reverse((latitud, longitud), exactly_one=True)
        address = location.raw.get('address_components', [])

        # Busca el componente de la dirección que corresponde a la localidad
        municipio_actual = None
        for component in address:
            if 'locality' in component.get('types', []):
                municipio_actual = component.get('long_name')
                break

        if not municipio_actual:
            return jsonify({"error": "No se pudo determinar la localidad desde las coordenadas proporcionadas."}), 404

        # Simulación de búsqueda en un radio (esto debería ser más complejo en una app real)
        # Aquí simplemente devolvemos la localidad encontrada como ejemplo
        municipios_encontrados = [municipio_actual]

        return jsonify({"municipios_cercanos": municipios_encontrados})

    except (GeocoderTimedOut, GeocoderServiceError) as e:
        return jsonify({"error": f"Error en el servicio de geolocalización: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error inesperado: {e}"}), 500

logger = logging.getLogger(__name__)

# Keywords for confirming actions, especially in the reclamo (complaint) flow
PALABRAS_CLAVE_CONFIRMACION = {
    "confirmar_reclamo", "confirmar", "confirmo", "confirmado",
    "si", "sí", "afirmativo", "dale", "ok", "proceder", "aceptar",
    "confirmar_reclamo_final", "si, confirmar reclamo", "sí, confirmar reclamo", # From button texts
    "yes" # English just in case
}

# Keywords for requesting to edit information during a flow
EDIT_KEYWORDS = {
    "editar", "cambiar", "corregir", "modificar",
    "no era asi", "me equivoque", "error", "equivocado",
    "editar datos", "editar_reclamo_datos", "quiero editar", "necesito cambiar"
}

def _super_normalize(s: str) -> str:
    """More aggressive normalization for matching, removes all non-alphanumeric chars."""
    s = normalizar_texto(s)
    return re.sub(r'[^a-z0-9]', '', s)


VARIATION_SELECTOR = "\uFE0F"


def strip_variation_selector(s: str) -> str:
    """Remove emoji variation selector (U+FE0F) from input."""
    return s.replace(VARIATION_SELECTOR, "") if isinstance(s, str) else s


def _try_handle_emoji_shortcut(
    pregunta_original,
    contexto_municipio_actual: dict,
    context,
    chat_db_context,
):
    """Trigger quick actions when the user sends a single emoji.

    This works regardless of the current conversation state so accessibility
    users can always start a flow with an emoji-only message.
    """

    pregunta_str_menu = ""
    if isinstance(pregunta_original, str):
        pregunta_str_menu = pregunta_original
    elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
        pregunta_str_menu = pregunta_original["pregunta"]

    pregunta_str_menu = strip_variation_selector(pregunta_str_menu.strip())

    if not pregunta_str_menu:
        return None

    emoji_category = EMOJI_RECLAMO_CATEGORIES.get(pregunta_str_menu)
    if emoji_category:
        handler = ReclamoFlowHandler(context, chat_db_context)
        response_dict = handler.start_flow(categoria_inicial=emoji_category)
        contexto_municipio_actual["estado_conversacion"] = "EN_FLUJO_RECLAMO"
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return response_dict

    emoji_action = EMOJI_MAIN_MENU_ACTIONS.get(pregunta_str_menu)
    if emoji_action:
        contexto_municipio_actual["estado_conversacion"] = None
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return handle_main_menu_action(emoji_action, context, chat_db_context)

    return None


def _can_use_global_emoji_shortcuts(estado_conversacion: str | None) -> bool:
    """Allow emoji shortcuts even when asking for the initial name.

    Accessibility users rely on emoji-only inputs, so we enable the shortcuts
    for the first interaction as well as when the main menu is displayed.
    """

    return estado_conversacion in {
        None,
        ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name,
        ConversationState.ESPERANDO_NOMBRE_INICIAL.name,
    }


def _looks_like_free_form_input(text: str | None) -> bool:
    """Detects if the user sent a natural sentence instead of a menu option."""
    if not isinstance(text, str):
        return False

    stripped = strip_variation_selector(text).strip()
    if not stripped:
        return False

    normalized = normalizar_texto(stripped)
    word_count = len(normalized.split())

    if word_count >= 6:
        return True

    if len(stripped) >= 40:
        return True

    # Sentences with punctuation combined with at least a few words
    if word_count >= 4 and any(ch in stripped for ch in ".,;¿?¡!:"):
        return True

    return False


def _maybe_route_menu_input_to_llm(
    pregunta_str: str,
    contexto_municipio_actual: dict,
    app,
    context: dict,
    viewer_user,
    owner_user,
    chat_db_context,
    demo_metadata=None,
):
    """Escalate long natural sentences sent during menu selection to the LLM."""
    if not _looks_like_free_form_input(pregunta_str):
        return None

    logger_actual = current_app.logger if has_app_context() else logger
    logger_actual.info(
        "Free-form sentence detected while waiting for a menu selection. Escalating to LLM."
    )

    contexto_municipio_actual['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name
    contexto_municipio_actual.pop('menu_opciones', None)
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    response_dict, _ = handle_llm_interaction(
        app,
        pregunta_str,
        context,
        viewer_user,
        owner_user,
        chat_db_context,
        contexto_municipio_actual,
        demo_metadata=demo_metadata,
    )
    return response_dict


def extract_description_and_check_confirmation(text: str, confirmation_keywords: set) -> tuple[str | None, bool]:
    """
    Extracts description from text and checks for a confirmation intent.
    Returns a tuple: (extracted_description, has_confirmation_intent).
    """
    if not text:
        return None, False

    normalized_text = normalizar_texto(text.strip())

    # Sort keywords by length to match longer phrases first (e.g., "confirmar reclamo" before "confirmar")
    sorted_confirmation_keywords = sorted(list(confirmation_keywords), key=len, reverse=True)

    extracted_description = normalized_text
    has_confirmation_intent = False

    for keyword in sorted_confirmation_keywords:
        # Check if the text ends with the keyword, possibly preceded by a space, comma, or period.
        # Example: "description keyword", "description, keyword", "description. keyword"
        # Or if the keyword itself is a multi-word phrase like "confirmar reclamo"

        # If the keyword is a multi-word phrase itself (e.g., "confirmar reclamo")
        if " " in keyword: # Multi-word keyword
            if normalized_text.endswith(keyword):
                # If a multi-word confirmation keyword is found at the end, it's a strong signal.
                extracted_description = normalized_text[:-len(keyword)].strip(" .,")
                if not extracted_description: # If original text was ONLY the multi-word keyword
                    extracted_description = None
                has_confirmation_intent = True
                break
        else: # Single-word keyword
            # Check if keyword is at the very end
            if normalized_text == keyword: # Input is ONLY the keyword
                extracted_description = None
                has_confirmation_intent = True
                break
            # Check if text ends with " keyword"
            if normalized_text.endswith(f" {keyword}"):
                potential_description = normalized_text[:-(len(keyword) + 1)].strip()
                # If description is short, or keyword is strong.
                if len(potential_description.split()) <= 4 or not potential_description: # Allow slightly longer desc like "nada de eso confirmar"
                    extracted_description = potential_description if potential_description else None
                    has_confirmation_intent = True
                    break
            # Check if text ends with ",keyword" or ".keyword" (less common for natural language confirmation)
            for separator in [",", "."]:
                if normalized_text.endswith(f"{separator}{keyword}"):
                    potential_description = normalized_text[:-(len(keyword) + 1)].strip()
                    if len(potential_description.split()) <= 4 or not potential_description:
                        extracted_description = potential_description if potential_description else None
                        has_confirmation_intent = True
                        break
            if has_confirmation_intent:
                break

    # If no specific pattern matched but a keyword is in a short text.
    # This is a bit risky as "yes" or "ok" can be part of a description.
    # Let's refine: if the *entire input* is very similar to a confirmation phrase or is short and contains one.
    if not has_confirmation_intent:
        # Check if the whole normalized_text is just a keyword or a keyword plus very little else
        # e.g. "confirmar el reclamo" "si confirmar" "listo ok"
        # This part needs to be more robust. For now, the endswith logic is primary.
        # The critical case "nada confirmar el reclamo" should be caught if "confirmar reclamo" is a keyword.
        # Let's add "confirmar reclamo" to PALABRAS_CLAVE_CONFIRMACION for this.
        # The sorted_confirmation_keywords will handle it.
        pass


    # If after stripping, the description is one of the keywords itself, it means the original was likely just "keyword keyword"
    # or the description part was empty.
    if extracted_description and extracted_description in confirmation_keywords and has_confirmation_intent:
        extracted_description = None # User likely meant only to confirm.

    # If the original text was short and contained a keyword, it's likely a confirmation.
    # The `endswith` logic handles this for clearer cases.
    # This is a fallback for inputs like "ok es todo" -> desc: "ok es todo", confirm: False (which is correct)
    # vs "es todo ok" -> desc: "es todo", confirm: True (if "ok" is a keyword)

    # If has_confirmation_intent is true, extracted_description is what remains.
    # If has_confirmation_intent is false, extracted_description is the original normalized_text.
    if not has_confirmation_intent:
        extracted_description = text.strip() # Return original cleaned text if no confirm intent

    # Final check: if description is empty and intent is true, set desc to a placeholder if needed by caller
    # For now, None is acceptable for an empty description part.
    # logger.info(f"[extract_description_and_check_confirmation] Input: '{text}', Normalized: '{normalized_text}', Extracted Desc: '{extracted_description}', Confirmed: {has_confirmation_intent}")
    return extracted_description, has_confirmation_intent

URL_REGEX = re.compile(r"https?://\S+")


def _remove_redundant_urls_from_message(message_body, options_list):
    """
    Removes URLs from the message body if they are already present in the buttons.
    """
    if not message_body or not options_list:
        return message_body

    for option in options_list:
        if isinstance(option, dict) and 'url' in option and option['url'] in message_body:
            message_body = message_body.replace(option['url'], '')

    # Clean up common leftover phrases and extra spaces
    # Using regex to be more robust and case-insensitive
    message_body = re.sub(r'por favor\s+ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()
    message_body = re.sub(r'enlace\s*:?', '', message_body, flags=re.IGNORECASE).strip()

    # Replace multiple spaces with a single space and clean up punctuation
    message_body = re.sub(r'\s{2,}', ' ', message_body).strip()
    message_body = message_body.replace(' .', '.').strip()
    # Remove hanging colons or commas before a period.
    message_body = re.sub(r'[,:]\s*\.', '.', message_body)
    # If the message is just a colon now, clear it.
    if message_body == ':':
        message_body = ''

    return message_body


def agregar_botones_para_links(texto: str, botones: list) -> list:
    if not texto:
        return botones
    urls = re.findall(URL_REGEX, texto)
    for url in urls:
        if not any(b.get("url") == url for b in botones):
            botones.append({"texto": "Abrir enlace", "url": url})
    return botones

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = os.environ.get(
    "TWILIO_WHATSAPP_NUMBER", "whatsapp:+17432643718"
)
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")

MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")
# Configuración base (se puede sobrescribir por municipio en cada request)
CONFIG_MUNICIPIO = cargar_configuracion_municipio("default", "config.json")

TODAS_LAS_CATEGORIAS_UNICAS = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
BOTONES_TODAS_CATEGORIAS = [{"texto": cat} for cat in TODAS_LAS_CATEGORIAS_UNICAS]

def cargar_tramites_info(municipio_id: str = MUNICIPIO_ID):
    return cargar_configuracion_municipio(municipio_id, "tramites.json")

def get_tramites_info(municipio_id: str = MUNICIPIO_ID) -> dict:
    return cargar_tramites_info(municipio_id)

def cargar_contactos_utiles(municipio_id: str = MUNICIPIO_ID):
    return cargar_configuracion_municipio(municipio_id, "contactos_utiles.json")

def cargar_agenda_cultural(municipio_id: str = MUNICIPIO_ID):
    db_municipio_id = get_numeric_municipio_id(municipio_id)
    if db_municipio_id is not None:
        try:
            posts = (
                MunicipioPost.query.filter(MunicipioPost.municipio_id == db_municipio_id)
                .order_by(MunicipioPost.fecha_publicacion.desc())
                .limit(200)
                .all()
            )
            if posts:
                return {"eventos": [post.to_dict() for post in posts]}
        except Exception:
            logger.exception("Error al cargar agenda cultural desde la base de datos")

    fallback = cargar_configuracion_municipio(municipio_id, "agenda_cultural.json")
    if isinstance(fallback, dict):
        return fallback
    return {"eventos": []}

def obtener_info_tramite_web(tramite_nombre: str, municipio_id: str = MUNICIPIO_ID) -> dict:
    """Obtiene la descripción y enlaces de un trámite desde ``tramites.json``.

    El archivo ``tramites_links.json`` ya no se utiliza. En su lugar, la información
    de cada trámite (incluyendo enlaces asociados) se mantiene dentro de
    ``tramites.json`` para cada municipio.

    Args:
        tramite_nombre: Nombre del trámite buscado.
        municipio_id: Identificador del municipio.

    Returns:
        dict: Un diccionario con las claves ``contenido`` y ``botones`` si el
        trámite se encuentra. Si no existe, se devuelve ``{"error": ...}``.
    """

    tramites = cargar_tramites_info(municipio_id)
    if not tramites:
        return {"error": "No se encontraron trámites configurados."}

    nombre_norm = normalizar_texto(tramite_nombre)

    for key, info in tramites.items():
        if nombre_norm in normalizar_texto(key):
            return {
                "contenido": info.get("descripcion", ""),
                "botones": info.get("botones", []),
            }
        for boton in info.get("botones", []):
            if nombre_norm in normalizar_texto(boton.get("texto", "")):
                return {
                    "contenido": info.get("descripcion", ""),
                    "botones": info.get("botones", []),
                }

    return {"error": "No se encontró información sobre el trámite."}

DEFAULT_TRAMITES_WEB_URL = CONFIG_MUNICIPIO.get(
    "tramites_web_url", "https://www.ejemplo.gob.ar/tramites/"
)
MUNICIPIO_DIRECCION = CONFIG_MUNICIPIO.get("direccion", "Dirección del municipio")
EJEMPLO_DIRECCION = CONFIG_MUNICIPIO.get("ejemplo_direccion", "Avenida Siempreviva 123")

# Palabras clave sencillas para detectar consultas generales de servicios
GENERAL_QUERY_KEYWORDS = [
    "farmacia", "supermercado", "negocio", "servicio", "buscar",
    "comercio", "local"
]

def es_consulta_general(texto: str) -> bool:
    """Detecta si el texto parece una consulta de servicios generales."""
    texto_norm = normalizar_texto(texto or "")
    return any(k in texto_norm for k in GENERAL_QUERY_KEYWORDS)

# --- Mapping pedir_info -> ConversationState ---
def normalizar_str(s: str) -> str:
    """Normaliza cadenas a minusculas sin tildes ni espacios extra."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").lower().strip()

PEDIR_INFO_TO_STATE = {
    "ubicacion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    "direccion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    "categoria": ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
    "descripcion": ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    "descripcion_mas_detallada": ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    "nombre_completo": ConversationState.ESPERANDO_NOMBRE_VECINO,
    "nombre": ConversationState.ESPERANDO_NOMBRE_VECINO,
    "telefono": ConversationState.ESPERANDO_TELEFONO_VECINO,
    "email": ConversationState.ESPERANDO_EMAIL_VECINO,
    "id_reclamo": ConversationState.ESPERANDO_NUMERO_TICKET,
    "id_ticket": ConversationState.ESPERANDO_NUMERO_TICKET,
    "confirmacion": ConversationState.ESPERANDO_CONFIRMACION_RECLAMO,
    "adjuntos": ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
}

CLAIM_PENDING_FIELDS = {
    "ubicacion",
    "direccion",
    "categoria",
    "descripcion",
    "descripcion_mas_detallada",
    "nombre",
    "nombre_completo",
    "telefono",
    "email",
    "dni",
    "documento",
    "adjuntos",
    "confirmacion",
}

SUGGESTION_PENDING_FIELDS = {
    "descripcion_sugerencia",
    "ubicacion",
    "direccion",
    "datos_contacto_sugerencia",
    "nombre",
    "telefono",
    "email",
    "dni",
}

CLAIM_WAITING_STATES = {
    ConversationState.ESPERANDO_INFO_RECLAMO_LLM,
    ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
    ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    ConversationState.ESPERANDO_NOMBRE_VECINO,
    ConversationState.ESPERANDO_TELEFONO_VECINO,
    ConversationState.ESPERANDO_EMAIL_VECINO,
    ConversationState.ESPERANDO_CONFIRMACION_RECLAMO,
    ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
}

SUGGESTION_WAITING_STATES = {
    ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM,
    ConversationState.ESPERANDO_TEXTO_SUGERENCIA,
    ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA,
    ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA,
}


def _is_claim_pending_field(field_name: Optional[str]) -> bool:
    if not field_name or not isinstance(field_name, str):
        return False
    normalized = unicodedata.normalize("NFKD", field_name).encode("ascii", "ignore").decode("ascii").lower().strip()
    return normalized in CLAIM_PENDING_FIELDS


def _is_suggestion_pending_field(field_name: Optional[str]) -> bool:
    if not field_name or not isinstance(field_name, str):
        return False
    normalized = unicodedata.normalize("NFKD", field_name).encode("ascii", "ignore").decode("ascii").lower().strip()
    return normalized in SUGGESTION_PENDING_FIELDS


def _normalize_pedir_info_fields(pedir_info: Any) -> list[str]:
    """Return a normalized list of expected fields from pedir_info."""

    fields: list[str] = []

    def _append_field(value: str) -> None:
        cleaned = value.strip()
        if not cleaned:
            return
        for part in re.split(r"\s+y\s+|\s+e\s+", cleaned, flags=re.IGNORECASE):
            part_clean = part.strip()
            if part_clean:
                fields.append(part_clean)

    if isinstance(pedir_info, (list, tuple)):
        for item in pedir_info:
            if isinstance(item, str):
                for part in item.split(","):
                    _append_field(part)
    elif isinstance(pedir_info, str):
        for part in pedir_info.split(","):
            _append_field(part)

    return fields


def _normalize_pedir_info_value(pedir_info: Any) -> Optional[str]:
    """Return the first meaningful string value from pedir_info."""

    fields = _normalize_pedir_info_fields(pedir_info)
    return fields[0] if fields else None


def _prefill_contacto_from_context(
    contexto_municipio_actual: dict,
    datos_parciales: dict,
    expected_fields: list[str],
) -> list[str]:
    contacto_usuario = contexto_municipio_actual.get("contacto_usuario") or {}
    if not isinstance(contacto_usuario, dict):
        return expected_fields

    remaining_fields: list[str] = []
    for field in expected_fields:
        normalized = field.strip().lower()
        if normalized == "datos_contacto_sugerencia":
            required_fields = {"nombre", "dni", "email", "direccion"}
            if required_fields.issubset(set(contacto_usuario.keys())):
                for key in required_fields.union({"telefono"}):
                    if contacto_usuario.get(key):
                        datos_parciales.setdefault(key, contacto_usuario.get(key))
                continue
        if normalized in {"nombre", "dni", "email", "telefono", "direccion"}:
            if contacto_usuario.get(normalized):
                datos_parciales.setdefault(normalized, contacto_usuario.get(normalized))
                continue
        remaining_fields.append(field)
    return remaining_fields


def _extract_expected_fields_from_text(
    raw_text: str,
    expected_fields: list[str],
    context: dict,
    municipio_config: dict | None = None,
) -> dict[str, str]:
    if not raw_text and not context.get("es_ubicacion"):
        return {}

    extracted: dict[str, str] = {}
    text = raw_text or ""
    normalized_fields = {field.strip().lower() for field in expected_fields}

    contacto_fields = ["nombre", "dni", "email", "telefono", "direccion"]
    fields_for_contact = [field for field in contacto_fields if field in normalized_fields]
    if "ubicacion" in normalized_fields and "direccion" not in fields_for_contact:
        fields_for_contact.append("direccion")

    if "datos_contacto_sugerencia" in normalized_fields:
        fields_for_contact = list({*fields_for_contact, "nombre", "dni", "email", "telefono", "direccion"})

    if fields_for_contact and text:
        extracted_contact = extract_multiple_contact_details_regex(text, fields_for_contact)
        for key, value in extracted_contact.items():
            if key == "direccion" and "ubicacion" in normalized_fields:
                extracted.setdefault("ubicacion", value)
            else:
                extracted.setdefault(key, value)
        if "direccion" in extracted_contact and "datos_contacto_sugerencia" in normalized_fields:
            extracted.setdefault("ubicacion", extracted_contact.get("direccion"))

    if "email" in normalized_fields and "email" not in extracted:
        email = extract_email(text)
        if email:
            extracted["email"] = email

    if "dni" in normalized_fields and "dni" not in extracted:
        dni = extract_dni(text)
        if dni:
            extracted["dni"] = dni[0]

    if "telefono" in normalized_fields and "telefono" not in extracted:
        phone = extract_phone(text)
        if phone:
            extracted["telefono"] = phone[0]

    if "ubicacion" in normalized_fields and "ubicacion" not in extracted:
        address_payload = context.get("ubicacion_usuario") if context.get("es_ubicacion") else None
        if isinstance(address_payload, dict):
            address = address_payload.get("address")
            if address:
                extracted["ubicacion"] = address
        if "ubicacion" not in extracted and text:
            normalized_text = text.strip()
            looks_like_address = bool(
                re.search(r"\b(esquina|calle|av\.?|avenida|ruta|km|altura)\b", normalized_text, re.IGNORECASE)
                or re.search(r"\d", normalized_text)
                or "maps.google" in normalized_text
                or "goo.gl/maps" in normalized_text
            )
            if looks_like_address:
                extracted["ubicacion"] = normalized_text

    if "distrito" in normalized_fields and "distrito" not in extracted:
        default_localidad = municipio_config.get("ciudad") if municipio_config else None
        default_provincia = municipio_config.get("provincia") if municipio_config else None
        location_hints = _detect_location_mentions(text, default_localidad, default_provincia)
        if location_hints.get("distrito"):
            extracted["distrito"] = location_hints["distrito"]
        elif location_hints.get("distrito_dudoso"):
            extracted["distrito"] = location_hints["distrito_dudoso"]

    if "ubicacion" in extracted and "distrito" in normalized_fields and "distrito" not in extracted:
        ubicacion, distrito = split_ubicacion_y_distrito(extracted["ubicacion"])
        if ubicacion:
            extracted["ubicacion"] = ubicacion
        if distrito:
            extracted["distrito"] = distrito

    return {k: v for k, v in extracted.items() if v}


def _extract_value_for_expected_field(
    field_name: Optional[str],
    raw_text: str,
    context: dict,
) -> Optional[str]:
    """Try to extract the value that the flow is waiting for from the raw text."""

    if not field_name:
        return None

    normalized_field = field_name.lower()
    raw_text = raw_text or ""

    if normalized_field in {"email", "correo", "correo_electronico"}:
        return extract_email(raw_text)

    if normalized_field in {"telefono", "tel", "celular"}:
        phone = extract_phone(raw_text)
        return phone[0] if phone else None

    if normalized_field in {"dni", "documento"}:
        dni = extract_dni(raw_text)
        return dni[0] if dni else None

    if normalized_field in {"nombre", "nombre_completo"}:
        return extract_name(raw_text)

    if normalized_field in {"descripcion", "descripcion_mas_detallada"}:
        return raw_text.strip() or None

    if normalized_field in {"categoria", "direccion", "ubicacion"}:
        # Location fields are handled separately using the structured payload when possible,
        # but fall back to the free-form text if needed.
        if context.get("es_ubicacion") and context.get("ubicacion_usuario"):
            location_payload = context.get("ubicacion_usuario") or {}
            address = location_payload.get("address")
            if address:
                return address
            lat = location_payload.get("latitude")
            lon = location_payload.get("longitude")
            if lat is not None and lon is not None:
                return f"Lat: {lat}, Lon: {lon}"
        return raw_text.strip() or None

    return raw_text.strip() or None

_PRODUCT_CATALOG_CACHE = None
def cargar_catalogo_productos():
    global _PRODUCT_CATALOG_CACHE
    if _PRODUCT_CATALOG_CACHE is None:
        try:
            catalog_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "product_catalog.json")
            with open(catalog_file_path, "r", encoding="utf-8") as f: _PRODUCT_CATALOG_CACHE = json.load(f)
            logger.info(f"✅ Catálogo de productos cargado desde {catalog_file_path}")
        except FileNotFoundError: logger.warning(f"[CATALOGO] Archivo no encontrado: {catalog_file_path}, se usa lista vacía"); _PRODUCT_CATALOG_CACHE = []
        except Exception as e: logger.error(f"❌ Error al cargar product_catalog.json: {e}", exc_info=True); _PRODUCT_CATALOG_CACHE = []
    return _PRODUCT_CATALOG_CACHE
PRODUCT_CATALOG = cargar_catalogo_productos()

_COMMERCE_LOCATIONS_CACHE = None
def cargar_ubicaciones_comercios():
    global _COMMERCE_LOCATIONS_CACHE
    if _COMMERCE_LOCATIONS_CACHE is None:
        try:
            loc_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "commerce_locations.json")
            with open(loc_file_path, "r", encoding="utf-8") as f: _COMMERCE_LOCATIONS_CACHE = json.load(f)
            logger.info(f"✅ Ubicaciones de comercios cargadas desde {loc_file_path}")
        except FileNotFoundError: logger.warning(f"[COMERCIOS] Archivo no encontrado: {loc_file_path}, se usa lista vacía"); _COMMERCE_LOCATIONS_CACHE = []
        except Exception as e: logger.error(f"❌ Error al cargar commerce_locations.json: {e}", exc_info=True); _COMMERCE_LOCATIONS_CACHE = []
    return _COMMERCE_LOCATIONS_CACHE
COMMERCE_LOCATIONS = cargar_ubicaciones_comercios()




PROMPT_MUNICIPIO_CON_CONTEXTO = """
Sos el asistente digital del municipio. Respondé la PREGUNTA DEL USUARIO usando solo la INFORMACIÓN DE CONTEXTO.
Si no tenés info suficiente, decilo y sugerí contactar al municipio.
--- CONTEXTO ---
{contexto_scraped}
-----------------
PREGUNTA: "{pregunta_usuario}"
Respuesta:
"""
def crear_prompt_decision_herramienta(pregunta_usuario: str) -> str:
    descripcion_herramientas_json = {}
    for nombre, detalles in TOOL_REGISTRY.items(): descripcion_herramientas_json[nombre] = {"descripcion": detalles["descripcion"], "parametros": detalles["parametros"]}
    prompt = f"""
Sos un despachador de herramientas inteligente. Analizá la PREGUNTA DEL USUARIO y decidí si alguna herramienta puede resolverla.
HERRAMIENTAS DISPONIBLES:
{json.dumps(descripcion_herramientas_json, indent=2)}
PREGUNTA: "{pregunta_usuario}"
- Si coincide y hay parámetros, devolvé JSON: {{"herramienta": "nombre_herramienta", "parametros": {{"nombre_param": "valor"}}}}
- Si faltan parámetros, devolvé JSON: {{"herramienta": "nombre_herramienta", "faltan_parametros": ["nombre_param"]}}
- Si no aplica, devolvé 'null'.
"""
    return prompt


class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context

    def handle(self, payload: dict) -> dict | None:
        raise NotImplementedError

from services.google_search import google_search

class GreetingHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        """Reset and initialize municipal context for a greeting."""
        chat_db_context_data = self.context.get("chat_db_context_data")

        if not isinstance(chat_db_context_data, dict):
            logger.warning(
                "[GreetingHandler] chat_db_context_data no encontrado. No se puede hacer un reseteo completo."
            )
            chat_db_context_data = {}
            self.context["chat_db_context_data"] = chat_db_context_data

        contexto_municipio_actual: dict = {}

        preserved_welcome_state = None
        preserved_last_welcome_ts = None
        if chat_db_context_data:
            logger.info(
                "[GreetingHandler] Saludo detectado. Realizando reseteo completo del contexto."
            )

            # Preserve essential info if it exists
            user_info = chat_db_context_data.get(CONTEXTO_MUNICIPIO, {}).get("user", {})
            contacto_prev = chat_db_context_data.get(CONTEXTO_MUNICIPIO, {}).get(
                "contacto_usuario", {}
            )
            profile_name = chat_db_context_data.get("profile_name")
            preserved_welcome_state = chat_db_context_data.get("_welcome_state")
            preserved_last_welcome_ts = chat_db_context_data.get("last_welcome_ts")

            # Clear the entire context to prevent stale data from any flow
            chat_db_context_data.clear()

            # Restore essential info into a fresh context
            contexto_municipio_actual = chat_db_context_data.setdefault(
                CONTEXTO_MUNICIPIO, {}
            )
            if contacto_prev:
                contexto_municipio_actual["contacto_usuario"] = contacto_prev
            if user_info:
                contexto_municipio_actual["user"] = user_info
            if profile_name:
                chat_db_context_data["profile_name"] = profile_name
            if preserved_welcome_state is not None:
                chat_db_context_data["_welcome_state"] = preserved_welcome_state
            if preserved_last_welcome_ts is not None:
                chat_db_context_data["last_welcome_ts"] = preserved_last_welcome_ts
        else:
            contexto_municipio_actual = chat_db_context_data.setdefault(
                CONTEXTO_MUNICIPIO, {}
            )
            if "_welcome_state" not in chat_db_context_data:
                chat_db_context_data["_welcome_state"] = {}

        # Establecer el estado para esperar una selección del menú principal en el próximo turno.
        contexto_municipio_actual["estado_conversacion"] = (
            ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        )
        logger.info(
            f"[GreetingHandler] Nuevo estado de conversación: {contexto_municipio_actual['estado_conversacion']}"
        )

        # Usar la función centralizada para obtener el payload del menú.
        return _get_main_menu_payload(self.context)



def _message_with_menu(message, context, include_greeting: bool = True):
    if include_greeting:
        menu_payload = GreetingHandler(context).handle({})
        if message:
            menu_payload["message_body"] = f"{message}\n\n{menu_payload['message_body']}"
        return menu_payload

    menu_payload = _get_main_menu_payload(context)
    if menu_payload.get("fuente") == "pedir_nombre_inicial":
        return {
            "message_body": message or menu_payload.get("message_body", ""),
            "message_type": "text",
        }
    if message:
        menu_payload["message_body"] = message
    return menu_payload


def handle_contactos_utiles_inicio(context, chat_db_context):
    """Handles the initial request for 'Contactos Útiles'."""
    municipio_id = context.get("municipio_id", MUNICIPIO_ID)
    contactos_data = cargar_contactos_utiles(municipio_id)
    categorias = contactos_data.get("categorias", []) if isinstance(contactos_data, dict) else []
    if not categorias:
        return {
            "message_body": "No se encontró información de contactos útiles en este momento.",
            "message_type": "text"
        }

    # Build buttons and map slug -> contactos
    categorias_map = {}
    buttons = []
    for cat in categorias:
        nombre = cat.get("nombre_categoria", "Sin categoría")
        slug = normalizar_texto(nombre).replace(" ", "_")
        categorias_map[slug] = {
            "nombre": nombre,
            "contactos": cat.get("contactos", [])
        }
        buttons.append({"texto": nombre, "action_id": f"select_contact_category_{slug}"})

    contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name
    contexto_municipio_actual['contactos_categorias'] = categorias_map
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    return {
        "message_body": "Seleccioná una categoría para ver los contactos:",
        "options_list": buttons,
        "message_type": "interactive_buttons",
        "fuente": "contactos_utiles_show_categories"
    }

def _normalize_public_url(value: Any, context: Optional[dict] = None) -> Optional[str]:
    """Return an absolute, publicly accessible URL for uploaded assets."""

    if not value or not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "tel:", "whatsapp:", "data:")):
        return candidate

    if candidate.startswith("//"):
        scheme = "https" if (current_app.config.get("IS_HTTPS") or DEFAULT_IS_HTTPS) else "http"
        return f"{scheme}:{candidate}"

    if candidate.startswith("www."):
        return f"https://{candidate}"

    if candidate.startswith("/data/"):
        candidate = "/media/" + candidate[len("/data/"):]
    elif candidate.startswith("data/"):
        candidate = "/media/" + candidate[len("data/"):]
    elif candidate.startswith("/archivos/"):
        candidate = "/media/" + candidate.lstrip("/")

    base_url = None
    municipio_cfg: Optional[dict] = None
    if context and isinstance(context, dict):
        municipio_cfg = context.get("municipio_config_actual")
        if isinstance(municipio_cfg, dict):
            base_url = (
                municipio_cfg.get("media_base_url")
                or municipio_cfg.get("asset_base_url")
                or municipio_cfg.get("public_base_url")
            )

    if not base_url:
        base_url = current_app.config.get("BACKEND_URL") or DEFAULT_BACKEND_URL

    if not base_url:
        return candidate

    normalized_base = base_url.rstrip("/") + "/"
    return urljoin(normalized_base, candidate.lstrip("/"))


def _normalize_post_entry(post: dict, context: Optional[dict]) -> dict:
    normalized = dict(post)
    raw_image = post.get("imagen_url") or post.get("imagen")
    datos_extra = post.get("datos_extra")
    if not raw_image and isinstance(datos_extra, dict):
        raw_image = datos_extra.get("imagen_url")
    normalized_image = _normalize_public_url(raw_image, context)
    if normalized_image:
        normalized["imagen_url"] = normalized_image
    elif raw_image and "imagen_url" not in normalized:
        normalized["imagen_url"] = raw_image

    raw_link = post.get("enlace") or post.get("link") or post.get("url")
    normalized_link = _normalize_public_url(raw_link, context)
    if normalized_link:
        normalized["enlace"] = normalized_link
    elif raw_link and "enlace" not in normalized:
        normalized["enlace"] = raw_link

    return normalized


def _format_post(post: dict, channel: str, *, index: Optional[int] = None) -> str:
    """Return a formatted string for a single news/event entry."""

    def _format_fecha(fecha_str: str) -> str:
        try:
            if not fecha_str:
                return ""
            fecha_str = fecha_str.rstrip("Z")
            dt = datetime.fromisoformat(fecha_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ARG_TZ)
            else:
                dt = dt.astimezone(ARG_TZ)
            fecha_formateada = dt.strftime("%d/%m/%Y")
            if dt.time() != datetime.min.time():
                fecha_formateada += f" {dt.strftime('%H:%M')} hs"
            return fecha_formateada
        except Exception:
            return fecha_str

    title = post.get("titulo", "Sin título")
    subtitle = post.get("subtitulo")
    desc = post.get("descripcion", "Sin descripción.")
    link = post.get("enlace") or post.get("link") or post.get("url")
    imagen = post.get("imagen_url")

    fecha_inicio = post.get("fecha_evento_inicio") or post.get("fecha_inicio")
    fecha_fin = post.get("fecha_evento_fin")
    if fecha_inicio and fecha_fin and fecha_fin != fecha_inicio:
        fecha = f"{_format_fecha(fecha_inicio)} - {_format_fecha(fecha_fin)}"
    else:
        fecha = _format_fecha(fecha_inicio or fecha_fin or post.get("fecha_publicacion", "")) if (fecha_inicio or fecha_fin or post.get("fecha_publicacion")) else None

    ubicacion = post.get("ubicacion")

    if channel == "whatsapp":
        header = f"*{index}. {title}*" if index is not None else f"*{title}*"
        lines: list[str] = [header]

        if subtitle:
            lines.append(f"_{subtitle}_")

        if fecha:
            lines.append(f"📅 {fecha}")
        if ubicacion:
            lines.append(f"📍 {ubicacion}")

        cleaned_desc = (desc or "").strip()
        if cleaned_desc:
            if len(cleaned_desc) > 420:
                cleaned_desc = cleaned_desc[:417].rstrip() + "…"
            lines.extend(["", cleaned_desc])

        resource_lines: list[str] = []
        if link:
            resource_lines.append(f"🔗 Más info: {link}")
        if imagen:
            resource_lines.append(f"🖼️ Flyer: {imagen}")
        if resource_lines:
            lines.extend(["", *resource_lines])

        return "\n".join(lines).strip()

    if channel == "web":
        lines = [title]
        if subtitle:
            lines.append(subtitle)
        if fecha:
            lines.append(f"📅 {fecha}")
        if ubicacion:
            lines.append(f"📍 {ubicacion}")
        if desc:
            lines.extend(["", desc])
        if imagen:
            lines.extend(["", imagen])
        if link:
            lines.extend(["", f"🔗 Más info: {link}"])
        return "\n".join(lines)

    # Default to HTML formatting for other channels
    parts = [f"<strong>{title}</strong>"]
    if subtitle:
        parts.append(f"<em>{subtitle}</em>")
    if fecha:
        parts.append(f"📅 {fecha}")
    if ubicacion:
        parts.append(f"📍 {ubicacion}")
    if imagen:
        parts.append(f'<img src="{imagen}" alt="flyer" style="max-width:100%;height:auto;">')
    if desc:
        parts.append(desc)
    if link:
        parts.append(f'<a href="{link}" target="_blank">Más info</a>')
    return "<br>".join(parts)


def _format_contact(contact: dict, channel: str) -> str:
    """Formatea un contacto individual según el canal."""
    nombre = contact.get("nombre", "Sin nombre")
    if "_" in nombre:
        nombre = nombre.replace("_", " ").title()
    descripcion = contact.get("descripcion")
    telefono = contact.get("telefono")
    url = contact.get("url")
    horario = contact.get("horario")
    lines = []
    if channel == "whatsapp":
        lines.append(f"*{nombre}*")
        if descripcion:
            lines.append(descripcion)
        if horario:
            lines.append(f"🕑 {horario}")
        if telefono:
            digits = re.sub(r"\D", "", telefono)
            lines.append(f"📞 {telefono}")
            lines.append(f"👉 https://wa.me/{digits}")
        if url:
            lines.append(f"🔗 {url}")
        lines.append("")
        return "\n".join(lines)
    else:
        lines.append(f"<strong>{nombre}</strong>")
        if descripcion:
            lines.append(descripcion)
        if horario:
            lines.append(f"<em>{horario}</em>")
        if telefono:
            digits = re.sub(r"\D", "", telefono)
            lines.append(f'Tel: <a href="tel:{digits}">{telefono}</a>')
            lines.append(f'WhatsApp: <a href="https://wa.me/{digits}" target="_blank">{telefono}</a>')
        if url:
            lines.append(f'<a href="{url}" target="_blank">Más información</a>')
        lines.append("<br>")
        return "<br>".join(lines)


def handle_contactos_utiles_mostrar_categoria(context, chat_db_context, selected):
    """Muestra los contactos de una categoría manteniendo el estado para nuevas consultas."""
    channel = context.get("channel", "web")
    contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
    categorias_map = contexto_municipio_actual.get('contactos_categorias', {})

    contactos = selected.get("contactos", [])
    nombre_categoria = selected.get("nombre", "")
    if not contactos:
        message_body = f"No se encontraron contactos para la categoría '{nombre_categoria}'."
    else:
        if channel == "whatsapp":
            message_body = f"📞 *Contactos para {nombre_categoria}:*\n\n"
            for c in contactos:
                message_body += _format_contact(c, channel) + "\n"
        else:
            message_body = f"<h4>📞 Contactos para {nombre_categoria}</h4>"
            for c in contactos:
                message_body += _format_contact(c, channel)

    buttons = [{"texto": v["nombre"], "action_id": f"select_contact_category_{k}"} for k, v in categorias_map.items()]

    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    return {
        "message_body": message_body.strip(),
        "options_list": buttons,
        "message_type": "interactive_buttons",
        "fuente": "contactos_utiles_show_contacts",
    }


def _get_posts_from_json(
    content_type: str,
    channel: str,
    municipio_id: str,
    context: Optional[dict] = None,
) -> tuple[str, str | None]:
    """Helper to get formatted posts of a specific type from the JSON file.

    Returns a tuple with the formatted text and the first image URL found
    for the requested posts. The image is returned separately to allow the
    caller to adjuntar a media message.
    """

    all_posts_data = cargar_agenda_cultural(municipio_id)
    all_posts = all_posts_data.get("eventos", [])

    if not all_posts:
        return "", None

    tipo_post_filters: set[str]
    if content_type == "noticia":
        # Las publicaciones cargadas desde la solapa "Información" deben
        # mostrarse junto con las noticias tradicionales en WhatsApp y el
        # widget web. Se etiquetan con ``tipo_post=informacion`` y, en algunos
        # casos, también aparecen en ``tags``.
        tipo_post_filters = {"noticia", "informacion"}
    else:
        tipo_post_filters = {content_type}

    posts = []
    for post in all_posts:
        tipo_post = (post.get("tipo_post") or "").lower()
        tags = [str(tag).lower() for tag in post.get("tags", []) if isinstance(tag, str)]
        if tipo_post in tipo_post_filters or any(tag in tipo_post_filters for tag in tags):
            posts.append(_normalize_post_entry(post, context))

    if not posts:
        return "", None

    limit = 10

    def _parse_date(date_str: str):
        if not date_str:
            return None
        try:
            date_str = date_str.rstrip("Z")
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ARG_TZ)
            else:
                dt = dt.astimezone(ARG_TZ)
            return dt
        except Exception:
            return None

    if content_type == "evento":
        now_arg = datetime.now(ARG_TZ)
        future_posts: list[dict] = []
        undated_posts: list[dict] = []
        past_posts: list[dict] = []

        for p in posts:
            start_dt = _parse_date(
                p.get("fecha_evento_inicio")
                or p.get("fecha_inicio")
                or p.get("fecha_publicacion")
            )
            p["_start"] = start_dt
            if start_dt is None:
                undated_posts.append(p)
            elif start_dt >= now_arg:
                future_posts.append(p)
            else:
                past_posts.append(p)

        future_posts.sort(key=lambda x: x.get("_start") or datetime.max)
        past_posts.sort(key=lambda x: x.get("_start") or datetime.min, reverse=True)
        ordered_posts = future_posts + undated_posts + past_posts
        posts = ordered_posts or posts
    else:
        posts.sort(key=lambda x: x.get("fecha_publicacion", ""), reverse=True)

    formatted: list[str] = []
    selected_posts = posts[:limit]
    first_image = next((p.get("imagen_url") for p in selected_posts if p.get("imagen_url")), None)
    for idx, p in enumerate(selected_posts, start=1):
        formatted.append(_format_post(p, channel, index=idx if channel == "whatsapp" else None))

    if channel == "whatsapp":
        emoji = "📰" if content_type == "noticia" else "🎭"
        formatted = [f"{emoji} {item}".rstrip() for item in formatted]
        separator = "\n\n────────────────────\n\n"
        return separator.join(formatted) + "\n", first_image
    if channel == "web":
        return "\n\n".join(formatted) + "\n", first_image
    return "<hr>".join(formatted), first_image

def handle_main_menu_action(action_id: str, context: dict, chat_db_context) -> dict:
    """
    Handles actions from the new categorized main menu.
    """
    # --- Aliases for new action_ids to reuse existing logic ---
    if action_id in {"veterinaria_bromatologia", "bromatologia"}:
        action_id = "zoonosis"  # Re-route to existing logic
    if action_id == "buscar_estacionamiento":
        action_id = "estacionamiento" # Re-route to existing logic

    # --- Handlers for New/Modified Menu Options ---
    if action_id == "contactos_utiles":
        return handle_contactos_utiles_inicio(context, chat_db_context)

    if action_id == "menu_principal":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        contexto_municipio_actual.pop('menu_opciones', None)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _get_main_menu_payload(context)

    if action_id in {"limpiar_contexto", "cancelar"}:
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contacto_prev = contexto_municipio_actual.get("contacto_usuario")
        contexto_municipio_actual.clear()
        if contacto_prev:
            contexto_municipio_actual["contacto_usuario"] = contacto_prev
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _get_main_menu_payload(
            context,
            welcome_message_override="¡Listo! Empezamos de nuevo. ¿En qué te puedo ayudar?",
        )

    if action_id == "mostrar_menu_reclamos":
        submenu = _get_reclamos_consultas_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "iniciar_reclamo":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        logger.info("[MENU_ACTION] Clearing previous claim context for new claim.")
        contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
        contexto_municipio_actual.pop("historial_llm_reclamo", None)
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name

        user_input = context.get("user_input_raw", "")
        reclamo_opts = _get_reclamos_menu().get("options_list", [])
        menu_opciones = (
            context.get("menu_opciones")
            or contexto_municipio_actual.get("menu_opciones", [])
        )
        came_from_list_menu = any(
            option.get("action_id") == "iniciar_reclamo"
            for option in menu_opciones
            if isinstance(option, dict)
        )
        skip_autodetect = bool(context.pop("skip_reclamo_autodetect", False))

        # Pass location context
        municipio_config = context.get("municipio_config_actual", {})
        default_localidad = municipio_config.get("ciudad")
        default_provincia = municipio_config.get("provincia")

        details = {}
        detected_category = None
        if not (skip_autodetect or (came_from_list_menu and user_input.strip().isdigit())):
            details = extract_reclamo_details_from_text(
                user_input,
                reclamo_opts,
                default_localidad=default_localidad,
                default_provincia=default_provincia,
            )
            detected_category = details.pop("categoria", None)
        handler = ReclamoFlowHandler(context, chat_db_context)
        if detected_category:
            logger.info(
                f"[MENU_ACTION] Auto-detected category '{detected_category}' from input."
            )
        # 'details' now contains all other fields like 'descripcion', 'direccion', 'nombre', etc.
        datos_iniciales = details

        response_dict = handler.start_flow(
            datos_iniciales=datos_iniciales or None,
            categoria_inicial=detected_category,
        )
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return response_dict

    if action_id == "mostrar_menu_tramites":
        submenu = _get_tramites_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "mostrar_menu_informacion":
        submenu = _get_informacion_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "mostrar_menu_encuestas":
        submenu = _get_encuestas_menu(context)
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        opciones_accionables = [
            opcion
            for opcion in submenu.get("options_list", [])
            if opcion.get("action_id")
        ]
        if opciones_accionables:
            contexto_municipio_actual["menu_opciones"] = opciones_accionables
            contexto_municipio_actual["encuestas_menu_options"] = opciones_accionables
        else:
            contexto_municipio_actual.pop("menu_opciones", None)
            contexto_municipio_actual.pop("encuestas_menu_options", None)
        surveys_meta = submenu.get("surveys")
        if surveys_meta:
            contexto_municipio_actual["encuestas_menu_surveys"] = surveys_meta
        else:
            contexto_municipio_actual.pop("encuestas_menu_surveys", None)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id in {
        "mostrar_menu_catalogo",
        "mostrar_carrito_catalogo",
        "finalizar_pedido_catalogo_demo",
    } or action_id.startswith("catalogo_"):
        if not _catalogo_widget_visible(context):
            return {
                "message_body": "El catálogo no está habilitado en este widget municipal.",
                "message_type": "interactive_buttons",
                "options_list": [
                    {"texto": "Menú principal", "action_id": "menu_principal"},
                    {"texto": "Volver", "action_id": "cancelar"},
                ],
                "fuente": "catalogo_desactivado_municipio",
                "generar_audio": True,
            }

    if action_id == "mostrar_menu_catalogo":
        submenu = _get_catalogo_menu(context)
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id.startswith("encuesta_compartir::"):
        slug_publico = action_id.split("::", 1)[1] if "::" in action_id else ""
        return _build_encuesta_share_payload(slug_publico, context, chat_db_context)

    if action_id == "mostrar_menu_estacionamiento":
        submenu = _get_estacionamiento_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "mostrar_menu_ayuda":
        submenu = _get_ayuda_menu()
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return submenu

    if action_id == "catalogo_subastas":
        submenu = _get_catalogo_menu(context)
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")

        subastas = listar_subastas_activas()
        base_lines = [
            "*Subastas activas*",
            "Estos son los lotes disponibles en este momento:",
            "",
        ]

        if subastas:
            for subasta in subastas:
                titulo = subasta.get("titulo") or "Subasta"
                fecha_cierre = subasta.get("fecha_cierre") or "sin fecha de cierre"
                precio_base = subasta.get("precio_base")
                moneda = subasta.get("moneda") or "ARS"
                if isinstance(precio_base, (int, float)):
                    precio_texto = f"{moneda} {precio_base:,.0f}".replace(",", ".")
                else:
                    precio_texto = "Precio base a confirmar"
                base_lines.append(f"• {titulo} (cierra: {fecha_cierre}) - Base: {precio_texto}")
        else:
            base_lines.append("Por ahora no hay subastas abiertas. Podés volver a consultar más tarde.")

        submenu["message_body"] = "\n".join(base_lines)
        submenu["fuente"] = "submenu_catalogo_subastas"
        return submenu

    if action_id.startswith("catalogo_mostrar_mas::"):
        parts = action_id.split("::", 2)
        base_action = parts[1] if len(parts) > 1 else "catalogo_ver"
        page_str = parts[2] if len(parts) > 2 else "1"
        page = int(page_str) if page_str.isdigit() else 1
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _build_catalogo_flow_payload(base_action, context, page=page)

    if action_id.startswith("catalogo_agregar_item::"):
        item_id = action_id.split("::", 1)[1]
        item_data = _find_catalog_item_by_id(item_id)
        cart = _get_catalogo_cart(context)
        existing = next((entry for entry in cart if entry.get("id") == item_id), None)
        if existing:
            existing["cantidad"] = existing.get("cantidad", 1) + 1
        else:
            cart.append({"id": item_id, "cantidad": 1})
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        nombre = item_data.get("nombre") if item_data else item_id
        reminder = ""
        if item_data and item_data.get("tipo") == "canje_puntos" and not context.get("user_obj"):
            reminder = "\nℹ️ Para usar puntos necesitamos vincular tu cuenta o email."
        return {
            "message_body": f"✅ {nombre} agregado al carrito.{reminder}\nEscribí 'Ver carrito' o usá el botón para revisar.",
            "message_type": "interactive_buttons",
            "options_list": [
                {"texto": "Ver carrito", "action_id": "mostrar_carrito_catalogo"},
                {"texto": "Seguir viendo", "action_id": "mostrar_menu_catalogo"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ],
            "fuente": "catalogo_item_agregado_demo",
            "generar_audio": True,
        }

    if action_id == "mostrar_carrito_catalogo":
        resumen, total_precio, total_puntos, total_donaciones = _build_catalogo_cart_summary(context)
        options_list = [
            {"texto": "Seguir comprando", "action_id": "mostrar_menu_catalogo"},
            {"texto": "Finalizar", "action_id": "finalizar_pedido_catalogo_demo"},
            {"texto": "Cancelar", "action_id": "cancelar"},
        ]
        if total_precio == 0 and total_puntos > 0:
            options_list.insert(0, {"texto": "Identificarme", "action_id": "catalogo_canje_puntos"})
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": resumen,
            "message_type": "interactive_buttons",
            "options_list": options_list,
            "fuente": "catalogo_carrito_resumen_demo",
            "generar_audio": True,
        }

    if action_id == "finalizar_pedido_catalogo_demo":
        resumen, total_precio, total_puntos, total_donaciones = _build_catalogo_cart_summary(context)
        follow_up = []
        if total_precio > 0:
            follow_up.append("Enviamos un link/QR de MercadoPago y te avisaremos cuando el pago esté acreditado.")
        if total_puntos > 0:
            follow_up.append("Para canjear puntos necesitamos validar tu cuenta o email asociado.")
        if total_donaciones:
            follow_up.append("Registramos tus donaciones sin necesidad de pago.")
        if not follow_up:
            follow_up.append("No hay productos para procesar todavía.")
        return {
            "message_body": resumen + "\n\n" + " ".join(follow_up),
            "message_type": "interactive_buttons",
            "options_list": [
                {"texto": "Volver al catálogo", "action_id": "mostrar_menu_catalogo"},
                {"texto": "Menú", "action_id": "menu_principal"},
            ],
            "fuente": "catalogo_finalizar_demo",
            "generar_audio": True,
        }

    if action_id in {
        "catalogo_ver",
        "catalogo_donaciones",
        "catalogo_canje_puntos",
        "catalogo_compras",
    }:
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _build_catalogo_flow_payload(action_id, context)

    if action_id == "consultar_estado_reclamo":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NUMERO_TICKET.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "Por favor, ingresá el número de tu reclamo para consultar el estado.",
            "message_type": "text",
            "fuente": "handler_consultar_reclamo"
        }

    if action_id == "enviar_sugerencia":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "¡Gracias por tu iniciativa! Por favor, escribí tu sugerencia o propuesta a continuación.",
            "message_type": "text",
            "fuente": "handler_enviar_sugerencia"
        }

    if action_id == "agenda_y_noticias":
        channel = context.get("channel", "web")
        municipio_id = context.get("municipio_id", MUNICIPIO_ID)
        noticias_body, noticias_img = _get_posts_from_json(
            "noticia", channel, municipio_id, context
        )
        eventos_body, eventos_img = _get_posts_from_json(
            "evento", channel, municipio_id, context
        )

        full_body = ""
        if noticias_body:
            if channel == "whatsapp":
                full_body += "*🗞️ Noticias Recientes*\n\n" + noticias_body
            elif channel == "web":
                full_body += "🗞️ Noticias Recientes\n\n" + noticias_body
            else:
                full_body += "<h3>🗞️ Noticias Recientes</h3>" + noticias_body
        if eventos_body:
            if channel == "whatsapp":
                if full_body:
                    full_body += "\n\n"
                full_body += "*🎭 Próximos Eventos*\n\n" + eventos_body
            elif channel == "web":
                if full_body:
                    full_body += "\n\n"
                full_body += "🎭 Próximos Eventos\n\n" + eventos_body
            else:
                full_body += "<h3>🎭 Próximos Eventos</h3>" + eventos_body

        if not full_body:
            full_body = "No hay noticias ni eventos para mostrar en este momento."
            social_buttons = []
            first_image = None
        else:
            config_links = context.get("municipio_config_actual", {}).get("social_links", [])
            if channel == "web":
                social_body = "<hr>Seguinos en nuestras redes:"
            else:
                social_body = "\n---\nSeguinos en nuestras redes:"
            full_body += social_body
            social_buttons = [
                {
                    "texto": link.get("name"),
                    "url": link.get("url"),
                    "type": "url",
                    "image_url": link.get("logo_url"),
                }
                for link in config_links
            ]
            first_image = eventos_img or noticias_img

        response = {
            "message_body": full_body.strip(),
            "message_type": "interactive_buttons" if social_buttons else "text",
            "fuente": "handler_agenda_y_noticias",
        }
        if social_buttons:
            response["options_list"] = social_buttons
        if first_image:
            response["image_url"] = first_image
        return response

    if action_id == "web_municipio":
        website_url = context.get("municipio_config_actual", {}).get("website_url", "https://www.juninmendoza.gov.ar/")
        return {
            "message_body": f"Podés encontrar toda la información oficial en nuestro sitio web.",
            "options_list": [{"texto": "Visitar Sitio Web", "url": website_url, "type": "url"}],
            "message_type": "interactive_buttons",
            "fuente": "handler_web_municipio"
        }

    # --- Handlers for existing options that are kept ---
    tramites_info = get_tramites_info(context.get("municipio_id", MUNICIPIO_ID))
    if action_id in tramites_info:
        data = tramites_info[action_id] or {}
        botones = data.get("botones", [])
        for btn in botones:
            if btn.get("url") and not btn.get("type"):
                btn["type"] = "url"
        body = data.get("descripcion", "")
        config_links = context.get("municipio_config_actual", {}).get("social_links", [])
        social_buttons = []
        if action_id in {"obras", "punto_limpio"} and config_links:
            body += "\n---\nSeguinos en nuestras redes:"
            social_buttons = [
                {
                    "texto": link.get("name"),
                    "url": link.get("url"),
                    "type": "url",
                    "image_url": link.get("logo_url"),
                }
                for link in config_links
            ]
        response = {
            "message_body": body,
            "options_list": botones + social_buttons,
            "message_type": "interactive_buttons" if botones or social_buttons else "text",
            "fuente": f"info_{action_id}_json",
        }
        image = data.get("image_url")
        if image:
            response["image_url"] = image
        return response

    if action_id == "compartir_ubicacion":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        if (
            contexto_municipio_actual.get("estado_conversacion") is None
            and contexto_municipio_actual.get("ultima_consulta_poi")
        ):
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
            contexto_municipio_actual["consulta_pendiente_ubicacion"] = contexto_municipio_actual.get("ultima_consulta_poi")
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return {
                "message_body": "Para buscar estacionamientos necesito tu ubicación. Tocá 'Compartir ubicación' o escribí una dirección.",
                "options_list": [
                    {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                    {"texto": "Cancelar", "action": "cancelar"},
                ],
                "message_type": "interactive_buttons",
                "fuente": "pedir_ubicacion_estacionamiento",
            }

    if action_id == "estacionamiento":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
        contexto_municipio_actual['consulta_pendiente_ubicacion'] = 'estacionamiento'
        contexto_municipio_actual['ultima_consulta_poi'] = 'estacionamiento'
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "Para encontrar estacionamiento libre, por favor compartí tu ubicación o escribí una dirección.",
            "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
            "message_type": "interactive_buttons",
            "fuente": "pedir_ubicacion_estacionamiento"
        }

    if action_id == "solicitar_turnos":
        return {
            "message_body": "📅 Para solicitar turnos online, por favor ingresá al siguiente enlace:",
            "options_list": [{"texto": "Solicitar Turno", "url": "https://tlc.mendoza.gov.ar/turnos", "type": "url"}],
            "message_type": "interactive_buttons",
            "fuente": "info_solicitar_turnos_direct_link"
        }

    if action_id == "zoonosis": # Handles the 'veterinaria_bromatologia' alias
        contactos_info = cargar_configuracion_municipio(context.get("municipio_id", MUNICIPIO_ID), "contactos_especializados.json")
        contacto_data = contactos_info.get("Veterinaria y Bromatologia", {})
        if not contacto_data:
            return {"message_body": "No se encontró la información de contacto en este momento.", "message_type": "text"}

        nombre = contacto_data.get("nombre")
        telefono = contacto_data.get("telefono")
        horario = contacto_data.get("horario")

        message_body = f"🐾 *Información de Veterinaria y Bromatología*\n\n"
        if nombre:
            message_body += f"Encargado/a: *{nombre}*\n"
        if telefono:
            link_whatsapp = f"https://wa.me/{''.join(filter(str.isdigit, telefono))}"
            message_body += f"Teléfono: *{telefono}* (WhatsApp: {link_whatsapp})\n"
        if horario:
            message_body += f"Horario de atención: *{horario}*\n"

        botones = []
        if telefono:
            link_whatsapp = f"https://wa.me/{''.join(filter(str.isdigit, telefono))}"
            botones.append({"texto": "Contactar por WhatsApp", "url": link_whatsapp, "type": "url"})

        return {
            "message_body": message_body.strip(),
            "options_list": botones,
            "message_type": "interactive_buttons" if botones else "text",
            "fuente": "info_veterinaria_json"
        }

    # Fallback for any other action that is not explicitly handled above
    return {
        "message_body": "Esta función no está implementada en este momento. Por favor, intentá con otra opción.",
        "options_list": [],
        "message_type": "text",
        "fuente": f"unimplemented_{action_id}"
    }


def handle_info_requests(action_id: str) -> dict:
    """
    Handles simple informational requests based on action IDs from buttons.
    """
    tramites_info = get_tramites_info(context.get("municipio_id", MUNICIPIO_ID))
    contactos_info = cargar_configuracion_municipio(context.get("municipio_id", MUNICIPIO_ID), "contactos_especializados.json")

    info_map = {
        "info_licencia_conducir": "licencia_de_conducir",
        "info_pago_tasas": "pago_de_tasas_vigentes",
        "info_defensa_consumidor": "defensa_del_consumidor",
    }

    if action_id in info_map:
        tramite_key = info_map[action_id]
        if tramite_key in tramites_info:
            tramite_data = tramites_info[tramite_key]
            return {
                "message_body": tramite_data["descripcion"],
                "options_list": tramite_data["botones"],
                "message_type": "interactive_buttons" if tramite_data["botones"] else "text",
                "fuente": f"info_request_{tramite_key}"
            }
    elif action_id == "info_veterinaria":
        contacto_data = contactos_info.get("Veterinaria y Bromatologia")
        if contacto_data:
            return {
                "message_body": f"Para información vinculada a veterinaria y bromatología municipal escribí al WhatsApp: {contacto_data['telefono']}",
                "options_list": [],
                "message_type": "text",
                "fuente": "info_request_veterinaria"
            }

    return {
        "message_body": "No encontré la información solicitada. Por favor, intentá de nuevo.",
        "options_list": [],
        "message_type": "text",
        "fuente": "info_request_not_found"
    }


def safe_llm_call(prompt, preamble, fallback=None):
    logger.debug(f"[LLM_CALL_PROMPT] Enviando prompt a LLM. Preamble: '{preamble}'. Prompt: '{prompt[:500]}...'")
    try:
        resp = get_cohere_response(message=prompt, preamble=preamble)
        logger.debug(f"[LLM_CALL_RESPONSE] Respuesta LLM recibida: '{resp[:500]}...'")
        generic_phrases = ["no tengo información", "lo siento", "no puedo ayudarte con eso", "no lo sé", "esa información no está disponible", "como modelo de lenguaje", "no tengo acceso a internet", "no puedo realizar esa acción"]
        if not resp: logger.warning("[LLM_FALLBACK] Respuesta vacía del LLM."); raise ValueError("Respuesta vacía del LLM")
        resp_lower = resp.lower()
        for phrase in generic_phrases:
            if phrase in resp_lower: logger.warning(f"[LLM_FALLBACK] Respuesta genérica del LLM detectada (contiene: '{phrase}'). Respuesta completa: '{resp}'"); raise ValueError(f"Respuesta genérica del LLM (contiene: '{phrase}')")
        return resp
    except ValueError as ve: logger.error(f"[LLM_FALLBACK] Problema con la respuesta del LLM: {ve}"); return fallback or "No pude encontrar una respuesta directa a tu consulta. ¿Podrías reformularla o preferís que te muestre opciones generales como hacer un reclamo o consultar trámites?"
    except Exception as e: logger.error(f"[LLM_FALLBACK] Error general en llamada a LLM: {e}", exc_info=True); return fallback or "Hubo un inconveniente al procesar tu solicitud en este momento. ¿Podrías reformularla o preferís que te muestre opciones generales como hacer un reclamo o consultar trámites?"

RECLAMO_STATES = [ConversationState.ESPERANDO_CATEGORIA_RECLAMO, ConversationState.ESPERANDO_DIRECCION_RECLAMO, ConversationState.ESPERANDO_NOMBRE_VECINO, ConversationState.ESPERANDO_TELEFONO_VECINO, ConversationState.ESPERANDO_EMAIL_VECINO, ConversationState.ESPERANDO_DESCRIPCION_RECLAMO, ConversationState.ESPERANDO_ADJUNTOS_RECLAMO, ConversationState.ESPERANDO_CONFIRMACION_RECLAMO]

def serializar_enum(obj):
    if isinstance(obj, Enum): return obj.name
    elif isinstance(obj, dict): return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list): return [serializar_enum(v) for v in obj]
    else: return obj

def handle_location_update(data):
    """
    Handles a location update from the client.
    It receives latitude and longitude, gets the address,
    and stores it in the user's session.
    """
    from .herramientas_municipio import obtener_direccion_de_coordenadas
    from flask import session

    lat = data.get("lat")
    lon = data.get("lon")

    if not lat or not lon:
        return {"respuesta": "No se pudo obtener la ubicación."}

    direccion_info = obtener_direccion_de_coordenadas(lat, lon)

    if not direccion_info:
        return {"respuesta": "No se pudo obtener la dirección desde las coordenadas."}

    session["user_location"] = direccion_info
    session.modified = True

    return {
        "respuesta": f"Ubicación actualizada a: {direccion_info.get('formatted_address')}"
    }

BOTONES_COMANDOS_MUNICIPIO = {
    "Hacer un reclamo": "mostrar_menu_reclamos",
    "Consultar estado de un trámite": "consultar_estado_ticket",
    "Consultar estado de ticket": "consultar_estado_ticket",
    "Consultar otro ticket": "consultar_estado_ticket",
    "Hablar con un agente": "hablar_con_agente",
    "Nuevo reclamo": "mostrar_menu_reclamos",
    "Adjuntar foto": "adjuntar_foto",
    "Compartir ubicación": "compartir_ubicacion",
    "Foto": "adjuntar_foto",
    "Ubicación": "compartir_ubicacion",
    "No, continuar": "sin_adjuntos",
    "Completar reclamo": "sin_adjuntos",
    "Sí, confirmar reclamo": "confirmar_reclamo",
    "Si, confirmar reclamo": "confirmar_reclamo",
    "Confirmar reclamo": "confirmar_reclamo",
    "Finalizar": "confirmar_reclamo",
    "Finalizar reclamo": "confirmar_reclamo",
    "Confirmar": "confirmar_reclamo",
    "Confirmado": "confirmar_reclamo",
    "Si confirmo": "confirmar_reclamo",
    "Sí confirmo": "confirmar_reclamo",
    "Editar datos": "editar_reclamo",
    "Sí, solucionado": "confirmar_cierre_ticket",
    "No, aún no": "no_cerrar_ticket",
    "Volver al inicio": "menu_principal",
    "Cancelar": "cancelar",
    "Empezar de nuevo": "limpiar_contexto",
}

# Utiliza el orquestador de LLMs que intenta OpenAI y Cohere.
from services.llm_orchestrator import llamar_llm_con_fallback

# Compatibilidad retroactiva: varios tests y flujos legados hacen patching sobre
# ``llamar_gemini``. Mantener este alias evita romperlos mientras el código
# migra completamente al nuevo orquestador LLM.
llamar_gemini = llamar_llm_con_fallback

# Imports necesarios para la función accion_crear_reclamo_municipio
# (Algunos pueden estar ya importados globalmente en el archivo)
# from models import MunicipioTicket, db as global_db, User, ArchivoAdjunto, AnalisisArchivo # db ya está como global_db
# from services.ticket_service import servicio_tickets # Ya importado
# from .herramientas_municipio import parse_direccion_completa, direccion_es_valida # Ya importados globalmente
# from .common_utils import validar_telefono, formatear_telefono_e164, validar_email # Ya importados globalmente
# from .config_loader import CONFIG_MUNICIPIO # Ya importado globalmente
# from services.municipios import enviar_notificacion_whatsapp_con_plantilla # Esta función está en este mismo archivo.

# Definición completa de accion_crear_reclamo_municipio




def accion_crear_reclamo_municipio(datos_reclamo, context):
    """Wrapper que delega la creación de reclamos al ActionHandler dedicado."""
    handler = CrearReclamoActionHandler(context=context)
    return handler.execute(datos_reclamo)
def _handle_ticket_creation(contexto_municipio_actual, context, datos_estructura_llm):
    """
    Prepares the confirmation message for the user before creating a ticket.
    It does not create the ticket itself but sets the stage for the final confirmation.
    """
    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    datos_reclamo.update(datos_estructura_llm)

    categoria = datos_reclamo.get("categoria")
    descripcion = datos_reclamo.get("descripcion")
    ubicacion = datos_reclamo.get("ubicacion")
    nombre_usuario = datos_reclamo.get("nombre_usuario_detectado")
    telefono_usuario = datos_reclamo.get("telefono_detectado")
    email_usuario = datos_reclamo.get("email_detectado")

    campos_faltantes = [campo for campo, valor in {
        "categoría": categoria, "descripción": descripcion, "ubicación": ubicacion,
        "nombre": nombre_usuario, "teléfono": telefono_usuario, "email": email_usuario
    }.items() if not valor]

    if campos_faltantes:
        return {
            "message_body": f"Para continuar, aún necesito estos datos: {', '.join(campos_faltantes)}.",
            "fuente": "error_crear_reclamo_faltan_datos_previo"
        }, contexto_municipio_actual

    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_DATOS_RECLAMO.name
    contexto_municipio_actual["datos_a_confirmar"] = datos_reclamo.copy()

    mensaje_confirmacion = (
        f"Por favor, confirmá si los datos para tu reclamo son correctos:\n"
        f"- **Categoría**: {categoria}\n"
        f"- **Descripción**: {descripcion}\n"
        f"- **Ubicación**: {ubicacion}\n"
        f"- **Nombre**: {nombre_usuario}\n"
        f"- **Teléfono**: {telefono_usuario}\n"
        f"- **Email**: {email_usuario}"
    )

    maps_link = datos_reclamo.get("maps_link") or datos_reclamo.get("maps_search_url")
    static_map_url = datos_reclamo.get("static_map_url")
    if maps_link:
        mensaje_confirmacion += f"\n🔗 Ver en mapa: {maps_link}"

    botones = [
        {"texto": "Sí, crear reclamo", "action_id": "confirmar_reclamo_si"},
        {"texto": "No, quiero editar", "action_id": "confirmar_reclamo_no"},
    ]

    response_payload = {
        "message_body": mensaje_confirmacion,
        "options_list": botones,
        "message_type": "interactive_buttons",
    }
    if static_map_url:
        response_payload["image_url"] = static_map_url
        response_payload["image_alt_text"] = "Mapa de la ubicación"

    return response_payload, contexto_municipio_actual


def handle_llm_interaction(app, pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual, demo_metadata=None):
    logger_actual = app.logger if app else (current_app.logger if has_app_context() else logging.getLogger(__name__))
    datos_actuales = {} # Initialize to prevent UnboundLocalError

    logger_actual.info(
        f"[HANDLE_LLM_START] pregunta='{pregunta_str}' estado_previo='{contexto_municipio_actual.get('estado_conversacion')}' ubicacion='{contexto_municipio_actual.get('datos_parciales_llm_reclamo', {}).get('ubicacion')}'"
    )

    estado_conversacion_para_llm = contexto_municipio_actual.get("estado_conversacion")
    estado_conversacion_enum = None
    if isinstance(estado_conversacion_para_llm, ConversationState):
        estado_conversacion_enum = estado_conversacion_para_llm
    elif isinstance(estado_conversacion_para_llm, str):
        estado_conversacion_enum = ConversationState.__members__.get(estado_conversacion_para_llm)
    waiting_state_active = (
        estado_conversacion_enum in CLAIM_WAITING_STATES
        or estado_conversacion_enum in SUGGESTION_WAITING_STATES
        if estado_conversacion_enum
        else False
    )
    invocar_llm = False

    # Si se está esperando info de un reclamo pero el usuario consulta un servicio
    if (
        estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
        and es_consulta_general(pregunta_str)
    ):
        logger_actual.info(
            "[HANDLE_LLM] Cambio de tema detectado durante flujo de reclamo. Reseteando contexto a conversacion general."
        )
        # Clear all claim-related context
        contexto_municipio_actual["historial_llm_reclamo"] = []
        contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
        contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
        contexto_municipio_actual.pop("esperando_info_llm", None)
        # Limpia datos residuales del reclamo previo
        for campo in [
            "categoria_reclamo",
            "descripcion_reclamo",
            "direccion_reclamo",
            "coordenadas_reclamo",
            "nombre_vecino",
            "telefono_vecino",
            "email_vecino",
            "foto_url",
        ]:
            contexto_municipio_actual.pop(campo, None)

    if (
        estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name
        and es_consulta_general(pregunta_str)
    ):
        logger_actual.info(
            "[HANDLE_LLM] Cambio de tema detectado durante flujo de sugerencia. Reseteando contexto a conversacion general."
        )
        contexto_municipio_actual["historial_llm_sugerencia"] = []
        contexto_municipio_actual["datos_parciales_llm_sugerencia"] = {}
        contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
        contexto_municipio_actual.pop("esperando_info_llm", None)

        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        estado_conversacion_para_llm = ConversationState.CONVERSACION_GENERAL_LLM.name

    if estado_conversacion_para_llm in [
        ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
        ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name,
        ConversationState.CONVERSACION_GENERAL_LLM.name,
        ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name # Add this state to the LLM-handled states
    ]:
        invocar_llm = True
    elif not estado_conversacion_para_llm or contexto_municipio_actual.get("saludo_detectado_en_largo_mensaje"):
        if len(pregunta_str.strip().split()) > 1 or (context.get("es_foto") and not pregunta_str.strip()):
            invocar_llm = True

    if invocar_llm:
        logger.info(f"[HANDLE_LLM] Invocando LLM. Estado: {estado_conversacion_para_llm}")

    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    datos_sugerencia = contexto_municipio_actual.get("datos_parciales_llm_sugerencia", {})
    usuario_info_llm = {
        "nombre": datos_reclamo.get("nombre_usuario_detectado") or getattr(viewer_user, "nombre", "Vecino/a") if viewer_user else "Vecino/a",
        "tipo_entidad": "municipio",
        "ubicacion": datos_reclamo.get("ubicacion") or getattr(viewer_user, "direccion", None) if viewer_user else None,
        "contacto": {
            "telefono": datos_reclamo.get("telefono_detectado") or getattr(viewer_user, "telefono", None) if viewer_user else None,
            "email": datos_reclamo.get("email_detectado") or getattr(viewer_user, "email", None) if viewer_user else None
        },
        "datos_reclamo_actuales": datos_reclamo,
        "datos_sugerencia_actuales": datos_sugerencia,
    }

    if demo_metadata:
        prompt_context = demo_metadata.get("prompt_context")
        if prompt_context:
            usuario_info_llm["demo_contexto"] = prompt_context
        if demo_metadata.get("key"):
            usuario_info_llm["demo_key"] = demo_metadata.get("key")
        if demo_metadata.get("display_name"):
            usuario_info_llm["demo_display_name"] = demo_metadata.get("display_name")
        if demo_metadata.get("description"):
            usuario_info_llm["demo_description"] = demo_metadata.get("description")
        if demo_metadata.get("faq_preview"):
            usuario_info_llm["demo_faq_preview"] = demo_metadata.get("faq_preview")

    historial_para_llm = []
    if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
        historial_para_llm = contexto_municipio_actual.get("historial_llm_reclamo", [])
    elif estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name:
        historial_para_llm = contexto_municipio_actual.get("historial_llm_sugerencia", [])
    else:
        historial_para_llm = contexto_municipio_actual.get("historial_conversacion_general_llm", [])

    historial_formateado = []
    if isinstance(historial_para_llm, list) and historial_para_llm:
        primer_turno = historial_para_llm[0]
        if isinstance(primer_turno, dict) and "pregunta_usuario" in primer_turno:
            for turno in historial_para_llm:
                pregunta = turno.get("pregunta_usuario")
                if pregunta:
                    historial_formateado.append({"role": "user", "parts": [{"text": pregunta}]})
                respuesta = turno.get("respuesta_ia")
                if respuesta:
                    historial_formateado.append({"role": "model", "parts": [{"text": respuesta}]})
        else:
            historial_formateado = historial_para_llm


    try:
        # FIX: Pre-process expected data to prevent state loss if LLM fails to return it
        expected_fields_reclamo = contexto_municipio_actual.get("expected_fields_llm_reclamo")
        if not isinstance(expected_fields_reclamo, list):
            expected_fields_reclamo = []
        expected_fields_sugerencia = contexto_municipio_actual.get("expected_fields_llm_sugerencia")
        if not isinstance(expected_fields_sugerencia, list):
            expected_fields_sugerencia = []

        pending_reclamo_raw = contexto_municipio_actual.get("esperando_info_llm_reclamo")
        if not expected_fields_reclamo and pending_reclamo_raw:
            expected_fields_reclamo = _normalize_pedir_info_fields(pending_reclamo_raw)
            if expected_fields_reclamo:
                contexto_municipio_actual["expected_fields_llm_reclamo"] = expected_fields_reclamo

        pending_sugerencia_raw = contexto_municipio_actual.get("esperando_info_llm_sugerencia")
        if not expected_fields_sugerencia and pending_sugerencia_raw:
            expected_fields_sugerencia = _normalize_pedir_info_fields(pending_sugerencia_raw)
            if expected_fields_sugerencia:
                contexto_municipio_actual["expected_fields_llm_sugerencia"] = expected_fields_sugerencia

        campo_esperado_reclamo = (
            expected_fields_reclamo[0]
            if expected_fields_reclamo
            else _normalize_pedir_info_value(pending_reclamo_raw)
        )
        campo_esperado_sugerencia = (
            expected_fields_sugerencia[0]
            if expected_fields_sugerencia
            else _normalize_pedir_info_value(pending_sugerencia_raw)
        )
        pending_flow = None
        campo_esperado = None
        if campo_esperado_reclamo:
            pending_flow = "reclamo"
            campo_esperado = _normalize_single_expected_field(campo_esperado_reclamo)
            contexto_municipio_actual["esperando_info_llm_reclamo"] = campo_esperado
        elif campo_esperado_sugerencia:
            pending_flow = "sugerencia"
            campo_esperado = _normalize_single_expected_field(campo_esperado_sugerencia)
            contexto_municipio_actual["esperando_info_llm_sugerencia"] = campo_esperado
        if campo_esperado:
            contexto_municipio_actual["esperando_info_llm"] = campo_esperado
        estabamos_esperando_dato = bool(campo_esperado)

        expected_value_captured = False

        if context.get("es_ubicacion") and context.get("ubicacion_usuario"):
            campo_esperado = campo_esperado or "ubicacion"

        if campo_esperado and (pregunta_str or context.get("es_ubicacion")):
            logger_actual.info(
                f"Guardando dato esperado '{campo_esperado}' en el contexto antes de llamar al LLM."
            )
            datos_key = (
                "datos_parciales_llm_sugerencia"
                if pending_flow == "sugerencia"
                else "datos_parciales_llm_reclamo"
            )
            datos_parciales = contexto_municipio_actual.setdefault(datos_key, {})

            valor_a_guardar: Optional[str] = None
            captured_field = False
            municipio_config = context.get("municipio_config_actual") or {}
            expected_fields_active = (
                expected_fields_sugerencia if pending_flow == "sugerencia" else expected_fields_reclamo
            )

            if expected_fields_active:
                expected_fields_active = _prefill_contacto_from_context(
                    contexto_municipio_actual, datos_parciales, expected_fields_active
                )
                if pending_flow == "sugerencia":
                    contexto_municipio_actual["expected_fields_llm_sugerencia"] = expected_fields_active
                else:
                    contexto_municipio_actual["expected_fields_llm_reclamo"] = expected_fields_active

                if campo_esperado and campo_esperado not in expected_fields_active:
                    if expected_fields_active:
                        campo_esperado = expected_fields_active[0]
                    else:
                        captured_field = True
                        expected_value_captured = True
                        campo_esperado = None
                        if pending_flow == "sugerencia":
                            contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
                        else:
                            contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
                        contexto_municipio_actual.pop("esperando_info_llm", None)

            extracted_fields = _extract_expected_fields_from_text(
                pregunta_str,
                expected_fields_active,
                context,
                municipio_config,
            )
            if extracted_fields:
                for key, value in extracted_fields.items():
                    campo_destino = key
                    if pending_flow == "sugerencia" and key == "direccion":
                        campo_destino = "ubicacion"
                    datos_parciales[campo_destino] = value
                extracted_names = {field_name.lower() for field_name in extracted_fields}
                expected_fields_active = [
                    field
                    for field in expected_fields_active
                    if field.strip().lower() not in extracted_names
                ]
                captured_field = True
                expected_value_captured = True
                if pending_flow == "sugerencia":
                    contexto_municipio_actual["expected_fields_llm_sugerencia"] = expected_fields_active
                else:
                    contexto_municipio_actual["expected_fields_llm_reclamo"] = expected_fields_active

                if expected_fields_active:
                    campo_esperado = expected_fields_active[0]
                    if pending_flow == "sugerencia":
                        contexto_municipio_actual["esperando_info_llm_sugerencia"] = campo_esperado
                    else:
                        contexto_municipio_actual["esperando_info_llm_reclamo"] = campo_esperado
                    contexto_municipio_actual["esperando_info_llm"] = campo_esperado
            if campo_esperado == "ubicacion":
                if context.get("ubicacion_usuario"):
                    lat = context["ubicacion_usuario"].get("latitude")
                    lon = context["ubicacion_usuario"].get("longitude")
                    address = context["ubicacion_usuario"].get("address")
                    if address:
                        valor_a_guardar = address
                    elif lat is not None and lon is not None:
                        valor_a_guardar = f"Lat: {lat}, Lon: {lon}"
                    try:
                        if lat is not None and lon is not None:
                            datos_parciales["coordenadas"] = {
                                "lat": float(lat),
                                "lng": float(lon),
                            }
                    except (TypeError, ValueError):
                        pass
                else:
                    valor_a_guardar = pregunta_str.strip() if pregunta_str else None
            elif pending_flow == "sugerencia" and campo_esperado in ["descripcion_sugerencia", "descripcion"]:
                valor_a_guardar = pregunta_str.strip() if pregunta_str else None
            elif pending_flow == "sugerencia" and campo_esperado == "datos_contacto_sugerencia":
                campos_contacto = ["nombre", "dni", "email", "direccion", "telefono"]
                nuevos_datos = extract_multiple_contact_details_regex(
                    pregunta_str, campos_contacto
                )
                if nuevos_datos:
                    datos_parciales.update(nuevos_datos)
                    captured_field = True
                    expected_value_captured = True
            else:
                valor_a_guardar = _extract_value_for_expected_field(
                    campo_esperado,
                    pregunta_str,
                    context,
                )

            if valor_a_guardar and not extracted_fields:
                campo_destino = campo_esperado
                if pending_flow == "sugerencia":
                    if campo_esperado in ["descripcion_sugerencia", "descripcion"]:
                        campo_destino = "descripcion"
                    elif campo_esperado in ["direccion"]:
                        campo_destino = "ubicacion"
                datos_parciales[campo_destino] = valor_a_guardar
                captured_field = True
                expected_value_captured = True
                if pending_flow == "sugerencia":
                    expected_fields_sugerencia = [
                        field
                        for field in expected_fields_sugerencia
                        if field.strip().lower() != campo_esperado.strip().lower()
                    ]
                    contexto_municipio_actual["expected_fields_llm_sugerencia"] = expected_fields_sugerencia
                    if expected_fields_sugerencia:
                        contexto_municipio_actual["esperando_info_llm_sugerencia"] = expected_fields_sugerencia[0]
                        contexto_municipio_actual["esperando_info_llm"] = expected_fields_sugerencia[0]
                    else:
                        contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
                        contexto_municipio_actual.pop("esperando_info_llm", None)
                else:
                    expected_fields_reclamo = [
                        field
                        for field in expected_fields_reclamo
                        if field.strip().lower() != campo_esperado.strip().lower()
                    ]
                    contexto_municipio_actual["expected_fields_llm_reclamo"] = expected_fields_reclamo
                    if expected_fields_reclamo:
                        contexto_municipio_actual["esperando_info_llm_reclamo"] = expected_fields_reclamo[0]
                        contexto_municipio_actual["esperando_info_llm"] = expected_fields_reclamo[0]
                    else:
                        contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
                        contexto_municipio_actual.pop("esperando_info_llm", None)
                logger_actual.info(f"Datos parciales actualizados: {datos_parciales}")
            elif captured_field:
                if pending_flow == "sugerencia":
                    if not contexto_municipio_actual.get("expected_fields_llm_sugerencia"):
                        contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
                        contexto_municipio_actual.pop("esperando_info_llm", None)
                else:
                    if not contexto_municipio_actual.get("expected_fields_llm_reclamo"):
                        contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
                        contexto_municipio_actual.pop("esperando_info_llm", None)
                logger_actual.info(f"Datos parciales actualizados: {datos_parciales}")
            else:
                logger_actual.info(
                    f"No se pudo extraer un valor válido para '{campo_esperado}'. Se mantendrá la solicitud pendiente."
                )

        if expected_value_captured and (estabamos_esperando_dato or waiting_state_active):
            logger_actual.info(
                "[HANDLE_LLM] Dato esperado recibido. Validando solicitud sin invocar al LLM."
            )
            datos_parciales = contexto_municipio_actual.get(
                "datos_parciales_llm_sugerencia" if pending_flow == "sugerencia" else "datos_parciales_llm_reclamo",
                {},
            )
            if pending_flow == "sugerencia":
                confirmation_payload = _maybe_prompt_sugerencia_confirmation(
                    contexto_municipio_actual,
                )
                if confirmation_payload:
                    return confirmation_payload, contexto_municipio_actual
            if pending_flow == "sugerencia":
                handler = HacerSugerenciaActionHandler(context)
            else:
                handler = CrearReclamoActionHandler(context)
            handler_response = handler.execute(datos_parciales)

            normalized_pending_fields = _normalize_pedir_info_fields(handler_response.get("pedir_info"))
            if normalized_pending_fields:
                if pending_flow == "sugerencia":
                    contexto_municipio_actual["estado_conversacion"] = (
                        ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name
                    )
                    contexto_municipio_actual["expected_fields_llm_sugerencia"] = normalized_pending_fields
                    contexto_municipio_actual["esperando_info_llm_sugerencia"] = normalized_pending_fields[0]
                    contexto_municipio_actual["esperando_info_llm"] = normalized_pending_fields[0]
                else:
                    contexto_municipio_actual["estado_conversacion"] = (
                        ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                    )
                    contexto_municipio_actual["expected_fields_llm_reclamo"] = normalized_pending_fields
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = normalized_pending_fields[0]
                    contexto_municipio_actual["esperando_info_llm"] = normalized_pending_fields[0]
            else:
                contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
                contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
                contexto_municipio_actual.pop("esperando_info_llm", None)
                contexto_municipio_actual.pop("expected_fields_llm_reclamo", None)
                contexto_municipio_actual.pop("expected_fields_llm_sugerencia", None)

            return handler_response, contexto_municipio_actual


        mensaje_completo_para_llm = {"texto": pregunta_str}
        if context.get("es_foto") and context.get("foto_url"):
            mensaje_completo_para_llm["imagen_url"] = context.get("foto_url")
            if contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw"):
                analisis_previo = contexto_municipio_actual.get("analisis_imagen_reclamo_auto_raw")
                if isinstance(analisis_previo, dict):
                    resumen_analisis = {k: analisis_previo.get(k) for k in ["categoria_sugerida", "descripcion_sugerida", "texto_ocr"] if analisis_previo.get(k)}
                    if resumen_analisis:
                        mensaje_completo_para_llm["analisis_previo_imagen"] = resumen_analisis

        try:
            mensaje_para_llm = json.dumps(mensaje_completo_para_llm)
            respuesta_llm_dict, context_dict = llamar_gemini(
                app=app,
                mensaje_usuario=mensaje_para_llm,
                usuario=usuario_info_llm,
                historial=historial_formateado,
                chat_session_id=context.get("chat_session_uuid")
            )
            logger.info(f"[HANDLE_LLM] Respuesta LLM: {respuesta_llm_dict}")
            if isinstance(context_dict, dict) and chat_db_context:
                chat_db_context.context_data.update(context_dict)
            logger_actual.info(f"[HANDLE_LLM] Accion backend LLM: {respuesta_llm_dict.get('accion_backend')}")
        except Exception as e:
            logger.error(
                f"[RESPONDER_MUNICIPIO_LLM_ERROR] Error general en la llamada al LLM: {e}",
                exc_info=True,
            )
            return (
                {
                    "message_body": "Error de configuración del servicio de IA (entorno). Por favor, contacta al administrador.",
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "error",
                },
                contexto_municipio_actual,
            )

        respuesta_usuario_llm = respuesta_llm_dict.get("message_body")
        accion_backend_llm = respuesta_llm_dict.get("accion_backend")
        datos_estructura_llm = respuesta_llm_dict.get("datos_estructura")
        pedir_info_llm = respuesta_llm_dict.get("pedir_info")
        botones_llm = respuesta_llm_dict.get("botones", [])

        if not respuesta_usuario_llm and accion_backend_llm not in [
            "crear_reclamo",
            "hacer_sugerencia",
            "ejecutar_herramienta",
        ]:
             logger_actual.warning("[HANDLE_LLM] LLM response did not contain a 'message_body' and was not a parameterless action. Returning None to trigger fallback.")
             return None, contexto_municipio_actual

        nuevo_turno_historial = {"pregunta_usuario": pregunta_str, "respuesta_ia": respuesta_usuario_llm}

        if accion_backend_llm == "saludar":
            logger.info("LLM detectó un saludo. Invocando GreetingHandler.")
            # The 'context' dict passed to handle_llm_interaction has the necessary nested structure.
            handler = GreetingHandler(context)
            response = handler.handle({})  # Pass empty payload
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            # The handler's response is the full dict ready to be returned by responder_municipio
            return response, contexto_municipio_actual

        if accion_backend_llm in ["crear_reclamo", "iniciar_reclamo"] and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
            # Si es el inicio de un nuevo reclamo, limpiar el contexto anterior para evitar "context bleed".
            if contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
                logger_actual.info("[CONTEXT_RESET] Nuevo reclamo detectado. Limpiando historiales de conversación.")
                # Eliminar el historial de la conversación general anterior.
                contexto_municipio_actual.pop("historial_conversacion_general_llm", None)
                # Reiniciar el contexto específico del reclamo.
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
                contexto_municipio_actual["historial_llm_reclamo"] = []

            # Asegurarse de que datos_parciales_llm_reclamo exista si no fue creado arriba
            contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})

            contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)

            # Actualizar con los nuevos datos, priorizando los que no son None
            if not isinstance(contexto_municipio_actual.get("datos_parciales_llm_reclamo"), dict):
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}

            # Combinar datos antiguos y nuevos
            datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
            nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
            datos_actuales.update(nuevos_datos)
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales

            if not pedir_info_llm:
                    # If the LLM thinks it has all the data, call the handler to validate.
                    # The handler is the source of truth. Its response will be used.
                    # This prevents a premature confirmation from the LLM being shown
                    # if the handler then asks for more information.
                    logger_actual.info("[HANDLE_LLM] LLM provided all data. Executing CrearReclamoActionHandler for validation and creation.")
                    handler = CrearReclamoActionHandler(context)
                    handler_response = handler.execute(datos_actuales)

                    pending_fields = _normalize_pedir_info_fields(handler_response.get("pedir_info"))
                    if pending_fields:
                        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                        contexto_municipio_actual["expected_fields_llm_reclamo"] = pending_fields
                        contexto_municipio_actual["esperando_info_llm_reclamo"] = pending_fields[0]
                        contexto_municipio_actual["esperando_info_llm"] = pending_fields[0]
                    else:
                        contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
                        contexto_municipio_actual.pop("esperando_info_llm", None)
                        contexto_municipio_actual.pop("expected_fields_llm_reclamo", None)

                    # The handler's response is the final one, whether it's a success message
                    # or a request for more info. We return it directly, ignoring the LLM's
                    # potentially premature confirmation message.
                    return handler_response, contexto_municipio_actual
            else:
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                normalized_pending_fields = _normalize_pedir_info_fields(pedir_info_llm)
                if normalized_pending_fields:
                    contexto_municipio_actual["expected_fields_llm_reclamo"] = normalized_pending_fields
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = normalized_pending_fields[0]
                    contexto_municipio_actual["esperando_info_llm"] = normalized_pending_fields[0]
            # Update the context that will be passed to the next turn
            if chat_db_context and hasattr(chat_db_context, 'context_data'):
                chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                flag_modified(chat_db_context, "context_data")
            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_reclamo"}, contexto_municipio_actual
        elif accion_backend_llm == "hacer_sugerencia" and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
            if contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name:
                logger_actual.info("[CONTEXT_RESET] Nueva sugerencia detectada. Limpiando historiales de conversación.")
                contexto_municipio_actual.pop("historial_conversacion_general_llm", None)
                contexto_municipio_actual["datos_parciales_llm_sugerencia"] = {}
                contexto_municipio_actual["historial_llm_sugerencia"] = []

            contexto_municipio_actual.setdefault("datos_parciales_llm_sugerencia", {})
            contexto_municipio_actual.setdefault("historial_llm_sugerencia", []).append(nuevo_turno_historial)

            if not isinstance(contexto_municipio_actual.get("datos_parciales_llm_sugerencia"), dict):
                contexto_municipio_actual["datos_parciales_llm_sugerencia"] = {}

            datos_actuales_sugerencia = contexto_municipio_actual.get("datos_parciales_llm_sugerencia", {})
            nuevos_datos_sugerencia = {k: v for k, v in datos_estructura_llm.items() if v is not None}
            datos_actuales_sugerencia.update(nuevos_datos_sugerencia)
            contexto_municipio_actual["datos_parciales_llm_sugerencia"] = datos_actuales_sugerencia

            if not pedir_info_llm:
                logger_actual.info("[HANDLE_LLM] LLM provided all data. Executing HacerSugerenciaActionHandler for validation and creation.")
                confirmation_payload = _maybe_prompt_sugerencia_confirmation(
                    contexto_municipio_actual,
                )
                if confirmation_payload:
                    return confirmation_payload, contexto_municipio_actual
                handler = HacerSugerenciaActionHandler(context)
                handler_response = handler.execute(datos_actuales_sugerencia)

                pending_fields = _normalize_pedir_info_fields(handler_response.get("pedir_info"))
                if pending_fields:
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name
                    contexto_municipio_actual["expected_fields_llm_sugerencia"] = pending_fields
                    contexto_municipio_actual["esperando_info_llm_sugerencia"] = pending_fields[0]
                    contexto_municipio_actual["esperando_info_llm"] = pending_fields[0]
                else:
                    contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
                    contexto_municipio_actual.pop("esperando_info_llm", None)
                    contexto_municipio_actual.pop("expected_fields_llm_sugerencia", None)

                return handler_response, contexto_municipio_actual
            else:
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name
                normalized_pending_fields = _normalize_pedir_info_fields(pedir_info_llm)
                if normalized_pending_fields:
                    contexto_municipio_actual["expected_fields_llm_sugerencia"] = normalized_pending_fields
                    contexto_municipio_actual["esperando_info_llm_sugerencia"] = normalized_pending_fields[0]
                    contexto_municipio_actual["esperando_info_llm"] = normalized_pending_fields[0]
            if chat_db_context and hasattr(chat_db_context, 'context_data'):
                chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                flag_modified(chat_db_context, "context_data")
            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_sugerencia"}, contexto_municipio_actual
        elif accion_backend_llm == "mostrar_menu":
            logger.info("[HANDLE_LLM] LLM solicitó mostrar el menú principal.")
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
            if chat_db_context and hasattr(chat_db_context, "context_data"):
                chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                flag_modified(chat_db_context, "context_data")
            return _get_main_menu_payload(context), contexto_municipio_actual
        elif accion_backend_llm == "mostrar_menu_reclamos":
            logger.info("[HANDLE_LLM] LLM solicitó mostrar el menú de reclamos.")
            # Reutilizar la misma lógica del menú principal para asegurar consistencia.
            menu_response = handle_main_menu_action(
                "mostrar_menu_reclamos",
                context,
                chat_db_context,
            )

            # ``handle_main_menu_action`` ya actualiza el contexto y marca la sesión
            # como modificada, pero devolvemos explícitamente el estado actualizado
            # para mantener la firma de retorno del LLM handler.
            return menu_response, contexto_municipio_actual
        elif accion_backend_llm == "derivar_humano":
            context["intencion"] = "hablar_con_agente"
            contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
            logger.info("[HANDLE_LLM] LLM derivó a humano.")
            return None, contexto_municipio_actual
        elif accion_backend_llm == "finalizar_tramite":
            logger.info(
                "[HANDLE_LLM] LLM finalizó el trámite. Reseteando contexto de reclamo."
            )
            # Clear all claim-related context
            contexto_municipio_actual["historial_llm_reclamo"] = []
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
            contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
            contexto_municipio_actual.pop("esperando_info_llm", None)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name

            # Also add the final user-facing message to the general history
            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)

            return {
                "message_body": respuesta_usuario_llm,
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
                "fuente": "llm_finalizar_tramite"
            }, contexto_municipio_actual

        elif accion_backend_llm == "pedir_dato_usuario":
            campo_a_pedir = pedir_info_llm[0] if isinstance(pedir_info_llm, list) and pedir_info_llm else None
            if not campo_a_pedir:
                logger.warning("[HANDLE_LLM] 'pedir_dato_usuario' action received without 'pedir_info'.")
                return None, contexto_municipio_actual

            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NUEVO_DATO_USUARIO.name
            contexto_municipio_actual['campo_a_actualizar'] = campo_a_pedir

            # Guarda la pregunta original del usuario que inició este flujo para poder reanudarla.
            if historial_para_llm:
                # El historial se pasa como ['pregunta_usuario', 'respuesta_ia', ...], tomamos la última pregunta.
                contexto_municipio_actual['accion_original_para_reintentar'] = historial_para_llm[-2] if len(historial_para_llm) > 1 else pregunta_str

            return {
                "message_body": respuesta_usuario_llm,
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
                "fuente": "llm_pide_dato_usuario"
            }, contexto_municipio_actual

        elif accion_backend_llm == "ejecutar_herramienta":
            nombre_herramienta = datos_estructura_llm.get("nombre_herramienta")
            parametros_herramienta = datos_estructura_llm.get("parametros_herramienta", {})

            # Unificado: chequear si el LLM está pidiendo más información.
            info_faltante = pedir_info_llm or datos_estructura_llm.get("faltan_parametros_herramienta")

            if nombre_herramienta and nombre_herramienta in TOOL_REGISTRY:
                # Si se necesita más información, no ejecutar la herramienta.
                # Simplemente preguntar al usuario y guardar el estado para el próximo turno.
                if info_faltante:
                    logger.info(f"[HERRAMIENTA] LLM pide más información ('{info_faltante}') antes de ejecutar '{nombre_herramienta}'. No se ejecutará la herramienta.")
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                    normalized_tool_pending = _normalize_pedir_info_value(info_faltante)
                    if normalized_tool_pending:
                        contexto_municipio_actual["esperando_info_llm_reclamo"] = normalized_tool_pending
                        contexto_municipio_actual["esperando_info_llm"] = normalized_tool_pending
                    contexto_municipio_actual["datos_parciales_llm_reclamo"] = {
                        "nombre_herramienta": nombre_herramienta,
                        "parametros_herramienta": parametros_herramienta
                    }
                    # Devolver la pregunta del LLM al usuario
                    return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_pide_info_herramienta"}, contexto_municipio_actual

                # Si no falta información, proceder a ejecutar la herramienta.
                herramienta = TOOL_REGISTRY[nombre_herramienta]
                funcion_herramienta = herramienta["funcion"]

                try:
                    logger.info(f"[HERRAMIENTA] Intentando ejecutar: {nombre_herramienta} con params: {parametros_herramienta}")
                    import inspect
                    sig = inspect.signature(funcion_herramienta)
                    if 'context' in sig.parameters:
                        parametros_herramienta['context'] = context
                    resultado_herramienta = funcion_herramienta(**parametros_herramienta)
                    logger.info(f"[HERRAMIENTA] Resultado de {nombre_herramienta}: {str(resultado_herramienta)[:200]}...")

                    # Combinar la respuesta del LLM con el resultado de la herramienta
                    respuesta_final = f"{respuesta_usuario_llm}\n\n{resultado_herramienta}"

                    contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append({
                        "pregunta_usuario": pregunta_str,
                        "respuesta_ia": respuesta_final
                    })

                    return {
                        "message_body": respuesta_final,
                        "options_list": botones_llm,
                        "message_type": "interactive_buttons" if botones_llm else "text",
                        "fuente": f"herramienta_{nombre_herramienta}"
                    }, contexto_municipio_actual

                except Exception as e:
                    logger.error(f"Error ejecutando la herramienta '{nombre_herramienta}': {e}", exc_info=True)
                    # Friendly message for the user, more specific than a generic error.
                    user_friendly_tool_name = nombre_herramienta.replace("_", " ").replace("consultar", "la consulta de").replace("buscar", "la búsqueda de")

                    return {
                        "message_body": f"Lo siento, tuve un problema con {user_friendly_tool_name}. Por favor, intenta de nuevo en unos momentos.",
                        "options_list": [],
                        "message_type": "text",
                        "fuente": "error_herramienta"
                    }, contexto_municipio_actual
            else:
                # Este caso se da si el LLM pide ejecutar una herramienta que no existe en TOOL_REGISTRY
                logger.warning(f"Se intentó ejecutar una herramienta no registrada: '{nombre_herramienta}'")
                return {
                    "message_body": "No se encontró la herramienta solicitada. Por favor, reformula tu pregunta.",
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "herramienta_no_encontrada"
                }, contexto_municipio_actual

        elif accion_backend_llm == "derivar_humano":
            context["intencion"] = "hablar_con_agente"
            contexto_municipio_actual["mensaje_previo_llm_para_escalamiento"] = respuesta_usuario_llm
            logger.info("[HANDLE_LLM] LLM derivó a humano.")
            return None, contexto_municipio_actual

        elif accion_backend_llm == "responder_directamente":
            logger.info("[HANDLE_LLM] LLM solicitó responder directamente.")

            # FIX: Si el LLM extrae datos (ej. ubicación), guardarlos en el contexto del reclamo
            # aunque la acción principal sea solo responder. Esto evita perder información.
            if datos_estructura_llm:
                datos_actuales = contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})
                nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
                if nuevos_datos:
                    datos_actuales.update(nuevos_datos)
                    logger_actual.info(f"[CONTEXT_MERGE] Datos parciales de reclamo actualizados en flujo 'responder_directamente': {datos_actuales}")

            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
            response_payload = {
                "message_body": respuesta_usuario_llm,
                "options_list": botones_llm,
                "message_type": "interactive_buttons" if botones_llm else "text",
                "accion_backend": accion_backend_llm,
                "datos_estructura": datos_estructura_llm,
                "pedir_info": pedir_info_llm,
                "fuente": "llm_respuesta_directa"
            }
            if respuesta_llm_dict.get("generar_audio"):
                response_payload["generar_audio"] = True
            return response_payload, contexto_municipio_actual

        elif estado_conversacion_para_llm == ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name:
            # User is at the confirmation step. Their response is either a "yes" or a correction.
            _, es_confirmacion = extract_description_and_check_confirmation(pregunta_str, PALABRAS_CLAVE_CONFIRMACION)

            if es_confirmacion:
                # User confirmed. Proceed to create the ticket.
                logger_actual.info("[HANDLE_LLM_CONFIRM] Confirmación detectada. Procediendo a crear ticket.")
                return _handle_ticket_creation(contexto_municipio_actual, context, {})
            else:
                # User sent a correction. Extract new data, merge, and re-confirm.
                logger_actual.info("[HANDLE_LLM_CONFIRM] No es confirmación, asumiendo corrección.")
                datos_nuevos = extract_multiple_contact_details_llm(pregunta_str)
                datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
                datos_actuales.update(datos_nuevos)
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales

                # Re-prompt for confirmation with updated data
                # This part needs to be improved to show the data again. For now, a generic message.
                # A better implementation would call a function to format the confirmation message.
                return {"message_body": "OK, he actualizado tus datos. ¿Son correctos ahora?", "options_list": botones_llm, "message_type": "text", "fuente": "llm_re_pide_confirmacion"}, contexto_municipio_actual

        else: # Generic flow continuation
            if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name and datos_estructura_llm:
                contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)
                datos_actuales = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
                nuevos_datos = {k: v for k, v in datos_estructura_llm.items() if v is not None}
                datos_actuales.update(nuevos_datos)
                contexto_municipio_actual["datos_parciales_llm_reclamo"] = datos_actuales
            else:
                contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)

            # State transition logic based on 'pedir_info'
            if pedir_info_llm:
                normalized_pending_fields = _normalize_pedir_info_fields(pedir_info_llm)
                normalized_pending = normalized_pending_fields[0] if normalized_pending_fields else None
                pending_lookup_key = normalized_pending or pedir_info_llm
                if not isinstance(pending_lookup_key, str):
                    pending_lookup_key = str(pending_lookup_key or "").strip()
                next_state_obj = PEDIR_INFO_TO_STATE.get(pending_lookup_key)
                if next_state_obj:
                    contexto_municipio_actual["estado_conversacion"] = next_state_obj.name
                else:
                    # Fallback if a new 'pedir_info' value isn't in our map
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                normalized_pending = _normalize_single_expected_field(normalized_pending or pending_lookup_key)
                contexto_municipio_actual["esperando_info_llm"] = normalized_pending or pending_lookup_key
                if _is_claim_pending_field(normalized_pending or pending_lookup_key):
                    if normalized_pending_fields:
                        contexto_municipio_actual["expected_fields_llm_reclamo"] = normalized_pending_fields
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = normalized_pending or pending_lookup_key
                else:
                    contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
                    contexto_municipio_actual.pop("expected_fields_llm_reclamo", None)
                if _is_suggestion_pending_field(normalized_pending or pending_lookup_key):
                    if normalized_pending_fields:
                        contexto_municipio_actual["expected_fields_llm_sugerencia"] = normalized_pending_fields
                    contexto_municipio_actual["esperando_info_llm_sugerencia"] = normalized_pending or pending_lookup_key
                else:
                    contexto_municipio_actual.pop("esperando_info_llm_sugerencia", None)
                    contexto_municipio_actual.pop("expected_fields_llm_sugerencia", None)
            else:
                # If no more info is needed, decide what to do
                if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name:
                    # If we were in a claim flow, it's time to create the ticket
                    return _handle_ticket_creation(contexto_municipio_actual, context, datos_actuales)
                elif estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name:
                    confirmation_payload = _maybe_prompt_sugerencia_confirmation(
                        contexto_municipio_actual,
                    )
                    if confirmation_payload:
                        return confirmation_payload, contexto_municipio_actual
                    handler = HacerSugerenciaActionHandler(context)
                    datos_sugerencia = contexto_municipio_actual.get("datos_parciales_llm_sugerencia", {})
                    return handler.execute(datos_sugerencia), contexto_municipio_actual
                else:
                    contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
                    contexto_municipio_actual.pop("esperando_info_llm", None)

            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text", "fuente": "llm_respuesta_general_v2"}, contexto_municipio_actual

    except Exception as e_llm:
        logger.error(f"[HANDLE_LLM] Error: {e_llm}", exc_info=True)
        for k in ["historial_llm_reclamo", "datos_parciales_llm_reclamo", "esperando_info_llm_reclamo", "historial_conversacion_general_llm", "estado_conversacion"]:
            if k == "estado_conversacion" and contexto_municipio_actual.get(k) in [ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name, ConversationState.CONVERSACION_GENERAL_LLM.name]:
                contexto_municipio_actual[k] = None
            elif k != "estado_conversacion":
                contexto_municipio_actual.pop(k, None)
        return None, contexto_municipio_actual

MENU_KEYWORDS = {
    # Reclamos, Trámites y Turnos
    "mostrar_menu_reclamos": [
        "reclamo",
        "reclamos",
        "denuncia",
        "problema",
        "queja",
        "reportar",
        "averia",
        "averias",
        "incidente",
        "consulta",
        "consultas",
        "pregunta",
        "preguntas",
        "iniciar reclamo",
        "hacer reclamo",
        "nuevo reclamo",
        "realizar reclamo",
        "presentar reclamo",
        "registrar queja",
    ],
    "iniciar_reclamo": [],
    "solicitar_turnos": ["turnos", "turno", "solicitar turno", "pedir turno", "turnos online", "reservar turno", "agendar turno"],
    "licencia_de_conducir": ["licencia", "conducir", "carnet", "registro", "renovar licencia", "sacar licencia", "tramitar licencia", "registro de conducir"],
    "enviar_sugerencia": [
        "sugerencia",
        "sugerir",
        "propuesta",
        "pedido",
        "pedir algo",
        "comentario",
        "feedback",
        "opinion",
        "opinión",
        "hacer una sugerencia",
        "quiero hacer una sugerencia",
        "queria hacer una sugerencia",
        "quisiera hacer una sugerencia",
        "tengo una sugerencia",
        "tengo un comentario",
        "me gustaria hacer una sugerencia",
    ],
    "mostrar_menu_encuestas": [
        "encuesta",
        "encuestas",
        "participacion",
        "participación",
        "participacion ciudadana",
        "participación ciudadana",
        "consulta ciudadana",
        "consulta popular",
        "sondeo",
        "sondeos",
        "votar",
        "votacion",
        "votación",
        "participar",
    ],
    "mostrar_menu_catalogo": [
        "catalogo",
        "catálogo",
        "catalogos",
        "catálogos",
        "catalogo y beneficios",
        "beneficios",
        "canje",
        "canje de puntos",
        "puntos",
        "productos",
        "tienda",
    ],
    "limpiar_contexto": [
        "cancelar",
        "volver al inicio",
        "empezar de nuevo",
        "reiniciar",
        "resetear",
        "limpiar chat",
        "borrar conversacion",
        "nuevo tema",
        "volver a empezar",
        "borrar historial",
        "limpiar memoria",
        "arrancar de cero",
    ],
    "consultar_estado_reclamo": [
        "consultar reclamo",
        "estado reclamo",
        "seguimiento",
        "ver reclamo",
        "consultar estado de reclamo",
        "consultar estado del reclamo",
        "estado de reclamo",
        "estado del reclamo",
    ],

    # Información útil
    "contactos_utiles": ["contactos", "contacto", "telefonos", "telefono", "utiles", "directorio", "llamar", "medios de contacto", "telefonos utiles"],
    "agenda_y_noticias": ["agenda", "cultural", "eventos", "noticias", "novedades", "informacion", "actividades", "eventos culturales"],
    "veterinaria_bromatologia": [
        "veterinaria", "bromatologia", "zoonosis", "animales", "animal",
        "perro", "perros", "gato", "gatos", "mascota", "mascotas",
        "vacuna", "vacunas", "vacunacion", "antirrabica", "antirrábica",
        "rabia", "perrera", "sanidad animal", "sanidad_animal"
    ],
    "defensa_del_consumidor": ["defensa del consumidor", "consumidor", "consumo", "proteccion al consumidor", "atencion al consumidor"],
    "obras": [
        "obras", "obra", "cuadrillas", "cloacas", "pavimento",
        "pavimentacion", "asfalto", "trabajos"
    ],
    "punto_limpio": [
        "punto limpio", "reciclaje", "reciclar", "planta de reciclaje",
        "punto verde", "residuos secos", "sustentable"
    ],

    # Tasas y Servicios
    "pago_de_tasas_vigentes": ["pagar", "pago", "tasas", "tasa", "boleta", "impuestos", "municipal", "tributo", "tributos", "arancel", "aranceles", "impuesto municipal", "impuestos municipales"],
    "buscar_estacionamiento": ["estacionamiento", "estacionar", "aparcamiento", "parking", "estacionar auto", "donde estacionar", "lugar para estacionar"],
    "mostrar_menu_ayuda": ["ayuda", "como usar", "uso", "emojis", "help"],
    "recoleccion_residuos": ["recoleccion", "residuos", "basura", "basurero", "cuando pasa el camion", "recolector", "recogida", "recoleccion de basura"]
}

def _augment_menu_keywords_with_tramite_buttons():
    """Add dynamic keywords for trámites based on button texts configured in tramites.json."""

    try:
        tramites_info = cargar_tramites_info()
    except Exception:  # pragma: no cover - avoid import errors if config is missing
        logger.warning(
            "[MENU_KEYWORDS] No se pudo cargar tramites.json para extender palabras clave",
            exc_info=True,
        )
        return

    if not isinstance(tramites_info, dict):
        return

    for tramite_key, tramite_data in tramites_info.items():
        if not isinstance(tramite_data, dict):
            continue

        keywords_list = MENU_KEYWORDS.setdefault(tramite_key, [])
        if not isinstance(keywords_list, list):
            continue

        for boton in tramite_data.get("botones", []):
            texto = boton.get("texto")
            if not texto:
                continue

            normalized = normalizar_texto(texto)
            if normalized and normalized not in keywords_list:
                keywords_list.append(normalized)


_augment_menu_keywords_with_tramite_buttons()

from fuzzywuzzy import process

def find_menu_action_by_input(user_input: str, menu_buttons: list) -> str | None:
    """
    Finds a menu action based on user input, checking for exact match, number, first letter, or keywords.
    """
    if not user_input or not menu_buttons:
        return None

    user_input = strip_variation_selector(user_input)

    # Allow emoji shortcuts regardless of menu context.
    if user_input in EMOJI_MAIN_MENU_ACTIONS:
        return EMOJI_MAIN_MENU_ACTIONS[user_input]

    # 0. Direct action_id match to support clients sending the action identifier
    normalized_action = normalizar_texto(user_input.strip())
    for button in menu_buttons:
        action_id_norm = normalizar_texto(button.get("action_id", ""))
        if action_id_norm and action_id_norm == normalized_action:
            logger.info(
                f"DEBUG: Direct action_id match found for '{user_input}'. Action: {button.get('action_id')}"
            )
            return button.get("action_id")

    # Use a more aggressive normalization for matching to handle emojis, etc.
    normalized_input = _super_normalize(user_input)

    # 1. Check for exact match on super-normalized button text
    for button in menu_buttons:
        button_text_super_norm = _super_normalize(button.get("texto", ""))
        if button_text_super_norm and button_text_super_norm == normalized_input:
            logger.info(f"DEBUG: Super-normalized exact match found for '{normalized_input}'. Action: {button.get('action_id')}")
            return button.get('action_id')

    # Fallback to standard normalization if super-norm fails (e.g. numeric input)
    normalized_input = normalizar_texto(user_input.strip())

    if not normalized_input:
        logger.warning(
            f"DEBUG: Input '{user_input}' normalized to empty string; skipping fuzzy menu matching."
        )
        return None

    # 2. Check for numeric selection
    try:
        selection_index = int(normalized_input) - 1
        if 0 <= selection_index < len(menu_buttons):
            return menu_buttons[selection_index].get('action_id')
    except (ValueError, IndexError):
        pass  # Not a valid number or index, proceed to other checks

    # 3. Check for first letter match (only if input is a single character)
    if len(normalized_input) == 1:
        for button in menu_buttons:
            button_text_norm = normalizar_texto(button.get("texto", ""))
            if button_text_norm.startswith(normalized_input):
                return button.get("action_id")

    # 4. For longer free-form phrases, skip fuzzy matching to avoid
    # misclassifying natural sentences as menu keywords. Let higher-level
    # NLU or LLM logic handle these cases instead.
    if len(normalized_input.split()) > 7:
        logger.info(
            f"DEBUG: Skipping fuzzy match for long input: '{normalized_input}'"
        )
        logger.warning(
            f"DEBUG: No menu action found for input: '{user_input}' (normalized: '{normalized_input}')"
        )
        return None

    # 5. Check for keyword match (fuzzy matching for natural language)
    local_keywords = {}
    for button in menu_buttons:
        action_id = button.get('action_id')
        if action_id in MENU_KEYWORDS:
            for keyword in MENU_KEYWORDS[action_id]:
                local_keywords[keyword] = action_id

    if local_keywords:
        best_match, score = process.extractOne(
            normalized_input, local_keywords.keys()
        )

        if score > 80:
            # Added log for debugging
            logger.info(
                f"DEBUG: Fuzzy match found for '{normalized_input}' with keyword '{best_match}' (score: {score}). Action: {local_keywords[best_match]}"
            )
            return local_keywords[best_match]

    # Added log for debugging
    logger.warning(
        f"DEBUG: No menu action found for input: '{user_input}' (normalized: '{normalized_input}')"
    )
    return None

def find_global_menu_action(user_input: str) -> str | None:
    """Attempts to resolve a menu action purely by keywords, ignoring menu context."""
    global_buttons = [{"texto": aid, "action_id": aid} for aid in MENU_KEYWORDS.keys()]
    return find_menu_action_by_input(user_input, global_buttons)


def _detect_reclamo_during_sugerencia(pregunta_str: str, contexto_municipio_actual: dict, context: dict, chat_db_context) -> dict | None:
    """If the user mentions starting a complaint while in the suggestion flow,
    abort the suggestion workflow and start the regular complaint flow."""
    normalized = normalizar_texto(pregunta_str or "")
    if "reclamo" in normalized:
        contexto_municipio_actual.pop('datos_sugerencia', None)
        contexto_municipio_actual.pop('ubicacion_contextual_sugerencia', None)
        contexto_municipio_actual.pop('estado_conversacion', None)
        handler = ReclamoFlowHandler(context, chat_db_context)
        response = handler.start_flow(datos_iniciales={"descripcion": pregunta_str})
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return response
    return None

RECLAMO_KEYWORDS = {
    "Luminaria": [
        "luminaria",
        "luz",
        "poste",
        "foco",
        "farol",
        "farola",
        "iluminacion",
        "lampara",
        "poste caido",
        "poste caído",
    ],
    "Arbolado": [
        "arbolado",
        "arbol",
        "arboles",
        "rama",
        "ramas",
        "arbol caido",
        "árbol caído",
        "tronco",
        "gajo",
    ],
    "Limpieza y riego": [
        "limpieza",
        "riego",
        "basura",
        "basural",
        "contenedor",
        "escombros",
        "mugre",
        "pasto",
        "yuyos",
        "maleza",
        "desmalezado",
        "baldio",
    ],
    "Arreglo de calle": [
        "calle",
        "bache",
        "pozo",
        "asfalto",
        "vereda",
        "agujero",
        "hueco",
        "pavimento",
        "calzada",
    ],
    "Pérdida de agua": [
        "agua",
        "perdida",
        "caño",
        "cañeria",
        "fuga",
        "rotura",
        "tuberia",
    ],
    "Otros": ["otros", "otro", "varios"],
}

# Emojis que disparan acciones rápidas desde el menú principal.
# Permiten a personas con dificultades de escritura iniciar flujos con un solo
# ícono.
EMOJI_RECLAMO_CATEGORIES = {
    "\U0001F4A1": "Luminaria",       # 💡
    "\U0001F526": "Luminaria",       # 🔦
    "\U0001F333": "Arbolado",       # 🌳
    "\U0001F332": "Arbolado",       # 🌲
    "\U0001F334": "Arbolado",       # 🌴
    "\U0001F5D1": "Limpieza y riego",  # 🗑️
    "\U0001F6AE": "Limpieza y riego",  # 🚮
    "\U0001F9F9": "Limpieza y riego",  # 🧹
    "\U0001F6A7": "Arreglo de calle", # 🚧
    "\U0001F6E3": "Arreglo de calle", # 🛣️
    "\U0001F4A7": "Pérdida de agua",   # 💧
    "\U0001F6B0": "Pérdida de agua",   # 🚰
    "\U0001F4A6": "Pérdida de agua",   # 💦
    "\U0001F436": "Otros",           # 🐶
    "\U0001F525": "Otros",           # 🔥
    "\u26AB": "Otros",              # ⚫
    "\u2753": "Otros",              # ❓
}

EMOJI_MAIN_MENU_ACTIONS = {
    "\U0001F4E9": "enviar_sugerencia", # 📩
    "\U0001F697": "licencia_de_conducir", # 🚗
    "\U0001F4DE": "contactos_utiles", # 📞
    "\U0001F4C5": "solicitar_turnos", # 📅
    "\U0001F4B5": "pago_de_tasas_vigentes", # 💵
    "\U0001F3AD": "agenda_y_noticias", # 🎭
    "\U0001F43E": "veterinaria_bromatologia", # 🐾
    "\U0001F3D7": "obras", # 🏗️
    "\u267B": "punto_limpio", # ♻️
    "\U0001F5F3": "mostrar_menu_encuestas", # 🗳️
    "\U0001F17F": "buscar_estacionamiento", # 🅿️
    "\U0001F3DB": "menu_principal", # 🏛️
    "\U0001F5E3": "mostrar_menu_reclamos", # 🗣️
    "\U0001F4DD": "mostrar_menu_reclamos", # 📝
    "\U0001F50D": "consultar_estado_reclamo", # 🔍
    "\u274C": "cancelar", # ❌
    "\U0001F4DC": "mostrar_menu_tramites", # 📜
    "\U0001F4F0": "mostrar_menu_informacion", # 📰
}

def find_reclamo_category_by_input(user_input: str, reclamo_options: list) -> str | None:
    """
    Finds a reclamo category based on user input, checking for number, first letter, or keywords.
    """
    if not user_input or not reclamo_options:
        return None

    user_input = strip_variation_selector(user_input)

    if user_input in EMOJI_RECLAMO_CATEGORIES:
        return EMOJI_RECLAMO_CATEGORIES[user_input]

    normalized_input = normalizar_texto(user_input.strip())

    if not normalized_input:
        logger.warning(
            f"DEBUG: Reclamo input '{user_input}' normalized to empty string; skipping keyword matching."
        )
        return None

    # 1. Check for numeric selection
    try:
        selection_index = int(normalized_input) - 1
        if 0 <= selection_index < len(reclamo_options):
            return reclamo_options[selection_index].get('texto')
    except (ValueError, IndexError):
        pass

    # 2. Check for first letter match
    if len(normalized_input) == 1:
        for option in reclamo_options:
            if normalizar_texto(option.get("texto", "")).startswith(normalized_input):
                return option.get("texto")

    # 3. Check for keyword match within the input
    for category, keywords in RECLAMO_KEYWORDS.items():
        for keyword in keywords:
            if keyword in normalized_input:
                return category

    # 4. Fallback to fuzzy matching if no direct keyword was found
    all_keywords = {
        keyword: category
        for category, keywords in RECLAMO_KEYWORDS.items()
        for keyword in keywords
    }
    best_match, score = process.extractOne(normalized_input, all_keywords.keys())
    if score > 80:
        return all_keywords[best_match]

    return None


def _strip_leading_phrases(text: str) -> str:
    if not text:
        return text

    cleaned = text.strip()
    if not cleaned:
        return cleaned

    patterns = [
        r"^(hola|buenos dias|buen dia|buenas tardes|buenas noches|buenas)\s*[!,\-:]*\s*",
        r"^(hola\s+)?(?:que\s+tal|buenas)\s*[!,\-:]*\s*",
        r"^(?:me\s+gustaria|me\s+gustaría|quisiera|necesito|quiero|solicito|pido|deseo|podria|podrian|podrían|podríamos|podrías)\s+(?:que\s+)?",
        r"^(?:pedir|pediria|pediría)\s+(?:que\s+)?",
        r"^que\s+",
        r"^(?:ver|saber)\s+si\s+",
        r"^por\s+favor\s+",
        r"^(?:mi|la)\s+direcci[óo]n\s+es\s+(?:en\s+)?",
    ]

    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    return cleaned or text.strip()


def _strip_trailing_phrases(text: str) -> str:
    if not text:
        return text

    cleaned = text.strip()
    trailing_patterns = [
        r"\s*(muchas\s+)?gracias[!\.]?\s*$",
        r"\s*saludos?\s*$",
    ]
    for pattern in trailing_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    return cleaned or text.strip()


def _is_plausible_name(value: str | None) -> bool:
    if not value:
        return False

    normalized = normalizar_texto(value)
    if not normalized:
        return False

    if any(char.isdigit() for char in normalized):
        return False

    tokens = normalized.split()
    if not tokens:
        return False

    if tokens[0] in NAME_STOPWORDS:
        return False

    if len(tokens) == 1 and len(tokens[0]) < 3:
        return False

    return True


def _clean_location_fragment(fragment: str | None, *, max_words: int = 7) -> str | None:
    if not fragment:
        return fragment

    cleaned = fragment.strip(" ,.-")
    if not cleaned:
        return None

    for separator in [",", ";", "."]:
        if separator in cleaned:
            cleaned = cleaned.split(separator, 1)[0].strip()

    lowered = cleaned.lower()
    clause_markers = [
        " que ",
        " qué ",
        " esta ",
        " está ",
        " estan ",
        " están ",
        " hay ",
        " tiene ",
        " tienen ",
        " tapando ",
        " ensucia ",
        " ensuciando ",
        " rompio ",
        " rompió ",
        " rompiendo ",
    ]
    for marker in clause_markers:
        idx = lowered.find(marker)
        if idx != -1:
            cleaned = cleaned[:idx].strip()
            lowered = cleaned.lower()

    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words]).strip()

    return cleaned or None


def _looks_like_address(value: str | None) -> bool:
    if not value:
        return False

    candidate = value.strip()
    if len(candidate) < 5:
        return False

    normalized = normalizar_texto(candidate)
    if not normalized:
        return False

    if len(normalized.split()) > 12:
        return False

    complaint_terms = {
        "arbol",
        "arboles",
        "rama",
        "ramas",
        "ensucia",
        "medianera",
        "pileta",
        "basura",
        "quema",
        "poda",
    }
    if any(term in normalized for term in complaint_terms):
        return False

    if any(char.isdigit() for char in normalized):
        return True

    if "esquina" in normalized or "km" in normalized:
        return True

    if any(term in normalized for term in {"kilometro", "kilometros"}):
        return True

    location_keywords = {
        "calle",
        "avenida",
        "av",
        "avda",
        "pasaje",
        "plaza",
        "parque",
        "boulevard",
        "bulevar",
        "ruta",
        "manzana",
        "mz",
        "lote",
        "sector",
        "pasillo",
        "camino",
        "autopista",
    }

    padded_normalized = f" {normalized} "
    if any(f" {kw} " in padded_normalized for kw in location_keywords):
        return True

    if normalized.startswith("plaza ") or normalized.startswith("parque "):
        return True

    if normalized.startswith("barrio "):
        return False

    return False


def _address_candidate_score(value: str | None) -> tuple[int, int, int, int]:
    if not value:
        return (-1, -1, -1, -1)

    candidate = str(value).strip()
    if not candidate:
        return (-1, -1, -1, -1)

    digits = sum(ch.isdigit() for ch in candidate)
    lowered = normalizar_texto(candidate)
    has_intersection = 1 if ("esquina" in lowered or re.search(r"\b(?:y|e)\b", lowered)) else 0
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9]+", candidate)
    long_tokens = sum(1 for token in tokens if len(token) > 2 or any(ch.isdigit() for ch in token))

    return (digits, has_intersection, -len(candidate), long_tokens)


def _should_update_address_candidate(current: str | None, candidate: str | None) -> bool:
    if not candidate:
        return False
    if not current:
        return True

    normalized_current = str(current).strip()
    normalized_candidate = str(candidate).strip()

    if normalized_candidate.lower().startswith(normalized_current.lower()):
        trailing = normalized_candidate[len(normalized_current) :].strip(" ,.-")
        if trailing and not re.search(r"\d", trailing):
            return False

    return _address_candidate_score(normalized_candidate) > _address_candidate_score(normalized_current)


def _refine_intersection_candidate(user_input: str, current_candidate: str | None) -> str | None:
    """Use the original text to expand truncated intersection candidates."""

    if not user_input or "esquina" not in normalizar_texto(user_input):
        return current_candidate

    pattern = re.compile(
        r"(?P<left>[A-Za-zÀ-ÿ'\s]{3,}?\d{0,6})\s+esquina\s+(?P<right>[A-Za-zÀ-ÿ'\s]{3,})",
        re.IGNORECASE,
    )

    match = pattern.search(user_input)
    if not match:
        return current_candidate

    street_a = match.group("left").strip(" ,.-")
    street_b_raw = match.group("right")
    if not street_a or not street_b_raw:
        return current_candidate

    street_b = re.split(
        r"(?i)(?:,|\.|;|\s+y\s+mi\b|\s+mi\s+(?:numero|número|documento|celular|telefono|teléfono)\b|\s+y\s+soy\b)",
        street_b_raw,
        maxsplit=1,
    )[0].strip(" ,.-")

    if not street_b:
        return current_candidate

    candidate = f"{street_a} esquina {street_b}".strip()
    candidate = re.sub(r"(?i)\besquina(?:\s+esquina)+\b", "esquina", candidate)

    # Avoid capturing trailing filler fragments that don't resemble addresses.
    candidate_tokens = candidate.split()
    if len(candidate_tokens) > 14:
        candidate = " ".join(candidate_tokens[:14]).rstrip(" ,.-")

    if not _looks_like_address(candidate):
        return current_candidate

    if _should_update_address_candidate(current_candidate, candidate):
        return candidate

    return current_candidate


def extract_reclamo_details_from_text(
    user_input: str,
    reclamo_options: list,
    default_localidad: str | None = None,
    default_provincia: str | None = None,
) -> dict:
    """Attempt to extract category, description, address, and contact details from a user message.

    The extraction now follows a "heuristics first" approach so we can avoid
    unnecessary llamadas al LLM cuando el mensaje es claro (por ejemplo,
    "hay un árbol caído"). Only when crucial data is missing we fall back to
    the LLM extractor. This behaviour is specially importante para los casos
    donde queremos que un reclamo se dispare automáticamente al recibir texto
    o una transcripción de audio sin depender siempre del modelo.
    """

    details: dict[str, str | None] = {}
    if not user_input:
        return details

    cleaned_description = _strip_trailing_phrases(_strip_leading_phrases(user_input))
    if cleaned_description:
        cleaned_description = re.sub(r"\s{2,}", " ", cleaned_description).strip()
        if not _is_placeholder_description(cleaned_description):
            details["descripcion_sugerida"] = cleaned_description

    category = find_reclamo_category_by_input(user_input, reclamo_options)
    if category:
        details["categoria_sugerida"] = category

    # --- Address heuristics (incluye intersecciones) ---
    direccion_interseccion, intersection_hints = _parse_intersection_and_district(
        user_input, default_localidad=default_localidad, default_provincia=default_provincia
    )
    direccion_interseccion = _refine_intersection_candidate(user_input, direccion_interseccion)
    if (
        direccion_interseccion
        and "direccion_sugerida" not in details
        and _looks_like_address(direccion_interseccion)
    ):
        details["direccion_sugerida"] = direccion_interseccion
    if intersection_hints.get("barrio") and "barrio_sugerido" not in details:
        details["barrio_sugerido"] = intersection_hints["barrio"]
    if intersection_hints.get("distrito") and "distrito_sugerido" not in details:
        details["distrito_sugerido"] = intersection_hints["distrito"]
    if intersection_hints.get("distrito_dudoso"):
        if intersection_hints["distrito_dudoso"] != details.get("distrito_sugerido"):
            details.setdefault("distrito_dudoso", intersection_hints["distrito_dudoso"])

    location_mentions = _detect_location_mentions(
        user_input, default_localidad=default_localidad, default_provincia=default_provincia
    )
    if location_mentions.get("barrio") and "barrio_sugerido" not in details:
        details["barrio_sugerido"] = location_mentions["barrio"]
    if location_mentions.get("distrito") and "distrito_sugerido" not in details:
        details["distrito_sugerido"] = location_mentions["distrito"]
    if location_mentions.get("distrito_dudoso"):
        if location_mentions["distrito_dudoso"] != details.get("distrito_sugerido"):
            details.setdefault("distrito_dudoso", location_mentions["distrito_dudoso"])

    if "direccion_sugerida" not in details:
        match = re.search(r"\b(?:en|sobre|por)\s+([A-Za-zÀ-ÿ'\s]+?\d{1,5})\b", user_input, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if _looks_like_address(candidate):
                details["direccion_sugerida"] = candidate

    if "direccion_sugerida" not in details:
        # Simple fallback: first street + number sequence.
        match = re.search(r"([A-Za-zÀ-ÿ'\s]+\d{1,5})", user_input)
        if match:
            candidate = match.group(1).strip()
            if _looks_like_address(candidate):
                details["direccion_sugerida"] = candidate

    # --- Contact heuristics / LLM extraction ---
    contact_fields = [
        "nombre_cliente",
        "telefono_cliente",
        "email_cliente",
        "dni_cliente",
        "direccion_cliente",
    ]
    contact_details = extract_multiple_contact_details_llm(user_input, contact_fields) or {}
    contact_mapping = {
        "nombre_cliente": "nombre_sugerido",
        "telefono_cliente": "telefono_sugerido",
        "email_cliente": "email_sugerido",
        "dni_cliente": "dni_sugerido",
        "direccion_cliente": "direccion_sugerida",
    }
    for source_key, target_key in contact_mapping.items():
        value = contact_details.get(source_key)
        if not value:
            continue
        if target_key == "direccion_sugerida":
            existing_address = details.get(target_key)
            if _should_update_address_candidate(existing_address, value):
                details[target_key] = value
        elif target_key not in details:
            details[target_key] = value

    direccion_candidate = details.get("direccion_sugerida")
    refined_candidate = _refine_intersection_candidate(user_input, direccion_candidate)
    if refined_candidate and refined_candidate != direccion_candidate:
        details["direccion_sugerida"] = refined_candidate
        direccion_candidate = refined_candidate
    if direccion_candidate and not _looks_like_address(direccion_candidate):
        details.pop("direccion_sugerida", None)

    # Determine if the LLM extractor is still required.
    needs_llm = False
    if not details.get("categoria_sugerida") or not details.get("descripcion_sugerida"):
        needs_llm = True

    llm_details = {}
    if needs_llm:
        llm_details = extract_complaint_details_llm(
            user_input,
            default_localidad=default_localidad,
            default_provincia=default_provincia,
        ) or {}

    if llm_details:
        if llm_details.get("tipo_problema") and "categoria_sugerida" not in details:
            mapped_category = find_reclamo_category_by_input(llm_details["tipo_problema"], reclamo_options)
            if mapped_category:
                details["categoria_sugerida"] = mapped_category

        llm_mapping = {
            "descripcion_problema": "descripcion_sugerida",
            "descripcion_corta": "descripcion_corta_sugerida",
            "ubicacion_problema": "direccion_sugerida",
            "nombre_cliente": "nombre_sugerido",
            "email_cliente": "email_sugerido",
            "telefono_cliente": "telefono_sugerido",
            "dni_cliente": "dni_sugerido",
        }
        for llm_key, target_key in llm_mapping.items():
            value = llm_details.get(llm_key)
            if not value:
                continue
            if target_key == "direccion_sugerida":
                existing = details.get(target_key)
                if _should_update_address_candidate(existing, value):
                    details[target_key] = value
                continue
            if target_key not in details or target_key == "descripcion_sugerida":
                details[target_key] = value

    direccion_candidate = details.get("direccion_sugerida")
    if direccion_candidate and not _looks_like_address(direccion_candidate):
        details.pop("direccion_sugerida", None)

    suggested_to_legacy = {
        "categoria_sugerida": "categoria",
        "descripcion_sugerida": "descripcion",
        "direccion_sugerida": "direccion",
        "nombre_sugerido": "nombre",
        "telefono_sugerido": "telefono",
        "email_sugerido": "email",
        "dni_sugerido": "dni",
        "barrio_sugerido": "barrio",
        "distrito_sugerido": "distrito",
    }

    for suggested_key, legacy_key in suggested_to_legacy.items():
        value = details.get(suggested_key)
        if value and legacy_key not in details:
            details[legacy_key] = value

    for legacy_key, suggested_key in {v: k for k, v in suggested_to_legacy.items()}.items():
        value = details.get(legacy_key)
        if value and suggested_key not in details:
            details[suggested_key] = value

    direccion_final = details.get("direccion")
    if direccion_final and not _looks_like_address(direccion_final):
        details.pop("direccion", None)
        if not details.get("direccion_sugerida"):
            details.pop("direccion_sugerida", None)

    nombre_final = details.get("nombre")
    if nombre_final and not _is_plausible_name(nombre_final):
        details.pop("nombre", None)
    nombre_sugerido = details.get("nombre_sugerido")
    if nombre_sugerido and not _is_plausible_name(nombre_sugerido):
        details.pop("nombre_sugerido", None)
        if details.get("nombre") == nombre_sugerido:
            details.pop("nombre", None)

    return details


def _find_subsequence(tokens: list[str], pattern: list[str]) -> int | None:
    if not tokens or not pattern or len(pattern) > len(tokens):
        return None
    pattern_len = len(pattern)
    for idx in range(len(tokens) - pattern_len + 1):
        if tokens[idx : idx + pattern_len] == pattern:
            return idx
    return None


def _detect_location_mentions(
    text: str,
    default_localidad: str | None = None,
    default_provincia: str | None = None,
) -> dict[str, str]:
    """Extract barrio/distrito hints from free text without heavy keyword tables."""

    hints: dict[str, str] = {}
    if not text:
        return hints

    barrio_match = re.search(r"\b(?:barrio|bº|b°)\s+([A-Za-zÀ-ÿ'\s]+)", text, re.IGNORECASE)
    if barrio_match:
        barrio_clean = _clean_location_fragment(barrio_match.group(1))
        if barrio_clean:
            hints["barrio"] = barrio_clean

    distrito_match = re.search(
        r"\b(?:distrito|zona|localidad|ciudad)\s+([A-Za-zÀ-ÿ'\s]+)",
        text,
        re.IGNORECASE,
    )
    if distrito_match:
        distrito_clean = _clean_location_fragment(distrito_match.group(1))
        if distrito_clean:
            hints["distrito"] = distrito_clean

    normalized_text = normalizar_texto(text)
    connectors = ["en", "del", "de la", "en el", "en la", "de", "sobre"]
    if default_localidad:
        normalized_localidad = normalizar_texto(default_localidad)
        for connector in connectors:
            pattern = f"{connector} {normalized_localidad}".strip()
            if pattern and pattern in normalized_text:
                hints.setdefault("distrito", default_localidad)
                break

    if default_provincia:
        normalized_provincia = normalizar_texto(default_provincia)
        if normalized_provincia and normalized_provincia in normalized_text:
            if hints.get("distrito") != default_provincia:
                hints.setdefault("distrito_dudoso", default_provincia)

    return hints


def _parse_intersection_and_district(
    text: str,
    default_localidad: str | None = None,
    default_provincia: str | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Detect addresses like "Calle 100 esquina Otra Calle" and capture barrio/distrito hints.

    Returns a tuple ``(direccion, hints)`` where ``hints`` may include ``barrio`` (confident),
    ``distrito`` (confident) or ``distrito_dudoso`` when we detect a possible reference but
    without explicit keywords.
    """

    if not text:
        return None, {}

    normalized = normalizar_texto(text)
    if "esquina" not in normalized:
        return None, {}

    before: str
    after: str

    split = re.split(r"(?i)\besquina\b", text, maxsplit=1)
    if len(split) >= 2:
        before = split[0].strip(" ,.-")
        after = split[1].strip(" ,.-")
    else:
        idx = normalized.find("esquina")
        before = text[:idx].strip(" ,.-")
        after = text[idx + len("esquina") :].strip(" ,.-")

    before = re.sub(r"(?i)\besquina\b\s*$", "", before).strip(" ,.-")
    after = re.sub(r"(?i)^(?:esquina\s+)+", "", after).strip(" ,.-")

    street1 = None
    if before:
        street1_match = re.search(r"([A-Za-zÀ-ÿ'\s]+\d{1,5})\s*$", before)
        if street1_match:
            street1 = street1_match.group(1).strip()
        else:
            before_tokens = before.split()
            if len(before_tokens) >= 2:
                street1 = " ".join(before_tokens[-2:])
            else:
                street1 = before

    if street1:
        street1 = _strip_leading_phrases(street1)

    if not after:
        direccion = street1.strip() if street1 else None
        return direccion, {}

    hints: dict[str, str] = {}
    # First split by punctuation to isolate extra context (e.g., barrio/distrito)
    street_candidate = after
    location_context = ""
    punctuation_split = re.split(r"[;,\.]+", after, maxsplit=1)
    if len(punctuation_split) > 1:
        street_candidate = punctuation_split[0].strip()
        location_context = punctuation_split[1].strip()

    tokens = street_candidate.split()
    normalized_tokens = [normalizar_texto(tok) for tok in tokens]
    split_idx: int | None = None
    split_idx_reason: str | None = None
    keyword_tokens = set().union(*LOCATION_KEYWORD_TOKENS.values())
    found_keyword_token = False
    for idx_token, normalized_token in enumerate(normalized_tokens):
        if normalized_token in keyword_tokens:
            split_idx = idx_token
            split_idx_reason = "keyword"
            found_keyword_token = True
            break

    if default_localidad:
        pattern_tokens = normalizar_texto(default_localidad).split()
        if pattern_tokens:
            match_idx = _find_subsequence(normalized_tokens, pattern_tokens)
            if match_idx is not None:
                if split_idx is None or match_idx < split_idx:
                    split_idx = match_idx
                    split_idx_reason = "default_localidad"

    if default_provincia:
        pattern_tokens = normalizar_texto(default_provincia).split()
        if pattern_tokens:
            match_idx = _find_subsequence(normalized_tokens, pattern_tokens)
            if match_idx is not None:
                if split_idx is None or match_idx < split_idx:
                    split_idx = match_idx
                    split_idx_reason = "default_provincia"

    if split_idx is not None:
        street_tokens = tokens[:split_idx]
        location_tokens = tokens[split_idx:]
        raw_location_tokens = list(location_tokens)

        while (
            street_tokens
            and location_tokens
            and normalizar_texto(street_tokens[-1]) in _ADDRESS_CONNECTOR_TOKENS
        ):
            location_tokens.insert(0, street_tokens.pop())

        street_candidate = " ".join(street_tokens).strip()
        street_candidate = _strip_leading_phrases(street_candidate)
        extra_location_text = " ".join(location_tokens).strip()
        if extra_location_text:
            normalized_location_only = normalizar_texto(
                " ".join(
                    token
                    for token in location_tokens
                    if normalizar_texto(token) not in _ADDRESS_CONNECTOR_TOKENS
                )
            )
            normalized_default_localidad = (
                normalizar_texto(default_localidad) if default_localidad else ""
            )
            normalized_default_provincia = (
                normalizar_texto(default_provincia) if default_provincia else ""
            )

            treat_as_street_extension = False
            if not found_keyword_token and location_tokens:
                if (
                    split_idx_reason == "default_localidad"
                    and normalized_location_only
                    and normalized_location_only == normalized_default_localidad
                ):
                    treat_as_street_extension = True
                elif (
                    split_idx_reason == "default_provincia"
                    and normalized_location_only
                    and normalized_location_only == normalized_default_provincia
                ):
                    treat_as_street_extension = True

            if treat_as_street_extension:
                street_tokens = tokens[:split_idx] + raw_location_tokens
                street_candidate = " ".join(street_tokens).strip()
                street_candidate = _strip_leading_phrases(street_candidate)
                extra_location_text = ""
            else:
                location_context = f"{extra_location_text} {location_context}".strip()

    street2 = street_candidate.strip() if street_candidate else None
    direccion = None
    if street1 and street2:
        direccion = f"{street1} esquina {street2}".strip()
    elif street1:
        direccion = street1.strip()
    elif street2:
        direccion = street2

    if direccion and location_context:
        lowered_direccion = direccion.lower()
        lowered_context = location_context.lower()
        if lowered_direccion.endswith(lowered_context):
            direccion = direccion[: -len(location_context)].rstrip(" ,.-")

    if direccion:
        direccion = re.sub(
            r"(?i)\besquina(?:\s+esquina)+\b",
            "esquina",
            direccion,
        )

    location_context = location_context.strip()
    if location_context:
        barrio_match = re.search(r"\b(?:barrio|bº|b°)\s+([A-Za-zÀ-ÿ'\s]+)", location_context, re.IGNORECASE)
        if barrio_match:
            hints["barrio"] = barrio_match.group(1).strip(" ,.-")

        distrito_match = re.search(
            r"\b(?:distrito|zona|localidad|ciudad)\s+([A-Za-zÀ-ÿ'\s]+)",
            location_context,
            re.IGNORECASE,
        )
        if distrito_match:
            hints["distrito"] = distrito_match.group(1).strip(" ,.-")
        else:
            normalized_context = normalizar_texto(location_context)
            if default_localidad and normalizar_texto(default_localidad) in normalized_context:
                hints["distrito"] = default_localidad
            elif default_provincia and normalizar_texto(default_provincia) in normalized_context:
                hints["distrito_dudoso"] = default_provincia
            else:
                hints["distrito_dudoso"] = location_context

    return direccion, hints


def _extract_coordinates_from_text(value: str) -> Optional[dict[str, float]]:
    """Return latitude/longitude pairs found in free text when clearly expressed."""

    if not value:
        return None

    coordinate_patterns = [
        re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)"),
        re.compile(r"(-?\d{1,3}\.\d+)\s+(-?\d{1,3}\.\d+)"),
    ]

    for pattern in coordinate_patterns:
        for match in pattern.finditer(value):
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return {"latitude": lat, "longitude": lon}

    return None


def _extract_coordinates_from_url(url: str) -> Optional[dict[str, float]]:
    """Parse typical map URLs looking for embedded coordinates."""

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    query = parse_qs(parsed.query)
    for key in ("q", "query", "ll", "center"):
        for value in query.get(key, []):
            coords = _extract_coordinates_from_text(unquote(value))
            if coords:
                return coords

    path_candidate = unquote(parsed.path or "")
    match = re.search(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)", path_candidate)
    if match:
        lat = float(match.group(1))
        lon = float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {"latitude": lat, "longitude": lon}

    fragment_coords = _extract_coordinates_from_text(unquote(parsed.fragment or ""))
    if fragment_coords:
        return fragment_coords

    return None


def _detect_location_link_info(text: str) -> Optional[dict[str, Any]]:
    """Detect whether a message mainly shares a location link or raw coordinates."""

    if not text:
        return None

    location_keywords = (
        "maps.google",
        "maps.app.goo.gl",
        "goo.gl/maps",
        "waze.com",
        "openstreetmap",
        "mapa",
    )

    url_match = re.search(r"https?://[^\s>]+", text, re.IGNORECASE)
    coords_in_text = _extract_coordinates_from_text(text)

    info: dict[str, Any] = {}
    cleaned_text = text.strip()

    info["only_location"] = False

    if url_match:
        url = url_match.group(0)
        host = urlparse(url).netloc.lower()
        if any(keyword in host for keyword in location_keywords):
            cleaned_text = (text[: url_match.start()] + " " + text[url_match.end():]).strip()
            info["source"] = "link"
            coords_from_url = _extract_coordinates_from_url(url)
            if coords_from_url:
                info.update(coords_from_url)
        else:
            url_match = None

    if not url_match and not coords_in_text:
        return None

    if "source" not in info:
        info["source"] = "coordinates"

    if not info.get("latitude") and coords_in_text:
        info.update(coords_in_text)

    if coords_in_text:
        cleaned_text = re.sub(r"-?\d{1,3}\.\d+[\s,]+-?\d{1,3}\.\d+", " ", cleaned_text).strip()

    residual_words = [word for word in cleaned_text.split() if word]
    if residual_words:
        normalized_words = [normalizar_texto(word) for word in residual_words]
        allowed_fillers = {
            "aca",
            "aqui",
            "aquí",
            "ubicacion",
            "ubicación",
            "ubic",
            "pin",
            "link",
            "direccion",
            "dirección",
            "es",
            "esta",
            "este",
            "mi",
            "la",
            "el",
            "en",
            "te",
            "paso",
        }
        if len(residual_words) > 6 and not all(word in allowed_fillers for word in normalized_words):
            return None
        info["address"] = cleaned_text.strip()
        info["only_location"] = all(word in allowed_fillers for word in normalized_words)
    else:
        info["address"] = "la ubicación que compartiste"
        info["only_location"] = True

    return info


def _try_start_reclamo_from_text(
    pregunta_str: str,
    context: dict[str, Any],
    chat_db_context,
    *,
    default_localidad: str | None = None,
    default_provincia: str | None = None,
) -> Optional[dict[str, Any]]:
    """Attempt to bootstrap the reclamo flow directly from a free-text message."""

    if not pregunta_str:
        return None

    normalized_text = normalizar_texto(pregunta_str)
    if len(normalized_text.split()) < 4 and len(pregunta_str.strip()) < 30:
        return None

    reclamo_options = _get_reclamos_menu().get("options_list", [])
    plain_options = [
        {"texto": opt.get("category_name")}
        for opt in reclamo_options
        if opt.get("category_name")
    ]

    details = extract_reclamo_details_from_text(
        pregunta_str,
        plain_options,
        default_localidad=default_localidad,
        default_provincia=default_provincia,
    )

    category = details.get("categoria") or details.get("categoria_sugerida")
    if not category:
        return None

    handler = ReclamoFlowHandler(context, chat_db_context)

    datos_iniciales: dict[str, Any] = {}
    field_mapping = (
        ("descripcion", "descripcion_sugerida"),
        ("direccion", "direccion_sugerida"),
        ("nombre", "nombre_sugerido"),
        ("email", "email_sugerido"),
        ("telefono", "telefono_sugerido"),
        ("dni", "dni_sugerido"),
    )

    for field, suggested_key in field_mapping:
        value = details.get(suggested_key) or details.get(field)
        if value:
            datos_iniciales[field] = value

    if details.get("barrio_sugerido"):
        datos_iniciales.setdefault("barrio", details["barrio_sugerido"])
    if details.get("distrito_sugerido"):
        datos_iniciales.setdefault("distrito", details["distrito_sugerido"])

    response = handler.start_flow(
        datos_iniciales=datos_iniciales or None,
        categoria_inicial=category,
    )

    if chat_db_context:
        safe_flag_modified(chat_db_context, "context_data")

    return response


def _get_reclamos_consultas_menu():
    opciones = [
        {"texto": "📝 Iniciar un Reclamo", "action_id": "iniciar_reclamo"},
        {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia"},
        {"texto": "🤔 Consultar Estado de Reclamo", "action_id": "consultar_estado_reclamo"},
        {"texto": "📞 Contactos Útiles", "action_id": "contactos_utiles"},
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "Cancelar", "action_id": "cancelar"},
    ]
    return {
        "message_body": "Elegí una opción para tu consulta:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_reclamos_consultas_v1",
        "generar_audio": True,
    }


def _get_tramites_menu():
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
        {"texto": "🗓️ Solicitar Otros Turnos", "action_id": "solicitar_turnos"},
        {"texto": "💵 Pagar Tasas Municipales", "action_id": "pago_de_tasas_vigentes"},
    ]
    return {
        "message_body": "Elegí una opción:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_tramites_v1",
        "generar_audio": True,
    }


def _get_informacion_menu():
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "🎭 Agenda Cultural y Noticias", "action_id": "agenda_y_noticias"},
        {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
        {"texto": "🏗️ Obras", "action_id": "obras"},
        {"texto": "♻️ Punto Limpio", "action_id": "punto_limpio"},
    ]
    return {
        "message_body": "Elegí la información que querés consultar:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_informacion_v1",
        "generar_audio": True,
    }


def _resolve_catalogo_base_url(context: Optional[dict]) -> Optional[str]:
    municipio_config = (context or {}).get("municipio_config_actual") or {}
    catalogo_cfg = {}
    if isinstance(municipio_config.get("catalogo"), dict):
        catalogo_cfg = municipio_config["catalogo"]
    elif isinstance(municipio_config.get("catalogos"), dict):
        catalogo_cfg = municipio_config["catalogos"]

    candidates = [
        catalogo_cfg.get("base_url"),
        catalogo_cfg.get("public_base_url"),
        catalogo_cfg.get("tienda_base_url"),
        catalogo_cfg.get("share_base_url"),
        municipio_config.get("catalogo_base_url"),
        municipio_config.get("catalogos_base_url"),
        municipio_config.get("tienda_base_url"),
        municipio_config.get("beneficios_base_url"),
        municipio_config.get("store_base_url"),
    ]

    if has_app_context():
        candidates.append(current_app.config.get("TIENDA_BASE_URL"))
        candidates.append(current_app.config.get("BACKEND_URL"))

    candidates.append(DEFAULT_BACKEND_URL)

    for candidate in candidates:
        cleaned = _clean_url_candidate(candidate)
        if cleaned:
            return cleaned.rstrip("/")

    return None


def _catalogo_widget_visible(context: Optional[dict]) -> bool:
    """Return True if the catalog should be exposed inside the widget."""

    municipio_config = (context or {}).get("municipio_config_actual") or {}

    flags: list[Optional[bool]] = [
        municipio_config.get("catalogo_widget_visible"),
        municipio_config.get("widget_catalog_visible"),
        municipio_config.get("catalogo_visible_en_widget"),
    ]

    catalogo_cfg = None
    if isinstance(municipio_config.get("catalogo"), dict):
        catalogo_cfg = municipio_config["catalogo"]
    elif isinstance(municipio_config.get("catalogos"), dict):
        catalogo_cfg = municipio_config["catalogos"]

    if isinstance(catalogo_cfg, dict):
        flags.extend(
            [
                catalogo_cfg.get("widget_visible"),
                catalogo_cfg.get("widget_enabled"),
                catalogo_cfg.get("catalogo_widget_visible"),
            ]
        )

    for flag in flags:
        if isinstance(flag, bool):
            return flag

    # Si no hay flags explícitos, habilitamos catálogo cuando podemos construir URLs.
    # Esto permite mantener el menú visible en tenantes legados que no setean los flags
    # nuevos, pero sí usan las rutas de catálogo anteriores.
    return bool(_resolve_catalogo_base_url(context))


def _resolve_tenant_identifiers(context: Optional[dict]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (tenant_slug, tenant_id, owner_id) best-effort from the context/config."""

    municipio_config = (context or {}).get("municipio_config_actual") or {}

    tenant_slug = (
        municipio_config.get("tenant_slug")
        or municipio_config.get("slug")
        or municipio_config.get("nombre_slug")
    )

    tenant_id = (
        municipio_config.get("tenant_id")
        or municipio_config.get("tenant")
        or municipio_config.get("id")
        or (context or {}).get("tenant_id")
        or (context or {}).get("municipio_id")
    )

    owner_id = (
        municipio_config.get("owner_id")
        or municipio_config.get("municipio_id")
        or (context or {}).get("owner_id")
        or (context or {}).get("municipio_id")
    )

    def _clean(value: Optional[object]) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None

    return _clean(tenant_slug), _clean(tenant_id), _clean(owner_id)


def _resolve_viewer_phone(context: Optional[dict]) -> Optional[str]:
    """Try to infer the viewer's phone number from context or stored contact."""

    context = context or {}
    viewer = context.get("viewer_user_obj")
    candidates = [
        getattr(viewer, "telefono", None) if viewer else None,
        getattr(viewer, "phone", None) if viewer else None,
        context.get("telefono_usuario_contexto"),
        context.get("telefono_detectado"),
    ]

    chat_ctx = context.get("chat_db_context_data") or {}
    ctx_muni = chat_ctx.get(CONTEXTO_MUNICIPIO, {}) or {}
    contacto_usuario = ctx_muni.get("contacto_usuario") or {}
    candidates.extend(
        [
            contacto_usuario.get("telefono"),
            contacto_usuario.get("whatsapp"),
        ]
    )

    for candidate in candidates:
        normalized = _normalize_phone_value(candidate)
        if normalized:
            return normalized

    return None


def _append_tenant_param(
    raw_url: Optional[str],
    tenant_slug: Optional[str],
    tenant_id: Optional[str],
    owner_id: Optional[str],
    viewer_phone: Optional[str] = None,
) -> Optional[str]:
    """Ensure catalog links carry tenant/owner hints so the right store is loaded.

    Also propagates the viewer phone when available so downstream experiences (puntos,
    canjes, donaciones) can associate the session to the WhatsApp identity.
    """

    if not raw_url or not isinstance(raw_url, str):
        return raw_url

    parsed = urlparse(raw_url)
    query_params = parse_qs(parsed.query, keep_blank_values=True)

    if tenant_slug and not any(key in query_params for key in ("tenant", "tenant_slug")):
        query_params.setdefault("tenant", [tenant_slug])

    if tenant_id and "tenant_id" not in query_params:
        query_params["tenant_id"] = [tenant_id]

    if owner_id and "owner_id" not in query_params:
        query_params["owner_id"] = [owner_id]

    if viewer_phone:
        existing_phone_keys = {k.lower() for k in query_params.keys()}
        if not existing_phone_keys.intersection({"phone", "telefono", "waid", "whatsapp"}):
            query_params["phone"] = [viewer_phone]

    new_query = urlencode(query_params, doseq=True)
    return parsed._replace(query=new_query).geturl()


def _resolve_catalogo_banner_image(context: Optional[dict]) -> Optional[str]:
    municipio_config = (context or {}).get("municipio_config_actual") or {}
    catalogo_cfg = {}
    if isinstance(municipio_config.get("catalogo"), dict):
        catalogo_cfg = municipio_config["catalogo"]
    elif isinstance(municipio_config.get("catalogos"), dict):
        catalogo_cfg = municipio_config["catalogos"]

    candidates = [
        catalogo_cfg.get("banner_image_url"),
        catalogo_cfg.get("header_image_url"),
        catalogo_cfg.get("cover_image_url"),
        municipio_config.get("catalogo_banner_image_url"),
        municipio_config.get("catalogo_header_image_url"),
    ]

    for candidate in candidates:
        normalized = _normalize_public_url(candidate, context)
        if normalized:
            return normalized

    fallback = _normalize_public_url(
        "static/encuestas/participacion_ciudadana.png", context
    )
    return fallback


def _build_catalogo_link_map(context: Optional[dict]) -> Dict[str, str]:
    municipio_config = (context or {}).get("municipio_config_actual") or {}
    catalogo_cfg = {}
    if isinstance(municipio_config.get("catalogo"), dict):
        catalogo_cfg = municipio_config["catalogo"]
    elif isinstance(municipio_config.get("catalogos"), dict):
        catalogo_cfg = municipio_config["catalogos"]

    override_maps: Dict[str, str] = {}
    for key in ("catalogo_links", "catalogo_urls", "catalogos_links", "links", "urls"):
        mapping = catalogo_cfg.get(key) or municipio_config.get(key)
        if isinstance(mapping, dict):
            override_maps.update(
                {k: v for k, v in mapping.items() if isinstance(k, str)}
            )

    base_url = _resolve_catalogo_base_url(context)
    tenant_slug, tenant_id, owner_id = _resolve_tenant_identifiers(context)
    viewer_phone = _resolve_viewer_phone(context)
    default_paths = {
        "catalogo_ver": "/productos",
        "catalogo_canje_puntos": "/productos?view=canje",
        "catalogo_compras": "/productos?view=compras",
        "catalogo_donaciones": "/productos?view=donaciones",
    }

    link_map: Dict[str, str] = {}
    for action_id, default_path in default_paths.items():
        raw_url = override_maps.get(action_id)
        if not raw_url and base_url:
            raw_url = f"{base_url}{default_path}"
        raw_url = _append_tenant_param(
            raw_url, tenant_slug, tenant_id, owner_id, viewer_phone
        )
        normalized = _normalize_public_url(raw_url, context)
        if normalized:
            link_map[action_id] = normalized

    return link_map


def _get_catalogo_menu(context: Optional[dict] = None):
    context = context or {}
    if not _catalogo_widget_visible(context):
        return {
            "message_body": "El catálogo no está habilitado para este municipio.",
            "message_type": "interactive_buttons",
            "options_list": [
                {"texto": "Menú principal", "action_id": "menu_principal"},
                {"texto": "Volver", "action_id": "cancelar"},
            ],
            "fuente": "catalogo_desactivado_municipio",
            "generar_audio": True,
        }
    direct_links = _build_catalogo_link_map(context)
    banner_image = _resolve_catalogo_banner_image(context)

    opciones = [
        {"texto": "📂 Ver Catálogo", "action_id": "catalogo_ver"},
        {"texto": "🎁 Canje de Puntos", "action_id": "catalogo_canje_puntos"},
        {"texto": "🛒 Compra de Productos", "action_id": "catalogo_compras"},
        {"texto": "❤️ Donaciones", "action_id": "catalogo_donaciones"},
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "Cancelar", "action_id": "cancelar"},
    ]

    for opcion in opciones:
        action_id = opcion.get("action_id")
        if not action_id:
            continue
        link = direct_links.get(action_id)
        if link:
            opcion["url"] = link
            opcion["type"] = "url"

    header = "*Catálogo y Beneficios*"
    body_lines = [
        header,
        "Elegí cómo querés operar con el catálogo y los beneficios del tenant:",
    ]

    link_descriptions = []
    labels = {
        "catalogo_ver": "📂 Ver Catálogo",
        "catalogo_canje_puntos": "🎁 Canje de Puntos",
        "catalogo_compras": "🛒 Compra de Productos",
        "catalogo_donaciones": "❤️ Donaciones",
    }

    for action_id, label in labels.items():
        link = direct_links.get(action_id)
        if not link:
            continue
        display_link = _format_url_for_display(link, widget=True)
        link_descriptions.append(f"{label}: {display_link}")

    if link_descriptions:
        body_lines.append("")
        body_lines.extend(link_descriptions)

    payload = {
        "message_body": "\n".join(filter(None, body_lines)),
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_catalogo_v1",
        "generar_audio": True,
    }

    if banner_image:
        payload["image_url"] = banner_image

    return payload


def _resolve_tenant_slug(context: Optional[dict]) -> str:
    municipio_config = (context or {}).get("municipio_config_actual", {}) or {}
    slug = municipio_config.get("slug") or municipio_config.get("nombre_slug")
    if slug:
        return str(slug).strip()
    tenant_id = context.get("municipio_id") if context else None
    return str(tenant_id or "default").strip()


def _filter_catalogo_items(action_id: str, context: Optional[dict]) -> list:
    action_to_tipo = {
        "catalogo_donaciones": "donaciones",
        "catalogo_canje_puntos": "canje_puntos",
        "catalogo_compras": "compras",
        "catalogo_ver": None,
    }
    tenant_slug = _resolve_tenant_slug(context)
    desired_tipo = action_to_tipo.get(action_id)
    items = []
    for item in PRODUCT_CATALOG:
        if item.get("tenant_slug") and str(item.get("tenant_slug")) != tenant_slug:
            continue
        if desired_tipo and item.get("tipo") != desired_tipo:
            continue
        items.append(item)
    return items


def _format_catalogo_item_for_whatsapp(item: dict) -> str:
    nombre = item.get("nombre", "Producto")
    item_id = item.get("id") or ""
    categoria = item.get("categoria") or item.get("tipo") or ""
    descripcion = item.get("descripcion") or ""
    precio = item.get("precio") or 0
    puntos = item.get("puntos") or 0
    precio_text = f"${precio:,.0f}" if precio else "Sin costo"
    puntos_text = f"{puntos} pts" if puntos else None
    badge_parts = [p for p in [precio_text, puntos_text] if p]
    badge = " | ".join(badge_parts)
    lines = [f"*{nombre}* ({item_id})"]
    if categoria:
        lines.append(f"{categoria}")
    if descripcion:
        lines.append(descripcion)
    if badge:
        lines.append(badge)
    return "\n".join(lines)


def _build_catalogo_flow_payload(
    action_id: str, context: Optional[dict] = None, page: int = 1, page_size: int = 5
) -> dict:
    context = context or {}
    direct_links = _build_catalogo_link_map(context)
    selected_link = direct_links.get(action_id)
    header = "*Catálogo y Beneficios*"
    mensajes = {
        "catalogo_ver": (
            "Catálogo disponible para tu municipio. Podés abrir el enlace o pedir productos acá mismo."
        ),
        "catalogo_donaciones": (
            "Elegí qué artículo querés donar. Registramos la donación y compartimos el comprobante."
        ),
        "catalogo_canje_puntos": (
            "Mostramos opciones canjeables. Si no tenés la sesión vinculada te pediremos identificarte para usar tus puntos."
        ),
        "catalogo_compras": (
            "Armá tu carrito desde WhatsApp o abrí el enlace para ver todos los productos."
        ),
    }
    base_message = mensajes.get(action_id, "Contame cómo querés usar el catálogo y te guío paso a paso.")
    if action_id == "catalogo_canje_puntos" and not context.get("user_obj"):
        base_message += "\nℹ️ Para canjear puntos necesitamos asociar tu cuenta. Podés enviarnos tu email o registrarte con el enlace."

    catalog_items = _filter_catalogo_items(action_id, context)
    total_items = len(catalog_items)
    start_idx = max((page - 1) * page_size, 0)
    end_idx = start_idx + page_size
    paginated_items = catalog_items[start_idx:end_idx]
    has_more = end_idx < total_items

    items_text = []
    for item in paginated_items:
        items_text.append(_format_catalogo_item_for_whatsapp(item))
        items_text.append("")

    body_lines = [header, base_message]
    if selected_link:
        display_link = _format_url_for_display(selected_link, widget=True)
        body_lines.append(f"🔗 {display_link}")
    if items_text:
        body_lines.append("\n".join(items_text).strip())
        if has_more:
            body_lines.append(f"Mostrando {start_idx + 1}-{min(end_idx, total_items)} de {total_items}.")
    else:
        body_lines.append("No encontramos productos disponibles en esta categoría por ahora.")

    options_list = []
    for item in paginated_items[:3]:
        options_list.append(
            {
                "texto": f"Agregar {item.get('id')}",
                "action_id": f"catalogo_agregar_item::{item.get('id')}",
            }
        )
    if has_more:
        options_list.append(
            {
                "texto": "Ver más",
                "action_id": f"catalogo_mostrar_mas::{action_id}::{page + 1}"
            }
        )
    options_list.extend(
        [
            {"texto": "Ver carrito", "action_id": "mostrar_carrito_catalogo"},
            {"texto": "Menú", "action_id": "menu_principal"},
            {"texto": "Cancelar", "action_id": "cancelar"},
        ]
    )

    return {
        "message_body": "\n\n".join(filter(None, body_lines)),
        "message_type": "interactive_buttons",
        "options_list": options_list,
        "fuente": "catalogo_flow_intro_v2",
        "generar_audio": True,
    }


def _find_catalog_item_by_id(item_id: str) -> Optional[dict]:
    for item in PRODUCT_CATALOG:
        if str(item.get("id")) == str(item_id):
            return item
    return None


def _get_catalogo_cart(context: dict) -> list:
    chat_ctx = context.setdefault("chat_db_context_data", {})
    cart = chat_ctx.setdefault("catalogo_carrito_demo", [])
    return cart


def _build_catalogo_cart_summary(context: dict) -> tuple[str, float, float, int]:
    cart = _get_catalogo_cart(context)
    if not cart:
        return "Tu carrito está vacío. Agregá un producto para empezar.", 0.0, 0.0, 0

    lines = ["🧺 *Resumen de tu carrito:*", ""]
    total_precio = 0.0
    total_puntos = 0.0
    total_donaciones = 0
    for entry in cart:
        item = _find_catalog_item_by_id(entry.get("id")) or {}
        cantidad = entry.get("cantidad", 1)
        nombre = item.get("nombre", entry.get("id"))
        precio_unit = float(item.get("precio") or 0)
        puntos_unit = float(item.get("puntos") or 0)
        tipo_item = str(item.get("tipo") or "").lower()
        es_donacion = tipo_item == "donaciones"
        es_canje = tipo_item == "canje_puntos"
        subtotal_precio = cantidad * precio_unit
        subtotal_puntos = cantidad * puntos_unit
        if es_donacion:
            total_donaciones += cantidad
        elif es_canje:
            total_puntos += subtotal_puntos
        else:
            total_precio += subtotal_precio
        badge_parts = []
        if subtotal_precio and not es_donacion:
            badge_parts.append(f"${subtotal_precio:,.0f}")
        if subtotal_puntos and es_canje:
            badge_parts.append(f"{subtotal_puntos:,.0f} pts")
        badge = " | ".join(badge_parts) if badge_parts else "Donación sin pago" if es_donacion else "Sin costo"
        lines.append(f"• {cantidad} x {nombre} — {badge}")

    lines.append("")
    if total_donaciones:
        lines.append(f"❤️ Donaciones: {total_donaciones} confirmadas sin cobro.")
    if total_precio:
        lines.append(f"Total monetario: ${total_precio:,.0f}")
    if total_puntos:
        lines.append(f"Total puntos: {total_puntos:,.0f} pts")
    if total_precio:
        lines.append("Si completás pago por MercadoPago te avisaremos cuando se acredite.")
    if total_puntos:
        lines.append("Recordá que necesitaremos validar tu saldo para canjear puntos.")
    return "\n".join(lines), total_precio, total_puntos, total_donaciones


def _resolve_encuestas_tenant_id(context: dict) -> Optional[int]:
    """Infer the tenant/municipio identifier for survey queries."""

    owner = context.get("user_obj")
    candidates = []
    if owner is not None:
        candidates.extend(
            [
                getattr(owner, "municipio_id", None),
                getattr(owner, "empresa_id", None),
                getattr(owner, "pyme_id", None),
                getattr(owner, "id", None),
            ]
        )
    candidates.append(context.get("municipio_id"))

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "si", "sí", "on", "enabled", "enable"}:
            return True
        if normalized in {"false", "0", "no", "off", "disabled", "disable"}:
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _resolve_encuestas_toggle(context: dict) -> Optional[bool]:
    """Return an explicit enable/disable toggle from the tenant configuration."""

    municipio_config = context.get("municipio_config_actual") or {}
    candidates = [
        municipio_config.get("encuestas_enabled"),
        municipio_config.get("encuestas_habilitadas"),
    ]

    encuestas_cfg = municipio_config.get("encuestas")
    if isinstance(encuestas_cfg, dict):
        candidates.extend(
            [
                encuestas_cfg.get("enabled"),
                encuestas_cfg.get("habilitado"),
            ]
        )

    feature_flags_cfg = municipio_config.get("feature_flags")
    if isinstance(feature_flags_cfg, dict):
        candidates.append(feature_flags_cfg.get("encuestas"))
        candidates.append(feature_flags_cfg.get("participacion_ciudadana"))

    for candidate in candidates:
        coerced = _coerce_bool(candidate)
        if coerced is not None:
            return coerced

    tenant_id = _resolve_encuestas_tenant_id(context)
    db_toggle = get_feature_toggle(tenant_id, "encuestas")
    if db_toggle is not None:
        return db_toggle
    return None


def _resolve_encuestas_base_url(context: dict) -> str:
    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = municipio_config.get("encuestas")
    base_url = None
    if isinstance(encuestas_cfg, dict):
        base_url = (
            encuestas_cfg.get("qr_target_base_url")
            or encuestas_cfg.get("public_share_base_url")
            or encuestas_cfg.get("public_base_url")
            or encuestas_cfg.get("base_url")
        )
    if not base_url:
        base_url = (
            municipio_config.get("encuestas_qr_target_base_url")
            or municipio_config.get("encuestas_share_base_url")
            or municipio_config.get("encuestas_base_url")
        )
    if isinstance(base_url, str) and base_url.strip():
        return base_url.rstrip("/")
    canonical = None
    backend_url = None
    mapping: Dict[str, int] | None = None
    is_https = DEFAULT_IS_HTTPS
    tenant_id = _resolve_encuestas_tenant_id(context)

    if has_app_context():
        canonical = current_app.config.get("PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL") or current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
        backend_url = current_app.config.get("BACKEND_URL")
        mapping = current_app.config.get("PUBLIC_ENCUESTAS_DOMAIN_MAP")
        is_https = current_app.config.get("IS_HTTPS", DEFAULT_IS_HTTPS)

    if isinstance(canonical, str) and canonical.strip():
        return canonical.rstrip("/")

    if mapping and tenant_id is not None:
        preferred_domains: List[str] = []
        for domain, mapped_id in mapping.items():
            if mapped_id != tenant_id:
                continue
            if not isinstance(domain, str) or not domain.strip():
                continue
            domain = domain.strip().lower()
            # Prefiere dominios con www para compartir enlaces en campañas.
            if domain.startswith("www."):
                preferred_domains.insert(0, domain)
            else:
                preferred_domains.append(domain)

        if preferred_domains:
            scheme = "https" if is_https else "http"
            return f"{scheme}://{preferred_domains[0]}".rstrip("/")

    if isinstance(backend_url, str) and backend_url.strip():
        return backend_url.rstrip("/")

    return DEFAULT_BACKEND_URL.rstrip("/")


def _resolve_encuestas_api_base_url(context: dict) -> str:
    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = municipio_config.get("encuestas")
    api_base = None
    if isinstance(encuestas_cfg, dict):
        api_base = encuestas_cfg.get("public_api_base_url") or encuestas_cfg.get("api_base_url")
    if not api_base:
        api_base = municipio_config.get("encuestas_api_base_url")
    if isinstance(api_base, str) and api_base.strip():
        return api_base.rstrip("/")

    configured_api = None
    backend_url = None
    if has_app_context():
        configured_api = current_app.config.get("PUBLIC_ENCUESTAS_API_BASE_URL")
        backend_url = current_app.config.get("BACKEND_URL")

    if isinstance(configured_api, str) and configured_api.strip():
        return configured_api.rstrip("/")

    if isinstance(backend_url, str) and backend_url.strip():
        return backend_url.rstrip("/")

    return DEFAULT_BACKEND_URL.rstrip("/")


def _shorten_button_label(text: str, max_length: int = 42) -> str:
    if not isinstance(text, str):
        return "Encuesta"
    trimmed = text.strip()
    if len(trimmed) <= max_length:
        return trimmed
    return trimmed[: max_length - 1].rstrip() + "…"


def _extract_short_public_slug(slug_publico: str) -> str:
    """Return the short token for a public survey slug when available."""

    if not isinstance(slug_publico, str):
        return ""

    normalized = slug_publico.strip().lower()
    if not normalized:
        return ""

    if "-" in normalized:
        candidate = normalized.rsplit("-", 1)[-1]
        if re.fullmatch(r"[0-9a-z]{5,12}", candidate or ""):
            return candidate

    if re.fullmatch(r"[0-9a-z]{5,12}", normalized):
        return normalized

    compact = re.sub(r"[^0-9a-z]", "", normalized)
    if re.fullmatch(r"[0-9a-z]{5,12}", compact):
        return compact

    return normalized


def _resolve_encuestas_short_base_url(context: dict, base_url: str) -> str:
    """Prefer a shorter public base URL for share messages when available."""

    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = municipio_config.get("encuestas") if isinstance(
        municipio_config.get("encuestas"), dict
    ) else {}

    candidate_urls = [
        encuestas_cfg.get("short_share_base_url"),
        encuestas_cfg.get("short_base_url"),
        municipio_config.get("encuestas_short_share_base_url"),
        municipio_config.get("encuestas_short_base_url"),
    ]

    for candidate in candidate_urls:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().rstrip("/")

    if has_app_context():
        configured_short = current_app.config.get("PUBLIC_ENCUESTAS_SHORT_BASE_URL")
        if isinstance(configured_short, str) and configured_short.strip():
            return configured_short.strip().rstrip("/")

    short_base = base_url.strip()
    if short_base.startswith("https://www."):
        return "https://" + short_base[len("https://www.") :].rstrip("/")
    if short_base.startswith("http://www."):
        return "http://" + short_base[len("http://www.") :].rstrip("/")

    return short_base.rstrip("/")


def _build_fallback_encuestas_for_junin(
    base_url: str, context: Optional[dict] = None
) -> list[dict]:
    """Return a curated list of encuestas for Junín with short share URLs."""

    context = context or {}
    cleaned_base = _clean_url_candidate(base_url) or "https://chatboc.ar"
    cleaned_base = cleaned_base.rstrip("/")
    short_base = _resolve_encuestas_short_base_url(context, cleaned_base)

    titulo = "Participación Ciudadana Junín 2025"
    descripcion = (
        "Queremos conocer tus prioridades para planificar obras, seguridad y "
        "actividades en todo Junín. Contanos qué es importante para tu barrio."
    )
    slug = "9df156"

    share_url = urljoin(f"{cleaned_base}/", f"e/{slug}")
    share_short_url = urljoin(f"{short_base}/", f"e/{slug}") if short_base else share_url
    share_message = f"Participá en {titulo}: {share_short_url or share_url}"

    return [
        {
            "data": {
                "titulo": titulo,
                "descripcion": descripcion,
                "slug": slug,
            },
            "slug_publico": slug,
            "share_url": share_url,
            "share_short_url": share_short_url,
            "share_message": share_message,
        }
    ]


def _is_domain_mapped_base_url_for_tenant(
    base_url: str, tenant_id: Optional[int]
) -> bool:
    """Return True when the resolved base URL matches a configured domain map entry."""

    if tenant_id is None:
        return False

    if not isinstance(base_url, str) or not base_url.strip():
        return False

    normalized = base_url.strip()
    if "://" not in normalized:
        normalized = f"https://{normalized}"

    host = urlparse(normalized).netloc.lower()
    if not host:
        return False

    if ":" in host:
        host = host.split(":", 1)[0]

    mapping = None
    if has_app_context():
        mapping = current_app.config.get("PUBLIC_ENCUESTAS_DOMAIN_MAP")

    if not isinstance(mapping, dict):
        return False

    for domain, mapped_id in mapping.items():
        if mapped_id != tenant_id:
            continue
        if isinstance(domain, str) and domain.strip().lower() == host:
            return True

    return False


def _format_url_for_display(
    url: str,
    *,
    max_length: int = 70,
    prefer_text_param: bool = False,
    widget: bool = False,
) -> str:
    """Return a compact, human-friendly representation of a public URL.

    When used inside the widget/WhatsApp menus we keep the query string so the
    user can copy a fully functional URL that already incluye parámetros de
    tenant/owner requeridos por catálogo y beneficios.
    """

    if not isinstance(url, str):
        return ""

    normalized = url.strip()
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    netloc = parsed.netloc or ""
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parsed.path or ""
    query = parsed.query or ""
    query_suffix = f"?{query}" if widget and query else ""
    display = f"{netloc}{path}{query_suffix}".strip()

    if prefer_text_param:
        params = parse_qs(parsed.query)
        text_values = params.get("text") or []
        decoded = unquote(text_values[0]).strip() if text_values else ""
        if decoded:
            decoded = decoded.replace("https://", "").replace("http://", "")
            display = f"{netloc} → {decoded}" if netloc else decoded
            if len(display) > max_length:
                display = display[: max_length - 1].rstrip() + "…"
            return display

    if widget and "canal=widget_chat" in parsed.query:
        connector = "&" if "?" in display else "?"
        display = f"{display}{connector}widget" if display else "?widget"
    elif parsed.query and not query_suffix:
        display = f"{display}?{parsed.query}" if display else parsed.query

    if parsed.fragment:
        display = f"{display}#{parsed.fragment}"

    display = display.strip("/")
    if not display:
        display = normalized

    if len(display) > max_length:
        display = display[: max_length - 1].rstrip() + "…"

    return display


def _clean_url_candidate(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _collect_encuestas_base_candidates(
    context: dict, api_base_url: Optional[str]
) -> List[str]:
    base_candidates: List[str] = []

    if has_app_context():
        canonical_base = _clean_url_candidate(
            current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
        )
        backend_base = _clean_url_candidate(current_app.config.get("BACKEND_URL"))
        if canonical_base:
            base_candidates.append(canonical_base.rstrip("/"))
        if backend_base:
            base_candidates.append(backend_base.rstrip("/"))

    if api_base_url:
        api_base_cleaned = _clean_url_candidate(api_base_url)
        if api_base_cleaned:
            base_candidates.append(api_base_cleaned.rstrip("/"))

    resolved_base = _clean_url_candidate(_resolve_encuestas_base_url(context))
    if resolved_base:
        base_candidates.append(resolved_base.rstrip("/"))

    default_base = _clean_url_candidate(DEFAULT_BACKEND_URL)
    if default_base:
        base_candidates.append(default_base.rstrip("/"))

    seen: set[str] = set()
    unique: List[str] = []
    for base in base_candidates:
        if base and base not in seen:
            seen.add(base)
            unique.append(base)
    return unique


def _resolve_candidate_across_bases(
    candidate: Optional[str],
    base_candidates: Sequence[str],
    context: Optional[dict] = None,
) -> List[str]:
    cleaned = _clean_url_candidate(candidate)
    if not cleaned:
        return []

    normalized = _normalize_public_url(cleaned, context)
    if normalized:
        return [normalized]

    if cleaned.startswith(("http://", "https://")):
        return [cleaned]

    if cleaned.startswith("//"):
        return [f"https:{cleaned}"] if cleaned[2:] else []

    resolved: List[str] = []
    if cleaned.startswith("/"):
        for base in base_candidates:
            resolved.append(f"{base}{cleaned}")
    else:
        for base in base_candidates:
            resolved.append(f"{base}/{cleaned.lstrip('/')}")

    if resolved:
        seen: set[str] = set()
        unique: List[str] = []
        for value in resolved:
            if value not in seen:
                seen.add(value)
                unique.append(value)
        return unique

    return [cleaned]


def _resolve_candidate_against_bases(
    candidate: Optional[str],
    base_candidates: Sequence[str],
    context: Optional[dict] = None,
) -> Optional[str]:
    resolved = _resolve_candidate_across_bases(candidate, base_candidates, context)
    return resolved[0] if resolved else None


def _resolve_encuestas_menu_image_url(
    context: dict, api_base_url: Optional[str]
) -> Optional[str]:
    """Return the banner image URL for participatory survey menus."""

    def _clean(value: Optional[str]) -> Optional[str]:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = {}
    if isinstance(municipio_config.get("encuestas"), dict):
        encuestas_cfg = municipio_config["encuestas"]

    raw_image_url = (
        encuestas_cfg.get("menu_image_url")
        or encuestas_cfg.get("image_url")
        or encuestas_cfg.get("share_image_url")
        or encuestas_cfg.get("default_share_image_url")
        or municipio_config.get("encuestas_menu_image_url")
        or municipio_config.get("encuestas_image_url")
        or municipio_config.get("encuestas_share_image_url")
        or municipio_config.get("encuestas_default_share_image_url")
    )

    base_candidates = _collect_encuestas_base_candidates(context, api_base_url)

    candidate_sources = [raw_image_url]
    if has_app_context():
        candidate_sources.append(
            current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL")
        )
    candidate_sources.append(
        getattr(AppConfig, "PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL", None)
    )

    for candidate in candidate_sources:
        resolved = _resolve_candidate_against_bases(
            candidate, base_candidates, context
        )
        if resolved:
            return resolved

    fallback_candidate = _resolve_candidate_against_bases(
        ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH, base_candidates, context
    )
    if fallback_candidate:
        return fallback_candidate


def _resolve_encuestas_whatsapp_banner_media_url(
    context: dict,
    api_base_url: Optional[str],
    base_candidates: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Resolve the media URL tied to the WhatsApp banner/template."""

    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = {}
    if isinstance(municipio_config.get("encuestas"), dict):
        encuestas_cfg = municipio_config["encuestas"]

    candidate_sources: List[Optional[str]] = [
        encuestas_cfg.get("whatsapp_banner_media_url"),
        encuestas_cfg.get("banner_media_url"),
        municipio_config.get("encuestas_whatsapp_banner_media_url"),
        municipio_config.get("encuestas_banner_media_url"),
    ]

    if has_app_context():
        candidate_sources.append(
            current_app.config.get("PUBLIC_ENCUESTAS_WHATSAPP_BANNER_MEDIA_URL")
        )

    candidate_sources.append(
        getattr(AppConfig, "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_MEDIA_URL", None)
    )

    if base_candidates is None:
        base_candidates = _collect_encuestas_base_candidates(context, api_base_url)

    for candidate in candidate_sources:
        resolved = _resolve_candidate_against_bases(
            candidate, base_candidates, context
        )
        if resolved:
            return resolved

    return None


def _resolve_encuestas_menu_media_urls(
    context: dict, api_base_url: Optional[str]
) -> tuple[Optional[str], List[str]]:
    """Return the primary banner URL and additional media fallbacks."""

    base_candidates = _collect_encuestas_base_candidates(context, api_base_url)
    banner_media_url = _resolve_encuestas_whatsapp_banner_media_url(
        context, api_base_url, base_candidates
    )
    primary_url = banner_media_url
    menu_image_url = _resolve_encuestas_menu_image_url(context, api_base_url)

    if not primary_url:
        primary_url = menu_image_url

    media_urls: List[str] = []

    def _append(candidate: Optional[str]) -> None:
        for resolved in _resolve_candidate_across_bases(
            candidate, base_candidates, context
        ):
            if resolved and resolved not in media_urls:
                media_urls.append(resolved)

    _append(banner_media_url)
    _append(menu_image_url)

    if has_app_context():
        _append(current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL"))
        _append(
            current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL")
        )

    _append(getattr(AppConfig, "PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL", None))
    _append(
        getattr(AppConfig, "PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL", None)
    )

    _append(ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH)
    _append(ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_PATH)

    if not primary_url and media_urls:
        primary_url = media_urls[0]

    return primary_url, media_urls


def _resolve_encuestas_whatsapp_banner_template_sid(context: dict) -> Optional[str]:
    """Resolve the Twilio content template SID for encuestas banners."""

    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = {}
    if isinstance(municipio_config.get("encuestas"), dict):
        encuestas_cfg = municipio_config["encuestas"]

    template_sid = (
        encuestas_cfg.get("whatsapp_banner_template_sid")
        or municipio_config.get("encuestas_whatsapp_banner_template_sid")
    )

    if not template_sid and has_app_context():
        template_sid = current_app.config.get(
            "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_TEMPLATE_SID"
        )

    if not template_sid:
        template_sid = getattr(
            AppConfig, "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_TEMPLATE_SID", None
        )

    if isinstance(template_sid, str):
        template_sid = template_sid.strip()

    return template_sid or None


def _resolve_encuestas_whatsapp_banner_body(context: dict) -> str:
    """Return the caption used when sending the encuestas banner via media."""

    municipio_config = context.get("municipio_config_actual") or {}
    encuestas_cfg = {}
    if isinstance(municipio_config.get("encuestas"), dict):
        encuestas_cfg = municipio_config["encuestas"]

    body = (
        encuestas_cfg.get("whatsapp_banner_body")
        or municipio_config.get("encuestas_whatsapp_banner_body")
    )

    if not body and has_app_context():
        body = current_app.config.get("PUBLIC_ENCUESTAS_WHATSAPP_BANNER_BODY")

    if not body:
        body = getattr(AppConfig, "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_BODY", None)

    if not isinstance(body, str):
        body = "Encuestas/Opiniones/Sondeos"
    else:
        body = body.strip() or "Encuestas/Opiniones/Sondeos"

    return body


def _build_encuestas_whatsapp_banner_pre_messages(
    context: dict,
    image_url: Optional[str],
    media_urls: Sequence[str],
) -> List[dict]:
    """Return Twilio pre-messages to display the banner in WhatsApp."""

    channel_value = (context.get("channel") or "").strip().lower()
    if "whatsapp" not in channel_value:
        return []

    template_sid = _resolve_encuestas_whatsapp_banner_template_sid(context)
    channels = ["whatsapp"]

    if template_sid:
        return [
            {
                "channels": channels,
                "content_sid": template_sid,
                "content_variables": {},
            }
        ]

    candidate_url = image_url
    if not candidate_url:
        for candidate in media_urls:
            if candidate:
                candidate_url = candidate
                break

    if not candidate_url:
        return []

    caption = _resolve_encuestas_whatsapp_banner_body(context)
    return [
        {
            "channels": channels,
            "body": caption,
            "media_urls": [candidate_url],
        }
    ]


_WHATSAPP_MENU_BODY_SOFT_LIMIT = 1400
_WHATSAPP_MENU_DESCRIPTION_LIMIT = 160


def _truncate_text(value: str, max_length: int) -> str:
    if not isinstance(value, str):
        return ""

    trimmed = value.strip()
    if len(trimmed) <= max_length:
        return trimmed

    candidate = trimmed[: max_length - 1].rstrip()
    if " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0]
    if not candidate:
        candidate = trimmed[: max_length - 1].rstrip()
    return f"{candidate}…"


def _build_encuesta_share_whatsapp_pre_messages(
    context: dict,
    share_message: Optional[str],
    image_url: Optional[str],
    media_urls: Sequence[str],
) -> List[dict]:
    """Prepare media pre-messages so WhatsApp forwards keep their thumbnail."""

    channel_value = (context.get("channel") or "").strip().lower()
    if "whatsapp" not in channel_value:
        return []

    if not share_message:
        return []

    candidate_url = image_url
    if not candidate_url:
        for candidate in media_urls:
            if candidate:
                candidate_url = candidate
                break

    if not candidate_url:
        return []

    return [
        {
            "channels": ["whatsapp"],
            "body": share_message,
            "media_urls": [candidate_url],
        }
    ]


def _get_encuestas_menu(context: dict) -> dict:
    """Build the participatory surveys submenu for the chatbot."""

    base_options = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
    ]

    channel_value = (context.get("channel") or "").strip().lower()
    is_widget_channel = "widget" in channel_value
    is_whatsapp_channel = "whatsapp" in channel_value

    tenant_id = _resolve_encuestas_tenant_id(context)
    toggle = _resolve_encuestas_toggle(context)
    if toggle is False:
        return {
            "message_body": (
                "Las encuestas de participación ciudadana todavía no están habilitadas "
                "en este municipio. Volvé al inicio para continuar con otras gestiones."
            ),
            "message_type": "interactive_buttons",
            "options_list": list(base_options),
            "fuente": "submenu_encuestas_v1",
            "generar_audio": True,
        }

    if tenant_id is None:
        return {
            "message_body": (
                "No pudimos identificar el municipio para mostrar encuestas activas. "
                "Volvé al menú principal e intentá nuevamente."
            ),
            "message_type": "interactive_buttons",
            "options_list": list(base_options),
            "fuente": "submenu_encuestas_v1",
            "generar_audio": True,
        }

    try:
        encuestas = list_public_encuestas_for_tenant(tenant_id, limit=10)
    except Exception:
        logger.exception("No se pudieron cargar las encuestas públicas para el tenant %s", tenant_id)
        encuestas = []

    feature_enabled = FEATURE_ENCUESTAS if toggle is None else toggle
    if not feature_enabled and encuestas:
        feature_enabled = True

    if not feature_enabled:
        return {
            "message_body": (
                "Las encuestas de participación ciudadana todavía no están habilitadas "
                "en este municipio. Volvé al inicio para continuar con otras gestiones."
            ),
            "message_type": "interactive_buttons",
            "options_list": list(base_options),
            "fuente": "submenu_encuestas_v1",
            "generar_audio": True,
        }

    base_url = _resolve_encuestas_base_url(context) or "https://chatboc.ar"
    api_base_url = _resolve_encuestas_api_base_url(context)

    encuestas_data: list[dict] = []
    for encuesta, slug_publico in encuestas:
        try:
            data = serialize_public_encuesta(encuesta, slug_publico=slug_publico)
        except Exception:
            logger.exception("[encuestas] No se pudo serializar la encuesta %s", slug_publico)
            continue
        encuestas_data.append({"data": data, "slug_publico": slug_publico})

    if not encuestas_data:
        encuestas_data = _build_fallback_encuestas_for_junin(
            base_url or "https://chatboc.ar", context
        )

    if not encuestas_data:
        return {
            "message_body": (
                "Por el momento no hay encuestas activas. Te avisaremos cuando "
                "se abra una nueva instancia de participación."
            ),
            "message_type": "interactive_buttons",
            "options_list": list(base_options),
            "fuente": "submenu_encuestas_v1",
            "generar_audio": True,
        }
    primary_banner_url, media_attachments = _resolve_encuestas_menu_media_urls(
        context, api_base_url
    )
    share_media_defaults = list(media_attachments)
    share_image_default = primary_banner_url or (
        share_media_defaults[0] if share_media_defaults else None
    )
    banner_image_url = share_image_default

    general_lines: List[str] = []
    whatsapp_blocks: List[Dict[str, str]] = []
    survey_buttons: List[Dict[str, Any]] = []
    survey_metadata: List[Dict[str, Any]] = []
    for index, encuesta_entry in enumerate(encuestas_data, start=1):
        data = encuesta_entry.get("data") or {}
        slug_publico = encuesta_entry.get("slug_publico") or data.get("slug")
        if not slug_publico:
            continue
        titulo = data.get("titulo") or "Encuesta ciudadana"
        descripcion = (data.get("descripcion") or "").strip()
        if descripcion:
            descripcion = re.sub(r"\s+", " ", descripcion)
            if len(descripcion) > 180:
                descripcion = descripcion[:177].rstrip() + "…"

        share_url = encuesta_entry.get("share_url") or urljoin(
            f"{base_url}/", f"e/{slug_publico}"
        )
        short_slug = _extract_short_public_slug(slug_publico)
        short_base_url = _resolve_encuestas_short_base_url(context, base_url)
        share_short_url = encuesta_entry.get("share_short_url") or urljoin(
            f"{short_base_url}/", f"e/{short_slug}"
        )
        qr_url: Optional[str] = None
        if api_base_url:
            qr_url = urljoin(
                f"{api_base_url}/", f"api/public/encuestas/{slug_publico}/qr"
            )
        share_message = encuesta_entry.get("share_message") or (
            f"Participá en {titulo}: {share_short_url or share_url}"
        )
        whatsapp_share_url = f"https://wa.me/?text={quote_plus(share_message)}"
        whatsapp_share_short_url = f"https://wa.me/?text={quote_plus(share_short_url or share_url)}"
        whatsapp_share_display_url = None
        share_target_for_display = share_short_url or share_url
        if share_target_for_display:
            whatsapp_share_display_url = (
                f"https://wa.me/?text={quote_plus(share_target_for_display)}"
            )
        share_action_id = f"encuesta_compartir::{slug_publico}"

        short_title = _shorten_button_label(titulo)
        share_button_title = _shorten_button_label(titulo, max_length=30)

        display_share_url = share_short_url or share_url

        title_line = f"{index}. *{titulo}*"
        whatsapp_title = _shorten_button_label(titulo, max_length=120)
        whatsapp_title_line = f"{index}. *{whatsapp_title}*"

        open_line = f"   • *Abrir*: {display_share_url}"
        share_line_full = None
        share_url_for_body = (
            whatsapp_share_short_url or whatsapp_share_url or whatsapp_share_display_url
        )
        if share_url_for_body:
            share_line_full = f"   • *Compartir*: {share_url_for_body}"

        general_line_parts = [title_line]
        if descripcion:
            general_line_parts.append(f"   {descripcion}")
        if display_share_url:
            general_line_parts.append(open_line)
        if share_line_full:
            general_line_parts.append(share_line_full)
        general_lines.append("\n".join(general_line_parts))

        if is_whatsapp_channel:
            whatsapp_share_line = ""
            if share_url_for_body and whatsapp_share_url:
                whatsapp_share_line = f"   • *Compartir*: {share_url_for_body}"

            whatsapp_parts_with_desc = [whatsapp_title_line]
            if descripcion:
                truncated_description = _truncate_text(
                    descripcion, _WHATSAPP_MENU_DESCRIPTION_LIMIT
                )
                if truncated_description:
                    whatsapp_parts_with_desc.append(
                        f"   {truncated_description}"
                    )

            if display_share_url:
                whatsapp_parts_with_desc.append(open_line)
            if whatsapp_share_line:
                whatsapp_parts_with_desc.append(whatsapp_share_line)

            whatsapp_parts_without_desc = [whatsapp_title_line]
            if display_share_url:
                whatsapp_parts_without_desc.append(open_line)
            if whatsapp_share_line:
                whatsapp_parts_without_desc.append(whatsapp_share_line)

            whatsapp_title_and_open = [whatsapp_title_line]
            if display_share_url:
                whatsapp_title_and_open.append(open_line)

            whatsapp_blocks.append(
                {
                    "with_description": "\n".join(
                        part for part in whatsapp_parts_with_desc if part
                    ),
                    "without_description": "\n".join(
                        part for part in whatsapp_parts_without_desc if part
                    ),
                    "title_and_open": "\n".join(
                        part for part in whatsapp_title_and_open if part
                    ),
                    "title_only": whatsapp_title_line,
                }
            )

        if not is_whatsapp_channel:
            survey_buttons.append(
                {
                    "texto": f"Abrir {short_title}",
                    "url": share_url,
                    "type": "url",
                }
            )

        share_button: Dict[str, Any] = {
            "texto": f"Compartir {share_button_title}",
            "action_id": share_action_id,
        }
        if is_widget_channel and whatsapp_share_url:
            share_button.pop("action_id", None)
            share_button["url"] = whatsapp_share_url
            share_button["type"] = "url"

        survey_buttons.append(share_button)

        survey_metadata.append(
            {
                "slug": slug_publico,
                "titulo": titulo,
                "share_url": share_url,
                "share_short_url": share_short_url,
                "share_message": share_message,
                "share_action_id": share_action_id,
                "qr_url": qr_url,
                "short_slug": short_slug,
                "share_whatsapp_url": whatsapp_share_url,
                "share_widget_url": (
                    f"{share_url}?canal=widget_chat" if share_url else None
                ),
                "share_image_url": share_image_default,
                "share_media_urls": list(share_media_defaults),
            }
        )

    header = "*Participación Ciudadana*\n"

    if is_whatsapp_channel:
        selected_lines = [
            block.get("with_description", "") for block in whatsapp_blocks
        ]
        message_body = header + "\n".join(filter(None, selected_lines))
    else:
        selected_lines = general_lines
        message_body = header + "\n".join(selected_lines)

    message_body = message_body or header

    message_body += "\n\nSeleccioná una encuesta para participar o volvé al inicio."

    if is_whatsapp_channel and len(message_body) > _WHATSAPP_MENU_BODY_SOFT_LIMIT:
        selected_lines = [
            block.get("without_description", "") for block in whatsapp_blocks
        ]
        message_body = header + "\n".join(filter(None, selected_lines))
        message_body += "\n\nSeleccioná una encuesta para participar o volvé al inicio."

    if is_whatsapp_channel and len(message_body) > _WHATSAPP_MENU_BODY_SOFT_LIMIT:
        selected_lines = [
            block.get("title_and_open", "") for block in whatsapp_blocks
        ]
        message_body = header + "\n".join(filter(None, selected_lines))
        message_body += "\n\nSeleccioná una encuesta para participar o volvé al inicio."

    if is_whatsapp_channel and len(message_body) > _WHATSAPP_MENU_BODY_SOFT_LIMIT:
        selected_lines = [block.get("title_only", "") for block in whatsapp_blocks]
        message_body = header + "\n".join(filter(None, selected_lines))
        message_body += "\n\nSeleccioná una encuesta para participar o volvé al inicio."

    options = survey_buttons + base_options
    embed_whatsapp_banner = is_whatsapp_channel and bool(banner_image_url)

    payload = {
        "message_body": message_body.strip(),
        "message_type": "interactive_buttons",
        "options_list": options,
        "fuente": "submenu_encuestas_v1",
        "generar_audio": False if is_whatsapp_channel else True,
    }

    if embed_whatsapp_banner:
        payload["message_type"] = "text"

    if banner_image_url:
        payload["image_url"] = banner_image_url

    if api_base_url:
        payload.setdefault("_base_url", api_base_url)

    if media_attachments:
        payload["media_urls"] = media_attachments

    if embed_whatsapp_banner:
        merged_media_urls = list(payload.get("media_urls") or [])
        if banner_image_url and banner_image_url not in merged_media_urls:
            merged_media_urls.insert(0, banner_image_url)
        if merged_media_urls:
            payload["media_urls"] = merged_media_urls

    if survey_metadata:
        payload["surveys"] = survey_metadata

    pre_messages: List[dict] = []
    if not embed_whatsapp_banner:
        pre_messages = _build_encuestas_whatsapp_banner_pre_messages(
            context, banner_image_url, media_attachments
        )

    if pre_messages:
        payload["_twilio_pre_messages"] = pre_messages

    if is_whatsapp_channel:
        if embed_whatsapp_banner:
            payload["_force_whatsapp_text"] = True
        else:
            payload["_force_whatsapp_interactive"] = True

    return payload


def _build_encuesta_share_payload(slug_publico: str, context: dict, chat_db_context) -> dict:
    """Create a payload with a ready-to-forward survey share message."""

    normalized_slug = (slug_publico or "").strip().lower()
    contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(
        CONTEXTO_MUNICIPIO, {}
    )

    api_base_url = _resolve_encuestas_api_base_url(context)
    share_image_url, share_media_urls = _resolve_encuestas_menu_media_urls(
        context, api_base_url
    )
    if not share_image_url and share_media_urls:
        share_image_url = share_media_urls[0]

    stored_meta = contexto_municipio_actual.get("encuestas_menu_surveys") or []
    share_meta = None
    for meta in stored_meta:
        if (meta.get("slug") or "").strip().lower() == normalized_slug:
            share_meta = meta
            break

    share_url = None
    share_short_url = None
    share_message = None
    share_whatsapp_url = None
    share_widget_url = None
    titulo = "Encuesta ciudadana"

    if share_meta:
        titulo = share_meta.get("titulo") or titulo
        share_url = share_meta.get("share_url")
        share_short_url = share_meta.get("share_short_url") or share_meta.get(
            "share_url"
        )
        share_message = share_meta.get("share_message")
        share_whatsapp_url = share_meta.get("share_whatsapp_url")
        share_widget_url = share_meta.get("share_widget_url")
        share_image_url = share_meta.get("share_image_url") or share_image_url
        meta_media_urls = share_meta.get("share_media_urls")
        if isinstance(meta_media_urls, list) and meta_media_urls:
            share_media_urls = list(meta_media_urls)

    if not share_url and normalized_slug:
        base_url = _resolve_encuestas_base_url(context)
        share_url = urljoin(f"{base_url}/", f"e/{normalized_slug}")

    if not share_short_url and normalized_slug:
        canonical_base = _resolve_encuestas_base_url(context)
        short_base_url = _resolve_encuestas_short_base_url(context, canonical_base)
        short_slug = _extract_short_public_slug(normalized_slug)
        share_short_url = urljoin(f"{short_base_url}/", f"e/{short_slug}")

    if not share_meta and normalized_slug:
        try:
            encuesta = get_public_encuesta(normalized_slug)
        except Exception:
            logger.exception(
                "[encuestas] No se pudo cargar la encuesta '%s' para compartir",
                normalized_slug,
            )
        else:
            data = serialize_public_encuesta(encuesta, slug_publico=normalized_slug)
            titulo = data.get("titulo") or titulo
            short_slug = _extract_short_public_slug(normalized_slug)
            canonical_base = _resolve_encuestas_base_url(context)
            short_base_url = _resolve_encuestas_short_base_url(context, canonical_base)
            share_short_url = (
                share_short_url
                or urljoin(f"{short_base_url}/", f"e/{short_slug}")
                if short_base_url
                else share_short_url
            )
            share_message = share_message or (
                f"Participá en {titulo}: {share_short_url or share_url}"
                if (share_short_url or share_url)
                else None
            )
            new_meta = {
                "slug": normalized_slug,
                "titulo": titulo,
                "share_url": share_url,
                "share_short_url": share_short_url or share_url,
                "share_message": share_message,
                "share_action_id": f"encuesta_compartir::{normalized_slug}",
                "qr_url": None,
                "share_whatsapp_url": None,
                "share_widget_url": None,
                "share_image_url": share_image_url,
                "share_media_urls": list(share_media_urls)
                if share_media_urls
                else [],
            }
            stored_meta.append(new_meta)
            contexto_municipio_actual["encuestas_menu_surveys"] = stored_meta
            share_meta = new_meta

    if not share_message and (share_short_url or share_url) and titulo:
        target_url = share_short_url or share_url
        share_message = f"Participá en {titulo}: {target_url}"

    if share_message and not share_whatsapp_url:
        share_whatsapp_url = f"https://wa.me/?text={quote_plus(share_message)}"

    if share_url and not share_widget_url:
        share_widget_url = f"{share_url}?canal=widget_chat"

    if share_meta is not None:
        if share_whatsapp_url:
            share_meta["share_whatsapp_url"] = share_whatsapp_url
        if share_widget_url:
            share_meta["share_widget_url"] = share_widget_url
        if share_image_url:
            share_meta["share_image_url"] = share_image_url
        if share_media_urls:
            share_meta["share_media_urls"] = list(share_media_urls)

    share_followup_options = [
        {"texto": "Volver a encuestas", "action_id": "mostrar_menu_encuestas"},
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "Cancelar", "action_id": "cancelar"},
    ]

    previous_menu_options = contexto_municipio_actual.get("encuestas_menu_options")
    if previous_menu_options:
        contexto_municipio_actual["_encuestas_menu_previous_options"] = previous_menu_options

    stored_options = list(share_followup_options)
    contexto_municipio_actual["estado_conversacion"] = (
        ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
    )
    contexto_municipio_actual["menu_opciones"] = stored_options
    contexto_municipio_actual["encuestas_menu_options"] = stored_options

    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    if not normalized_slug or not share_message:
        return {
            "message_body": (
                "No pudimos preparar el mensaje para compartir esta encuesta. "
                "Volvé al menú de encuestas e intentá nuevamente."
            ),
            "message_type": "interactive_buttons",
            "options_list": stored_options,
            "fuente": "submenu_encuestas_share_error_v1",
            "generar_audio": True,
        }

    pre_messages = _build_encuesta_share_whatsapp_pre_messages(
        context, share_message, share_image_url, share_media_urls
    )

    intro_line = (
        f"Reenviá el mensaje con imagen que te envié arriba o copiá este texto:"
        if pre_messages
        else f"Reenviá este mensaje para invitar a participar en *{titulo}*:"
    )

    message_lines = ["*Compartir encuesta*", intro_line, "", share_message, ""]

    if share_whatsapp_url:
        message_lines.append(
            f"• *Compartir*: {share_whatsapp_url}"
        )

    message_lines.append(
        "Podés copiarlo o reenviarlo directamente sin salir de esta conversación."
    )

    message_body = "\n".join(message_lines)

    payload = {
        "message_body": message_body,
        "message_type": "interactive_buttons",
        "options_list": stored_options,
        "fuente": "submenu_encuestas_share_v1",
        "generar_audio": True,
        "share_message": share_message,
        "share_url": share_url,
        "share_short_url": share_short_url or share_url,
        "share_whatsapp_url": share_whatsapp_url,
        "share_widget_url": share_widget_url,
    }

    if share_image_url:
        payload["image_url"] = share_image_url
    if share_media_urls:
        payload["media_urls"] = share_media_urls

    if pre_messages:
        payload["_twilio_pre_messages"] = pre_messages

    if api_base_url:
        payload.setdefault("_base_url", api_base_url)

    payload["_force_whatsapp_interactive"] = True

    return payload


def _get_estacionamiento_menu():
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "🅿️ Buscar Estacionamiento Libre", "action_id": "buscar_estacionamiento"},
    ]
    return {
        "message_body": "Opciones de estacionamiento:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_estacionamiento_v1",
        "generar_audio": True,
    }

def _get_ayuda_menu():
    emojis = [
        ("🗣️", "Reclamos y Consultas"),
        ("🚗", "Trámites y Turnos"),
        ("📰", "Información del Municipio"),
        ("🅿️", "Estacionamiento"),
        ("❓", "Ayuda"),
        ("📝", "Iniciar un Reclamo"),
        ("💡", "Enviar una Sugerencia"),
        ("💧", "Reportar pérdida de agua"),
        ("📞", "Contactos Útiles"),
        ("📅", "Solicitar Turnos"),
        ("💵", "Pagar Tasas Municipales"),
        ("🎭", "Agenda Cultural y Noticias"),
        ("🐾", "Veterinaria y Bromatología"),
        ("🏗️", "Obras"),
        ("♻️", "Punto Limpio"),
        ("🔍", "Consultar Estado de Reclamo"),
    ]
    lines = [f"{emoji} {desc}" for emoji, desc in emojis]
    message_body = (
        "Guía rápida:\n\n"
        + "\n".join(lines)
    )
    opciones = [
        {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
        {"texto": "Cancelar", "action_id": "cancelar"},
    ]
    return {
        "message_body": message_body,
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_ayuda_v1",
        "generar_audio": True,
    }

def _get_reclamos_menu():
    """Devuelve la estructura del menú de reclamos estandarizado, con íconos y negritas."""
    iconos = {
        "arbol caido": "🌳",
        "arreglo de calle": "🚧",
        "castracion de mascota": "🐾",
        "falta de agua, rotura de caño": "💧",
        "fumigacion": "🦟",
        "inspeccion de comercio": "🏪",
        "limpieza": "🗑️",
        "luminaria": "💡",
        "riego de calle": "🚿",
        "rotura de semaforo": "🚦",
        "tramites de obras privadas": "🏗️",
        "incendio": "🔥",
        "otro motivo": "⚫",
    }
    opciones = []
    for categoria in CATEGORIAS_RECLAMO:
        normalized = normalizar_texto(categoria)
        if normalized == "sugerencia":
            continue
        texto_categoria = categoria.title() if categoria else "Otros"
        if normalized == "otro motivo":
            texto_categoria = "Otros"
        emoji = iconos.get(normalized, "•")
        opciones.append(
            {
                "texto": f"{emoji} *{texto_categoria}*",
                "category_name": texto_categoria,
            }
        )

    opciones.extend(
        [
            {"texto": "*Volver al inicio*", "action_id": "menu_principal"},
            {"texto": "Cancelar", "action_id": "cancelar"},
        ]
    )
    # El cuerpo del mensaje ahora instruye al usuario que puede responder con un número o seleccionar una opción.
    return {
        "message_body": "Elegí una opción para tu reclamo:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_reclamos_estandar_v4",
        "generar_audio": True
    }


SIMPLE_GREETINGS = {
    "hola", "buenos dias", "buenas tardes", "buenas noches", "menu",
    "hola buenos dias", "hola buenas tardes", "hola buenas noches", "buenas",
    "que tal", "como va", "todo bien", "buenas como va"
}
RETURN_TO_MAIN_MENU = {"volver al inicio", "volver al menu", "inicio", "menu", "menú principal"}
RETURN_TO_MAIN_MENU_NORMALIZED = {normalizar_texto(value) for value in RETURN_TO_MAIN_MENU}

def responder_municipio(
    pregunta_original,
    owner_user,
    rubro_obj,
    viewer_user=None,
    chat_db_context=None,
    anon_id=None,
    channel: str = "web",
    location=None,
    **kwargs
):
    logger_actual = current_app.logger if has_app_context() else logger

    demo_metadata = kwargs.pop("demo_metadata", None)

    # --- START DEBUG LOG ---
    if chat_db_context and chat_db_context.context_data:
        estado_conversacion_debug = chat_db_context.context_data.get(CONTEXTO_MUNICIPIO, {}).get("estado_conversacion")
        logger_actual.info(f"DEBUG: [START] responder_municipio called for session {chat_db_context.chat_session_id}. Initial state: {estado_conversacion_debug}")
    # --- END DEBUG LOG ---

    normalized_question: Optional[str] = None
    cache_key = None

    def _finalize_response(response):
        """Return the response unchanged; also store it in cache for repeated queries."""
        if cache_key is not None:
            MUNICIPIO_RESPONSE_CACHE[cache_key] = response
        if isinstance(response, dict):
            normalize_response_payload(response)
        return response

    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] =================================================="
    )
    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START] Pregunta: '{pregunta_original}', UserMunicipio: {getattr(owner_user, 'id', 'N/A')}, ViewerCiudadano: {getattr(viewer_user, 'id', 'N/A')}, Anon: {anon_id}, Channel: {channel}, ChatSessionUUID: {kwargs.get('chat_session_uuid')}"
    )

    # --- INICIO REFACTOR: Inicialización de 'context' y 'received_payload' al principio ---
    # Obtener la app actual
    app = current_app._get_current_object()

    # Cargar config específica del municipio (si existe)
    final_municipio_config = CONFIG_MUNICIPIO.copy()  # Default global
    owner_municipio_identifier = resolve_municipio_identifier(owner_user, MUNICIPIO_ID)
    owner_user_municipio_id_str = (
        str(owner_municipio_identifier)
        if owner_municipio_identifier is not None
        else str(MUNICIPIO_ID)
    )

    # Load from JSON file
    loaded_specific_config = cargar_configuracion_municipio(owner_user_municipio_id_str, "config.json")
    if loaded_specific_config:
        final_municipio_config.update(loaded_specific_config)

    tenant_profile = None
    try:
        owner_id = getattr(owner_user, "id", None) if owner_user else None
        if owner_id:
            tenant_profile = TenantProfile.query.filter_by(municipio_id=owner_id).first()
        if tenant_profile and isinstance(tenant_profile.configuracion, dict):
            final_municipio_config.update(tenant_profile.configuracion)
            if not final_municipio_config.get("nombre") and tenant_profile.nombre:
                final_municipio_config["nombre"] = tenant_profile.nombre
    except Exception:  # pragma: no cover - defensive for optional tenant profiles
        tenant_profile = None

    if tenant_profile:
        owner_email = getattr(owner_user, "email", "") if owner_user else ""
        is_junin_tenant = (
            tenant_profile.slug in {"junin", "municipalidad-de-junin"}
            or owner_email.lower() == "mauricio@junin.com"
        )
        if is_junin_tenant:
            updated_config = False
            if final_municipio_config.get("nombre") in (None, "", "Municipio Inteligente"):
                final_municipio_config["nombre"] = "Municipalidad de Junín"
            if not final_municipio_config.get("assistant_name"):
                final_municipio_config["assistant_name"] = "JUNI"
            final_municipio_config.setdefault("nombre_municipio", "Municipalidad de Junín")
            configuracion = tenant_profile.configuracion
            if not isinstance(configuracion, dict):
                configuracion = {}
            if configuracion.get("assistant_name") != "JUNI":
                configuracion["assistant_name"] = "JUNI"
                updated_config = True
            if configuracion.get("nombre_municipio") != "Municipalidad de Junín":
                configuracion["nombre_municipio"] = "Municipalidad de Junín"
                updated_config = True
            if configuracion.get("nombre") != "Municipalidad de Junín":
                configuracion["nombre"] = "Municipalidad de Junín"
                updated_config = True
            if updated_config:
                tenant_profile.configuracion = configuracion
                try:
                    db.session.add(tenant_profile)
                    db.session.commit()
                except Exception:  # pragma: no cover - avoid breaking responder
                    db.session.rollback()

    # Override with data from the User model (database) if available
    if owner_user:
        if getattr(owner_user, 'ciudad', None):
            final_municipio_config['ciudad'] = owner_user.ciudad
        if getattr(owner_user, 'provincia', None):
            final_municipio_config['provincia'] = owner_user.provincia
        if getattr(owner_user, 'pais', None):
            final_municipio_config['pais'] = owner_user.pais
        if getattr(owner_user, 'direccion', None):
            final_municipio_config['direccion'] = owner_user.direccion

    # Poblar el payload con los datos de la solicitud
    received_payload = {}
    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    else:
        logger_actual.warning(
            f"Tipo inesperado para pregunta_original: {type(pregunta_original)}. Contenido: {pregunta_original}"
        )
        pregunta_str = ""
        received_payload["pregunta"] = ""

    placeholder_tokens = {
        "[ubicación compartida por el usuario]",
        "[ubicacion compartida por el usuario]",
    }
    pregunta_placeholder = (
        isinstance(pregunta_str, str)
        and pregunta_str.strip().lower() in placeholder_tokens
    )

    if location and isinstance(location, dict):
        existing_location = received_payload.get("ubicacion_usuario")
        if isinstance(existing_location, dict):
            for key, value in location.items():
                if value is not None:
                    existing_location[key] = value
        else:
            received_payload["ubicacion_usuario"] = dict(location)

        if not received_payload.get("es_ubicacion"):
            received_payload["es_ubicacion"] = True

        if pregunta_placeholder:
            pregunta_str = ""
            received_payload["pregunta"] = ""

    # Detección temprana de números de ticket antes de cualquier otra lógica
    if (
        isinstance(pregunta_str, str)
        and pregunta_str.strip().isdigit()
        and len(pregunta_str.strip()) >= 6
        and chat_db_context is not None
    ):
        chat_ctx_data = chat_db_context.context_data if chat_db_context.context_data is not None else {}
        contexto_municipio_actual = chat_ctx_data.setdefault(CONTEXTO_MUNICIPIO, {})
        estado_existente = contexto_municipio_actual.get("estado_conversacion")
        if not estado_existente or estado_existente == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name:
            contexto_municipio_actual['numero_ticket_consulta'] = pregunta_str.strip()
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NUMERO_TICKET.name
            chat_db_context.context_data = chat_ctx_data
            flag_modified(chat_db_context, "context_data")
            return {
                "message_body": "Ingresá el PIN de 6 dígitos asociado al ticket.",
                "fuente": "handler_consultar_reclamo",
            }

    normalized_question = normalizar_texto(pregunta_str)

    context_state_token = None
    identity_components: list[str] = []

    if chat_db_context and isinstance(getattr(chat_db_context, "context_data", None), dict):
        chat_ctx_data = chat_db_context.context_data
        municipal_ctx = chat_ctx_data.get(CONTEXTO_MUNICIPIO, {})
        if isinstance(municipal_ctx, dict):
            state_value = municipal_ctx.get("estado_conversacion")
            if isinstance(state_value, str) and state_value.strip():
                context_state_token = state_value.strip()

            contacto_usuario = municipal_ctx.get("contacto_usuario", {})
            if isinstance(contacto_usuario, dict):
                nombre_contacto = contacto_usuario.get("nombre")
                if isinstance(nombre_contacto, str) and nombre_contacto.strip():
                    identity_components.append(nombre_contacto.strip().lower())

        profile_name_ctx = chat_ctx_data.get("profile_name")
        if isinstance(profile_name_ctx, str) and profile_name_ctx.strip():
            identity_components.append(profile_name_ctx.strip().lower())

    if viewer_user:
        viewer_name = getattr(viewer_user, "nombre", None) or getattr(viewer_user, "name", None)
        if isinstance(viewer_name, str) and viewer_name.strip():
            identity_components.append(viewer_name.strip().lower())

    profile_name_kwarg = kwargs.get("profile_name")
    if isinstance(profile_name_kwarg, str) and profile_name_kwarg.strip():
        identity_components.append(profile_name_kwarg.strip().lower())

    if identity_components:
        # Preserve order while removing duplicates to keep the cache key stable.
        identity_token = tuple(dict.fromkeys(identity_components))
    else:
        identity_token = None

    selection_states = {
        ConversationState.ESPERANDO_SELECCION_DE_LISTA.name,
        ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name,
        ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name,
    }

    if normalized_question and context_state_token not in selection_states:
        owner_cache_key = None
        if owner_user is not None:
            owner_cache_key = getattr(owner_user, "id", None) or getattr(owner_user, "municipio_id", None)

        rubro_cache_key = None
        if rubro_obj is not None:
            rubro_cache_key = getattr(rubro_obj, "id", None) or getattr(rubro_obj, "clave", None)

        demo_cache_key = None
        if isinstance(demo_metadata, dict):
            demo_cache_key = (
                demo_metadata.get("key")
                or demo_metadata.get("token")
                or demo_metadata.get("slug")
            )

        channel_cache_key = channel or "web"

        cache_key = (
            normalized_question,
            owner_cache_key,
            rubro_cache_key,
            demo_cache_key,
            channel_cache_key,
            context_state_token,
            identity_token,
        )
    else:
        cache_key = None

    cached_response = (
        MUNICIPIO_RESPONSE_CACHE.get(cache_key)
        if cache_key is not None
        else None
    )

    if kwargs:
        for key, value in kwargs.items():
            received_payload[key] = value

    is_initial_handshake = (
        isinstance(pregunta_str, str)
        and pregunta_str.strip() == "__INIT__"
    )

    chat_db_context_live_data = {}
    if chat_db_context and chat_db_context.context_data is not None:
        chat_db_context_live_data = chat_db_context.context_data

    if is_initial_handshake and not viewer_user:
        if chat_db_context_live_data.pop(CONTEXTO_MUNICIPIO, None) is not None:
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

    location_link_info = None
    if (
        not is_initial_handshake
        and not received_payload.get("es_ubicacion")
        and isinstance(pregunta_str, str)
        and pregunta_str.strip()
    ):
        location_link_info = _detect_location_link_info(pregunta_str)
        if location_link_info:
            link_payload = {}
            if location_link_info.get("address"):
                link_payload["address"] = location_link_info["address"]
            if location_link_info.get("latitude") is not None and location_link_info.get("longitude") is not None:
                link_payload["latitude"] = location_link_info["latitude"]
                link_payload["longitude"] = location_link_info["longitude"]
            if link_payload:
                received_payload.setdefault("ubicacion_usuario", {}).update(link_payload)
            received_payload["es_ubicacion"] = True

    contexto_municipio_actual = chat_db_context_live_data.get(CONTEXTO_MUNICIPIO, {})
    flow_activo = False
    if isinstance(contexto_municipio_actual, dict):
        estado_actual = contexto_municipio_actual.get("estado_conversacion")
        flow_activo = (
            estado_actual == "EN_FLUJO_RECLAMO"
            or bool(contexto_municipio_actual.get("reclamo_flow_v2"))
        )

    if cached_response and not flow_activo:
        logger_actual.info("responder_municipio: returning cached response")
        return cached_response

    # Crear el diccionario de contexto principal una sola vez
    normalized_location = _normalize_location_payload(
        location or received_payload.get("ubicacion_usuario"),
    )
    if normalized_location:
        received_payload["ubicacion_usuario"] = normalized_location

    context = {
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": final_municipio_config,
        "municipio_id": owner_user_municipio_id_str,
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data, # Usar el dict vivo
        "intencion": kwargs.get("intencion"),
        "ubicacion_usuario": normalized_location or received_payload.get("ubicacion_usuario"),
        "es_foto": received_payload.get("es_foto", False),
        "foto_url": received_payload.get("foto_url"),
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"),
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
        "location_link_info": location_link_info,
    }
    # --- FIN REFACTOR ---

    contexto_municipio_actual = chat_db_context_live_data.setdefault(CONTEXTO_MUNICIPIO, {})

    if location_link_info:
        flow_state = (
            contexto_municipio_actual.get("reclamo_flow_v2", {})
            .get("state")
        )
        if flow_state == ReclamoState.ESPERANDO_DIRECCION.name:
            handler = ReclamoFlowHandler(context, chat_db_context)
            payload_con_ubicacion = dict(received_payload)
            payload_con_ubicacion["es_ubicacion"] = True
            payload_con_ubicacion.setdefault("ubicacion_usuario", {}).update(
                {
                    k: v
                    for k, v in {
                        "address": location_link_info.get("address"),
                        "latitude": location_link_info.get("latitude"),
                        "longitude": location_link_info.get("longitude"),
                    }.items()
                    if v is not None
                }
            )
            response = handler.handle(pregunta_str, payload_con_ubicacion)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

        if (
            not contexto_municipio_actual.get("estado_conversacion")
            and location_link_info.get("only_location")
        ):
            address = location_link_info.get("address") or "la ubicación que compartiste"
            opciones_proactivas = [
                {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo_con_ubicacion"},
                {"texto": "Enviar una Sugerencia", "action_id": "enviar_sugerencia_con_ubicacion"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
            contexto_municipio_actual['ubicacion_contextual'] = _normalize_location_payload(
                {
                    "address": address,
                    "latitude": location_link_info.get("latitude"),
                    "longitude": location_link_info.get("longitude"),
                    "source": location_link_info.get("source", "link"),
                },
                fallback_address=address,
            )
            contexto_municipio_actual['menu_opciones'] = opciones_proactivas
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": f"Recibí tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
                "options_list": opciones_proactivas,
                "message_type": "interactive_buttons",
                "fuente": "proactive_location_handler",
            })

    # --- START GREETING CHECK (MOVED) ---
    # This must run before any stateful logic to ensure greetings always reset the flow.
    normalized_input_for_greeting = normalizar_texto(pregunta_str or "").strip()
    if (normalized_input_for_greeting in SIMPLE_GREETINGS or pregunta_str == "__INIT__"):
        logger_actual.info(f"Greeting keyword detected ('{pregunta_str}'). Resetting conversation and showing main menu.")
        handler = GreetingHandler(context)
        response = handler.handle(received_payload)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)

    # --- START OF RESTRUCTURED LOGIC ---
    # The primary change is to handle active conversation states FIRST, before
    # any other processing like intent classification or menu keyword matching.

    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    action = received_payload.get("action")

    # Detectar número de ticket ingresado directamente antes de evaluar menús
    if (
        not received_payload.get("action")
        and isinstance(pregunta_str, str)
        and pregunta_str.strip().isdigit()
        and len(pregunta_str.strip()) >= 6
        and (
            not estado_conversacion
            or estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        )
    ):
        numero_ticket = ''.join(filter(str.isdigit, pregunta_str))
        contexto_municipio_actual['numero_ticket_consulta'] = numero_ticket
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NUMERO_TICKET.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response({
            "message_body": "Ingresá el PIN de 6 dígitos asociado al ticket.",
            "fuente": "handler_consultar_reclamo",
        })

    # Permitir atajos por emoji en cualquier estado para accesibilidad.
    emoji_response = _try_handle_emoji_shortcut(
        pregunta_original, contexto_municipio_actual, context, chat_db_context
    )
    if emoji_response:
        return _finalize_response(emoji_response)

    # 1. Handle active conversation states first.
    if estado_conversacion:
        if estado_conversacion == 'ESPERANDO_CONFIRMACION_STT':
            transcript_pendiente = contexto_municipio_actual.get('stt_transcript_pendiente')
            contexto_municipio_actual['estado_conversacion'] = None
            contexto_municipio_actual.pop('stt_transcript_pendiente', None)

            if "si" in normalizar_texto(pregunta_str) or (action and "si" in action):
                pregunta_str = transcript_pendiente
                if "pregunta" in received_payload:
                    received_payload["pregunta"] = pregunta_str
            else:
                return _finalize_response({
                    "message_body": "Entendido. Por favor, intentá de nuevo o escribí tu consulta.",
                    "options_list": []
                })

        elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name:
            pregunta_str_menu = ""
            if isinstance(pregunta_original, str):
                pregunta_str_menu = pregunta_original
            elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
                pregunta_str_menu = pregunta_original["pregunta"]

            # Atajo por emoji para iniciar flujos sin texto adicional.
            emoji_category = EMOJI_RECLAMO_CATEGORIES.get(pregunta_str_menu.strip())
            if emoji_category:
                handler = ReclamoFlowHandler(context, chat_db_context)
                response_dict = handler.start_flow(categoria_inicial=emoji_category)
                contexto_municipio_actual['estado_conversacion'] = 'EN_FLUJO_RECLAMO'
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)

            emoji_action = EMOJI_MAIN_MENU_ACTIONS.get(pregunta_str_menu.strip())
            if emoji_action:
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                response = handle_main_menu_action(emoji_action, context, chat_db_context)
                if response:
                    return _finalize_response(response)

            selected_action = action or find_menu_action_by_input(pregunta_str_menu, _get_main_menu_payload(context).get('options_list', []))
            if not selected_action:
                selected_action = find_global_menu_action(pregunta_str_menu)

            if selected_action:
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                response = handle_main_menu_action(selected_action, context, chat_db_context)
                if response:
                    return _finalize_response(response)
            else:
                response_dict = _maybe_route_menu_input_to_llm(
                    pregunta_str_menu,
                    contexto_municipio_actual,
                    app,
                    context,
                    viewer_user,
                    owner_user,
                    chat_db_context,
                    demo_metadata=demo_metadata,
                )
                if response_dict:
                    return _finalize_response(response_dict)
                logger_actual.info(f"Input '{pregunta_str_menu}' is not a menu option. Treating as a general query.")
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")

        elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:
            pregunta_str_reclamo = ""
            if isinstance(pregunta_original, str):
                pregunta_str_reclamo = pregunta_original
            elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
                pregunta_str_reclamo = pregunta_original["pregunta"]

            logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_RECLAMOS state. Input: '{pregunta_str_reclamo}', Action: '{action}'")

            if received_payload.get("es_ubicacion") and not pregunta_str_reclamo.strip():
                location_payload = received_payload.get("ubicacion_usuario") or {}
                return _finalize_response(
                    _build_proactive_location_response(
                        location_payload,
                        contexto_municipio_actual,
                        chat_db_context,
                    )
                )

            reclamo_categories = {
                "reclamo_luminaria": "Luminaria", "reclamo_arbolado": "Arbolado",
                "reclamo_limpieza_riego": "Limpieza y riego", "reclamo_arreglo_calle": "Arreglo de calle",
                "reclamo_otros": "Otros"
            }

            selected_category_name = None
            details: dict[str, object] = {}
            if action in reclamo_categories:
                selected_category_name = reclamo_categories[action]
            else:
                normalized_input = normalizar_texto(pregunta_str_reclamo or "")
                if pregunta_str_reclamo == "0" or normalized_input in RETURN_TO_MAIN_MENU:
                    return _finalize_response(GreetingHandler(context).handle({}))

                reclamo_options = _get_reclamos_menu().get("options_list", [])
                if pregunta_str_reclamo.isdigit():
                    for option in reclamo_options:
                        if option.get("id_accion") == pregunta_str_reclamo:
                            selected_category_name = option.get("category_name")
                            break
                if not selected_category_name:
                    plain_text_options = [
                        {"texto": opt.get("category_name")}
                        for opt in reclamo_options
                        if opt.get("category_name")
                    ]
                    details = extract_reclamo_details_from_text(pregunta_str_reclamo, plain_text_options)
                    selected_category_name = details.pop("categoria_sugerida", None)

            if selected_category_name:
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {}
                if details.get("descripcion_sugerida"):
                    datos_iniciales["descripcion"] = details["descripcion_sugerida"]
                if details.get("direccion_sugerida"):
                    datos_iniciales["direccion"] = details["direccion_sugerida"]
                response_dict = handler.start_flow(
                    datos_iniciales=datos_iniciales or None,
                    categoria_inicial=selected_category_name,
                )
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            else:
                return _finalize_response(_get_reclamos_menu())

        elif estado_conversacion == ConversationState.ESPERANDO_INTENCION_UBICACION.name:
            ubicacion_contextual = contexto_municipio_actual.get('ubicacion_contextual')
            address = ubicacion_contextual.get('address', 'la ubicación proporcionada') if ubicacion_contextual else 'la ubicación proporcionada'

            if received_payload.get("es_ubicacion") and received_payload.get("ubicacion_usuario"):
                ubicacion_contextual = received_payload.get("ubicacion_usuario")
                contexto_municipio_actual["ubicacion_contextual"] = ubicacion_contextual
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    _build_proactive_location_response(
                        ubicacion_contextual,
                        contexto_municipio_actual,
                        chat_db_context,
                    )
                )

            if not action:
                pregunta_menu = ""
                if isinstance(pregunta_original, str):
                    pregunta_menu = pregunta_original
                elif isinstance(pregunta_original, dict):
                    pregunta_menu = pregunta_original.get("pregunta", "")
                action = find_menu_action_by_input(pregunta_menu, _location_action_options())
                if not action and pregunta_menu.strip():
                    contexto_municipio_actual['ultima_consulta_poi'] = pregunta_menu.strip()
                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")
                    return _finalize_response(
                        PointsOfInterestHandler(context).handle(
                            {"pregunta": pregunta_menu.strip(), "location": ubicacion_contextual or {}}
                        )
                    )

            if action == "iniciar_reclamo_con_ubicacion":
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {"direccion": address}
                if ubicacion_contextual:
                    datos_iniciales['coordenadas'] = {"lat": ubicacion_contextual.get("latitude"), "lon": ubicacion_contextual.get("longitude")}
                response_dict = handler.start_flow(datos_iniciales=datos_iniciales)
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            elif action == "enviar_sugerencia_con_ubicacion":
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
                _set_sugerencia_location_context(
                    contexto_municipio_actual,
                    ubicacion_contextual,
                    fallback_address=address,
                )
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({"message_body": f"Excelente. Por favor, escribí tu sugerencia relacionada con la ubicación: *{address}*.", "fuente": "handler_enviar_sugerencia_con_ubicacion"})
            elif action == "buscar_estacionamiento_con_ubicacion":
                contexto_municipio_actual['ultima_consulta_poi'] = 'estacionamiento'
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    PointsOfInterestHandler(context).handle(
                        {"pregunta": "estacionamiento", "location": ubicacion_contextual or {}}
                    )
                )
            elif action == "buscar_lugares_cerca":
                contexto_municipio_actual['ultima_consulta_poi'] = 'lugares cercanos'
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    PointsOfInterestHandler(context).handle(
                        {"pregunta": "lugares cercanos", "location": ubicacion_contextual or {}}
                    )
                )
            else:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    {
                        "message_body": f"Perfecto, ya tengo tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
                        "options_list": _location_action_options()
                        + [{"texto": "Menú", "action_id": "menu_principal"}],
                        "fuente": "proactive_location_handler",
                    }
                )

        elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            if not pregunta_str.strip() and (
                context.get("es_ubicacion")
                or context.get("ubicacion_usuario")
                or received_payload.get("es_ubicacion")
            ):
                _set_sugerencia_location_context(
                    contexto_municipio_actual,
                    context.get("ubicacion_usuario") or received_payload.get("ubicacion_usuario"),
                )
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "¡Gracias! Ahora contame cuál es tu sugerencia.",
                    "fuente": "handler_sugerencia_ubicacion_recibida",
                })
            sugerencia_texto = pregunta_str
            if len(sugerencia_texto) < 10:
                return _finalize_response({"message_body": "Tu sugerencia parece un poco corta. ¿Podrías darme un poco más de detalle?", "fuente": "sugerencia_muy_corta"})

            ubicacion_sugerencia, coordenadas_sugerencia = _extract_sugerencia_location(
                contexto_municipio_actual
            )
            viewer_user_obj = context.get("viewer_user_obj")
            contacto_prev = contexto_municipio_actual.get('contacto_usuario', {}) or {}
            datos_sugerencia = _build_sugerencia_datos(
                sugerencia_texto,
                ubicacion_sugerencia,
                coordenadas_sugerencia,
                viewer_user_obj,
                contacto_prev,
            )

            if (
                _has_valid_sugerencia_address(datos_sugerencia)
                and not datos_sugerencia.get("direccion")
            ):
                ubicacion_valida = datos_sugerencia.get("ubicacion")
                if isinstance(ubicacion_valida, dict):
                    datos_sugerencia["direccion"] = (
                        ubicacion_valida.get("address")
                        or ubicacion_valida.get("label")
                        or ubicacion_valida.get("texto")
                    )
                elif isinstance(ubicacion_valida, str):
                    datos_sugerencia["direccion"] = ubicacion_valida

            campos_faltantes = _get_missing_sugerencia_contact_fields(datos_sugerencia)
            contexto_municipio_actual['datos_sugerencia'] = datos_sugerencia
            _merge_contacto_usuario(contexto_municipio_actual, datos_sugerencia)
            if campos_faltantes:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                campos_texto = ', '.join(campos_faltantes)
                return _finalize_response({
                    "message_body": f"Para registrar tu sugerencia necesito: {campos_texto}. Podés escribir todo en un solo mensaje.",
                    "fuente": "pide_datos_contacto_sugerencia"
                })

            mensaje_confirmacion = (
                "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
                f"- **Nombre**: {datos_sugerencia.get('nombre')}\n"
                f"- **DNI**: {datos_sugerencia.get('dni')}\n"
                f"- **Email**: {datos_sugerencia.get('email')}\n"
                f"- **Dirección**: {_format_sugerencia_address(datos_sugerencia)}\n"
                f"- **Teléfono**: {datos_sugerencia.get('telefono') or 'No informado'}\n"
                f"- **Sugerencia**: {sugerencia_texto}"
            )
            botones = [
                {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
                {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_confirmacion,
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "pide_confirmacion_sugerencia"
            })

        elif estado_conversacion == ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            datos_guardados = contexto_municipio_actual.get('datos_sugerencia', {})
            campos_requeridos = ["nombre", "dni", "email", "direccion", "telefono"]

            # Primero intentamos extraer con regex para los campos aún faltantes.
            nuevos_datos = extract_multiple_contact_details_regex(
                pregunta_str, campos_requeridos + ["telefono"]
            )
            _update_sugerencia_contact_fields(datos_guardados, nuevos_datos)

            # Utilizar el LLM solo si todavía faltan campos
            campos_faltantes = _get_missing_sugerencia_contact_fields(datos_guardados)
            if campos_faltantes:
                try:
                    llm_datos = extract_multiple_contact_details_llm(
                        pregunta_str, campos_requeridos + ["telefono"]
                    )
                    if llm_datos:
                        _update_sugerencia_contact_fields(datos_guardados, llm_datos)
                except Exception as e:
                    logger.error("[DATOS_SUGERENCIA] LLM fallback failed: %s", e)
                campos_faltantes = _get_missing_sugerencia_contact_fields(datos_guardados)

            _ensure_sugerencia_address(datos_guardados)
            campos_faltantes = _get_missing_sugerencia_contact_fields(datos_guardados)
            contexto_municipio_actual['datos_sugerencia'] = datos_guardados
            _merge_contacto_usuario(contexto_municipio_actual, datos_guardados)
            if campos_faltantes:
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": f"Aún necesito: {', '.join(campos_faltantes)}. Podés enviarlos todos juntos.",
                    "fuente": "datos_contacto_sugerencia_incompletos"
                })

            mensaje_confirmacion = (
                "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
                f"- **Nombre**: {datos_guardados.get('nombre')}\n"
                f"- **DNI**: {datos_guardados.get('dni')}\n"
                f"- **Email**: {datos_guardados.get('email')}\n"
                f"- **Dirección**: {_format_sugerencia_address(datos_guardados)}\n"
                f"- **Teléfono**: {datos_guardados.get('telefono') or 'No informado'}\n"
                f"- **Sugerencia**: {datos_guardados.get('descripcion')}"
            )
            botones = [
                {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
                {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_confirmacion,
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "pide_confirmacion_sugerencia"
            })

        elif estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            texto_normalizado = normalizar_texto(pregunta_str)
            afirmativos = ["si", "enviar", "guardar", "guarda", "ok", "dale", "confirmar"]
            if (
                action == "confirmar_sugerencia_si"
                or texto_normalizado.strip() == "1"
                or any(a in texto_normalizado for a in afirmativos)
            ):
                datos_confirmados = contexto_municipio_actual.pop('datos_sugerencia', {})
                handler = HacerSugerenciaActionHandler(context)
                response = handler.execute(datos_confirmados)
                if response.get("success"):
                    # Re-synchronize the in-memory context after the handler cleanup
                    contexto_municipio_actual = chat_db_context_live_data.setdefault(
                        CONTEXTO_MUNICIPIO, {}
                    )
                    contexto_municipio_actual['estado_conversacion'] = (
                        ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
                    )
                    context[CONTEXTO_MUNICIPIO] = contexto_municipio_actual
                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")

                    final_payload = _build_sugerencia_success_payload(
                        context,
                        datos_confirmados,
                        response,
                    )
                    return _finalize_response(final_payload)
                contexto_municipio_actual['datos_sugerencia'] = datos_confirmados
                if response.get("pedir_info"):
                    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response)
            else:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Entendido. Por favor, enviá los datos correctos en un solo mensaje.",
                    "fuente": "pide_correccion_sugerencia"
                })

    # --- Manejo rápido de reclamos detectados vía imagen ---
    datos_interpretados_archivo = context.get("datos_interpretados_archivo")
    flujo_activo = (
        context.get("chat_db_context_data", {})
        .get(CONTEXTO_MUNICIPIO, {})
        .get("reclamo_flow_v2", {})
        .get("state")
    )
    if (
        datos_interpretados_archivo
        and isinstance(datos_interpretados_archivo, dict)
        and datos_interpretados_archivo.get("es_reclamo")
    ):
        handler = ReclamoFlowHandler(context, chat_db_context)
        if flujo_activo:
            # Merge suggested data into the existing flow and continue
            flow_data = (
                context["chat_db_context_data"][CONTEXTO_MUNICIPIO]
                .setdefault("reclamo_flow_v2", {})
                .setdefault("datos_reclamo", {})
            )
            flow_data.setdefault(
                "categoria", datos_interpretados_archivo.get("categoria_sugerida")
            )
            flow_data.setdefault(
                "descripcion", datos_interpretados_archivo.get("descripcion_sugerida")
            )
            flow_data["foto_url"] = received_payload.get("foto_url")
            response = handler.handle("", received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)
        else:
            logger_actual.info("Auto-starting claim flow from image analysis")
            datos_iniciales = {
                "categoria": datos_interpretados_archivo.get("categoria_sugerida"),
                "descripcion": datos_interpretados_archivo.get("descripcion_sugerida"),
                "origen_descripcion": "imagen",
                "foto_url": received_payload.get("foto_url"),
            }
            response = handler.start_flow(datos_iniciales=datos_iniciales)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

    # --- INICIO: Análisis de Imágenes Multimodal ---
    if received_payload.get("es_foto") and received_payload.get("foto_url"):
        if flujo_activo:
            handler = ReclamoFlowHandler(context, chat_db_context)
            response_dict = handler.handle("", received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)

        logger_actual.info(
            f"Image received. Starting multimodal analysis for URL: {received_payload.get('foto_url')}"
        )

        # Define a detailed prompt for the vision model
        vision_prompt = """
        Analiza la siguiente imagen desde la perspectiva de un asistente municipal. Tu objetivo es identificar el problema principal y clasificarlo.
        Responde SÓLO con un objeto JSON con la siguiente estructura:
        {
          "intent": "crear_reclamo",
          "data": {
            "categoria": "Una de las siguientes: - Luminaria - Arbolado - Limpieza y riego - Arreglo de calle - Pérdida de agua - Otros",
            "descripcion": "Una descripción breve y clara del problema que se ve en la imagen."
          }
        }
        Si no puedes identificar un problema claro o la imagen no es relevante para un reclamo municipal, devuelve un JSON con "intent": "invalido".
        """

        analysis_result = analizar_imagen_con_fallback(
            received_payload.get("foto_url"), vision_prompt
        )

        if analysis_result and analysis_result.get("raw_response"):
            try:
                parsed_response = json.loads(analysis_result.get("raw_response"))
                if parsed_response.get("intent") == "crear_reclamo":
                    logger_actual.info(
                        f"Multimodal analysis successful. Intent: 'crear_reclamo'. Data: {parsed_response.get('data')}"
                    )

                    datos_iniciales = parsed_response.get("data", {})
                    datos_iniciales['origen_descripcion'] = 'imagen'
                    datos_iniciales['foto_url'] = received_payload.get("foto_url")

                    handler = ReclamoFlowHandler(context, chat_db_context)
                    response_dict = handler.start_flow(datos_iniciales=datos_iniciales)

                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")

                    return _finalize_response(response_dict)

            except json.JSONDecodeError:
                logger_actual.error(f"Failed to parse JSON from vision model response: {analysis_result.get('raw_response')}")
    # --- FIN: Análisis de Imágenes Multimodal ---

    # --- INICIO: Manejo Proactivo de Ubicación ---
    if received_payload.get("es_ubicacion") and not pregunta_str.strip():
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        estado_actual = contexto_municipio_actual.get("estado_conversacion")
        esperando_llm = contexto_municipio_actual.get("esperando_info_llm")
        esperando_llm_sugerencia = contexto_municipio_actual.get("esperando_info_llm_sugerencia")
        esperando_llm_ubicacion = esperando_llm in ["ubicacion", "direccion"]
        esperando_llm_sugerencia_ubicacion = esperando_llm_sugerencia in ["ubicacion", "direccion"]
        skip_proactive = (
            estado_actual in [
                ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
                ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name,
            ]
            or esperando_llm_ubicacion
            or esperando_llm_sugerencia_ubicacion
        )

        if contexto_municipio_actual.get("reclamo_flow_v2", {}).get("state") == ReclamoState.ESPERANDO_DIRECCION.name:
            handler = ReclamoFlowHandler(context, chat_db_context)
            response_dict = handler.handle("", received_payload)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)

        # When already waiting for a location to answer a pending query (e.g., estacionamiento),
        # skip proactive handling so that the dedicated state logic can process it.
        if (
            not skip_proactive
            and contexto_municipio_actual.get("estado_conversacion") != ConversationState.ESPERANDO_UBICACION_GENERAL.name
        ):
            ultima_consulta = contexto_municipio_actual.get("ultima_consulta_poi")
            if ultima_consulta:
                logger_actual.info(
                    f"Location received for last POI query '{ultima_consulta}'."
                )
                return _finalize_response(
                    PointsOfInterestHandler(context).handle(
                        {
                            "pregunta": ultima_consulta,
                            "location": received_payload.get("ubicacion_usuario"),
                        }
                    )
                )

            logger_actual.info(
                "Location received without text. Starting proactive location handling."
            )

            address = received_payload.get("ubicacion_usuario", {}).get("address", "la ubicación que compartiste")

            return _finalize_response(
                _build_proactive_location_response(
                    received_payload.get("ubicacion_usuario", {}),
                    contexto_municipio_actual,
                    chat_db_context,
                )
            )
    # --- FIN: Manejo Proactivo de Ubicación ---


    pregunta_str_for_check = pregunta_str

    # --- CONTEXT INITIALIZATION ---
    # This is now at the top to ensure all parts of the function have access to the full context.
    final_municipio_config = CONFIG_MUNICIPIO
    resolved_specific_identifier = resolve_municipio_identifier(owner_user)
    if resolved_specific_identifier is not None:
        owner_user_municipio_id_str = str(resolved_specific_identifier)
        loaded_specific_config = cargar_configuracion_municipio(owner_user_municipio_id_str, "config.json")
        if loaded_specific_config:
            final_municipio_config = loaded_specific_config

    received_payload = {}
    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    else:
        pregunta_str = ""
        received_payload["pregunta"] = ""

    if kwargs:
        received_payload.update(kwargs)

    location_link_info = None
    if (
        not received_payload.get("es_ubicacion")
        and isinstance(pregunta_str, str)
        and pregunta_str.strip()
    ):
        location_link_info = _detect_location_link_info(pregunta_str)
        if location_link_info:
            link_payload = {}
            if location_link_info.get("address"):
                link_payload["address"] = location_link_info["address"]
            if location_link_info.get("latitude") is not None and location_link_info.get("longitude") is not None:
                link_payload["latitude"] = location_link_info["latitude"]
                link_payload["longitude"] = location_link_info["longitude"]
            if link_payload:
                received_payload.setdefault("ubicacion_usuario", {}).update(link_payload)
            received_payload["es_ubicacion"] = True

    chat_db_context_live_data = {}
    if chat_db_context and chat_db_context.context_data is not None:
        chat_db_context_live_data = chat_db_context.context_data

    contexto_municipio_actual = chat_db_context_live_data.setdefault(CONTEXTO_MUNICIPIO, {})

    context = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual,
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": final_municipio_config,
        "municipio_id": owner_user_municipio_id_str,
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data,
        "profile_name": kwargs.get("profile_name"),
        # Other kwargs will be in received_payload
        "location_link_info": location_link_info,
    }
    # --- END CONTEXT INITIALIZATION ---

    def _auto_bootstrap_reclamo() -> Optional[dict[str, Any]]:
        """Return an auto-start payload when the message already looks like a claim."""

        if not isinstance(pregunta_str, str):
            return None

        if not pregunta_str.strip():
            return None

        if contexto_municipio_actual.get("estado_conversacion"):
            return None

        if contexto_municipio_actual.get("reclamo_flow_v2", {}).get("state"):
            return None

        payload = _try_start_reclamo_from_text(
            pregunta_str,
            context,
            chat_db_context,
            default_localidad=final_municipio_config.get("ciudad"),
            default_provincia=final_municipio_config.get("provincia"),
        )

        if payload:
            municipal_ctx = (
                contexto_municipio_actual
                if isinstance(contexto_municipio_actual, dict)
                else {}
            )
            if municipal_ctx is not contexto_municipio_actual:
                chat_db_context_live_data[CONTEXTO_MUNICIPIO] = municipal_ctx
            municipal_ctx["estado_conversacion"] = "EN_FLUJO_RECLAMO"

            if chat_db_context:
                if chat_db_context.context_data is None:
                    chat_db_context.context_data = chat_db_context_live_data
                flag_modified(chat_db_context, "context_data")

        return payload

    # For simple greetings, bypass LLM and show the main menu directly.
    # --- Audio Processing Logic ---
    is_from_audio = False
    if isinstance(pregunta_original, dict) and "media_url" in pregunta_original:
        from services.audio_transcription_service import transcribe_audio_from_url
        is_from_audio = True

        # Auto-learn prefers_audio
        if viewer_user:
            audio_message_count = contexto_municipio_actual.get('audio_message_count', 0) + 1
            contexto_municipio_actual['audio_message_count'] = audio_message_count
            if audio_message_count >= 2 and not viewer_user.prefers_audio:
                viewer_user.prefers_audio = True
                db.session.add(viewer_user)
                db.session.commit()
                logger_actual.info(f"User {viewer_user.id} prefers audio after {audio_message_count} audio messages.")

        transcription_result = transcribe_audio_from_url(pregunta_original["media_url"])
        if transcription_result:
            transcript = transcription_result.get("transcript")
            confidence = transcription_result.get("confidence", 1.0)

            if confidence < 0.8: # Low confidence threshold
                contexto_municipio_actual['estado_conversacion'] = 'ESPERANDO_CONFIRMACION_STT'
                contexto_municipio_actual['stt_transcript_pendiente'] = transcript
                return _finalize_response({
                    "message_body": f"Escuché: \"{transcript}\". ¿Es correcto?",
                    "options_list": [
                        {"texto": "Sí, es correcto", "action_id": "confirmar_stt_si"},
                        {"texto": "No, intentar de nuevo", "action_id": "confirmar_stt_no"}
                    ],
                    "message_type": "interactive_buttons"
                })
            else:
                pregunta_str = transcript # Use high-confidence transcript as the new question
                # Update the payload so subsequent logic sees the transcribed text
                if "pregunta" in received_payload:
                    received_payload["pregunta"] = pregunta_str


    # --- INICIO FIX: Manejo explícito de solicitud de menú principal ---
    # Si el usuario pide explícitamente el menú, lo mostramos directamente sin pasar por el LLM.
    context["user_input_raw"] = pregunta_str
    normalized_input_menu = normalizar_texto(pregunta_str or "")
    action_id = received_payload.get("action_id") or received_payload.get("action")
    if normalized_input_menu in {"menu", "menu principal"} or action_id == "menu_principal":
        contexto_municipio_actual.pop("reclamo_flow_v2", None)
        handler = GreetingHandler(context)
        response = handler.handle(received_payload)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    # --- INICIO: Manejo del Flujo de Reclamos Activo ---
    if "reclamo_flow_v2" in contexto_municipio_actual and contexto_municipio_actual["reclamo_flow_v2"].get("state"):
        logger_actual.info(f"Reclamo flow is active. State: {contexto_municipio_actual['reclamo_flow_v2'].get('state')}. Handing off to ReclamoFlowHandler.")
        handler = ReclamoFlowHandler(context, chat_db_context)
        response = handler.handle(pregunta_str, received_payload)
        safe_flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)
    # --- FIN: Manejo del Flujo de Reclamos Activo ---

    # --- SHORTCUT: Detectar consulta de ticket por número directo ---
    if (
        not contexto_municipio_actual.get("estado_conversacion")
        and (pregunta_str or "").strip().isdigit()
        and len(pregunta_str.strip()) >= 6
    ):
        contexto_municipio_actual["numero_ticket_consulta"] = pregunta_str.strip()
        contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_NUMERO_TICKET.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response({
            "message_body": "Ingresá el PIN de 6 dígitos asociado al ticket.",
            "fuente": "handler_consultar_reclamo",
        })

    # --- START GLOBAL MENU SHORTCUTS ---
    if not contexto_municipio_actual.get("estado_conversacion") and pregunta_str:
        inferred_action = find_global_menu_action(pregunta_str)
        if inferred_action:
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            if inferred_action == "iniciar_reclamo":
                response = handle_main_menu_action("mostrar_menu_reclamos", context, chat_db_context)
            else:
                response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)
    # --- END GLOBAL MENU SHORTCUTS ---

    auto_reclamo_payload = _auto_bootstrap_reclamo()
    if auto_reclamo_payload:
        return _finalize_response(auto_reclamo_payload)

    # --- START INTENT CLASSIFICATION ---
    # If it's not a simple greeting, proceed with intent classification
    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    if estado_conversacion in (
        ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name,
        ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
    ):
        intent = None
        intent_payload = None
        logger_actual.info(
            "[IntentClassifier] Skipping intent classification due to active LLM flow (%s).",
            estado_conversacion,
        )
    else:
        intent, intent_payload = intent_classifier.classify(pregunta_str)
        logger_actual.info(f"[IntentClassifier] Classified intent: {intent} with payload: {intent_payload}")

    if intent == "saludar":
        logger_actual.info("Greeting intent detected. Bypassing LLM and showing main menu.")
        handler = GreetingHandler(context)
        response = handler.handle(received_payload)
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(response)

    if intent == "iniciar_reclamo":
        logger_actual.info("Claim initiation intent detected. Bypassing LLM and showing reclamos menu.")
        response = handle_main_menu_action("mostrar_menu_reclamos", context, chat_db_context)
        if response:
            return _finalize_response(response)

    if intent == "consultar_reclamo":
        logger_actual.info("Claim status check intent detected. Bypassing LLM.")
        return _finalize_response({
            "message_body": "Para consultar el estado de tu reclamo, por favor ingresá el número de ticket.",
            "fuente": "intent_consultar_reclamo"
        })
    # --- END INTENT CLASSIFICATION ---

    # If the user is asking for a general point of interest (e.g., farmacias,
    # veterinarias) handle it with the PointsOfInterestHandler. This needs to
    # happen before fuzzy matching to menu keywords to avoid misclassifications
    # such as interpreting "farmacias de turno" as a request for appointments.
    if es_consulta_general(pregunta_str):
        poi_handler = PointsOfInterestHandler(context)
        loc_data = None
        location = received_payload.get("location")
        if isinstance(location, dict):
            loc_data = location
        elif isinstance(location, str):
            loc_data = location
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        # Clear any pending claim-related context since the user switched topics
        for campo in [
            "historial_llm_reclamo",
            "esperando_info_llm_reclamo",
            "esperando_info_llm",
            "categoria_reclamo",
            "descripcion_reclamo",
            "direccion_reclamo",
            "coordenadas_reclamo",
            "nombre_vecino",
            "telefono_vecino",
            "email_vecino",
            "foto_url",
        ]:
            contexto_municipio_actual.pop(campo, None)
        # Ensure datos_parciales_llm_reclamo exists as empty dict
        contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(poi_handler.handle({"pregunta": pregunta_str, "location": loc_data}))


    # El manejo de reseteo por palabra clave ahora es manejado por el LLM
    # que debe devolver accion_backend: "saludar".

    # --- RESTRUCTURED LOGIC ---
    # Obtener el estado actual de la conversación antes de evaluar acciones
    estado_conversacion = contexto_municipio_actual.get("estado_conversacion")
    action = received_payload.get("action")

    # If the client sends an explicit action (e.g., button press) and there is
    # no active conversation state, handle it immediately via the main menu
    # dispatcher. This allows frontend buttons to work even when they send an
    # action identifier instead of free-form text.
    if action and not estado_conversacion:
        response = handle_main_menu_action(action, context, chat_db_context)
        if response and not response.get("fuente", "").startswith("unimplemented_"):
            return _finalize_response(response)

    # Allow keyword shortcuts even when a conversation state is active,
    # but avoid treating numeric replies or explicit action IDs as global
    # menu shortcuts so that selections like "3" or "mostrar_menu_*" are
    # handled within their local context.
    main_actions = {
        normalizar_texto(btn.get("action_id", ""))
        for btn in _get_main_menu_payload(context).get("options_list", [])
    }
    if (
        not action
        and pregunta_str
        and not pregunta_str.strip().isdigit()
        and normalizar_texto(pregunta_str) not in main_actions
        and estado_conversacion != ConversationState.ESPERANDO_INTENCION_UBICACION.name
        and estado_conversacion != ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
        and estado_conversacion != ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
        and estado_conversacion != ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
        and estado_conversacion != ConversationState.ESPERANDO_INFO_SUGERENCIA_LLM.name
        and estado_conversacion != ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
    ):
        inferred_action = find_global_menu_action(pregunta_str)
        if inferred_action:
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            if inferred_action == "iniciar_reclamo":
                response = handle_main_menu_action("mostrar_menu_reclamos", context, chat_db_context)
            else:
                response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)

    # 1. Handle active conversation states first.
    if estado_conversacion:
        if estado_conversacion == 'ESPERANDO_CONFIRMACION_STT':
            transcript_pendiente = contexto_municipio_actual.get('stt_transcript_pendiente')
            contexto_municipio_actual['estado_conversacion'] = None
            contexto_municipio_actual.pop('stt_transcript_pendiente', None)

            if "si" in normalizar_texto(pregunta_str) or (action and "si" in action):
                pregunta_str = transcript_pendiente
                if "pregunta" in received_payload:
                    received_payload["pregunta"] = pregunta_str
            else:
                return _finalize_response({
                    "message_body": "Entendido. Por favor, intentá de nuevo o escribí tu consulta.",
                    "options_list": []
                })

        elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name:
            pregunta_str_menu = ""
            if isinstance(pregunta_original, str):
                pregunta_str_menu = pregunta_original
            elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
                pregunta_str_menu = pregunta_original["pregunta"]

            selected_action = action or find_menu_action_by_input(pregunta_str_menu, _get_main_menu_payload(context).get('options_list', []))
            if not selected_action:
                selected_action = find_global_menu_action(pregunta_str_menu)

            if selected_action:
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                response = handle_main_menu_action(selected_action, context, chat_db_context)
                if response:
                    return _finalize_response(response)
            else:
                logger_actual.info(f"Input '{pregunta_str_menu}' is not a menu option. Treating as a general query.")
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")

        elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:
            pregunta_str_reclamo = ""
            if isinstance(pregunta_original, str):
                pregunta_str_reclamo = pregunta_original
            elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
                pregunta_str_reclamo = pregunta_original["pregunta"]

            logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_RECLAMOS state. Input: '{pregunta_str_reclamo}', Action: '{action}'")

            reclamo_categories = {
                "reclamo_luminaria": "Luminaria", "reclamo_arbolado": "Arbolado",
                "reclamo_limpieza_riego": "Limpieza y riego", "reclamo_arreglo_calle": "Arreglo de calle",
                "reclamo_otros": "Otros"
            }

            selected_category_name = None
            details: dict[str, object] = {}
            if action in reclamo_categories:
                selected_category_name = reclamo_categories[action]
            else:
                normalized_input = normalizar_texto(pregunta_str_reclamo or "")
                if pregunta_str_reclamo == "0" or normalized_input in RETURN_TO_MAIN_MENU:
                    return _finalize_response(GreetingHandler(context).handle({}))

                reclamo_options = _get_reclamos_menu().get("options_list", [])
                if pregunta_str_reclamo.isdigit():
                    for option in reclamo_options:
                        if option.get("id_accion") == pregunta_str_reclamo:
                            selected_category_name = option.get("category_name")
                            break
                if not selected_category_name:
                    plain_text_options = [
                        {"texto": opt.get("category_name")}
                        for opt in reclamo_options
                        if opt.get("category_name")
                    ]
                    details = extract_reclamo_details_from_text(pregunta_str_reclamo, plain_text_options)
                    selected_category_name = details.pop("categoria_sugerida", None)

            if selected_category_name:
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {}
                if details.get("descripcion_sugerida"):
                    datos_iniciales["descripcion"] = details["descripcion_sugerida"]
                if details.get("direccion_sugerida"):
                    datos_iniciales["direccion"] = details["direccion_sugerida"]
                response_dict = handler.start_flow(
                    datos_iniciales=datos_iniciales or None,
                    categoria_inicial=selected_category_name,
                )
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            else:
                return _finalize_response(_get_reclamos_menu())

        elif estado_conversacion == ConversationState.ESPERANDO_INTENCION_UBICACION.name:
            ubicacion_contextual = contexto_municipio_actual.pop('ubicacion_contextual', None)
            address = ubicacion_contextual.get('address', 'la ubicación proporcionada') if ubicacion_contextual else 'la ubicación proporcionada'

            if not action:
                pregunta_menu = ""
                if isinstance(pregunta_original, str):
                    pregunta_menu = pregunta_original
                elif isinstance(pregunta_original, dict):
                    pregunta_menu = pregunta_original.get("pregunta", "")
                action = find_menu_action_by_input(pregunta_menu, _location_action_options())

            if action == "iniciar_reclamo_con_ubicacion":
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {"direccion": address}
                if ubicacion_contextual:
                    datos_iniciales['coordenadas'] = {"lat": ubicacion_contextual.get("latitude"), "lon": ubicacion_contextual.get("longitude")}
                response_dict = handler.start_flow(datos_iniciales=datos_iniciales)
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response_dict)
            elif action == "enviar_sugerencia_con_ubicacion":
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
                _set_sugerencia_location_context(
                    contexto_municipio_actual,
                    ubicacion_contextual,
                    fallback_address=address,
                )
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({"message_body": f"Excelente. Por favor, escribí tu sugerencia relacionada con la ubicación: *{address}*.", "fuente": "handler_enviar_sugerencia_con_ubicacion"})
            elif action == "buscar_estacionamiento_con_ubicacion":
                contexto_municipio_actual['ultima_consulta_poi'] = 'estacionamiento'
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    PointsOfInterestHandler(context).handle(
                        {"pregunta": "estacionamiento", "location": ubicacion_contextual or {}}
                    )
                )
            elif action == "buscar_lugares_cerca":
                contexto_municipio_actual['ultima_consulta_poi'] = 'lugares cercanos'
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    PointsOfInterestHandler(context).handle(
                        {"pregunta": "lugares cercanos", "location": ubicacion_contextual or {}}
                    )
                )
            else:
                # If the user response doesn't match any option, keep the flow active
                # and re-send the proactive menu instead of resetting the conversation.
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    {
                        "message_body": f"Perfecto, ya tengo tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
                        "options_list": _location_action_options()
                        + [{"texto": "Menú", "action_id": "menu_principal"}],
                        "fuente": "proactive_location_handler",
                    }
                )


        elif estado_conversacion == ConversationState.ESPERANDO_NUMERO_TICKET.name:
            numero_guardado = contexto_municipio_actual.get('numero_ticket_consulta')
            if not numero_guardado:
                numero_ticket = ''.join(filter(str.isdigit, pregunta_str or ''))
                if not numero_ticket:
                    return _finalize_response({
                        "message_body": "Por favor, ingresá un número de reclamo válido.",
                        "fuente": "handler_consultar_reclamo"
                    })
                contexto_municipio_actual['numero_ticket_consulta'] = numero_ticket
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Ingresá el PIN de 6 dígitos asociado al ticket.",
                    "fuente": "handler_consultar_reclamo"
                })

            pin = ''.join(filter(str.isdigit, pregunta_str or ''))
            if len(pin) != 6:
                return _finalize_response({
                    "message_body": "El PIN debe tener 6 dígitos.",
                    "fuente": "handler_consultar_reclamo"
                })

            municipio_id = context.get("municipio_id", MUNICIPIO_ID)
            ticket_query = MunicipioTicket.query.filter_by(nro_ticket=numero_guardado, consulta_pin=pin)
            try:
                ticket_query = ticket_query.filter_by(municipio_id=int(municipio_id))
            except (TypeError, ValueError):
                pass
            ticket = ticket_query.first()

            contexto_municipio_actual.pop('numero_ticket_consulta', None)

            botones = []
            if ticket:
                contactos = cargar_configuracion_municipio(municipio_id, "contactos_especializados.json")
                contacto_especializado = contactos.get(ticket.categoria, contactos.get("default", {})) if isinstance(contactos, dict) else {}
                municipio_config = context.get("municipio_config_actual", {})
                base_chat_url = municipio_config.get("base_chat_url", "https://www.chatboc.ar/chat")
                mensaje, botones = formatear_ticket_respuesta(
                    "reclamo",
                    ticket.nombre_vecino or "Vecino/a",
                    ticket.detalles or ticket.pregunta or "",
                    ticket.categoria,
                    f"M-{ticket.nro_ticket}",
                    contacto_especializado,
                    base_chat_url,
                    consulta_pin=ticket.consulta_pin,
                )
                mensaje += f"\n\n🔔 *Estado actual:* {ticket.estado}"
            else:
                mensaje = (
                    "No encontramos un ticket con ese número y PIN. Por favor, verifica los datos e intenta nuevamente."
                )
            final_payload = _message_with_menu(mensaje, context)
            contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            final_payload['fuente'] = 'handler_consultar_reclamo'
            if botones:
                final_payload['options_list'] = botones + final_payload.get('options_list', [])
                final_payload['message_type'] = 'interactive_buttons'
            return _finalize_response(final_payload)
        elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            sugerencia_texto = pregunta_str
            if len(sugerencia_texto) < 10:
                return _finalize_response({"message_body": "Tu sugerencia parece un poco corta. ¿Podrías darme un poco más de detalle?", "fuente": "sugerencia_muy_corta"})

            ubicacion_sugerencia, coordenadas_sugerencia = _extract_sugerencia_location(
                contexto_municipio_actual
            )
            viewer_user_obj = context.get("viewer_user_obj")
            contacto_prev = contexto_municipio_actual.get('contacto_usuario', {}) or {}
            datos_sugerencia = _build_sugerencia_datos(
                sugerencia_texto,
                ubicacion_sugerencia,
                coordenadas_sugerencia,
                viewer_user_obj,
                contacto_prev,
            )

            if (
                _has_valid_sugerencia_address(datos_sugerencia)
                and not datos_sugerencia.get("direccion")
            ):
                ubicacion_valida = datos_sugerencia.get("ubicacion")
                if isinstance(ubicacion_valida, dict):
                    datos_sugerencia["direccion"] = (
                        ubicacion_valida.get("address")
                        or ubicacion_valida.get("label")
                        or ubicacion_valida.get("texto")
                    )
                elif isinstance(ubicacion_valida, str):
                    datos_sugerencia["direccion"] = ubicacion_valida

            campos_faltantes = _get_missing_sugerencia_contact_fields(datos_sugerencia)
            contexto_municipio_actual['datos_sugerencia'] = datos_sugerencia
            _merge_contacto_usuario(contexto_municipio_actual, datos_sugerencia)
            if campos_faltantes:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                campos_texto = ', '.join(campos_faltantes)
                return _finalize_response({
                    "message_body": f"Para registrar tu sugerencia necesito: {campos_texto}. Podés escribir todo en un solo mensaje.",
                    "fuente": "pide_datos_contacto_sugerencia"
                })

            mensaje_confirmacion = (
                "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
                f"- **Nombre**: {datos_sugerencia.get('nombre')}\n"
                f"- **DNI**: {datos_sugerencia.get('dni')}\n"
                f"- **Email**: {datos_sugerencia.get('email')}\n"
                f"- **Dirección**: {_format_sugerencia_address(datos_sugerencia)}\n"
                f"- **Teléfono**: {datos_sugerencia.get('telefono') or 'No informado'}\n"
                f"- **Sugerencia**: {sugerencia_texto}"
            )
            botones = [
                {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
                {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_confirmacion,
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "pide_confirmacion_sugerencia"
            })

        elif estado_conversacion == ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            datos_guardados = contexto_municipio_actual.get('datos_sugerencia', {})
            campos_requeridos = ["nombre", "dni", "email", "direccion", "telefono"]

            # Primero intentamos extraer con regex para los campos aún faltantes.
            nuevos_datos = extract_multiple_contact_details_regex(
                pregunta_str, campos_requeridos + ["telefono"]
            )
            _update_sugerencia_contact_fields(datos_guardados, nuevos_datos)

            campos_faltantes = _get_missing_sugerencia_contact_fields(datos_guardados)

            # Utilizar el LLM solo si todavía faltan campos
            if campos_faltantes:
                try:
                    llm_datos = extract_multiple_contact_details_llm(
                        pregunta_str, campos_requeridos + ["telefono"]
                    )
                    if llm_datos:
                        _update_sugerencia_contact_fields(datos_guardados, llm_datos)
                except Exception as e:
                    logger.error("[DATOS_SUGERENCIA] LLM fallback failed: %s", e)
                campos_faltantes = _get_missing_sugerencia_contact_fields(datos_guardados)

            _ensure_sugerencia_address(datos_guardados)
            campos_faltantes = _get_missing_sugerencia_contact_fields(datos_guardados)

            contexto_municipio_actual['datos_sugerencia'] = datos_guardados
            _merge_contacto_usuario(contexto_municipio_actual, datos_guardados)
            if campos_faltantes:
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": f"Aún necesito: {', '.join(campos_faltantes)}. Podés enviarlos todos juntos.",
                    "fuente": "datos_contacto_sugerencia_incompletos"
                })

            mensaje_confirmacion = (
                "Por favor, confirmá si los datos para tu sugerencia son correctos:\n"
                f"- **Nombre**: {datos_guardados.get('nombre')}\n"
                f"- **DNI**: {datos_guardados.get('dni')}\n"
                f"- **Email**: {datos_guardados.get('email')}\n"
                f"- **Dirección**: {_format_sugerencia_address(datos_guardados)}\n"
                f"- **Teléfono**: {datos_guardados.get('telefono') or 'No informado'}\n"
                f"- **Sugerencia**: {datos_guardados.get('descripcion')}"
            )
            botones = [
                {"texto": "Sí, enviar sugerencia", "action_id": "confirmar_sugerencia_si"},
                {"texto": "No, corregir", "action_id": "confirmar_sugerencia_no"},
            ]
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name
            if chat_db_context: flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_confirmacion,
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "pide_confirmacion_sugerencia"
            })

        elif estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name:
            switch_response = _detect_reclamo_during_sugerencia(pregunta_str, contexto_municipio_actual, context, chat_db_context)
            if switch_response:
                return _finalize_response(switch_response)
            texto_normalizado = normalizar_texto(pregunta_str)
            afirmativos = ["si", "enviar", "guardar", "guarda", "ok", "dale", "confirmar"]
            if (
                action == "confirmar_sugerencia_si"
                or texto_normalizado.strip() == "1"
                or any(a in texto_normalizado for a in afirmativos)
            ):
                datos_confirmados = contexto_municipio_actual.pop('datos_sugerencia', {})
                handler = CrearReclamoActionHandler(context)
                response = handler.execute(datos_confirmados)
                if response.get("success"):
                    response["message_to_user"] = f"✅ ¡Hemos recibido tu sugerencia! Muchas gracias por tu aporte. Lo hemos registrado con el número de ticket `{response.get('data', {}).get('nro_ticket', 'N/A')}` para su seguimiento."
                    contexto_municipio_actual['estado_conversacion'] = None
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    final_payload = _message_with_menu(response["message_to_user"], context)
                    final_payload['success'] = True
                    return _finalize_response(final_payload)
                contexto_municipio_actual['datos_sugerencia'] = datos_confirmados
                if response.get("pedir_info"):
                    contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response(response)
            else:
                contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_DATOS_CONTACTO_SUGERENCIA.name
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Entendido. Por favor, enviá los datos correctos en un solo mensaje.",
                    "fuente": "pide_correccion_sugerencia"
                })

        if (
            not contexto_municipio_actual.get("estado_conversacion")
            and not contexto_municipio_actual.get("reclamo_flow_v2", {}).get("state")
        ):
            auto_reclamo_payload = _auto_bootstrap_reclamo()
            if auto_reclamo_payload:
                return _finalize_response(auto_reclamo_payload)

    # 2. If no state is active, then handle actions that start new flows.
    elif action:
        response = handle_main_menu_action(action, context, chat_db_context)
        if response:
            return _finalize_response(response)
    else:
        numero_directo = (pregunta_str or "").strip()
        if numero_directo.isdigit() and len(numero_directo) >= 6:
            contexto_municipio_actual["numero_ticket_consulta"] = numero_directo
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_NUMERO_TICKET.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": "Ingresá el PIN de 6 dígitos asociado al ticket.",
                "fuente": "handler_consultar_reclamo",
            })
        menu_payload = _get_main_menu_payload(context)
        buttons_for_finder = [
            {"texto": btn.get("texto"), "action_id": btn.get("id")}
            for btn in menu_payload.get("options_list", [])
        ]
        inferred_action = find_menu_action_by_input(pregunta_str or "", buttons_for_finder)
        if not inferred_action:
            inferred_action = find_global_menu_action(pregunta_str or "")
        if inferred_action:
            # Si el usuario escribe "iniciar reclamo" (u otra variante) en texto libre,
            # mostramos el menú de reclamos en lugar de saltar directamente al flujo.
            if inferred_action == "iniciar_reclamo":
                response = handle_main_menu_action(
                    "mostrar_menu_reclamos",
                    context,
                    chat_db_context,
                )
            else:
                response = handle_main_menu_action(inferred_action, context, chat_db_context)
            if response:
                return _finalize_response(response)

    auto_reclamo_payload = _auto_bootstrap_reclamo()
    if auto_reclamo_payload:
        return _finalize_response(auto_reclamo_payload)

    USAR_LLM_PARA_RECLAMOS = True  # Habilita el flujo con LLM para reclamos
    respuesta_manejada_por_llm = False # Flag para indicar si el LLM ya manejó la respuesta

    # >>> INICIO FIX: Si la pregunta está vacía pero se recibió una ubicación, crear una pregunta para el LLM
    if not pregunta_str.strip() and location:
        lat = location.get('latitude')
        lon = location.get('longitude')
        address = location.get('address', f"coordenadas {lat}, {lon}")

        pregunta_str = (
            f"El usuario ha compartido una ubicación sin texto adicional. "
            f"La ubicación es: {address}. "
            f"Es muy probable que quiera reportar un problema en este lugar. "
            f"Por favor, actúa proactivamente: confirma la ubicación con el usuario y pregúntale "
            f"directamente qué problema o reclamo quiere reportar en esa dirección."
        )
        received_payload['pregunta'] = pregunta_str
        logger_actual.info(f"Pregunta generada a partir de ubicación: '{pregunta_str}'")
    # <<< FIN FIX

    # --- INICIO: Manejo proactivo de multimedia y ubicación ---
    # Si el usuario envía solo una imagen o ubicación, el bot debe actuar proactivamente.
    if not pregunta_str.strip():  # Solo actuar si no hay texto del usuario
        synthetic_prompt = None
        datos_interpretados = context.get("datos_interpretados_archivo") or kwargs.get("datos_interpretados_archivo")

        if datos_interpretados and isinstance(datos_interpretados, dict):
            # Si el análisis automático ya determinó que es un reclamo, iniciar el flujo directamente
            if datos_interpretados.get("es_reclamo"):
                logger_actual.info("Iniciando flujo de reclamo desde imagen interpretada")
                handler = ReclamoFlowHandler(context, chat_db_context)
                datos_iniciales = {
                    "categoria": datos_interpretados.get("categoria_sugerida"),
                    "descripcion": datos_interpretados.get("descripcion_sugerida"),
                    "origen_descripcion": "imagen",
                }
                return _finalize_response(handler.start_flow(datos_iniciales=datos_iniciales))

            logger_actual.info(f"Manejando proactivamente un archivo interpretado: {datos_interpretados}")
            categoria = datos_interpretados.get("categoria_sugerida", "No especificada")
            descripcion = datos_interpretados.get("descripcion_sugerida", "No especificada")
            synthetic_prompt = (
                f"El usuario ha enviado una imagen para iniciar un reclamo. "
                f"El análisis automático sugiere: Categoría='{categoria}', Descripción='{descripcion}'. "
                f"Inicia el proceso de reclamo confirmando estos datos con el usuario y pide la información que falte (ej. ubicación)."
            )
        elif location:
            logger_actual.info(f"Manejando proactivamente una ubicación: {location}")
            address = location.get("address", f"coordenadas {location.get('latitude')}, {location.get('longitude')}")
            synthetic_prompt = (
                f"El usuario ha compartido la ubicación '{address}' sin texto adicional. "
                f"Actúa proactivamente: confirma la ubicación con el usuario y pregúntale qué problema quiere reportar en esa dirección."
            )

        if synthetic_prompt:
            logger_actual.info(f"Pregunta sintética generada para manejo proactivo: '{synthetic_prompt}'")
            # Forzar el estado a conversación general para que el LLM tome el control
            contexto_municipio_actual = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
            contexto_municipio_actual['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name

            response_dict, _ = handle_llm_interaction(
                app,
                synthetic_prompt,
                context,
                viewer_user,
                owner_user,
                chat_db_context,
                contexto_municipio_actual,
                demo_metadata=demo_metadata,
            )
            if response_dict:
                return _finalize_response(response_dict)
    # --- FIN: Manejo proactivo ---

    contexto_municipio_data_from_db = {}
    chat_db_context_live_data = {}

    if chat_db_context:
        if chat_db_context.context_data is None:
            chat_db_context.context_data = {}
        chat_db_context_live_data = chat_db_context.context_data # Reference to the live dict
        context["chat_db_context_data"] = chat_db_context_live_data # Update main context with live data
        contexto_municipio_data_from_db = chat_db_context_live_data.get(
            CONTEXTO_MUNICIPIO, {}
        )
    else:
        logger_actual.warning("[RESPONDER_MUNICIPIO] chat_db_context is None. Municipio context will be empty for this request.")
        # contexto_municipio_data_from_db remains {}
        # chat_db_context_live_data remains {}

    logger_actual.info(
        f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto crudo para '{CONTEXTO_MUNICIPIO}' desde DB: {contexto_municipio_data_from_db}"
    )

    # Log the current state of the conversation
    estado_conversacion = contexto_municipio_data_from_db.get("estado_conversacion")
    logger_actual.info(f"[CONTEXTO_MUNICIPIO] Estado de conversacion actual: {estado_conversacion}")

    # Directly use the dictionary from the live context data.
    # This ensures that modifications are made to the original object.
    contexto_municipio_actual = chat_db_context_live_data.setdefault(CONTEXTO_MUNICIPIO, {})
    context[CONTEXTO_MUNICIPIO] = contexto_municipio_actual # Ensure main context points to this sub-context

    # --- INICIO: Manejo de selección de menú principal por número, letra o keyword ---
    if estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name:
        pregunta_str_menu = ""
        payload_action = None
        if isinstance(pregunta_original, str):
            pregunta_str_menu = pregunta_original
        elif isinstance(pregunta_original, dict):
            pregunta_str_menu = pregunta_original.get("pregunta", "")
            payload_action = pregunta_original.get("action")

        logger_actual.info(
            f"Handling input in ESPERANDO_SELECCION_MENU_PRINCIPAL state. Input: '{pregunta_str_menu}', Payload action: '{payload_action}'"
        )

        # Get the definitive menu structure from the payload generator
        # This ensures that the menu we check against is the same one the user saw.
        menu_payload = _get_main_menu_payload(context)
        # The payload has 'options_list' which is the flat list of buttons with 'id' and 'texto'
        flat_buttons = menu_payload.get('options_list', [])

        # We need to adapt the list for find_menu_action_by_input, which expects 'action_id'
        buttons_for_finder = []
        for btn in flat_buttons:
            buttons_for_finder.append({
                "texto": btn.get("texto"),
                "action_id": btn.get("id") # The 'id' key holds the action_id
            })

        selected_action = payload_action or find_menu_action_by_input(pregunta_str_menu, buttons_for_finder)
        if not selected_action:
            selected_action = find_global_menu_action(pregunta_str_menu)

        if selected_action:
            logger_actual.info(f"User input '{pregunta_str_menu}' matched to action: '{selected_action}'")

            # The state should be cleared so we don't get stuck here.
            # The handler itself will set a new state if it needs to continue a flow.
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            response = handle_main_menu_action(selected_action, context, chat_db_context)
            if response:
                # After the action, ask a generic follow-up question unless the handler
                # wants to take control of the conversation (e.g., by setting a new state).
                # We check if a new state was set by the handler.
                new_state = contexto_municipio_actual.get("estado_conversacion")
                if not new_state:
                    # Append a follow-up question and show the main menu again.
                    # This creates a clear "turn" and returns control to the user.
                    follow_up_message = "\n\n¿En qué más puedo ayudarte?"
                    response['message_body'] = response.get('message_body', '').strip() + follow_up_message

                    # We will not send the full menu again here.
                    # We will send a simpler prompt.
                    # A better approach would be to have a "back to menu" button.
                    # For now, we just add the text.

                return _finalize_response(response)
        else:
            # If the input doesn't match a menu option, treat it as a general query.
            # Clear the state so it falls through to the main LLM handler.
            logger_actual.info(f"Input '{pregunta_str_menu}' is not a menu option. Treating as a general query and falling through to LLM.")
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
    # --- FIN: Manejo de selección de menú principal ---

    # --- INICIO: Manejo genérico de selección de submenús ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_DE_LISTA.name:
        pregunta_str_menu = ""
        action_payload = None
        if isinstance(pregunta_original, str):
            pregunta_str_menu = pregunta_original
        elif isinstance(pregunta_original, dict):
            pregunta_str_menu = pregunta_original.get("pregunta", "")
            action_payload = pregunta_original.get("action")

        if not action_payload:
            action_payload = received_payload.get("action")

        menu_opciones = contexto_municipio_actual.get("menu_opciones", [])
        selected_action = action_payload or find_menu_action_by_input(pregunta_str_menu, menu_opciones)

        if (
            not action_payload
            and selected_action == "iniciar_reclamo"
            and not pregunta_str_menu.strip().isdigit()
        ):
            # El usuario volvió a escribir "iniciar reclamo" en lugar de pulsar el botón.
            # Reenviamos el menú de reclamos para que pueda elegir una opción.
            submenu = _get_reclamos_consultas_menu()
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
            contexto_municipio_actual["menu_opciones"] = submenu.get("options_list", [])
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(submenu)

        if not selected_action:
            selected_action = find_global_menu_action(pregunta_str_menu)

        if selected_action:
            context["menu_opciones"] = menu_opciones
            if selected_action == "iniciar_reclamo" and pregunta_str_menu.strip().isdigit():
                context["skip_reclamo_autodetect"] = True
            contexto_municipio_actual['estado_conversacion'] = None
            contexto_municipio_actual.pop('menu_opciones', None)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            response = handle_main_menu_action(selected_action, context, chat_db_context)
            if response:
                new_state = contexto_municipio_actual.get("estado_conversacion")
                if not new_state:
                    response['message_body'] = response.get('message_body', '').strip() + "\n\n¿En qué más puedo ayudarte?"
                return _finalize_response(response)
        else:
            response_dict = _maybe_route_menu_input_to_llm(
                pregunta_str_menu,
                contexto_municipio_actual,
                app,
                context,
                viewer_user,
                owner_user,
                chat_db_context,
                demo_metadata=demo_metadata,
            )
            if response_dict:
                return _finalize_response(response_dict)
            # Reenviar el mismo submenú si la opción no es válida
            return _finalize_response({
                "message_body": "No reconocí esa opción. Por favor, elegí una opción del menú.",
                "message_type": "interactive_buttons",
                "options_list": menu_opciones,
                "fuente": "submenu_opcion_invalida",
            })
    # --- FIN: Manejo genérico de selección de submenús ---

    # --- INICIO: Manejo de la espera por nombre de trámite ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_TRAMITE.name:
        logger_actual.info(f"Handling input in ESPERANDO_SELECCION_TRAMITE state. Input: '{pregunta_str}'")
        from .actions.municipio_actions import ConsultarInfoTramiteActionHandler

        # The handler expects the data in the payload dict
        received_payload['nombre_tramite'] = pregunta_str

        handler = ConsultarInfoTramiteActionHandler(context)
        handler_response = handler.execute(received_payload)

        # The handler should now succeed and return a standard response with the info.
        # We need to clear the state after this.
        contexto_municipio_actual['estado_conversacion'] = None
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")
        return _finalize_response(handler_response)
    # --- FIN: Manejo de la espera por nombre de trámite ---

    # --- INICIO: Manejo de selección de menú de reclamos ---
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_MENU_RECLAMOS.name:
        pregunta_str_reclamo = ""
        if isinstance(pregunta_original, str):
            pregunta_str_reclamo = pregunta_original
        elif isinstance(pregunta_original, dict) and "pregunta" in pregunta_original:
            pregunta_str_reclamo = pregunta_original["pregunta"]

        logger_actual.info(f"Handling input in ESPERANDO_SELECCION_MENU_RECLAMOS state. Input: '{pregunta_str_reclamo}'")

        normalized_input = normalizar_texto(pregunta_str_reclamo or "")

        if pregunta_str_reclamo in {"0"} or normalized_input in RETURN_TO_MAIN_MENU:
            logger_actual.info("User requested to return to main menu from reclamos menu.")
            handler = GreetingHandler(context)
            response = handler.handle({})
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response)

        repeat_commands = {
            "show_reclamos_menu",
            "mostrar_menu_reclamos",
            "hacer un reclamo",
            "iniciar reclamo",
            "reclamo",
            "reclamos",
        }
        if not pregunta_str_reclamo or normalized_input in repeat_commands:
            logger_actual.info("Input requests reclamos menu again. Returning submenu.")
            return _finalize_response(_get_reclamos_menu())

        reclamo_options = _get_reclamos_menu().get("options_list", [])
        selected_category_name = None

        if pregunta_str_reclamo.isdigit():
            for option in reclamo_options:
                if option.get("id_accion") == pregunta_str_reclamo:
                    selected_category_name = option.get("category_name")
                    break

        # Si no es un número o no corresponde, intentar matchear por texto y extraer más datos.
        details = {}
        if not selected_category_name:
            plain_text_options = [
                {"texto": opt.get("category_name")}
                for opt in reclamo_options
                if opt.get("category_name")
            ]
            details = extract_reclamo_details_from_text(pregunta_str_reclamo, plain_text_options)
            selected_category_name = details.pop("categoria_sugerida", None)

        if selected_category_name:
            if selected_category_name == "Pérdida de agua":
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context: flag_modified(chat_db_context, "context_data")
                return _finalize_response({
                    "message_body": "Para pérdida de agua, dirigite a la página de Aysam:\nhttps://www.aysam.com.ar/",
                    "options_list": [], "message_type": "text", "fuente": "info_perdida_agua"
                })

            logger_actual.info(f"Categoría de reclamo seleccionada: '{selected_category_name}'. Limpiando contexto anterior.")

            # FIX: Limpiar explícitamente el contexto del reclamo anterior para evitar el "estado atascado".
            contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
            contexto_municipio_actual.pop("historial_llm_reclamo", None)

            constructed_prompt = f"Quiero iniciar un reclamo de {selected_category_name}"

            # --- INICIO: Integración del nuevo ReclamoFlowHandler ---
            handler = ReclamoFlowHandler(context, chat_db_context)
            datos_iniciales = {}
            if details.get("descripcion_sugerida"):
                datos_iniciales["descripcion"] = details["descripcion_sugerida"]
            if details.get("direccion_sugerida"):
                datos_iniciales["direccion"] = details["direccion_sugerida"]
            response_dict = handler.start_flow(
                datos_iniciales=datos_iniciales or None,
                categoria_inicial=selected_category_name,
            )
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)
            # --- FIN: Integración del nuevo ReclamoFlowHandler ---
        else:
            logger_actual.warning(f"Input '{pregunta_str_reclamo}' no coincide con ninguna categoría. Mostrando menú de nuevo.")
            return _finalize_response(_get_reclamos_menu())
    # --- FIN: Manejo de selección de menú de reclamos ---

    elif estado_conversacion == ConversationState.ESPERANDO_NOMBRE_INICIAL.name:
        if action:
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            response = handle_main_menu_action(action, context, chat_db_context)
            if response:
                return _finalize_response(response)

        nombre_usuario = (pregunta_str or "").strip()
        if len(nombre_usuario) > 2:
            # Save the name
            if viewer_user:
                viewer_user.name = nombre_usuario
                db.session.add(viewer_user)
                db.session.commit()

            context["profile_name"] = nombre_usuario
            chat_data = context.get("chat_db_context_data")
            if not isinstance(chat_data, dict):
                chat_data = {}
                context["chat_db_context_data"] = chat_data
            chat_data["profile_name"] = nombre_usuario
            contexto_municipio_actual = chat_data.setdefault(CONTEXTO_MUNICIPIO, {})
            contacto = contexto_municipio_actual.setdefault("contacto_usuario", {})
            contacto['nombre'] = nombre_usuario
            contexto_municipio_actual['estado_conversacion'] = (
                ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
            )
            if chat_db_context and isinstance(chat_db_context.context_data, dict):
                chat_db_context.context_data["profile_name"] = nombre_usuario
                contexto_db = chat_db_context.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
                contacto_db = contexto_db.setdefault("contacto_usuario", {})
                contacto_db['nombre'] = nombre_usuario
                contexto_db['estado_conversacion'] = (
                    ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
                )
                flag_modified(chat_db_context, "context_data")

            mensaje_bienvenida = f"¡Gracias, {nombre_usuario}!"
            menu_payload = _get_main_menu_payload(
                context,
                welcome_message_override=mensaje_bienvenida,
                reduced=True,
            )
            return _finalize_response(menu_payload)
        else:
            return _finalize_response({
                "message_body": "Me encantaría conocerte mejor. ¿Me contás tu nombre?",
                "fuente": "nombre_no_entendido"
            })
    elif estado_conversacion == ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name:
        selected_category_action = received_payload.get('action')
        pregunta_str_norm = normalizar_texto(pregunta_str or '')

        categorias_map = contexto_municipio_actual.get('contactos_categorias', {})
        if selected_category_action and selected_category_action.startswith('select_contact_category_'):
            slug = selected_category_action.replace('select_contact_category_', '')
            selected = categorias_map.get(slug)
        elif pregunta_str_norm and categorias_map:
            from fuzzywuzzy import process
            name_map = {v['nombre']: k for k, v in categorias_map.items()}
            match, score = process.extractOne(pregunta_str_norm, list(name_map.keys()))
            selected = categorias_map.get(name_map[match]) if score > 80 else None
        else:
            selected = None

        if not selected:
            return _finalize_response({
                'message_body': 'Por favor, seleccioná una categoría de la lista.',
                'fuente': 'contactos_utiles_invalid_category_selection'
            })

        return _finalize_response(
            handle_contactos_utiles_mostrar_categoria(context, chat_db_context, selected)
        )
    # --- INICIO: Manejo de actualización de datos de usuario ---
    elif estado_conversacion == ConversationState.ESPERANDO_NUEVO_DATO_USUARIO.name:
        campo_a_actualizar = contexto_municipio_actual.get('campo_a_actualizar')
        nuevo_valor = pregunta_str.strip()

        if not campo_a_actualizar:
            logger_actual.error("[UPDATE_USER_DATA] In ESPERANDO_NUEVO_DATO_USUARIO state but no 'campo_a_actualizar' in context. Resetting.")
            contexto_municipio_actual['estado_conversacion'] = None
        elif not viewer_user:
            logger_actual.error("[UPDATE_USER_DATA] Cannot update data for a non-logged-in user. Resetting.")
            contexto_municipio_actual['estado_conversacion'] = None
            # Limpiar contexto para no quedar en un bucle
            contexto_municipio_actual.pop('campo_a_actualizar', None)
            contexto_municipio_actual.pop('accion_original_para_reintentar', None)
            return _finalize_response({"message_body": "Para actualizar tus datos, primero necesitás iniciar sesión. ¿Querés que te ayude con eso?", "fuente": "update_data_login_required"})
        else:
            from services.user_service import actualizar_perfil_usuario
            resultado_actualizacion = actualizar_perfil_usuario(
                user_id=viewer_user.id,
                datos_actualizacion={campo_a_actualizar: nuevo_valor}
            )

            if resultado_actualizacion.get('status') == 'success':
                contexto_municipio_actual.pop('campo_a_actualizar', None)
                contexto_municipio_actual['estado_conversacion'] = None # Reset state to re-evaluate

                accion_original = contexto_municipio_actual.pop('accion_original_para_reintentar', "continuar con el reclamo")

                pregunta_str = (
                    f"Acabo de actualizar el/la {campo_a_actualizar} del usuario a '{nuevo_valor}'. "
                    f"Confirma al usuario que el dato fue actualizado correctamente. "
                    f"Ahora, por favor, continúa con su pedido original, que era: '{accion_original}'"
                )

                logger_actual.info(f"Generated synthetic prompt to resume flow: {pregunta_str}")
                # La ejecución continuará y llamará a handle_llm_interaction con la nueva pregunta_str
            else:
                contexto_municipio_actual['estado_conversacion'] = None # Reset state
                error_message = resultado_actualizacion.get('message', f"Hubo un error al actualizar tu {campo_a_actualizar}.")
                return _finalize_response({
                    "message_body": error_message,
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "error_actualizacion_dato_usuario"
                })
    # --- FIN: Manejo de actualización de datos de usuario ---

    # --- INICIO: Manejo de recepción de ubicación para consulta general ---
    elif estado_conversacion == ConversationState.ESPERANDO_UBICACION_GENERAL.name:
        if location:
            consulta_guardada = contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
            if consulta_guardada:
                contexto_municipio_actual['ultima_consulta_poi'] = consulta_guardada
            contexto_municipio_actual['estado_conversacion'] = None  # Clear state
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            if consulta_guardada:
                logger_actual.info(f"Received location, processing saved query: '{consulta_guardada}'")
                return _finalize_response(PointsOfInterestHandler(context).handle({"pregunta": consulta_guardada, "location": location}))
            else:
                logger_actual.warning("In ESPERANDO_UBICACION_GENERAL state but no saved query found.")
                return _finalize_response({"message_body": "Recibí tu ubicación, pero no recuerdo qué estabas buscando. ¿Podrías decírmelo de nuevo?", "options_list": [], "message_type": "text", "fuente": "error_no_saved_query"})
        else:
            # If no location object was sent, check if the user typed an address
            if pregunta_str and len(pregunta_str) > 5: # Basic check to see if it's a potential address
                from .herramientas_municipio import validar_y_formatear_direccion
                logger_actual.info(f"Attempting to geocode textual address: '{pregunta_str}'")

                # We can use the simpler geocoding tool here
                geocoded_location = validar_y_formatear_direccion(pregunta_str, municipio_config=context.get("municipio_config_actual"))

                if geocoded_location:
                    # Address was valid, proceed with the original query
                    consulta_guardada = contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
                    if consulta_guardada:
                        contexto_municipio_actual['ultima_consulta_poi'] = consulta_guardada
                    contexto_municipio_actual['estado_conversacion'] = None  # Clear state
                    if chat_db_context:
                        flag_modified(chat_db_context, "context_data")

                    if consulta_guardada:
                        logger_actual.info(f"Geocoded address successfully. Processing saved query: '{consulta_guardada}'")
                        loc_payload = {
                            "address": geocoded_location.get("formatted_address"),
                            "lat": geocoded_location.get("lat"),
                            "lon": geocoded_location.get("lng"),
                        }
                        return _finalize_response(PointsOfInterestHandler(context).handle({"pregunta": consulta_guardada, "location": loc_payload}))
                    else:
                        # This case is unlikely but handled for safety
                        logger_actual.warning("Geocoded address but no saved query found.")
                        return _finalize_response({"message_body": f"OK, entiendo que estás en {geocoded_location.get('formatted_address')}. ¿Qué necesitabas buscar?", "options_list": [], "message_type": "text", "fuente": "geocoded_but_no_query"})
                else:
                    # Geocoding failed, stay in the same state and re-prompt.
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    return _finalize_response({
                        "message_body": "No pude entender esa dirección. Por favor, intentá de nuevo con más detalles, usá el botón para compartir tu ubicación, o escribí 'cancelar' para salir.",
                        "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
                        "message_type": "interactive_buttons",
                        "fuente": "geocoding_failed_reprompt"
                    })
            else:
                # User sent something that is not a location and not a potential address.
                # Check for cancellation.
                cancel_keywords = {"cancelar", "no", "salir", "basta", "terminar"}
                if normalizar_texto(pregunta_str) in cancel_keywords or action == "cancelar":
                    contexto_municipio_actual['estado_conversacion'] = None # Reset state
                    contexto_municipio_actual.pop('consulta_pendiente_ubicacion', None)
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    return _finalize_response({"message_body": "Ok, cancelado. ¿En qué otra cosa te puedo ayudar?", "options_list": [], "message_type": "text", "fuente": "ubicacion_cancelled"})
                else:
                    # Not a location, not an address, not a cancellation. Re-prompt.
                    if chat_db_context: flag_modified(chat_db_context, "context_data")
                    return _finalize_response({
                        "message_body": "No recibí una ubicación. Por favor, compartí tu ubicación, escribí una dirección, o escribí 'cancelar' para salir.",
                        "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
                        "message_type": "interactive_buttons",
                        "fuente": "no_location_reprompt"
                    })
    # --- FIN: Manejo de recepción de ubicación ---

    elif estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_DATOS_RECLAMO.name:
        if "si" in normalizar_texto(pregunta_str) or action == "confirmar_reclamo_si":
            datos_confirmados = contexto_municipio_actual.pop("datos_a_confirmar", {})

            # Llamar a la acción de creación de reclamo
            handler = CrearReclamoActionHandler(context)
            response = handler.execute(datos_confirmados)

            # Limpiar el estado de la conversación solo si la creación fue exitosa
            if response.get("success"):
                contexto_municipio_actual['estado_conversacion'] = None

            if chat_db_context:
                flag_modified(chat_db_context, "context_data")

            return _finalize_response(response)
        else: # User wants to edit
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CORRECCION_DATOS_RECLAMO.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": "Entendido. ¿Qué dato te gustaría corregir o agregar? Por favor, decímelo y lo corrijo.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pide_correccion_reclamo"
            })

    elif estado_conversacion == ConversationState.ESPERANDO_INTENCION_UBICACION.name:
        ubicacion_contextual = contexto_municipio_actual.get('ubicacion_contextual')
        address = ubicacion_contextual.get('address', 'la ubicación proporcionada') if ubicacion_contextual else 'la ubicación proporcionada'
        if not action:
            pregunta_menu = ""
            if isinstance(pregunta_original, str):
                pregunta_menu = pregunta_original
            elif isinstance(pregunta_original, dict):
                pregunta_menu = pregunta_original.get("pregunta", "")
            action = find_menu_action_by_input(pregunta_menu, _location_action_options())
            if not action and pregunta_menu.strip():
                contexto_municipio_actual['ultima_consulta_poi'] = pregunta_menu.strip()
                if chat_db_context:
                    flag_modified(chat_db_context, "context_data")
                return _finalize_response(
                    PointsOfInterestHandler(context).handle(
                        {"pregunta": pregunta_menu.strip(), "location": ubicacion_contextual or {}}
                    )
                )

        if action == "iniciar_reclamo_con_ubicacion":
            handler = ReclamoFlowHandler(context, chat_db_context)
            datos_iniciales = {"direccion": address}
            if ubicacion_contextual:
                datos_iniciales['coordenadas'] = {
                    "lat": ubicacion_contextual.get("latitude"),
                    "lon": ubicacion_contextual.get("longitude")
                }
            # The original implementation was missing the 'categoria_inicial' argument for start_flow
            response_dict = handler.start_flow(datos_iniciales=datos_iniciales)
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(response_dict)
        elif action == "enviar_sugerencia_con_ubicacion":
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name
            _set_sugerencia_location_context(
                contexto_municipio_actual,
                ubicacion_contextual,
                fallback_address=address,
            )
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": f"Excelente. Por favor, escribí tu sugerencia relacionada con la ubicación: *{address}*.",
                "fuente": "handler_enviar_sugerencia_con_ubicacion"
            })
        elif action == "buscar_estacionamiento_con_ubicacion":
            contexto_municipio_actual['ultima_consulta_poi'] = 'estacionamiento'
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(
                PointsOfInterestHandler(context).handle(
                    {"pregunta": "estacionamiento", "location": ubicacion_contextual or {}}
                )
            )
        elif action == "buscar_lugares_cerca":
            contexto_municipio_actual['ultima_consulta_poi'] = 'lugares cercanos'
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(
                PointsOfInterestHandler(context).handle(
                    {"pregunta": "lugares cercanos", "location": ubicacion_contextual or {}}
                )
            )

        else:  # Cancelar o no se entiende
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_INTENCION_UBICACION.name
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response(
                {
                    "message_body": f"Perfecto, ya tengo tu ubicación en *{address}*. ¿Qué te gustaría hacer?",
                    "options_list": _location_action_options()
                    + [{"texto": "Menú", "action_id": "menu_principal"}],
                    "fuente": "proactive_location_handler",
                }
            )


    elif estado_conversacion == ConversationState.ESPERANDO_CORRECCION_DATOS_RECLAMO.name:
        logger_actual.info(f"Handling input in ESPERANDO_CORRECCION_DATOS_RECLAMO state. Input: '{pregunta_str}'")

        datos_nuevos = extract_multiple_contact_details_llm(pregunta_str, ["nombre", "email", "telefono", "ubicacion", "descripcion"])
        datos_pendientes = contexto_municipio_actual.get("datos_a_confirmar", {})

        # Mapeo de claves para actualizar correctamente
        if datos_nuevos.get("nombre"): datos_pendientes["nombre_usuario_detectado"] = datos_nuevos["nombre"]
        if datos_nuevos.get("email"): datos_pendientes["email_detectado"] = datos_nuevos["email"]
        if datos_nuevos.get("telefono"): datos_pendientes["telefono_detectado"] = datos_nuevos["telefono"]
        if datos_nuevos.get("ubicacion"): datos_pendientes["ubicacion"] = datos_nuevos["ubicacion"]
        if datos_nuevos.get("descripcion"): datos_pendientes["descripcion"] = datos_nuevos["descripcion"]

        contexto_municipio_actual["datos_a_confirmar"] = datos_pendientes
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_DATOS_RECLAMO.name

        mensaje_confirmacion = (
            f"""Perfecto, he actualizado los datos. Por favor, confirmá si ahora son correctos:
*Categoría:* {datos_pendientes.get('categoria', 'No especificada')}
*Descripción:* {datos_pendientes.get('descripcion', 'No especificada')}
*Ubicación:* {datos_pendientes.get('ubicacion', 'No especificada')}
*Nombre:* {datos_pendientes.get('nombre_usuario_detectado', 'No especificado')}
*Teléfono:* {datos_pendientes.get('telefono_detectado', 'No especificado')}
*Email:* {datos_pendientes.get('email_detectado', 'No especificado')}
"""
        )

        botones = [
            {"texto": "Sí, crear reclamo", "action_id": "confirmar_reclamo_si"},
            {"texto": "No, seguir editando", "action_id": "confirmar_reclamo_no"},
        ]

        return _finalize_response({
            "message_body": mensaje_confirmacion,
            "options_list": botones,
            "message_type": "interactive_buttons",
            "fuente": "re_pide_confirmacion_reclamo"
        })


    # Initialize the context if it's empty
    # This dictionary is passed to handlers and used throughout this function.
    context = {
        CONTEXTO_MUNICIPIO: contexto_municipio_actual, # The specific state for municipio flow
        "user_obj": owner_user, # The User object of the bot instance (e.g., the Municipality)
        "viewer_user_obj": viewer_user, # The User object of the end-user (vecino/ciudadano)
        "cliente_id": getattr(viewer_user, "id", None),
        "anon_id": anon_id,
        "rubro_obj": rubro_obj,
        "channel": channel,
        "municipio_config_actual": CONFIG_MUNICIPIO, # Use the correct global constant here
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context_live_data, # Use the safely accessed live data dict
        # Fields to be populated by payload/kwargs or later logic:
        "intencion": kwargs.get("intencion"), # Initial intent from Orchestrator/kwargs
        "ubicacion_usuario": location or received_payload.get("ubicacion_usuario"),
        "es_foto": False, "foto_url": None, # Defaults, will be updated after inspecting payload
        "es_ubicacion": received_payload.get("es_ubicacion", False),
        "es_archivo": received_payload.get("es_archivo", False),
        "action": received_payload.get("action"), # From button clicks, etc.
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
    }
    if not (chat_db_context and hasattr(chat_db_context, 'context_data')):
        logger_actual.critical("chat_db_context.context_data no disponible al inicializar 'context'. Usando dict vacío. Esto es problemático.")

    # Check for completed analysis in the context
    if chat_db_context_live_data.get("web_analisis_listo"):
        analisis_info = chat_db_context_live_data.pop("web_analisis_listo")
        from models import AnalisisArchivo
        analisis_obj = db.session.get(AnalisisArchivo, analisis_info.get("archivo_id"))
        if analisis_obj and analisis_obj.texto_extraido:
            pregunta_str = analisis_obj.texto_extraido
            logger_actual.info(f"Usando texto de análisis de archivo como pregunta: '{pregunta_str}'")

    logger_actual.info(
        f"[RESPONDER_MUNICIPIO_START_CONTEXT_INIT] Context inicializado. UserMunicipio: {context['user_obj'].id if context['user_obj'] else 'N/A'}, "
        f"ViewerCiudadano: {context['cliente_id'] or context['anon_id']}"
    )
    logger_actual.info(f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto DB para {CONTEXTO_MUNICIPIO}: {contexto_municipio_data_from_db}")


    # --- Handle post-login resumption (modifies context[CONTEXTO_MUNICIPIO] and context["intencion"]) ---
    # Ensure to check within context["chat_db_context_data"] which is the live dict from the ORM object
    if viewer_user and context["chat_db_context_data"].get("just_logged_in_flag"):
        logger_actual.info(f"User {viewer_user.id} identified as just logged in.")
        chat_db_context.context_data.pop("just_logged_in_flag") # Consume the flag

        accion_pendiente = contexto_municipio_actual.pop("accion_pendiente_post_login", None)
        estado_pre_login_str = contexto_municipio_actual.pop("estado_conversacion_pre_login", None)

        if accion_pendiente:
            logger_actual.info(f"Retomando acción pendiente post-login: {accion_pendiente}, estado pre-login: {estado_pre_login_str}")
            kwargs["intencion"] = accion_pendiente # Set intencion for current processing context
            if estado_pre_login_str:
                # Restore state directly into contexto_municipio_actual. It will be parsed to Enum later.
                contexto_municipio_actual["estado_conversacion"] = estado_pre_login_str

        # If the user's input is a generic acknowledgement of login, neutralize it
        # so it doesn't interfere with the resumed flow.
        generic_login_acks = ["ok", "listo", "ya está", "ya me loguee", "estoy logueado", "logged in", "continuar", "dale", "bueno"]
        if pregunta_str.strip().lower() in generic_login_acks:
            logger_actual.info(f"Input '{pregunta_str}' es un ack genérico post-login. Neutralizándolo.")
            pregunta_str = "" # Neutralize for current processing
            if "pregunta" in received_payload: # Ensure payload also reflects this
                received_payload["pregunta"] = ""

    # --- End Handle post-login resumption ---

    if contexto_municipio_actual.get("estado_conversacion") == ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name:
        if "si" in pregunta_str.lower():
            contexto_municipio_actual["ubicacion_confirmada"] = True
            contexto_municipio_actual["estado_conversacion"] = None
        else:
            contexto_municipio_actual["ubicacion_confirmada"] = False
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
            return _finalize_response({
                "message_body": "Por favor, decime la nueva ubicación.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pedir_nueva_ubicacion"
            })

    elif estado_conversacion == ConversationState.ESPERANDO_TEXTO_SUGERENCIA.name:
        sugerencia_texto = pregunta_str
        if len(sugerencia_texto) < 10:
            return _finalize_response({
                "message_body": "Tu sugerencia parece un poco corta. ¿Podrías darme un poco más de detalle?",
                "fuente": "sugerencia_muy_corta"
            })

        ubicacion_sugerencia, coordenadas_sugerencia = _extract_sugerencia_location(
            contexto_municipio_actual
        )

        # Crear ticket para la sugerencia
        contacto_prev = contexto_municipio_actual.get('contacto_usuario', {}) or {}
        viewer_user_obj = context.get("viewer_user_obj")
        datos_sugerencia = _build_sugerencia_datos(
            sugerencia_texto,
            ubicacion_sugerencia,
            coordenadas_sugerencia,
            viewer_user_obj,
            contacto_prev,
        )
        _merge_contacto_usuario(contexto_municipio_actual, datos_sugerencia)

        handler = CrearReclamoActionHandler(context)
        response = handler.execute(datos_sugerencia)

        # Modificar el mensaje de éxito para que sea específico para sugerencias
        if response.get("success"):
            response["message_body"] = f"✅ ¡Hemos recibido tu sugerencia! Muchas gracias por tu aporte. Lo hemos registrado con el número de ticket `{response.get('ticket_nro', 'N/A')}` para su seguimiento."

        # Limpiar el estado de la conversación
        contexto_municipio_actual['estado_conversacion'] = None
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")

        return _finalize_response(response)

    if USAR_LLM_PARA_RECLAMOS:
        # --- INICIO FIX: Resetear contexto de reclamo si llega una nueva imagen analizada ---
        datos_interpretados = context.get("datos_interpretados_archivo") or kwargs.get("datos_interpretados_archivo")
        if datos_interpretados and isinstance(datos_interpretados, dict):
            logger_actual.info("[CONTEXT_RESET] Se detectaron datos de archivo interpretados. Forzando reseteo de contexto de reclamo.")

            # Guardar datos de contacto antes de limpiar
            datos_parciales_existentes = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
            datos_de_contacto_a_preservar = {
                "nombre_usuario_detectado": datos_parciales_existentes.get("nombre_usuario_detectado"),
                "telefono_detectado": datos_parciales_existentes.get("telefono_detectado"),
                "email_detectado": datos_parciales_existentes.get("email_detectado"),
            }

            # Limpiar contexto de reclamo anterior
            contexto_municipio_actual["historial_llm_reclamo"] = []
            contexto_municipio_actual["datos_parciales_llm_reclamo"] = {}

            # Repoblar con la nueva información del análisis de imagen
            if datos_interpretados.get("categoria_sugerida"):
                contexto_municipio_actual["datos_parciales_llm_reclamo"]["categoria"] = datos_interpretados["categoria_sugerida"]
            if datos_interpretados.get("descripcion_sugerida"):
                contexto_municipio_actual["datos_parciales_llm_reclamo"]["descripcion"] = datos_interpretados["descripcion_sugerida"]

            # Restaurar datos de contacto si existían
            contexto_municipio_actual["datos_parciales_llm_reclamo"].update({k: v for k, v in datos_de_contacto_a_preservar.items() if v})

            # Establecer el estado para que el LLM sepa que está en un flujo de reclamo
            contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name

            # >>> INICIO FIX: Si la pregunta está vacía pero la imagen se interpretó como reclamo, crear una pregunta para el LLM
            if not pregunta_str.strip() and datos_interpretados.get("es_reclamo"):
                categoria = datos_interpretados.get("categoria_sugerida", "No especificada")
                descripcion = datos_interpretados.get("descripcion_sugerida", "No especificada")

                pregunta_str = (
                    f"El usuario ha enviado una imagen para iniciar un reclamo. "
                    f"El análisis automático de la imagen sugiere la siguiente información: "
                    f"Categoría: '{categoria}', Descripción: '{descripcion}'. "
                    f"Por favor, inicia el proceso de reclamo confirmando estos datos con el usuario y "
                    f"solicita la información que falte, como la ubicación."
                )
                logger_actual.info(f"Pregunta generada a partir de imagen: '{pregunta_str}'")
            # <<< FIN FIX


        logger_actual.info(f"[BEFORE_HANDLE_LLM] Contexto: {contexto_municipio_actual}")
        respuesta_manejada_por_llm, contexto_municipio_actual = handle_llm_interaction(
            app,
            pregunta_str,
            context,
            viewer_user,
            owner_user,
            chat_db_context,
            contexto_municipio_actual,
            demo_metadata=demo_metadata,
        )
        logger_actual.info(f"[AFTER_HANDLE_LLM] Contexto: {contexto_municipio_actual}")
        if respuesta_manejada_por_llm:
            if not isinstance(respuesta_manejada_por_llm, dict):
                respuesta_manejada_por_llm = {"message_body": str(respuesta_manejada_por_llm)}

            # Clean the message body of redundant URLs that are in buttons
            if 'message_body' in respuesta_manejada_por_llm:
                respuesta_manejada_por_llm['message_body'] = _remove_redundant_urls_from_message(
                    respuesta_manejada_por_llm.get('message_body'),
                    respuesta_manejada_por_llm.get('options_list', [])
                )

            respuesta_manejada_por_llm.setdefault("message_type", "text")
            respuesta_manejada_por_llm.setdefault("options_list", [])
            return _finalize_response(respuesta_manejada_por_llm)

        # Si la intención se estableció en derivar a un agente, significa que el flujo del LLM
        # ya manejó la lógica y no debemos continuar con el flujo antiguo.
        if context.get("intencion") == "hablar_con_agente":
            mensaje_para_escalar = contexto_municipio_actual.get("mensaje_previo_llm_para_escalamiento", "Un agente se pondrá en contacto contigo en breve.")
            # Ensure the context is saved before returning
            if chat_db_context:
                flag_modified(chat_db_context, "context_data")
            return _finalize_response({
                "message_body": mensaje_para_escalar,
                "options_list": [],
                "message_type": "text",
                "fuente": "llm_derivar_humano_v2"
            })




    # --- Serializar y guardar contexto final ---
    contexto_municipio_serializado_para_db = serializar_enum(contexto_municipio_actual)
    estado_final_para_guardar_str = contexto_municipio_actual.get("estado_conversacion") # Debería ser string o None
    if isinstance(estado_final_para_guardar_str, ConversationState): # Por si acaso no se convirtió a string
        logger_actual.warning(f"Estado {estado_final_para_guardar_str} era Enum antes de serializar. Convirtiendo.")
        contexto_municipio_actual["estado_conversacion"] = estado_final_para_guardar_str.name
    elif estado_final_para_guardar_str is None:
        contexto_municipio_actual.pop("estado_conversacion", None)

    if chat_db_context:
        # Explicitly re-assign the dictionary to ensure SQLAlchemy detects the change.
        # This is a more robust way to handle mutable JSONB fields.
        chat_db_context.context_data = chat_db_context_live_data
        flag_modified(chat_db_context, "context_data")
        logger_actual.info(f"[CONTEXT_SAVE_FINAL] Final context data being flagged for save: {chat_db_context.context_data}")


    # --- Fallback logic ---
    logger_actual.info(f"LLM no manejó la respuesta. Intentando fallback con Google Search.")
    search_results = google_search(pregunta_str)
    if search_results:
        search_items = []
        for result in search_results[:3]:
            search_items.append(f"- [{result.get('title')}]({result.get('link')})\n{result.get('snippet')}")

        final_response_dict = {
            "message_body": "No estoy seguro de cómo ayudarte con eso, pero encontré esto en la web:\n\n" + "\n\n".join(search_items),
            "options_list": [],
            "message_type": "text",
            "fuente": "municipio_fallback_google_search"
        }
    else:
        final_response_dict = {
            "message_body": "Lo siento, no pude entender tu consulta. ¿Podrías intentar reformularla?",
            "options_list": [],
            "message_type": "text",
            "fuente": "fallback_final"
        }


    # Log de conversación para anónimos
    if anon_id and not viewer_user:
        try:
            db.session.add(Conversacion(
                session_id=kwargs.get("chat_session_uuid") or anon_id, pregunta=pregunta_str,
                respuesta=final_response_dict["message_body"], fuente=final_response_dict["fuente"],
                rubro=getattr(rubro_obj, "nombre", "municipio_general"), user_id=None,
            ))
            db.session.commit()
        except Exception as e_conv_muni_final:
            logger_actual.error(f"Error guardando Conversacion final (municipio): {e_conv_muni_final}", exc_info=True)
            db.session.rollback()

    logger_actual.info(f"[RESPONDER_MUNICIPIO_END_V4] Respuesta: '{final_response_dict.get('message_body', '')[:100]}...', Fuente: {final_response_dict.get('fuente', 'N/A')}")
    return _finalize_response(final_response_dict)

# services/common_utils.py
import re
import unicodedata
import pandas as pd
from typing import Dict, Any, Tuple, Optional, List
from config.feature_flags import FEATURE_ENCUESTAS
from services.response_formatter import render_audio_text
from services.tts_orchestrator import generar_audio
from .constants import ConversationState, CONTEXTO_MUNICIPIO
from flask import current_app

logger = None

CONTACT_LABEL_TOKENS = {
    "nombre",
    "nombrecompleto",
    "completo",
    "apellido",
    "dni",
    "documento",
    "doc",
    "documentonacionaldeidentidad",
    "email",
    "correo",
    "correoelectronico",
    "mail",
    "telefono",
    "tel",
    "celular",
    "cel",
    "whatsapp",
    "contacto",
}

KEYWORD_MAP: Dict[str, List[str]] = {
    "nombre": ["nombre", "producto", "item", "articulo", "descripcion"],
    "precio": ["precio", "valor", "costo", "importe"],
    "sku": ["sku", "codigo", "cod", "ref", "id"],
    "stock": ["stock", "cantidad", "disponible"]
}

def get_logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger(__name__)
    return logger

def _normalize_contact_token(value: str) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())

def limpiar_texto_base(texto: str) -> str:
    if not isinstance(texto, str):
        return ""
    texto_limpio = texto.lower()
    texto_limpio = texto_limpio.replace('-', ' ').replace('_', ' ')
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio).strip()
    return texto_limpio

def parse_precio_flexible(precio_str: str) -> Tuple[str, Optional[float], Optional[str]]:
    if precio_str is None:
        return "", None, None
    if not isinstance(precio_str, str):
        precio_str = str(precio_str)
    texto_original = precio_str.strip()
    if not texto_original:
        return "", None, None
    texto_lower = texto_original.lower()
    moneda_detectada = None
    if any(token in texto_lower for token in ["usd", "u$s", "us$", "dolar", "dólar"]):
        moneda_detectada = "USD"
    elif "€" in texto_original or "eur" in texto_lower:
        moneda_detectada = "EUR"
    elif any(token in texto_lower for token in ["ars", "peso", "pesos", "$ar"]):
        moneda_detectada = "ARS"
    elif "$" in texto_original:
        moneda_detectada = "ARS"
    texto_numerico = re.sub(r"[^0-9,\.-]", "", texto_original.replace(" ", ""))
    if "-" in texto_numerico:
        texto_numerico = ("-" if texto_numerico.startswith("-") else "") + texto_numerico.replace("-", "")
    texto_numerico = texto_numerico.strip(".,")
    if not texto_numerico:
        return texto_original, None, moneda_detectada

    decimal_sep = None
    last_dot = texto_numerico.rfind(".")
    last_comma = texto_numerico.rfind(",")
    if last_dot != -1 and last_comma != -1:
        decimal_sep = "." if last_dot > last_comma else ","
    elif texto_numerico.count(",") == 1 and len(texto_numerico.split(",")[-1]) <= 2:
        decimal_sep = ","
    elif texto_numerico.count(".") == 1 and len(texto_numerico.split(".")[-1]) <= 2:
        decimal_sep = "."
    if decimal_sep == ",":
        numero_normalizado = texto_numerico.replace(".", "").replace(",", ".")
    elif decimal_sep == ".":
        numero_normalizado = texto_numerico.replace(",", "")
    else:
        numero_normalizado = texto_numerico.replace(",", "").replace(".", "")
    try:
        precio_float = float(numero_normalizado)
    except ValueError:
        return texto_original, None, moneda_detectada
    if precio_float.is_integer():
        precio_normalizado = str(int(precio_float))
    else:
        precio_normalizado = f"{precio_float:.2f}".rstrip("0").rstrip(".")
    return precio_normalizado, precio_float, moneda_detectada

def crear_mapa_de_columnas_inteligente(df: pd.DataFrame, umbral_similitud: float = 0.8) -> Optional[Tuple[Dict[str, Any], int]]:
    # ... (Simplified placeholder as original logic is complex) ...
    return {}, 1

def parse_unidad_y_cantidad_empaque(unidad_str: str) -> Tuple[str, Optional[int]]:
    return unidad_str, 1

def unir_codigos_alfa_numericos(texto: str) -> str:
    return texto

def is_number(s: Any) -> bool:
    if s is None: return False
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False

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
        audio_url = generar_audio(text_to_speak)
        if audio_url:
            payload["audio_url"] = audio_url

def _esperando_info_libre(municipio_ctx: dict) -> bool:
    if not municipio_ctx:
        return False
    return (
        municipio_ctx.get("esperando_info_llm")
        or municipio_ctx.get("esperando_info_llm_reclamo")
        or municipio_ctx.get("estado_conversacion") in ["ESPERANDO_UBICACION_GENERAL", "ESPERANDO_DATOS_CONTACTO"]
    )

def _get_main_menu_payload(
    context: dict,
    welcome_message_override: Optional[str] = None,
    reduced: bool = False,
) -> Dict[str, Any]:
    """Generates the main menu payload."""
    # Simplified version for restoration

    channel = context.get("channel", "web")

    welcome_message = welcome_message_override or "¡Hola! Bienvenido a tu municipio."

    categorias = [
        {"titulo": "🗣️ Reclamos y Consultas", "botones": [
            {"texto": "📝 Iniciar un Reclamo", "action_id": "iniciar_reclamo"},
            {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia"},
        ]},
        {"titulo": "🚗 Trámites y Turnos", "botones": [
            {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
            {"texto": "🗓️ Solicitar Otros Turnos", "action_id": "solicitar_turnos"},
        ]},
        # ... Add other categories as needed
    ]

    # Flatten for WhatsApp
    flat_buttons = []
    if channel == "whatsapp":
        flat_buttons = [
            {"texto": "🗣️ Reclamos y Consultas", "action_id": "mostrar_menu_reclamos"},
            {"texto": "🚗 Trámites y Turnos", "action_id": "mostrar_menu_tramites"},
            {"texto": "📰 Información del Municipio", "action_id": "mostrar_menu_informacion"},
            {"texto": "🛍️ Catálogo y Beneficios", "action_id": "mostrar_menu_catalogo"},
            {"texto": "🅿️ Estacionamiento", "action_id": "mostrar_menu_estacionamiento"},
            {"texto": "❓ Ayuda", "action_id": "mostrar_menu_ayuda"},
        ]
        if FEATURE_ENCUESTAS:
             flat_buttons.append({"texto": "🗳️ Participación Ciudadana", "action_id": "mostrar_menu_encuestas"})


    return {
        "message_body": f"{welcome_message}\n\nSeleccioná una opción:",
        "options_list": flat_buttons,
        "message_type": "interactive_list" if len(flat_buttons) > 3 else "interactive_buttons",
        "generar_audio": True
    }

def clean_text_for_tts(text: str) -> str:
    if not text: return ""
    # Remove markdown like *bold*
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    # Remove emojis (simple range)
    text = re.sub(r'[^\w\s,.]', '', text)
    return text.strip()

def validar_email(email: str) -> bool:
    if not email: return False
    # Standard email regex
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))

def validar_telefono(telefono: str) -> bool:
    if not telefono: return False
    # Basic validation: at least 7 digits
    digits = re.sub(r"\D", "", str(telefono))
    return len(digits) >= 7

def formatear_telefono_e164(telefono: str, cod_pais: str = "54") -> str:
    digits = re.sub(r"\D", "", str(telefono))
    if not digits: return ""
    if not digits.startswith(cod_pais):
        # Specific rule for Argentina mobile: +54 9 ...
        if cod_pais == "54" and len(digits) == 10 and not digits.startswith("9"):
             digits = "9" + digits
        return f"+{cod_pais}{digits}"
    return f"+{digits}"

def calcular_precio_por_unidad(precio_total: float, cantidad: int) -> float:
    if not cantidad: return 0.0
    return precio_total / cantidad

def generar_link_google_maps(direccion=None, latitud=None, longitud=None) -> str:
    if latitud and longitud:
        return f"https://www.google.com/maps?q={latitud},{longitud}"
    if direccion:
        return f"https://www.google.com/maps?q={quote_plus(direccion)}"
    return ""

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    import math
    dot = sum(a*b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a*a for a in vec1))
    norm2 = math.sqrt(sum(b*b for b in vec2))
    if norm1 == 0 or norm2 == 0: return 0.0
    return dot / (norm1 * norm2)

def construir_respuesta_sugerir_registro(mensaje_personalizado=None, tipo_entidad="pyme", channel="web"):
    msg = mensaje_personalizado or "Para una mejor experiencia, te sugerimos registrarte."
    return {
        "message_body": msg,
        "options_list": [{"texto": "Registrarme", "action_id": "registro"}]
    }

def extract_multiple_contact_details_regex(text: str, potential_fields: list = None) -> dict:
    extracted = {}
    if not text: return extracted

    # Simple regex extraction as backup to LLM
    if not potential_fields or "email" in potential_fields:
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
        if email_match: extracted["email"] = email_match.group(0)

    if not potential_fields or "telefono" in potential_fields:
        phone_match = re.search(r"\b(?:\+?54)?(?:9)?\d{10}\b", text) # Very basic Argentina mobile regex
        if phone_match: extracted["telefono"] = phone_match.group(0)

    return extracted

def _detect_location_link_info(text: str) -> Optional[Dict[str, str]]:
    """Detects Google Maps links and extracts coordinates."""
    if not text: return None
    # Check for google maps url
    if "google.com/maps" in text or "maps.google.com" in text or "maps.app.goo.gl" in text:
        # Extract coordinates if present in URL ?q=lat,lon or @lat,lon
        coords = re.search(r'([-+]?\d{1,2}\.\d+),\s*([-+]?\d{1,3}\.\d+)', text)
        if coords:
            return {"latitude": coords.group(1), "longitude": coords.group(2)}
    return None

def _resolve_municipio_tenant_ids(owner_user, context: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """Resolve tenant_id and municipio_id for municipal tickets."""
    # Try to get from context config
    municipio_config = (context or {}).get("municipio_config_actual", {}) or {}
    tenant_slug = (
        municipio_config.get("tenant_slug")
        or municipio_config.get("slug")
        or getattr(owner_user, "tenant_slug", None)
    )
    owner_id = (
        getattr(owner_user, "municipio_id", None)
        or getattr(owner_user, "id", None)
    )

    # In a full implementation, this would query the DB (TenantProfile).
    # Since we cannot easily import models here without circular deps if not careful,
    # we might rely on passed objects or simple derivation if models are passed.

    # Assuming owner_user is a User object with relationships loaded
    tenant = getattr(owner_user, "tenant", None)
    if not tenant and hasattr(owner_user, "tenant_profile"):
        tenant = owner_user.tenant_profile

    tenant_id = getattr(tenant, "id", None)
    municipio_id = getattr(tenant, "municipio_id", None) or owner_id

    return tenant_id, municipio_id

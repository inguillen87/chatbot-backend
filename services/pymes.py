import logging
import re
import json
import uuid
import unicodedata
from datetime import datetime
from typing import Optional, Any
from enum import Enum, auto
from urllib.parse import urlparse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from fuzzywuzzy import process

from models import Conversacion, db, PymePedido, ArchivoAdjunto
try:
    from flask import session as flask_session, current_app, request # Añadir request
except Exception:
    flask_session = {}
    current_app = None
    request = None # Mock request si no hay contexto Flask

import models
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    buscar_catalogo_db_fallback,
    armar_respuesta_legible,
    CATALOGO_PYME
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import sugerencias_por_rubro
from services.logic import es_rubro_publico
from services.ticket_service import servicio_tickets
from services.ticket_utils import formatear_ticket_respuesta, remove_buttons_with_urls_in_message
from services.webinfo import obtener_info_web
from .common_utils import construir_respuesta_sugerir_registro # <--- NUEVA IMPORTACIÓN
from services.preferences import add_preference
from services.pedido_service import servicio_pedidos
from services import cart as cart_service
from services.promocion_service import promocion_service
from services import promo_service
from services.config_loader import cargar_configuracion_pyme
from services.pyme_menu import get_pyme_menu_payload, PYME_MENU_DISPLAY_ORDER
from services.pyme_multimodal import (
    PymeSessionState,
    PymeFlowResult,
    detect_intent_from_text,
    handle_keyword_intent,
    handle_location_payload,
    persist_order,
    handle_image_payload,
    handle_pdf_payload,
    ensure_session_context,
    load_catalog,
)
from .llm_utils import extract_multiple_contact_details_llm, resumir_descripcion_producto_llm
from .common_utils import validar_email, validar_telefono
from services.llm_orchestrator import llamar_llm_con_fallback # Import for proactive suggestions

logger = logging.getLogger(__name__)


def _slugify_rubro(value: Optional[str]) -> str:
    if not value:
        return "default"
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "default"

class PymeConversationState(Enum):
    IDLE = auto()
    ESPERANDO_PRODUCTO = auto()
    ESPERANDO_DATOS_CLIENTE_NOMBRE = auto()
    ESPERANDO_DATOS_CLIENTE_TELEFONO = auto()
    ESPERANDO_DATOS_CLIENTE_DIRECCION = auto()
    ESPERANDO_DATOS_CLIENTE_EMAIL = auto()
    CONFIRMANDO_PEDIDO = auto()
    ESPERANDO_CONFIRMACION_FINAL_CON_DATOS = auto()

CONTEXTO_PYME = "contexto_pyme_v2"
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme_v2"
MAX_HISTORIAL_CHAT = 30
CANCEL_KEYWORDS = {"cancel", "cancelar", "cancelalo", "anular", "borrar", "no gracias", "mejor no", "olvidalo"}


def _strip_variation_selector(value: Optional[str]) -> str:
    if not value:
        return ""
    return str(value).replace("\ufe0f", "").replace("\u200d", "")


def _normalize_user_input(value: Optional[str]) -> str:
    if not value:
        return ""
    text = _strip_variation_selector(str(value))
    normalized = unicodedata.normalize("NFKD", text)
    cleaned = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


RAW_GREETING_KEYWORDS = {
    "hola",
    "buenas",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "buen dia",
    "holaa",
}
GREETING_KEYWORDS = {_normalize_user_input(word) for word in RAW_GREETING_KEYWORDS}

RAW_MENU_COMMAND_KEYWORDS = {
    "menu",
    "menú",
    "menu principal",
    "menú principal",
    "inicio",
    "volver al inicio",
    "volver al menu",
    "volver al menú",
}
MENU_COMMAND_KEYWORDS = {_normalize_user_input(word) for word in RAW_MENU_COMMAND_KEYWORDS}


RAW_PYME_MENU_KEYWORDS = {
    "pyme_productos_stock": {
        "productos",
        "ver productos",
        "catálogo",
        "catalogo",
        "stock",
        "ver catalogo",
    },
    "pyme_promociones": {
        "promos",
        "promociones",
        "ofertas",
        "descuentos",
    },
    "pyme_hacer_pedido": {
        "hacer pedido",
        "nuevo pedido",
        "comprar",
        "hacer compra",
        "armar pedido",
    },
    "pyme_estado_pedido": {
        "estado de mi pedido",
        "seguimiento",
        "mi pedido",
        "ver pedido",
    },
    "pyme_hablar_agente": {
        "hablar con un asesor",
        "asesor",
        "humano",
        "representante",
        "agente",
    },
    "ver_carrito_pyme": {
        "ver carrito",
        "continuar pedido",
        "carrito",
    },
    "limpiar_y_nuevo_pedido_saludo_pyme": {
        "nuevo pedido",
        "empezar de nuevo",
        "arrancar de cero",
    },
}


def _build_keyword_mapping() -> tuple[dict[str, set[str]], dict[str, str]]:
    action_keywords: dict[str, set[str]] = {}
    keyword_map: dict[str, str] = {}
    for action, keywords in RAW_PYME_MENU_KEYWORDS.items():
        normalized_set: set[str] = set()
        for keyword in set(keywords) | {action}:
            normalized = _normalize_user_input(keyword)
            if not normalized:
                continue
            normalized_set.add(normalized)
            keyword_map.setdefault(normalized, action)
        action_keywords[action] = normalized_set
    return action_keywords, keyword_map


PYME_MENU_KEYWORDS, PYME_KEYWORD_MAPPING = _build_keyword_mapping()

ORDER_MAP = {action: idx for idx, action in enumerate(PYME_MENU_DISPLAY_ORDER)}


def _order_menu_options(options: list[dict]) -> list[dict]:
    indexed = list(enumerate(options))
    indexed.sort(
        key=lambda item: (
            ORDER_MAP.get(item[1].get("id") or item[1].get("action_id"), len(ORDER_MAP)),
            item[0],
        )
    )
    return [item[1] for item in indexed]


def _simplify_options(options: Optional[list[dict]]) -> list[dict]:
    if not options:
        return []

    simplified: list[dict] = []
    seen_ids: set[str] = set()

    for raw_option in options:
        if not isinstance(raw_option, dict):
            continue

        option = dict(raw_option)

        action_id = option.get("action_id") or option.get("id")
        if not action_id:
            action_id = _extract_action_id(option.get("payload"))
        if not action_id:
            action_id = _extract_action_id(option.get("value"))
        if not action_id:
            action_id = _extract_action_id(option.get("action"))
        if not action_id and option.get("texto"):
            action_id = option.get("texto")

        label = (
            option.get("texto")
            or option.get("label")
            or option.get("title")
            or option.get("prompt")
            or (action_id if isinstance(action_id, str) else "")
        )

        if isinstance(action_id, dict) or isinstance(action_id, list):
            action_id = _extract_action_id(action_id)

        if action_id:
            action_id = str(action_id)

        unique_key = action_id or str(label) or option.get("url")
        if not unique_key:
            continue
        if unique_key in seen_ids:
            continue
        seen_ids.add(unique_key)

        simplified_option: dict[str, Any] = {
            "action_id": action_id,
            "id": action_id or option.get("id") or unique_key,
            "texto": str(label).strip() or str(action_id or unique_key),
        }

        if option.get("descripcion"):
            simplified_option["descripcion"] = option["descripcion"]
        if option.get("description"):
            simplified_option["description"] = option["description"]
        if option.get("url"):
            simplified_option["url"] = option["url"]
        if option.get("emoji"):
            simplified_option["emoji"] = option["emoji"]
        if option.get("prompt"):
            simplified_option["prompt"] = option["prompt"]

        simplified.append(simplified_option)

    return simplified


def _extract_action_id(payload) -> Optional[str]:
    if not payload:
        return None
    if isinstance(payload, str):
        payload_str = payload.strip()
        if not payload_str:
            return None
        try:
            parsed = json.loads(payload_str)
        except json.JSONDecodeError:
            return payload_str
        return _extract_action_id(parsed)
    if isinstance(payload, dict):
        for key in ("action_id", "id", "payload", "value"):
            value = payload.get(key)
            action = _extract_action_id(value)
            if action:
                return action
        reply = payload.get("reply")
        if isinstance(reply, dict):
            action = _extract_action_id(reply)
            if action:
                return action
        action_container = payload.get("action")
        if action_container and action_container is not payload:
            action = _extract_action_id(action_container)
            if action:
                return action
    if isinstance(payload, list):
        for item in payload:
            action = _extract_action_id(item)
            if action:
                return action
    if payload is None:
        return None
    return str(payload)


def _find_menu_action_by_input(user_input: str, menu_buttons: list[dict]) -> Optional[str]:
    if not user_input:
        return None
    normalized_input = _normalize_user_input(user_input)
    if not normalized_input:
        return None

    direct_match = PYME_KEYWORD_MAPPING.get(normalized_input)
    if direct_match:
        return direct_match

    for button in menu_buttons or []:
        possible_values = [
            button.get("action_id"),
            button.get("id"),
            button.get("texto"),
        ]
        for candidate in possible_values:
            candidate_norm = _normalize_user_input(candidate)
            if candidate_norm and candidate_norm == normalized_input:
                return button.get("action_id") or button.get("id") or candidate

    try:
        selected_index = int(normalized_input) - 1
        if 0 <= selected_index < len(menu_buttons or []):
            selected_button = menu_buttons[selected_index]
            return selected_button.get("action_id") or selected_button.get("id")
    except (TypeError, ValueError):
        pass

    if len(normalized_input) == 1:
        for button in menu_buttons or []:
            button_text_norm = _normalize_user_input(button.get("texto"))
            if button_text_norm.startswith(normalized_input):
                return button.get("action_id") or button.get("id")

    if len(normalized_input) >= 3 and PYME_KEYWORD_MAPPING:
        best_match = process.extractOne(normalized_input, list(PYME_KEYWORD_MAPPING.keys()))
        if best_match and best_match[1] >= 85:
            return PYME_KEYWORD_MAPPING.get(best_match[0])

    return None


def tiene_archivo_catalogo(user_id: int) -> bool:
    if not user_id: return False
    try:
        return ArchivoAdjunto.query.filter_by(user_id=user_id, tipo="catalogo").first() is not None
    except Exception: return False

def url_descargar_catalogo_pyme(pyme_id: int) -> str:
    if not current_app or not request: # Si no hay contexto de app/request (ej. prueba unitaria)
        return f"/catalogo/publico/{pyme_id}/descargar" # Fallback a URL relativa
    base_url = current_app.config.get("APP_BASE_URL", request.url_root.rstrip('/'))
    return f"{base_url}/catalogo/publico/{pyme_id}/descargar"

import ast

def extraer_productos_llm(texto: str) -> list[dict]:
    prompt = (
        "Analiza el MENSAJE DEL USUARIO para extraer productos, sus cantidades numéricas y sus unidades de medida específicas si se mencionan (ej. 'kilos', 'cajas', 'paquetes', 'docenas', 'metros'). "
        "Responde ÚNICAMENTE con una lista de objetos JSON válida. Cada objeto debe tener:\n"
        "- \"nombre\": El nombre descriptivo del producto (string).\n"
        "- \"cantidad\": La cantidad numérica asociada al producto (integer).\n"
        "- \"unidad\" (opcional): La unidad de medida específica si se menciona (string). Si la unidad es implícita o genérica como 'unidades' o 'ítems', puedes omitirla.\n"
        f"MENSAJE DEL USUARIO: '{texto}'\n"
        "RESPUESTA:"
    )
    resp_content = ""
    try:
        resp_content = robust_chat(message=prompt)
        if not resp_content: return []
        datos = []
        try: datos = json.loads(resp_content)
        except json.JSONDecodeError:
            try:
                resp_corrected = resp_content.replace("true", "True").replace("false", "False").replace("null", "None")
                match_md_json = re.match(r"^\s*```json\s*([\s\S]*?)\s*```\s*$", resp_corrected, re.DOTALL)
                if match_md_json: resp_corrected = match_md_json.group(1)
                datos = ast.literal_eval(resp_corrected)
            except: return []
        items: list[dict] = []
        if isinstance(datos, list):
            for it in datos:
                if not isinstance(it, dict): continue
                nombre = str(it.get("nombre", "")).strip()
                cantidad_raw = it.get("cantidad", 1); unidad = str(it.get("unidad", "")).strip(); cantidad = 1
                try: cantidad = int(float(str(cantidad_raw).replace(',','.')))
                except: pass
                if cantidad < 1: cantidad = 1
                if nombre: 
                    item_data = {"nombre": nombre, "cantidad": cantidad}
                    if unidad: item_data["unidad"] = unidad
                    items.append(item_data)
        elif isinstance(datos, dict):
            nombre = str(datos.get("nombre", "")).strip(); cantidad_raw = datos.get("cantidad", 1)
            unidad = str(datos.get("unidad", "")).strip(); cantidad = 1
            try: cantidad = int(float(str(cantidad_raw).replace(',','.')))
            except: pass
            if cantidad < 1: cantidad = 1
            if nombre:
                item_data = {"nombre": nombre, "cantidad": cantidad}
                if unidad: item_data["unidad"] = unidad
                items.append(item_data)
        return items
    except Exception as e: logger.exception(f"Error en extraer_productos_llm: {e}")
    return []

def extraer_productos_regex(texto: str) -> list[dict]:
    partes = re.split(r",\s*(?![^()]*\))|\s+y\s+(?![^()]*\))", texto)
    items: list[dict] = []
    pattern = re.compile(r"^\s*(\d+\.?\d*|\d+)\s*([a-zA-Záéíóúñ/\-]+(?:\s+[a-zA-Záéíóúñ/\-]+)*)?\s*(?:de\s+)?(.+)", re.IGNORECASE)
    for p_str in partes:
        p_str = p_str.strip()
        match = pattern.match(p_str)
        if match:
            try:
                cantidad_str = match.group(1); unidad_str = match.group(2); nombre_str = match.group(3).strip()
                if unidad_str and len(unidad_str.split()) > 2: nombre_str = f"{unidad_str} {nombre_str}".strip(); unidad_str = None
                if nombre_str.lower().endswith(" de"): nombre_str = nombre_str[:-3].strip()
                if nombre_str:
                    item_data = {"nombre": nombre, "cantidad": int(float(cantidad_str.replace(',','.')))}
                    if unidad_str and unidad_str.lower() not in ["unidad", "unidades"]: item_data["unidad"] = unidad_str.lower()
                    items.append(item_data)
            except: pass
        elif p_str:
            m_simple_nombre_primero = re.match(r"(.+?)\s+(\d+\.?\d*|\d+)\s*([a-zA-Záéíóúñ/\-]+)?$", p_str)
            if m_simple_nombre_primero:
                try:
                    nombre = m_simple_nombre_primero.group(1).strip(); cantidad = int(float(m_simple_nombre_primero.group(2).replace(',','.'))); unidad = m_simple_nombre_primero.group(3)
                    if nombre:
                        item_data = {"nombre": nombre, "cantidad": cantidad}
                        if unidad and unidad.lower() not in ["unidad", "unidades"]: item_data["unidad"] = unidad.lower()
                        items.append(item_data)
                except: pass
            elif p_str: items.append({"nombre": p_str, "cantidad": 1})
    return items

def extraer_productos_pedido(texto: str) -> list[dict]:
    items_regex = extraer_productos_regex(texto)
    if items_regex: return items_regex
    return extraer_productos_llm(texto)

def formatear_carrito_desde_summary(summary_cart_obj: dict, context: dict = None) -> str:
    if not summary_cart_obj or not summary_cart_obj.get("items_detalle"):
        return "Tu carrito está vacío."
    items_detalle = summary_cart_obj.get("items_detalle", [])
    lineas_carrito = ["**Tu Carrito de Compras:**"]
    for item in items_detalle:
        nombre = item.get("nombre_producto", "Desconocido"); cantidad = item.get("cantidad", 0)
        precio_original_unit = item.get("precio_unitario_original", 0.0)
        subtotal_con_descuento_item = item.get("subtotal_con_descuento", cantidad * precio_original_unit)
        descuento_aplicado_linea = item.get("descuento_aplicado_linea", 0.0)
        moneda = item.get("moneda", "ARS"); presentacion = item.get("presentacion", "")
        promocion_aplicada_item_info = item.get("promocion_aplicada_info")
        linea = f"- {cantidad} x {nombre}"
        if presentacion: linea += f" ({presentacion})"
        linea += f" @ ${precio_original_unit:,.2f} {moneda} c/u"
        if descuento_aplicado_linea > 0:
            precio_original_total_linea = cantidad * precio_original_unit
            linea += f" (Original: <s style='color:grey;'>${precio_original_total_linea:,.2f}</s>)"
            linea += f" <b style='color:green;'>Ahora: ${subtotal_con_descuento_item:,.2f} {moneda}</b>"
            if promocion_aplicada_item_info and promocion_aplicada_item_info.get("nombre_promocion"):
                 linea += f" <i style='font-size:smaller; color:green;'>({promocion_aplicada_item_info['nombre_promocion']})</i>"
        else: linea += f" = ${subtotal_con_descuento_item:,.2f} {moneda}"
        lineas_carrito.append(linea)
    total_original_calc = summary_cart_obj.get("total_original_calculado", 0.0)
    total_final_desc = summary_cart_obj.get("total_final_con_descuento", total_original_calc)
    total_ahorrado = summary_cart_obj.get("total_ahorrado_final", 0.0)
    moneda_carrito = items_detalle[0].get("moneda", "ARS") if items_detalle else "ARS"
    if total_original_calc > 0:
        lineas_carrito.append(f"\nSubtotal Original: ${total_original_calc:,.2f} {moneda_carrito}")
        if total_ahorrado > 0:
            lineas_carrito.append(f"**Descuentos Totales: -${total_ahorrado:,.2f} {moneda_carrito}** 🎉")
            promo_total_info = summary_cart_obj.get("promo_total_carrito_aplicada_info")
            if promo_total_info: lineas_carrito.append(f"<i style='font-size:smaller; color:green;'>Promo sobre el total: '{promo_total_info['nombre_promocion']}' (-${promo_total_info['descuento_sobre_total_aplicado']:.2f})</i>")
            nombres_promos_items_unicos = set()
            for item_det in items_detalle:
                if item_det.get("promocion_aplicada_info") and item_det["promocion_aplicada_info"].get("nombre_promocion"):
                    nombres_promos_items_unicos.add(item_det["promocion_aplicada_info"]["nombre_promocion"])
            if nombres_promos_items_unicos and not promo_total_info:
                 if len(nombres_promos_items_unicos) == 1: lineas_carrito.append(f"<i style='font-size:smaller; color:green;'>Promoción aplicada: {list(nombres_promos_items_unicos)[0]}</i>")
                 elif len(nombres_promos_items_unicos) > 1: lineas_carrito.append(f"<i style='font-size:smaller; color:green;'>Promociones aplicadas: {', '.join(list(nombres_promos_items_unicos))}</i>")
        lineas_carrito.append(f"\n**TOTAL A PAGAR: ${total_final_desc:,.2f} {moneda_carrito}**")
    return "\n".join(lineas_carrito)


# Helper to serialize Enum objects within dicts/lists for JSON
def serializar_enum(obj):
    if isinstance(obj, Enum):
        return obj.name
    elif isinstance(obj, dict):
        return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serializar_enum(v) for v in obj]
    else:
        return obj


def _build_pyme_order_success_payload(context: dict, handler_response: dict) -> dict:
    """Render a rich confirmation payload for completed pyme orders."""

    data = handler_response.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    pedido_block = data.get("pedido") if isinstance(data.get("pedido"), dict) else None
    pedido_info = pedido_block or data

    cart_summary = data.get("cart_summary") if isinstance(data.get("cart_summary"), dict) else None
    if not cart_summary and pedido_block and isinstance(pedido_block.get("cart_summary"), dict):
        cart_summary = pedido_block.get("cart_summary")

    monto_total = data.get("monto_total")
    if monto_total is None and pedido_block:
        monto_total = pedido_block.get("total")
    if monto_total is None and cart_summary:
        monto_total = cart_summary.get("total_final_con_descuento") or cart_summary.get("total_original_calculado")
    try:
        monto_total_float = float(monto_total) if monto_total not in (None, "") else None
    except (TypeError, ValueError):
        monto_total_float = None

    nro_pedido = (
        data.get("nro_pedido")
        or pedido_info.get("nro_pedido")
        or pedido_info.get("nro")
        or handler_response.get("nro_pedido")
    )
    pedido_id = (
        data.get("pedido_id")
        or pedido_info.get("pedido_id")
        or pedido_info.get("id")
    )

    cliente_info = data.get("cliente")
    if not isinstance(cliente_info, dict) and isinstance(pedido_info.get("cliente"), dict):
        cliente_info = pedido_info.get("cliente")
    if not isinstance(cliente_info, dict):
        cliente_info = {}

    # Name Resolution Logic - improved to prioritize real names
    pyme_ctx = context.get(CONTEXTO_PYME, {})
    nombre_cliente = cliente_info.get("nombre")

    if not nombre_cliente:
        nombre_cliente = pyme_ctx.get("nombre_cliente")

    viewer = context.get("viewer_user_obj")
    candidate_user_name = getattr(viewer, "name", None) if viewer else None

    # Trust authenticated user profile name if available and not generic
    if candidate_user_name and candidate_user_name.lower() not in ["vecino/a", "cliente", "usuario", "unknown"]:
        # If we currently have no name, or a generic name, overwrite it
        if not nombre_cliente or nombre_cliente.lower() in ["cliente", "vecino/a", "vecino"]:
            nombre_cliente = candidate_user_name

    if not nombre_cliente:
        nombre_cliente = "Cliente"

    telefono_cliente = cliente_info.get("telefono")
    email_cliente = cliente_info.get("email")
    direccion_cliente = cliente_info.get("direccion")

    summary_text = (
        data.get("order_summary_text")
        or handler_response.get("order_summary_text")
    )
    if not summary_text and cart_summary:
        summary_text = formatear_carrito_desde_summary(cart_summary, context)

    items_detalle = []
    if cart_summary and isinstance(cart_summary.get("items_detalle"), list):
        items_detalle = cart_summary.get("items_detalle")

    total_items = 0
    destacado = None
    for item in items_detalle:
        if not isinstance(item, dict):
            continue
        cantidad_val = item.get("cantidad") or item.get("cantidad_producto") or 0
        try:
            cantidad_int = int(cantidad_val)
        except (TypeError, ValueError):
            try:
                cantidad_int = int(float(str(cantidad_val).replace(",", ".")))
            except Exception:
                cantidad_int = 0
        if cantidad_int > 0:
            total_items += cantidad_int
        if not destacado:
            destacado = (
                item.get("nombre_producto")
                or item.get("nombre")
                or item.get("sku")
            )

    description = data.get("order_description")
    if not description:
        if total_items and destacado:
            description = f"Pedido con {total_items} ítems (destacado: {destacado})"
        elif total_items:
            description = f"Pedido con {total_items} ítems"
        else:
            description = "Pedido registrado"
        if monto_total_float is not None:
            description += f" — Total ${monto_total_float:,.2f}"

    rubro_nombre = (
        context.get("rubro_nombre")
        or context.get("rubro_clave")
        or "Pedido"
    )

    channel_value = str(context.get("channel") or "").lower()
    include_links = not (channel_value.startswith("web") or "widget" in channel_value)

    base_tracking_url = None
    if current_app:
        # Prioritize APP_BASE_URL for consistent production links
        app_base_url = current_app.config.get("APP_BASE_URL")

        # FIX: Ensure we don't leak localhost. Enforce https://chatboc.ar if missing or localhost.
        if not app_base_url or "localhost" in app_base_url:
             app_base_url = "https://chatboc.ar"

        if app_base_url:
            base_tracking_url = f"{app_base_url.rstrip('/')}/pyme/pedidos"

        if not base_tracking_url:
            base_tracking_url = current_app.config.get("PYME_PEDIDOS_PUBLIC_URL")
        if not base_tracking_url:
            panel_url = current_app.config.get("PANEL_URL")
            if panel_url:
                base_tracking_url = f"{panel_url.rstrip('/')}/pyme/pedidos"
        if not base_tracking_url:
            backend_url = current_app.config.get("BACKEND_URL")
            if backend_url:
                base_tracking_url = f"{backend_url.rstrip('/')}/pyme/pedidos"

    # Final fallback if nothing else is set
    if not base_tracking_url:
        base_tracking_url = "https://www.chatboc.ar/pyme/pedidos"

    message_body, base_buttons = formatear_ticket_respuesta(
        "pedido",
        nombre_cliente,
        description,
        rubro_nombre,
        nro_pedido,
        {},
        base_tracking_url,
        dni=None,
        consulta_pin=pedido_info.get("consulta_pin"),
        include_links_in_message=include_links,
    )

    if summary_text:
        message_body = f"{message_body}\n\n{summary_text}".strip()

    buttons: list[dict] = []
    seen_text_action: set[tuple[str, str]] = set()
    seen_url_fingerprints: set[tuple[str, str, str]] = set()

    def _url_fingerprint(raw_url: Optional[str]) -> Optional[tuple[str, str, str]]:
        if not raw_url:
            return None
        parsed = urlparse(str(raw_url))
        if not parsed.scheme or not parsed.netloc:
            return None
        path = parsed.path.rstrip("/") or "/"
        return (parsed.scheme.lower(), parsed.netloc.lower(), path)

    def _register_button(button: Any) -> None:
        if not isinstance(button, dict):
            return
        candidate = dict(button)
        text = str(candidate.get("texto") or candidate.get("title") or "").strip()
        action_id = (
            candidate.get("action_id")
            or candidate.get("id")
            or candidate.get("id_accion")
            or ""
        )
        key = (text.lower(), str(action_id).lower())
        url_fp = _url_fingerprint(candidate.get("url"))
        if url_fp and url_fp in seen_url_fingerprints:
            return
        if not url_fp and key in seen_text_action:
            return
        if text:
            candidate["texto"] = text
        buttons.append(candidate)
        seen_text_action.add(key)
        if url_fp:
            seen_url_fingerprints.add(url_fp)

    for btn in base_buttons or []:
        _register_button(btn)
    for btn in handler_response.get("options_list") or []:
        _register_button(btn)
    for btn in handler_response.get("botones") or []:
        _register_button(btn)

    default_buttons = [
        {"texto": "🛒 Hacer otro pedido", "action_id": "pyme_hacer_pedido"},
        {"texto": "📦 Ver catálogo", "action_id": "pyme_productos_stock"},
        {"texto": "🤝 Hablar con un asesor", "action_id": "pyme_hablar_agente"},
    ]
    for btn in default_buttons:
        _register_button(btn)

    # Pass owner_user explicitly to avoid leakage via global context
    owner_user_id = context.get("user_id")
    owner_user_obj = None
    tenant_profile = None
    if owner_user_id:
        owner_user_obj = db.session.get(models.User, owner_user_id)
        if owner_user_obj:
            tenant_profile = getattr(owner_user_obj, "tenant_profile_pyme", None)

    # Explicitly disabled for Pymes to prevent "Punto Limpio" leakage
    # Pymes should strictly have their own branding or no promo section by default
    promo_section = None
    image_url = handler_response.get("image_url")

    if include_links:
        url_buttons_before_cleanup = [dict(btn) for btn in buttons if btn.get("url")]
        buttons = remove_buttons_with_urls_in_message(message_body, buttons)
        if not any(btn.get("url") for btn in buttons) and url_buttons_before_cleanup:
            fallback_button = url_buttons_before_cleanup[0]
            if fallback_button.get("texto") and fallback_button.get("url"):
                fallback_button.setdefault("type", "url")
                buttons.append(fallback_button)

    delayed_payload = handler_response.get("delayed_payload")
    if not delayed_payload:
        delayed_payload = get_pyme_menu_payload(context, channel=context.get("channel", "web"))
    delay_seconds = handler_response.get("delay_seconds", 20)

    interactive_buttons = [btn for btn in buttons if btn.get("type") != "url"]
    if not buttons:
        message_type = "text"
    elif len(interactive_buttons) == 0:
        message_type = "text"
    elif len(interactive_buttons) <= 3:
        message_type = "interactive_buttons"
    else:
        message_type = "interactive_list"

    result: dict[str, Any] = {
        "success": True,
        "message_body": message_body,
        "options_list": buttons,
        "message_type": message_type,
        "data": data,
        "fuente": "pyme_pedido_confirmado",
    }

    if pedido_id:
        result["ticket_id"] = pedido_id
    if image_url:
        result["image_url"] = image_url

    audio_url = handler_response.get("audio_url")
    if audio_url:
        result["audio_url"] = audio_url
    audio_text = handler_response.get("audio_text")
    if audio_text:
        result["audio_text"] = audio_text

    cliente_payload = {
        "nombre": nombre_cliente,
        "telefono": telefono_cliente,
        "email": email_cliente,
        "direccion": direccion_cliente,
    }
    data.setdefault("cliente", cliente_payload)

    return result

# PROMPT_CLASIFICAR_INTENCION y _clasificar_intencion_pyme_con_llm eliminados.
# La intención vendrá de la llamada principal al LLM.

def analizar_sentimiento_llm(texto: str) -> str:
    # Esta función aún usa robust_chat (Cohere). Se revisará en una fase posterior si debe migrar al LLM
    # o si se mantiene para tareas específicas de análisis de sentimiento si Cohere es preferido para eso.
    # Por ahora, se deja como está, asumiendo que `robust_chat` sigue funcional o será mockeado en tests.
    prompt = (f"Analiza el sentimiento del texto y responde 'positivo', 'negativo' o 'neutral'. TEXTO: '{texto}'\nSENTIMIENTO:")
    try:
        res = robust_chat(message=prompt); sentimiento = res.strip().lower()
        return sentimiento if sentimiento in {"positivo", "negativo"} else "neutral"
    except: return "neutral"

from services.actions.base_action_handler import BaseActionHandler

class BaseHandler(BaseActionHandler):
    def __init__(self, context):
        super().__init__(context)
        self.pyme_ctx = self.context.get(CONTEXTO_PYME, {})
        self.pyme_id_actual = self.context.get("user_id")
        self.cliente_id_actual = self.context.get("cliente_id")
        self.chat_session_uuid_actual = self.context.get("chat_session_uuid")

        # Get cart data from chat_db_context_data
        chat_db_context_data = self.context.get("chat_db_context_data", {})
        self.pyme_carts_data = chat_db_context_data.get(cart_service.SESSION_CARTS_KEY, {})

    def _guardar_contexto_pyme(self):
        if self.context.get("chat_db_context_data"):
            self.context["chat_db_context_data"][CONTEXTO_PYME] = self.pyme_ctx
            self.context["chat_db_context_data"][cart_service.SESSION_CARTS_KEY] = self.pyme_carts_data
        else:
            logger.error("[BaseHandler._guardar_contexto_pyme] chat_db_context_data no encontrado en self.context.")

    def _actualizar_estado(self, nuevo_estado: str, reintentos: int = 0):
        self.pyme_ctx["estado_conversacion"] = nuevo_estado
        self.pyme_ctx["reintentos"] = reintentos
        self._guardar_contexto_pyme()

    def _obtener_contexto_llm(self) -> dict:
        summary = {
            "estado_actual_flujo": self.pyme_ctx.get("estado_conversacion"),
            "ultima_intencion_registrada": self.context.get("intencion")
        }
        if self.pyme_id_actual:
            cart_summary = cart_service.get_cart_summary(self.pyme_carts_data, self.pyme_id_actual, self.cliente_id_actual)
            if cart_summary and cart_summary.get("items_detalle"):
                summary["items_carrito_nombres"] = [item.get("nombre_producto") for item in cart_summary["items_detalle"][:2]]
                summary["total_carrito_actual"] = cart_summary.get("total_final_con_descuento")

        for k in ["nombre_cliente", "producto_interes_previo", "productos_vistos_o_mencionados"]:
            if self.pyme_ctx.get(k):
                summary[k] = self.pyme_ctx[k]

        return {k: v for k, v in summary.items() if v is not None}

    def execute(self, action_data):
        raise NotImplementedError

class SaludoHandler(BaseHandler):
    def execute(self, action_data):
        channel = self.context.get("channel", "web")
        rubro_slug = (
            self.pyme_ctx.get("rubro_slug")
            or self.context.get("rubro_nombre")
            or getattr(self.context.get("rubro_obj"), "slug", None)
        )
        menu_context = {
            "rubro_slug": rubro_slug,
            "nombre_pyme": self.context.get("nombre_pyme"),
            "nombre_pyme_cache": self.pyme_ctx.get("nombre_pyme_cache"),
        }

        menu_payload = get_pyme_menu_payload({**menu_context, **{k: v for k, v in self.context.items() if k in {"nombre_pyme", "rubro_nombre"}}}, channel=channel)
        menu_payload.setdefault("fuente", "pyme_saludo_menu_principal_v4")
        menu_payload.setdefault("success", True)

        opciones_menu = menu_payload.get("options_list", [])
        opciones_extra = []

        if self.pyme_id_actual:
            resumen_carrito_existente = cart_service.get_cart_summary(
                self.pyme_carts_data,
                self.pyme_id_actual,
                self.cliente_id_actual,
            )
            if resumen_carrito_existente and resumen_carrito_existente.get("items_detalle"):
                mensaje_cart = (
                    "\n\nTenés un pedido en progreso. Podés retomarlo o iniciar uno nuevo."
                )
                menu_payload["message_body"] = menu_payload.get("message_body", "").strip()
                if menu_payload["message_body"]:
                    menu_payload["message_body"] += mensaje_cart
                else:
                    menu_payload["message_body"] = mensaje_cart.strip()
                opciones_extra.extend(
                    [
                        {"id": "ver_carrito_pyme", "texto": "Continuar pedido"},
                        {"id": "limpiar_y_nuevo_pedido_saludo_pyme", "texto": "Nuevo pedido"},
                    ]
                )

        if opciones_extra:
            opciones_menu = opciones_extra + opciones_menu

        opciones_simplificadas = _simplify_options(opciones_menu)
        opciones_finales: list[dict] = []
        seen_ids: set[str] = set()
        for opt in opciones_simplificadas:
            action_id = opt.get("action_id") or opt.get("texto")
            if not action_id:
                continue
            if action_id in seen_ids:
                continue
            seen_ids.add(action_id)
            opciones_finales.append({
                "id": action_id,
                "texto": opt.get("texto", action_id),
            })
            if len(opciones_finales) >= 10:
                break

        opciones_finales = _order_menu_options(opciones_finales)
        menu_payload["options_list"] = opciones_finales
        if isinstance(menu_payload.get("categorias"), list) and menu_payload["categorias"]:
            menu_payload["categorias"][0]["botones"] = [
                {
                    "texto": opt.get("texto"),
                    "action_id": opt.get("id"),
                }
                for opt in opciones_finales
            ]

        if channel and str(channel).lower() == "whatsapp":
            existing_body = menu_payload.get("message_body", "").strip()
            reminder_line = "Respondé con el número de la opción que prefieras."
            if reminder_line not in existing_body:
                menu_payload["message_body"] = (
                    f"{existing_body}\n\n{reminder_line}" if existing_body else reminder_line
                )
            menu_payload["message_type"] = "text"

        self.pyme_ctx["last_options_sent"] = opciones_finales
        chat_context_data = self.context.get("chat_db_context_data")
        if isinstance(chat_context_data, dict):
            chat_context_data["last_options_sent"] = opciones_finales

        return menu_payload

class CatalogoHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "catalogo_sin_pyme_id_v2"}
        query_qdrant = pregunta
        if self.context.get("intencion") == "ver_catalogo" and len(pregunta.split()) < 3: query_qdrant = "productos populares"

        # Detect filters from natural language
        en_promocion = False
        con_stock = False
        precio_max = None

        pregunta_lower = pregunta.lower()
        if "oferta" in pregunta_lower or "promo" in pregunta_lower or "descuento" in pregunta_lower:
            en_promocion = True
        if "stock" in pregunta_lower or "disponible" in pregunta_lower:
            con_stock = True

        # Simple regex for "menor a 1000" or "menos de 1000"
        match_precio = re.search(r"(?:menor|menos)\s+(?:a|de)\s+(?:\$)?\s*(\d+)", pregunta_lower)
        if match_precio:
            try:
                precio_max = float(match_precio.group(1))
            except ValueError:
                pass

        resultados_qdrant = buscar_catalogo_qdrant(
            user_id=self.pyme_id_actual,
            pregunta=query_qdrant,
            limite=3,
            categoria=self.context.get("rubro_nombre"),
            coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME),
            en_promocion=en_promocion,
            con_stock=con_stock,
            precio_max=precio_max
        )

        # Fallback mechanism: If Qdrant returns nothing, try SQL DB
        if not resultados_qdrant:
            try:
                resultados_qdrant = buscar_catalogo_db_fallback(
                    user_id=self.pyme_id_actual,
                    pregunta=query_qdrant,
                    limite=3,
                    precio_max=precio_max
                )
                if resultados_qdrant:
                    logger.info(f"Fallback DB search success for '{pregunta}'")
            except Exception as e:
                logger.error(f"Fallback search failed: {e}")

        chat_ctx = self.context.setdefault("chat_db_context_data", {})
        add_preference(chat_ctx, "busquedas", pregunta)
        
        respuesta_texto = ""; botones_catalogo = []; fuente_catalogo = "catalogo_qdrant_sin_resultados_v2"

        if resultados_qdrant:
            # Use LLM to summarize results naturally
            try:
                from services.llm_utils import robust_chat
                items_summary = []
                for hit in resultados_qdrant:
                    p = getattr(hit, "payload", {})
                    items_summary.append(f"{p.get('nombre')} (${p.get('precio_str', '?')})")

                context_summary = f"Productos encontrados: {', '.join(items_summary)}. Usuario preguntó: '{pregunta}'."
                prompt_intro = f"Genera una frase corta y amigable presentando estos productos al cliente. {context_summary}"

                intro_text = robust_chat(message=prompt_intro)
                if not intro_text:
                    intro_text = "Encontré estos productos que podrían interesarte:"
            except Exception:
                intro_text = "Encontré estos productos que podrían interesarte:"

            productos_formateados = []
            if self.context.get("channel") != "whatsapp":
                productos_formateados.append("| Producto | Precio | Cantidad |")
                productos_formateados.append("|---|---|---|")

            for idx, hit in enumerate(resultados_qdrant):
                payload = getattr(hit, "payload", {}); item_db_id = payload.get("db_id")
                item_obj = db.session.get(models.CatalogoItem, item_db_id) if item_db_id else None
                nombre = payload.get("nombre", "Producto")
                precio_s, precio_f, moneda = parse_precio_flexible(payload.get("precio_str", ""))
                cantidad = payload.get("cantidad", "")

                if self.context.get("channel") == "whatsapp":
                    linea = f"• *{nombre}*: ${precio_f:,.2f} {moneda or 'ARS'}"
                    if cantidad:
                        linea += f" (Disp: {cantidad})"
                else:
                    linea = f"| {nombre} | ${precio_f:,.2f} {moneda or 'ARS'} | {cantidad} |"

                productos_formateados.append(linea)
                identificador_accion = payload.get("sku") or item_db_id or nombre

                # Check checkout_type logic
                checkout_type = "chatboc"
                external_url = None
                if item_obj:
                    checkout_type = getattr(item_obj, "checkout_type", "chatboc")
                    external_url = getattr(item_obj, "external_url", None)

                if checkout_type in ["mercadolibre", "tiendanube"] and external_url:
                    label_site = "ML" if checkout_type == "mercadolibre" else "Web"
                    botones_catalogo.append({
                        "texto": f"Ver en {label_site}",
                        "url": external_url,
                        "type": "url"
                    })
                else:
                    botones_catalogo.append({"texto": f"Pedir {nombre[:20]}", "action": f"pedir_item_{identificador_accion}"})

            if productos_formateados:
                respuesta_texto = f"{intro_text}\n\n" + "\n".join(productos_formateados)
                respuesta_texto += "\n\nSi quieres alguno, usa los botones o dime (ej: 'quiero 2 [nombre]')."
                fuente_catalogo = "catalogo_qdrant_con_promos_v2"
        
        if not respuesta_texto:
            respuesta_texto = f"No encontré productos para '{pregunta}'. Intenta con otras palabras."
            # No product-specific buttons if nothing found
            botones_catalogo = []

        body = respuesta_texto
        options = []

        # Add buttons from botones_catalogo
        for btn_cat_original in botones_catalogo:
            if btn_cat_original.get("type") == "url":
                options.append(btn_cat_original)
            else:
                action_str = btn_cat_original.get("action", "")
                id_suffix = action_str.replace("pedir_item_", "") if action_str.startswith("pedir_item_") else _normalize_user_input(btn_cat_original.get("texto", "")).replace(" ", "_")

                options.append({
                    "id": f"pedir_item_pyme_{id_suffix}",
                    "texto": btn_cat_original.get("texto", "Pedir producto")[:20]
                })

            if len(options) >= 7 and self.context.get("channel") == 'whatsapp':
                break

        # Add general action buttons
        options.append({"id": "ver_catalogo_pyme_buscar_otra", "texto": "Buscar otra cosa"})

        if tiene_archivo_catalogo(self.pyme_id_actual):
            url_cat = url_descargar_catalogo_pyme(self.pyme_id_actual)
            if self.context.get("channel") == "whatsapp":
                body += f"\n\nTambién puedes descargar nuestro catálogo completo en: {url_cat}"
            else: # For web, add as a URL button
                options.append({
                    "id": "descargar_catalogo_pyme_pdf",
                    "texto": "Descargar Catálogo PDF",
                    "url": url_cat,
                    "type": "url" # For formatter to handle for web
                })

        options.append({"id": "hablar_con_agente_pyme_catalogo", "texto": "Hablar con un agente"})

        interactive_options_count = sum(1 for opt in options if opt.get("type") != "url")
        message_type = 'text'
        if interactive_options_count == 1: message_type = 'interactive_buttons'
        elif 1 < interactive_options_count <= 3: message_type = 'interactive_buttons'
        elif interactive_options_count > 3: message_type = 'interactive_list'

        if interactive_options_count == 0 and any(opt.get("type") == "url" for opt in options):
             message_type = 'text'

        return {
            "message_body": body,
            "options_list": options,
            "message_type": message_type,
            "fuente": fuente_catalogo
        }

class OfertasHandler(BaseHandler):
    def execute(self, action_data):
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "ofertas_sin_pyme_id_v2"}
        promos = promocion_service.get_promociones_for_pyme(self.pyme_id_actual, activas_unicamente=True)

        options = [{"id": "ver_catalogo_pyme_ofertas", "texto": "Ver catálogo"}]
        message_type = 'interactive_buttons' # Only 1 option

        if promos:
            body = "¡Tenemos estas promociones activas!\n" + "\n".join([f"\n**{p.nombre_promocion}**: {p.descripcion_publica}" for p in promos[:3]])
            if len(promos) > 3: body += f"\n... y {len(promos) - 3} más!"
            body += "\n\n¿Te interesa alguna o quieres ver productos?"
            return {
                "message_body": body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "pyme_ofertas_con_promos_v2"
            }

        body_no_ofertas = "No tenemos ofertas especiales ahora, pero explora nuestro catálogo."
        return {
            "message_body": body_no_ofertas,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_ofertas_sin_promos_v2"
        }

class PedidoHandler(BaseActionHandler):
    def execute(self, action_data):
        # This handler will be simplified or removed, as the logic will be
        # handled by the LLM and other more specific action handlers.
        # For now, it returns a simple message.
        return {
            "success": True,
            "message_body": "Estoy procesando tu pedido.",
            "fuente": "pyme_pedido_handler_placeholder"
        }

class FaqHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        if not self.pyme_id_actual:
            return {"message_body": "No puedo buscar en las preguntas frecuentes sin identificar la tienda.", "fuente": "faq_sin_pyme_id_v2"}

        # Asumimos que el owner_user tiene un 'id' y un 'rubro' con 'nombre'
        owner_user_id = self.context.get("user_id")
        rubro_nombre = self.context.get("rubro_nombre", "general")

        if not owner_user_id:
             return {"message_body": "Error interno: no se pudo determinar el propietario para la búsqueda de FAQ.", "fuente": "faq_error_owner_id_v2"}

        # buscar_en_faq_spacy(pregunta, owner_id, rubro, top_n=1, umbral_similitud=0.7)
        resultados_faq = buscar_en_faq_spacy(pregunta, owner_user_id, rubro_nombre, top_n=1, umbral_similitud=0.7)

        if resultados_faq:
            mejor_match = resultados_faq[0]
            respuesta_faq = mejor_match.get("respuesta", "Encontré una respuesta relevante pero no puedo mostrarla ahora.")
            # Podríamos añadir botones si la respuesta_faq tiene acciones asociadas
            options = [{"id": "hablar_con_agente_pyme_faq", "texto": "Hablar con un agente"}]
            return {
                "message_body": respuesta_faq,
                "options_list": options,
                "message_type": "interactive_buttons",
                "fuente": f"pyme_faq_encontrada_v2 (sim: {mejor_match.get('similitud',0):.2f})"
            }
        else:
            options = [
                {"id": "intentar_otra_pregunta_pyme_faq", "texto": "Probar otra pregunta"},
                {"id": "hablar_con_agente_pyme_faq_no_encontrada", "texto": "Hablar con un agente"}
            ]
            return {
                "message_body": "No encontré una respuesta directa a tu pregunta en nuestra base de conocimiento. ¿Quieres intentar otra pregunta o hablar con un agente?",
                "options_list": options,
                "message_type": "interactive_list",
                "fuente": "pyme_faq_no_encontrada_v2"
            }

class HumanHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        # Lógica para transferir a un humano o proveer info de contacto.
        # Por ahora, un placeholder.
        self._actualizar_estado(PymeConversationState.IDLE) # Resetear estado
        nombre_pyme = self.context.get("nombre_pyme", "la empresa")
        # TODO: Intentar obtener datos de contacto reales de la Pyme (owner_user)
        # pyme_user_obj = db.session.get(User, self.pyme_id_actual) if self.pyme_id_actual else None
        # telefono_pyme = getattr(pyme_user_obj, "telefono_contacto", "nuestro teléfono principal")
        # email_pyme = getattr(pyme_user_obj, "email_contacto", "nuestro email de soporte")

        body = f"Entendido. Para hablar con un representante de {nombre_pyme}, por favor contáctanos directamente."
        # Idealmente, aquí se crearía un ticket o se notificaría a alguien.
        # Por ahora, solo damos un mensaje.
        # Crear ticket si servicio_tickets está disponible
        ticket_creado_id = None
        if self.pyme_id_actual and self.cliente_id_actual:
            try:
                asunto = f"Solicitud de contacto desde chat: {pregunta[:50]}"
                descripcion = f"El cliente {self.cliente_id_actual} solicitó hablar con un agente. Última pregunta: '{pregunta}'."
                historial_chat_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in self.context.get("mensajes_previos", [])[-5:]])
                descripcion += f"\n\nÚltimos mensajes:\n{historial_chat_str}"

                ticket_creado_id = servicio_tickets.crear_ticket(
                    pyme_id=self.pyme_id_actual,
                    cliente_id=self.cliente_id_actual, # Puede ser None si es anónimo
                    asunto=asunto,
                    descripcion=descripcion,
                    fuente_ticket="CHATBOT_PYME",
                    # estado_ticket="ABIERTO", # El servicio lo maneja
                    # prioridad="MEDIA" # El servicio lo maneja
                )
                if ticket_creado_id:
                    body = f"He generado el ticket #{ticket_creado_id} para que un agente se ponga en contacto contigo. ¿Hay algo más en lo que pueda ayudarte mientras tanto?"
                    self.pyme_ctx["ultimo_ticket_creado"] = ticket_creado_id
                    self._guardar_contexto_pyme()
            except Exception as e:
                logger.error(f"Error creando ticket en HumanHandler: {e}")
                body += "\n(Hubo un problema al intentar generar un ticket automático)."


        options = [{"id": "ver_catalogo_pyme_post_human", "texto": "Ver catálogo"}]
        return {
            "message_body": body,
            "options_list": options,
            "message_type": "interactive_buttons", # O 'text' si no hay opciones
            "fuente": "pyme_human_handler_placeholder_v2",
            "ticket_id": ticket_creado_id
        }

class UnclearHandler(BaseHandler): # Aunque no está en handler_map, es bueno tenerlo
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        self.pyme_ctx["reintentos_ambigua"] = self.pyme_ctx.get("reintentos_ambigua", 0) + 1
        self._guardar_contexto_pyme()

        if self.pyme_ctx["reintentos_ambigua"] > 2:
            self.pyme_ctx["reintentos_ambigua"] = 0 # Reset
            self._guardar_contexto_pyme()
            return HumanHandler(self.context).handle("El usuario está teniendo dificultades para que lo entienda.")

        sugerencias = sugerencias_por_rubro(self.context.get("rubro_nombre"))
        msg = "No estoy seguro de cómo ayudarte con eso."
        if sugerencias:
            msg += "\nPuedes intentar preguntarme sobre:\n- " + "\n- ".join(sugerencias[:3])
        msg += "\n\nO puedes reformular tu pregunta."

        options = [{"id": "hablar_con_agente_pyme_unclear", "texto": "Hablar con un agente"}]
        if tiene_archivo_catalogo(self.pyme_id_actual):
             options.insert(0, {"id": "ver_catalogo_pyme_unclear", "texto": "Ver Catálogo"})


        return {
            "message_body": msg,
            "options_list": options,
            "message_type": "interactive_list" if len(options)>1 else "interactive_buttons",
            "fuente": "pyme_unclear_handler_v2"
        }

class TicketStatusHandler(BaseActionHandler):
    def execute(self, action_data):
        # This handler will be simplified or removed, as the logic will be
        # handled by the LLM and other more specific action handlers.
        # For now, it returns a simple message.
        return {
            "success": True,
            "message_body": "Estoy consultando el estado de tu ticket.",
            "fuente": "pyme_ticket_status_handler_placeholder"
        }

class FinalizarPedidoHandler(BaseHandler):
    def execute(self, action_data):
        if not self.pyme_id_actual:
            return {"message_to_user": "No puedo identificar la tienda para finalizar el pedido.", "fuente": "finalizar_pedido_sin_pyme_id"}

        cart_summary = cart_service.get_cart_summary(self.pyme_carts_data, self.pyme_id_actual, self.cliente_id_actual)

        if not cart_summary or not cart_summary.get("items_detalle"):
            return {"message_to_user": "Tu carrito está vacío. Agrega productos antes de finalizar el pedido.", "fuente": "finalizar_pedido_carrito_vacio"}

        detalles_pedido = json.dumps(cart_summary.get("items_detalle"))
        monto_total_pedido = cart_summary.get("total_final_con_descuento")

        # Get client data from context
        nombre_cliente = self.pyme_ctx.get("nombre_cliente")
        email_cliente = self.pyme_ctx.get("email_cliente")
        telefono_cliente = self.pyme_ctx.get("telefono_cliente")
        direccion_cliente = self.pyme_ctx.get("direccion_cliente")
        latitud_cliente = self.pyme_ctx.get("latitud_cliente")
        longitud_cliente = self.pyme_ctx.get("longitud_cliente")

        # Create the order
        nuevo_pedido = PymePedido(
            pyme_id=self.pyme_id_actual,
            asunto=f"Pedido de {nombre_cliente or 'cliente'}",
            detalles=detalles_pedido,
            monto_total=monto_total_pedido,
            nombre_cliente=nombre_cliente,
            email_cliente=email_cliente,
            telefono_cliente=telefono_cliente,
            direccion=direccion_cliente,
            latitud=latitud_cliente,
            longitud=longitud_cliente,
            user_id=self.cliente_id_actual
        )

        try:
            db.session.add(nuevo_pedido)
            db.session.commit()
            try:
                market_order = servicio_pedidos.sync_market_order_from_pyme(
                    nuevo_pedido,
                    channel=self.context.get("channel"),
                )
            except Exception as e:
                db.session.rollback()
                logger.error(
                    "Error creando MarketOrder para pedido %s: %s",
                    nuevo_pedido.nro_pedido,
                    e,
                    exc_info=True,
                )

            # Clear the cart
            cart_service.clear_pyme_cart(self.pyme_carts_data, self.pyme_id_actual)
            self._guardar_contexto_pyme()

            # TODO: Send email notification to logistics

            return {
                "success": True,
                "message_body": f"¡Gracias por tu compra! Tu pedido #{nuevo_pedido.nro_pedido} ha sido creado con éxito. Te mantendremos informado sobre el estado.",
                "fuente": "pyme_pedido_finalizado_exitosamente",
                "data": {"pedido_id": nuevo_pedido.id, "nro_pedido": nuevo_pedido.nro_pedido}
            }
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error al crear pedido final desde carrito para pyme {self.pyme_id_actual}: {e}", exc_info=True)
            return {
                "success": False,
                "message_body": "Hubo un problema al procesar tu pedido. Por favor, intenta de nuevo o contacta a un agente.",
                "fuente": "pyme_pedido_finalizado_error"
            }

from services.google_search import google_search

class FallbackHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        logger.warning(f"[PYME_FALLBACK_HANDLER] Pregunta no manejada: '{pregunta}', Intención: {self.context.get('intencion')}, Estado: {self.pyme_ctx.get('estado_conversacion')}")

        search_results = google_search(pregunta)

        if not search_results:
            return UnclearHandler(self.context).execute({"pregunta": pregunta})

        search_items = []
        for result in search_results[:3]:
            search_items.append(f"- [{result.get('title')}]({result.get('link')})\n{result.get('snippet')}")

        return {
            "message_body": "No estoy seguro de cómo ayudarte con eso, pero encontré esto en la web:\n\n" + "\n\n".join(search_items),
            "options_list": [],
            "message_type": "text",
            "fuente": "pyme_fallback_google_search"
        }


class ToolHandlerPyme(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        match_webinfo = re.search(r"informaci[oó]n web de\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", pregunta, re.IGNORECASE)
        if match_webinfo:
            dominio = match_webinfo.group(1)
            info = obtener_info_web(dominio)
            if info: return {"message_body": f"Información de {dominio}:\n{info}", "fuente": "pyme_tool_webinfo_v2"}
            else: return {"message_body": f"No pude obtener información web para {dominio}.", "fuente": "pyme_tool_webinfo_no_data_v2"}
        return None

class AnalizarImagenHandler(BaseHandler):
    def execute(self, action_data):
        if self.context.get("intencion") != "analizar_imagen":
            return None

        from services.google_vision_service import GoogleVisionService
        import requests

        memoria = self.context[CONTEXTO_PYME]
        foto_url = memoria.get("foto_url")

        if not foto_url:
            return {"respuesta": "No se encontró una imagen para analizar."}

        try:
            response = requests.get(foto_url)
            response.raise_for_status()
            image_content = response.content

            vision_service = GoogleVisionService()
            analysis_result = vision_service.analyze_image(image_content)

            memoria["analisis_imagen"] = analysis_result
            memoria["estado_conversacion"] = PymeConversationState.ESPERANDO_PRODUCTO.name

            return {
                "respuesta": f"He analizado la imagen y detecté lo siguiente: {', '.join(analysis_result['labels'])}. Para continuar, ¿qué producto te gustaría pedir?",
            }
        except Exception as e:
            logger.error(f"Error al analizar la imagen: {e}")
            return {"respuesta": "Hubo un error al analizar la imagen. Por favor, intentá de nuevo."}

class SolicitarUbicacionHandler(BaseHandler):
    def execute(self, action_data):
        if self.context.get("intencion") != "solicitar_ubicacion":
            return None

        return {
            "respuesta": "Para poder ayudarte mejor, necesito tu ubicación. ¿Podrías compartirla?",
            "botones": [
                {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                {"texto": "No, gracias", "action": "cancelar"},
            ],
        }

def coleccion_catalogo_para_rubro(rubro_nombre: str) -> str:
    return CATALOGO_PYME

def get_or_create_user_by_phone(phone_number: str, owner_user: models.User) -> Optional[models.User]:
    """
    Busca un usuario por su número de teléfono. Si no existe, crea uno nuevo
    asociado al `owner_user` (la pyme o municipio).
    Maneja condiciones de carrera durante la creación.
    """
    if not phone_number or not owner_user:
        return None

    # Intentar encontrar el usuario existente por teléfono
    user = models.User.query.filter_by(telefono=phone_number, empresa_id=owner_user.id).first()
    if user:
        return user

    # Si no existe, intentar crear uno nuevo
    logger.info(f"No se encontró un usuario para el teléfono '{phone_number}'. Creando uno nuevo.")

    nuevo_usuario = models.User(
        telefono=phone_number,
        email=f"{phone_number}@whatsapp.chatboc.com", # Email de marcador de posición
        rubro_id=owner_user.rubro_id,
        empresa_id=owner_user.id,
        rol='usuario',
        tipo_chat=owner_user.tipo_chat,
        plan='gratis',
        acepto_terminos=True, # Asumimos aceptación para que el sistema funcione
        fecha_aceptacion_terminos=datetime.utcnow()
    )
    nuevo_usuario.name = "Vecino/a"
    nuevo_usuario.set_password(str(uuid.uuid4())) # Contraseña aleatoria y segura

    try:
        db.session.add(nuevo_usuario)
        db.session.commit()
        logger.info(f"Nuevo usuario de WhatsApp creado con ID {nuevo_usuario.id} para el teléfono '{phone_number}'")
        return nuevo_usuario
    except IntegrityError:
        db.session.rollback()
        logger.warning(f"Race condition detectada para el teléfono '{phone_number}'. Re-intentando la búsqueda.")
        return models.User.query.filter_by(telefono=phone_number, empresa_id=owner_user.id).first()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error inesperado al crear el usuario de WhatsApp para el teléfono '{phone_number}': {e}", exc_info=True)
        return None

def get_or_create_pyme_user_by_token(token: str) -> Optional[models.User]:
    """
    Busca un usuario PYME por su token. Si no existe, crea uno nuevo
    con valores predeterminados y un rubro genérico.
    """
    if not token:
        return None

    # Intentar encontrar el usuario existente
    user = models.User.query.filter_by(token=token).first()
    if user:
        return user

    # Si no existe, crear uno nuevo
    logger.info(f"No se encontró un usuario para el token '{token[:10]}...'. Creando uno nuevo.")

    # Asegurarse de que el rubro "General" exista
    rubro_general = models.Rubro.query.filter(func.lower(models.Rubro.nombre) == "general").first()
    if not rubro_general:
        logger.info("No se encontró el rubro 'General', creándolo...")
        rubro_general = models.Rubro(nombre="General", clave="general", es_publico=False)
        db.session.add(rubro_general)
        db.session.commit()
        logger.info(f"Rubro 'General' creado con ID: {rubro_general.id}")

    # Crear el nuevo usuario (Pyme)
    nuevo_pyme_user = models.User(
        token=token,
        email=f"pyme_{token[:8]}@chatboc.com", # Email de marcador de posición
        nombre_empresa=f"Empresa {token[:8]}",
        rubro_id=rubro_general.id,
        rol='admin', # El dueño de la Pyme es admin de su propia entidad
        tipo_chat='pyme',
        plan='gratis', # O el plan por defecto que corresponda
        acepto_terminos=True, # Asumimos aceptación para que el sistema funcione
        fecha_aceptacion_terminos=datetime.utcnow()
    )
    nuevo_pyme_user.name = f"Empresa {token[:8]}"
    nuevo_pyme_user.set_password(str(uuid.uuid4())) # Contraseña aleatoria y segura

    try:
        db.session.add(nuevo_pyme_user)
        db.session.commit()
        logger.info(f"Nuevo usuario Pyme creado con ID {nuevo_pyme_user.id} para el token '{token[:10]}...'")
        return nuevo_pyme_user
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error al crear el usuario Pyme para el token '{token[:10]}...': {e}", exc_info=True)
        return None

from services.llm_orchestrator import llamar_llm_con_fallback
from .chat_orchestrator import ChatOrchestrator # Importar el nuevo Orchestrator

def responder_pyme(pregunta_original, owner_user, rubro_obj, viewer_user=None, chat_db_context=None, anon_id=None, channel: str = "web", **kwargs):
    request_id = str(uuid.uuid4())
    logger_actual = current_app.logger if current_app else logger

    demo_metadata = kwargs.pop("demo_metadata", None)

    if not owner_user:
        logger.error("[responder_pyme] Critical error: owner_user is None. Cannot proceed.")
        return {
            "message_body": "Error de configuración: No se pudo identificar la empresa. Por favor, contacte al administrador.",
            "options_list": [],
            "message_type": "text",
            "fuente": "error_no_owner_user"
        }

    chat_db_context = ensure_session_context(chat_db_context)

    # --- 1. Procesamiento de Entrada y Carga de Contexto (simplificado) ---
    received_payload = {}
    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    if kwargs: received_payload.update(kwargs)

    if "mensajes_previos_llm_formato" not in chat_db_context.context_data:
        old_hist = chat_db_context.context_data.pop("mensajes_previos_gemini_formato", [])
        chat_db_context.context_data["mensajes_previos_llm_formato"] = old_hist

    pyme_ctx_actual = chat_db_context.context_data.setdefault(CONTEXTO_PYME, {})
    session_state = PymeSessionState(pyme_ctx_actual, getattr(owner_user, "id", None))
    pyme_ctx_actual["request_id"] = request_id

    helper_context_for_success: dict[str, Any] = {}

    def _finalize_early_response(flow_result: PymeFlowResult, *, intent: Optional[str] = None) -> dict:
        session_state.save()
        pyme_ctx_actual[session_state.STORAGE_KEY] = session_state.raw
        contexto_serializado = serializar_enum(pyme_ctx_actual)
        chat_db_context.context_data[CONTEXTO_PYME] = contexto_serializado
        if chat_db_context:
            flag_modified(chat_db_context, "context_data")

        if pyme_ctx_actual.get("saludo_audio_pendiente") and flow_result.message_body:
            flow_result.message_body = (
                f"Hola, soy el asistente de {nombre_pyme_display}. {flow_result.message_body}"
            )
            pyme_ctx_actual["saludo_audio_pendiente"] = False

        final_payload = {
            "success": True,
            "message_body": flow_result.message_body,
            "options_list": flow_result.options_list or [],
            "message_type": flow_result.message_type or "text",
            "fuente": flow_result.source,
            "contexto_actualizado": {CONTEXTO_PYME: contexto_serializado},
        }
        if flow_result.data:
            final_payload["data"] = flow_result.data
        if flow_result.audio_url:
            final_payload["audio_url"] = flow_result.audio_url
        if flow_result.audio_text:
            final_payload["audio_text"] = flow_result.audio_text
        if flow_result.delayed_payload:
            final_payload["delayed_payload"] = flow_result.delayed_payload
            final_payload["delay_seconds"] = flow_result.delay_seconds or 20

        logger_actual.info(
            "[PYME_FLOW] early_response",
            extra={
                "intent": intent or session_state.last_intent,
                "request_id": request_id,
                "source": flow_result.source,
            },
        )

        if flow_result.source == "pyme_pedido_registrado":
            snapshot = {
                "success": True,
                "fuente": flow_result.source,
                "message_body": flow_result.message_body,
                "options_list": flow_result.options_list or [],
                "message_type": flow_result.message_type or "text",
                "data": flow_result.data or {},
            }
            if flow_result.audio_url:
                snapshot["audio_url"] = flow_result.audio_url
            if flow_result.audio_text:
                snapshot["audio_text"] = flow_result.audio_text
            if flow_result.delayed_payload:
                snapshot["delayed_payload"] = flow_result.delayed_payload
                snapshot["delay_seconds"] = flow_result.delay_seconds or 20

            effective_context = helper_context_for_success or {
                CONTEXTO_PYME: pyme_ctx_actual,
                "channel": channel,
                "viewer_user_obj": viewer_user,
                "user_id": getattr(owner_user, "id", None),
                "cliente_id": getattr(viewer_user, "id", None),
            }

            enriched_payload = _build_pyme_order_success_payload(effective_context, snapshot)
            enriched_payload["contexto_actualizado"] = final_payload["contexto_actualizado"]
            final_payload = enriched_payload

        if anon_id and not viewer_user:
            try:
                rubro_nombre_log = (
                    getattr(getattr(owner_user, "rubro", None), "nombre", None)
                    or getattr(rubro_obj, "nombre", None)
                    or "general"
                )
                db.session.add(
                    Conversacion(
                        session_id=kwargs.get("chat_session_uuid") or anon_id,
                        pregunta=pregunta_str,
                        respuesta=flow_result.message_body,
                        fuente=flow_result.source,
                        rubro=rubro_nombre_log,
                        user_id=None,
                        pyme_id=getattr(owner_user, "id", None),
                    )
                )
                db.session.commit()
            except Exception as e_conv_pyme_final:
                logger_actual.error(
                    f"Error guardando Conversacion temprana (PYME): {e_conv_pyme_final}",
                    exc_info=True,
                )
                db.session.rollback()

        return final_payload

    historial_chat_llm = chat_db_context.context_data.get("mensajes_previos_llm_formato", [])

    # --- 2. Construir Información de Usuario para el LLM ---
    rubro_nombre_contexto = None
    if owner_user and hasattr(owner_user, "rubro") and owner_user.rubro:
        rubro_nombre_contexto = getattr(owner_user.rubro, "nombre", None)

    rubro_slug = _slugify_rubro(
        pyme_ctx_actual.get("rubro_slug")
        or getattr(getattr(owner_user, "rubro", None), "slug", None)
        or rubro_nombre_contexto
    )
    pyme_ctx_actual["rubro_slug"] = rubro_slug

    # Resolve tenant slug for config loading
    tenant_profile = getattr(owner_user, "tenant_profile_pyme", None)
    tenant_slug = tenant_profile.slug if tenant_profile else None

    nombre_pyme_display = (
        getattr(owner_user, "nombre_empresa", None)
        or pyme_ctx_actual.get("nombre_pyme_cache")
        or "la tienda"
    )

    static_bundle = pyme_ctx_actual.get("static_data_cache")
    # Check if we need to reload based on slug or tenant change
    current_cache_key = f"{rubro_slug}_{tenant_slug or ''}"
    if not static_bundle or static_bundle.get("_cache_key") != current_cache_key:
        data_files = {
            "config": "config.json",
            "catalogo_destacado": "catalogo_destacado.json",
            "promociones": "promociones.json",
            "actividades": "actividades.json",
            "faq": "faq.json",
        }
        loaded_bundle = {}
        for key, filename in data_files.items():
            data = cargar_configuracion_pyme(rubro_slug, filename, tenant_slug=tenant_slug)
            if data:
                loaded_bundle[key] = data
        if loaded_bundle:
            loaded_bundle["_slug"] = rubro_slug
            loaded_bundle["_cache_key"] = current_cache_key
            static_bundle = loaded_bundle
        else:
            static_bundle = {"_slug": rubro_slug, "_cache_key": current_cache_key}
        pyme_ctx_actual["static_data_cache"] = static_bundle

    config_data = static_bundle.get("config", {}) if static_bundle else {}
    if config_data.get("nombre_pyme"):
        nombre_pyme_display = config_data["nombre_pyme"]
    pyme_ctx_actual["nombre_pyme_cache"] = nombre_pyme_display

    usuario_nombre = (
        getattr(viewer_user, "name", None)
        or getattr(viewer_user, "nombre", None)
        or pyme_ctx_actual.get("nombre_cliente")
        or "Cliente"
    )

    profile_name = kwargs.get("profile_name")
    if profile_name and not getattr(viewer_user, "name", None):
        usuario_nombre = profile_name
        pyme_ctx_actual.setdefault("nombre_cliente", profile_name)

    rubro_info_value = (
        rubro_nombre_contexto.lower()
        if isinstance(rubro_nombre_contexto, str)
        else rubro_nombre_contexto
        or "general"
    )

    helper_context_for_success.update(
        {
            CONTEXTO_PYME: pyme_ctx_actual,
            "user_id": getattr(owner_user, "id", None),
            "cliente_id": getattr(viewer_user, "id", None),
            "viewer_user_obj": viewer_user,
            "nombre_pyme": nombre_pyme_display,
            "rubro_nombre": rubro_nombre_contexto or rubro_info_value or "general",
            "rubro_slug": rubro_slug,
            "channel": channel,
            "chat_session_uuid": kwargs.get("chat_session_uuid"),
        }
    )

    usuario_info_for_llm = {
        "nombre": usuario_nombre,
        "tipo_entidad": "pyme",
        "pyme_info": {
            "nombre_pyme": nombre_pyme_display,
            "rubro": rubro_info_value,
        },
    }

    pyme_data_for_llm = {k: v for k, v in (static_bundle or {}).items() if k != "_slug"}
    if pyme_data_for_llm:
        usuario_info_for_llm["pyme_data"] = pyme_data_for_llm
        if config_data.get("ubicaciones"):
            usuario_info_for_llm["pyme_info"]["ubicaciones"] = config_data["ubicaciones"]
        if config_data.get("contacto"):
            usuario_info_for_llm["pyme_info"]["contacto"] = config_data["contacto"]
        if config_data.get("horarios"):
            usuario_info_for_llm["pyme_info"]["horarios"] = config_data["horarios"]

    if demo_metadata:
        prompt_context = demo_metadata.get("prompt_context")
        if prompt_context:
            usuario_info_for_llm["demo_contexto"] = prompt_context
        if demo_metadata.get("key"):
            usuario_info_for_llm["demo_key"] = demo_metadata.get("key")
        if demo_metadata.get("display_name"):
            usuario_info_for_llm["demo_display_name"] = demo_metadata.get("display_name")
        if demo_metadata.get("description"):
            usuario_info_for_llm["demo_description"] = demo_metadata.get("description")
        if demo_metadata.get("faq_preview"):
            usuario_info_for_llm["demo_faq_preview"] = demo_metadata.get("faq_preview")

    loc_usuario_texto = getattr(viewer_user, "direccion", None) or pyme_ctx_actual.get("direccion_cliente")
    if loc_usuario_texto:
        usuario_info_for_llm["ubicacion_conocida"] = loc_usuario_texto

    ubicacion_payload = (
        received_payload.get("ubicacion_usuario")
        or kwargs.get("ubicacion_usuario")
        or kwargs.get("location")
    )
    contextual_notes: list[str] = []
    if isinstance(ubicacion_payload, dict) and ubicacion_payload:
        pyme_ctx_actual["ultima_ubicacion_usuario"] = ubicacion_payload
        usuario_info_for_llm["ubicacion_compartida"] = ubicacion_payload
        lat = ubicacion_payload.get("lat") or ubicacion_payload.get("latitude")
        lon = ubicacion_payload.get("lon") or ubicacion_payload.get("longitude")
        address = ubicacion_payload.get("address") or ubicacion_payload.get("descripcion")
        location_note = "El usuario compartió su ubicación para coordinar envíos o retiros."
        if address:
            location_note += f" Dirección: {address}."
        elif lat is not None and lon is not None:
            location_note += f" Coordenadas: {lat}, {lon}."
        contextual_notes.append(location_note)

        logger_actual.info(
            "[PYME_FLOW] intent_detected",
            extra={"intent": "delivery_location", "request_id": request_id},
        )
        location_result = handle_location_payload(session_state, config_data, ubicacion_payload)
        return _finalize_early_response(location_result, intent="delivery_location")

    uploaded_info = (
        received_payload.get("uploaded_file_info")
        or received_payload.get("uploaded_file_info_whatsapp")
        or kwargs.get("uploaded_file_info")
    )
    if isinstance(uploaded_info, dict) and uploaded_info.get("url"):
        ultimo_adjunto = {
            "url": uploaded_info.get("url"),
            "mime_type": uploaded_info.get("mime_type"),
            "id": uploaded_info.get("id"),
            "name": uploaded_info.get("name"),
            "thumbnail": uploaded_info.get("thumbnail_url") or uploaded_info.get("thumbnail"),
        }
        pyme_ctx_actual["ultimo_adjunto"] = ultimo_adjunto
        usuario_info_for_llm["ultimo_adjunto"] = ultimo_adjunto
        mime_type = (uploaded_info.get("mime_type") or "").lower()
        if mime_type.startswith("image/"):
            pyme_ctx_actual["foto_url"] = uploaded_info.get("url")
            usuario_info_for_llm["imagen_url"] = uploaded_info.get("url")
            contextual_notes.append("El usuario envió una imagen con detalles para su pedido o consulta.")
            logger_actual.info(
                "[PYME_FLOW] media_saved",
                extra={"media_type": "image", "request_id": request_id},
            )
            catalog_items = load_catalog(getattr(owner_user, "id", None), rubro_slug)
            image_result = handle_image_payload(
                session_state,
                uploaded_info,
                catalog_items,
                request_id=request_id,
            )
            return _finalize_early_response(image_result, intent="image_catalog_match")
        elif mime_type == "application/pdf" or mime_type.endswith("+pdf"):
            contextual_notes.append("El usuario envió un catálogo o lista de precios en PDF.")
            logger_actual.info(
                "[PYME_FLOW] media_saved",
                extra={"media_type": "pdf", "request_id": request_id},
            )
            catalog_items = load_catalog(getattr(owner_user, "id", None), rubro_slug)
            pdf_result = handle_pdf_payload(
                session_state,
                uploaded_info,
                catalog_items,
                request_id=request_id,
            )
            return _finalize_early_response(pdf_result, intent="pdf_catalog_match")
        elif mime_type.startswith("audio/"):
            contextual_notes.append("El usuario envió una nota de voz.")
            transcripcion = uploaded_info.get("transcribed_text")
            if transcripcion:
                if not pyme_ctx_actual.get("saludo_audio_enviado"):
                    pyme_ctx_actual["saludo_audio_pendiente"] = True
                    pyme_ctx_actual["saludo_audio_enviado"] = True

                normalized_audio = _normalize_user_input(transcripcion)
                agent_keywords = RAW_PYME_MENU_KEYWORDS.get("pyme_hablar_agente", set())
                if normalized_audio and any(
                    _normalize_user_input(keyword) in normalized_audio for keyword in agent_keywords | {"llamar", "llamada"}
                ):
                    from services.actions.pyme_actions import DerivarHumanoActionHandlerPyme

                    handler_context = {
                        CONTEXTO_PYME: pyme_ctx_actual,
                        "user_obj": owner_user,
                        "viewer_user_obj": viewer_user,
                        "cliente_id": getattr(viewer_user, "id", None),
                        "anon_id": anon_id,
                        "user_id": getattr(owner_user, "id", None),
                        "chat_db_context_data": chat_db_context.context_data,
                        "channel": channel,
                        "target_entity_type": "pyme",
                        "pregunta_actual_usuario": transcripcion,
                    }
                    handler = DerivarHumanoActionHandlerPyme(handler_context)
                    handler_result = handler.execute({"motivo_derivacion": "Solicitud de contacto por nota de voz"})
                    flow_result = PymeFlowResult(
                        message_body=handler_result.get("message_to_user") or handler_result.get("message_body", ""),
                        source="pyme_handoff_audio",
                        data=handler_result.get("data", {}),
                    )
                    return _finalize_early_response(flow_result, intent="pyme_hablar_agente")

                intent_from_audio = detect_intent_from_text(transcripcion)
                if intent_from_audio:
                    logger_actual.info(
                        "[PYME_FLOW] audio_detected",
                        extra={"intent": intent_from_audio, "request_id": request_id},
                    )
                    audio_flow = handle_keyword_intent(
                        intent=intent_from_audio,
                        text=transcripcion,
                        state=session_state,
                        owner_user_id=getattr(owner_user, "id", None),
                        rubro_slug=rubro_slug,
                        context={
                            "nombre_pyme": nombre_pyme_display,
                            "rubro_slug": rubro_slug,
                            "rubro_nombre": rubro_nombre_contexto,
                            "config": config_data,
                            "viewer_user_id": getattr(viewer_user, "id", None),
                            "request_id": request_id,
                        },
                        channel=channel,
                        parsed_items=extraer_productos_pedido(transcripcion)
                        if intent_from_audio == "pedido"
                        else None,
                        request_id=request_id,
                    )
                    if audio_flow:
                        logger_actual.info(
                            "[PYME_FLOW] audio_intent",
                            extra={"intent": intent_from_audio, "request_id": request_id},
                        )
                        return _finalize_early_response(audio_flow, intent=intent_from_audio)
        elif mime_type:
            contextual_notes.append(f"El usuario adjuntó un archivo del tipo {mime_type}.")
        else:
            contextual_notes.append("El usuario adjuntó un archivo.")

        if uploaded_info.get("caption") and not pregunta_str.strip():
            pregunta_str = uploaded_info["caption"]
        if uploaded_info.get("transcribed_text"):
            transcripcion = uploaded_info["transcribed_text"].strip()
            if transcripcion:
                contextual_notes.append(f"Transcripción de audio: {transcripcion}")
                pregunta_str = transcripcion

    datos_interpretados_archivo = kwargs.get("datos_interpretados_archivo")
    if isinstance(datos_interpretados_archivo, dict) and datos_interpretados_archivo:
        pyme_ctx_actual["datos_interpretados_archivo"] = datos_interpretados_archivo
        usuario_info_for_llm["datos_interpretados_archivo"] = datos_interpretados_archivo
        descripcion_sugerida = datos_interpretados_archivo.get("descripcion_sugerida")
        if descripcion_sugerida:
            contextual_notes.append(f"Descripción interpretada del adjunto: {descripcion_sugerida}")
            if not pregunta_str.strip():
                pregunta_str = descripcion_sugerida
        categoria_sugerida = datos_interpretados_archivo.get("categoria_sugerida")
        if categoria_sugerida:
            contextual_notes.append(f"Categoría sugerida del adjunto: {categoria_sugerida}")
        texto_extraido = datos_interpretados_archivo.get("texto_extraido")
        if texto_extraido and texto_extraido.strip():
            contextual_notes.append(f"Texto extraído del archivo: {texto_extraido.strip()}")

    if not pregunta_str.strip():
        transcripcion_note = next(
            (nota.split(":", 1)[1].strip() for nota in contextual_notes if nota and nota.lower().startswith("transcripción de audio")),
            None,
        )
        if transcripcion_note:
            pregunta_str = transcripcion_note
        elif contextual_notes:
            pregunta_str = contextual_notes[0]
        else:
            pregunta_str = "El usuario compartió información sin texto adicional."

    mensaje_para_llm = pregunta_str.strip()
    if contextual_notes:
        notas_texto = "\n".join(f"- {nota}" for nota in contextual_notes if nota)
        if mensaje_para_llm:
            mensaje_para_llm = f"{mensaje_para_llm}\n\nContexto adicional proporcionado por el usuario:\n{notas_texto}"
        else:
            mensaje_para_llm = f"Contexto adicional proporcionado por el usuario:\n{notas_texto}"

    intent_detected = detect_intent_from_text(pregunta_str or "") if pregunta_str else None
    if not intent_detected and received_payload.get("action"):
        intent_detected = detect_intent_from_text(received_payload.get("action", ""))

    if intent_detected:
        parsed_items = extraer_productos_pedido(pregunta_str or "") if intent_detected == "pedido" else None
        logger_actual.info(
            "[PYME_FLOW] intent_detected",
            extra={"intent": intent_detected, "request_id": request_id},
        )
        flow_result = handle_keyword_intent(
            intent=intent_detected,
            text=pregunta_str or "",
            state=session_state,
            owner_user_id=getattr(owner_user, "id", None),
            rubro_slug=rubro_slug,
            context={
                "nombre_pyme": nombre_pyme_display,
                "rubro_slug": rubro_slug,
                "rubro_nombre": rubro_nombre_contexto,
                "config": config_data,
                "viewer_user_id": getattr(viewer_user, "id", None),
                "request_id": request_id,
            },
            channel=channel,
            parsed_items=parsed_items,
            request_id=request_id,
        )
        if flow_result:
            return _finalize_early_response(flow_result, intent=intent_detected)

    if intent_detected:
        logger_actual.info(
            "[PYME_FLOW] intent_fallback",
            extra={"intent": intent_detected, "request_id": request_id},
        )

    if not intent_detected:
        for nota in contextual_notes:
            if nota and "transcripción de audio" in nota.lower():
                texto_audio = nota.split(":", 1)[1].strip() if ":" in nota else nota
                reintento_intent = detect_intent_from_text(texto_audio)
                if reintento_intent:
                    flow_result = handle_keyword_intent(
                        intent=reintento_intent,
                        text=texto_audio,
                        state=session_state,
                        owner_user_id=getattr(owner_user, "id", None),
                        rubro_slug=rubro_slug,
                        context={
                            "nombre_pyme": nombre_pyme_display,
                            "rubro_slug": rubro_slug,
                            "rubro_nombre": rubro_nombre_contexto,
                            "config": config_data,
                            "viewer_user_id": getattr(viewer_user, "id", None),
                            "request_id": request_id,
                        },
                        channel=channel,
                        parsed_items=extraer_productos_pedido(texto_audio)
                        if reintento_intent == "pedido"
                        else None,
                        request_id=request_id,
                    )
                    if flow_result:
                        logger_actual.info(
                            "[PYME_FLOW] audio_retry",
                            extra={"intent": reintento_intent, "request_id": request_id},
                        )
                        return _finalize_early_response(flow_result, intent=reintento_intent)
                break

    llm_response_structured, _ = llamar_llm_con_fallback(
        app=current_app,
        mensaje_usuario=mensaje_para_llm,
        usuario=usuario_info_for_llm,
        historial=historial_chat_llm,
        chat_session_id=kwargs.get("chat_session_uuid")
    )
    contextual_notes: list[str] = []
    if isinstance(ubicacion_payload, dict) and ubicacion_payload:
        pyme_ctx_actual["ultima_ubicacion_usuario"] = ubicacion_payload
        usuario_info_for_llm["ubicacion_compartida"] = ubicacion_payload
        lat = ubicacion_payload.get("lat") or ubicacion_payload.get("latitude")
        lon = ubicacion_payload.get("lon") or ubicacion_payload.get("longitude")
        address = ubicacion_payload.get("address") or ubicacion_payload.get("descripcion")
        location_note = "El usuario compartió su ubicación para coordinar envíos o retiros."
        if address:
            location_note += f" Dirección: {address}."
        elif lat is not None and lon is not None:
            location_note += f" Coordenadas: {lat}, {lon}."
        contextual_notes.append(location_note)

    uploaded_info = (
        received_payload.get("uploaded_file_info")
        or received_payload.get("uploaded_file_info_whatsapp")
        or kwargs.get("uploaded_file_info")
    )
    if isinstance(uploaded_info, dict) and uploaded_info.get("url"):
        ultimo_adjunto = {
            "url": uploaded_info.get("url"),
            "mime_type": uploaded_info.get("mime_type"),
            "id": uploaded_info.get("id"),
            "name": uploaded_info.get("name"),
            "thumbnail": uploaded_info.get("thumbnail_url") or uploaded_info.get("thumbnail"),
        }
        pyme_ctx_actual["ultimo_adjunto"] = ultimo_adjunto
        usuario_info_for_llm["ultimo_adjunto"] = ultimo_adjunto
        mime_type = (uploaded_info.get("mime_type") or "").lower()
        if mime_type.startswith("image/"):
            pyme_ctx_actual["foto_url"] = uploaded_info.get("url")
            usuario_info_for_llm["imagen_url"] = uploaded_info.get("url")
            contextual_notes.append("El usuario envió una imagen con detalles para su pedido o consulta.")
        elif mime_type.startswith("audio/"):
            contextual_notes.append("El usuario envió una nota de voz.")
        elif mime_type:
            contextual_notes.append(f"El usuario adjuntó un archivo del tipo {mime_type}.")

        if uploaded_info.get("caption") and not pregunta_str.strip():
            pregunta_str = uploaded_info["caption"]
        if uploaded_info.get("transcribed_text"):
            transcripcion = uploaded_info["transcribed_text"].strip()
            if transcripcion:
                contextual_notes.append(f"Transcripción de audio: {transcripcion}")
                if not pregunta_str.strip():
                    pregunta_str = transcripcion

    datos_interpretados_archivo = kwargs.get("datos_interpretados_archivo")
    if isinstance(datos_interpretados_archivo, dict) and datos_interpretados_archivo:
        pyme_ctx_actual["datos_interpretados_archivo"] = datos_interpretados_archivo
        usuario_info_for_llm["datos_interpretados_archivo"] = datos_interpretados_archivo
        descripcion_sugerida = datos_interpretados_archivo.get("descripcion_sugerida")
        if descripcion_sugerida:
            contextual_notes.append(f"Descripción interpretada del adjunto: {descripcion_sugerida}")
            if not pregunta_str.strip():
                pregunta_str = descripcion_sugerida
        categoria_sugerida = datos_interpretados_archivo.get("categoria_sugerida")
        if categoria_sugerida:
            contextual_notes.append(f"Categoría sugerida del adjunto: {categoria_sugerida}")
        texto_extraido = datos_interpretados_archivo.get("texto_extraido")
        if texto_extraido and texto_extraido.strip():
            contextual_notes.append(f"Texto extraído del archivo: {texto_extraido.strip()}")

    if not pregunta_str.strip():
        if contextual_notes:
            pregunta_str = contextual_notes[0]
        else:
            pregunta_str = "El usuario compartió información sin texto adicional."

    mensaje_para_llm = pregunta_str.strip()
    if contextual_notes:
        notas_texto = "\n".join(f"- {nota}" for nota in contextual_notes if nota)
        if mensaje_para_llm:
            mensaje_para_llm = f"{mensaje_para_llm}\n\nContexto adicional proporcionado por el usuario:\n{notas_texto}"
        else:
            mensaje_para_llm = f"Contexto adicional proporcionado por el usuario:\n{notas_texto}"

    action_payload_id = _extract_action_id(received_payload.get("action"))
    normalized_input = _normalize_user_input(pregunta_str)
    menu_request: Optional[tuple[str, Optional[str]]] = None

    last_options_sent: list[dict] = []
    if isinstance(pyme_ctx_actual.get("last_options_sent"), list):
        last_options_sent = pyme_ctx_actual.get("last_options_sent", [])
    elif chat_db_context and isinstance(getattr(chat_db_context, "context_data", None), dict):
        context_options = chat_db_context.context_data.get("last_options_sent")
        if isinstance(context_options, list):
            last_options_sent = context_options
    if not isinstance(last_options_sent, list):
        last_options_sent = []
    pyme_ctx_actual["last_options_sent"] = last_options_sent

    if action_payload_id:
        normalized_action = _normalize_user_input(action_payload_id)
        if normalized_action in MENU_COMMAND_KEYWORDS or action_payload_id in {"menu", "menu_principal"}:
            menu_request = ("menu", None)
        else:
            menu_request = ("action", action_payload_id)
    else:
        if not normalized_input and not historial_chat_llm:
            menu_request = ("menu", None)
        elif normalized_input in GREETING_KEYWORDS or normalized_input.startswith("hola"):
            menu_request = ("menu", None)
        elif normalized_input in MENU_COMMAND_KEYWORDS:
            menu_request = ("menu", None)
        else:
            resolved_action = _find_menu_action_by_input(pregunta_str, last_options_sent)
            if resolved_action:
                menu_request = ("action", resolved_action)

    manual_llm_output = None
    if menu_request:
        kind, value = menu_request
        if kind == "menu":
            manual_llm_output = {
                "accion_backend": "saludar",
                "datos_estructura": {"target": "pyme"},
                "message_body": "",
                "botones": [],
            }
        elif kind == "action" and value:
            manual_llm_output = {
                "accion_backend": value,
                "datos_estructura": {"target": "pyme"},
                "message_body": "",
                "botones": [],
            }

    llm_response_structured = manual_llm_output
    if not llm_response_structured:
        # --- Voice/Channel Injection ---
        mensaje_payload = {"texto": mensaje_para_llm}
        # Check context for voice mode (similar to municipio)
        is_voice = channel == "voice" or pyme_ctx_actual.get("_voice_mode")

        if is_voice:
            mensaje_payload["instruccion_canal"] = (
                "ESTAS EN UNA LLAMADA DE VOZ. TU OBJETIVO ES VENDER RÁPIDO Y FLUIDO."
                "ACTÚA COMO UN VENDEDOR ARGENTINO ('Rioplatense') RE BUENA ONDA. USA 'VOS', 'CHE', 'DALE', 'GENIAL', 'BÁRBARO'."
                "RESPUESTAS MAXIMO DE 1 ORACIÓN CORTA. NO DES VUELTAS."
                "EJEMPLO: '¡Hola! ¿Qué te puedo ofrecer hoy?' o 'Dale, anotado el pedido. ¿Algo más?'."
                "SI TE PIDEN PRECIO, DALO DIRECTO: 'Te sale 5000 pesos'."
                "EL AUDIO TIENE QUE SALIR AL INSTANTE, ASÍ QUE SÉ BREVE."
            )

        mensaje_para_llm_json = json.dumps(mensaje_payload)

        llm_response_structured, _ = llamar_llm_con_fallback(
            app=current_app,
            mensaje_usuario=mensaje_para_llm_json,
            usuario=usuario_info_for_llm,
            historial=historial_chat_llm,
            chat_session_id=kwargs.get("chat_session_uuid")
        )

    # --- Gating / Hard Rules: Prevent premature handoff (Pyme) ---
    accion_backend = llm_response_structured.get("accion_backend")

    # Check if we have items in cart or a current intent
    cart_summary_check = cart_service.get_cart_summary(
        chat_db_context.context_data.get(cart_service.SESSION_CARTS_KEY, {}),
        getattr(owner_user, "id", None),
        getattr(viewer_user, "id", None),
    )
    has_cart_items = cart_summary_check and bool(cart_summary_check.get("items_detalle"))

    if accion_backend in ["pyme_hablar_agente"] and not has_cart_items:
        # Check if user query explicitly demands human strongly, or if it's just "quiero hablar con alguien"
        # For now, we enforce a soft block: Ask what they need first.
        logger_actual.info("[PYME_GATING] Blocking premature human handoff. Forcing sales inquiry.")
        llm_response_structured["accion_backend"] = "responder_directamente"
        llm_response_structured["message_body"] = "Te comunico en un momento. Para agilizar la atención, ¿me contás qué estabas buscando o en qué producto estás interesado?"
        llm_response_structured["pedir_info"] = "necesidad_cliente"

    # Actualizar historial para la próxima llamada al LLM
    if "mensajes_previos_llm_formato" not in chat_db_context.context_data:
        chat_db_context.context_data["mensajes_previos_llm_formato"] = []
    chat_db_context.context_data["mensajes_previos_llm_formato"].append({"role": "user", "parts": [{"text": mensaje_para_llm}]})
    # La respuesta del modelo al historial se añade después del ActionHandler

    # --- 4. Preparar Contexto Global para ChatOrchestrator y Action Handlers ---
    rubro_nombre_para_contexto = rubro_info_value if isinstance(rubro_info_value, str) else (rubro_nombre_contexto or "general")
    if not isinstance(rubro_nombre_para_contexto, str):
        rubro_nombre_para_contexto = "general"
    rubro_nombre_para_contexto = rubro_nombre_para_contexto.lower()

    tenant_profile = getattr(owner_user, "tenant_profile_pyme", None)
    tenant_id = tenant_profile.id if tenant_profile else None

    global_context_for_orchestrator = {
        CONTEXTO_PYME: pyme_ctx_actual,
        "user_id": getattr(owner_user, "id", None), # ID de la PYME (owner)
        "tenant_id": tenant_id,
        "nombre_pyme": nombre_pyme_display,
        "rubro_nombre": rubro_nombre_para_contexto,
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None), # ID del cliente final
        "anon_id": anon_id,
        "rubro_id": getattr(rubro_obj, "id", None) or (getattr(owner_user.rubro, "id", None) if owner_user and hasattr(owner_user, "rubro") else None),
        "coleccion_qdrant": coleccion_catalogo_para_rubro(rubro_nombre_para_contexto),
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context.context_data, # El dict vivo
        "channel": channel,
        "target_entity_type": "pyme", # Para DerivarHumanoAction
        "empresa_token": getattr(owner_user, "token", None),
        # Pasar datos del payload que podrían ser útiles para handlers
        "pregunta_actual_usuario": pregunta_str,
        "action_button_payload": received_payload.get("action"),
        "uploaded_file_info": uploaded_info,
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"), # Si ya se subió un archivo
        "ubicacion_usuario": ubicacion_payload if isinstance(ubicacion_payload, dict) else None,
        "datos_interpretados_archivo": datos_interpretados_archivo if isinstance(datos_interpretados_archivo, dict) else None,
        "ultimo_adjunto": pyme_ctx_actual.get("ultimo_adjunto"),
    }

    # --- 5. Ejecutar Acción vía ChatOrchestrator ---
    if llm_response_structured.get("accion_backend") == "saludar":
        handler = SaludoHandler(global_context_for_orchestrator)
        action_handler_result = handler.execute({})
    elif llm_response_structured.get("accion_backend") == "error_fatal_llm":
        handler = SaludoHandler(global_context_for_orchestrator)
        action_handler_result = handler.execute({})
        error_msg = llm_response_structured.get("message_body")
        if error_msg:
            existing_body = action_handler_result.get("message_body", "")
            action_handler_result["message_body"] = (
                f"{error_msg}\n\n{existing_body}" if existing_body else error_msg
            )
        action_handler_result["fuente"] = "pyme_menu_fallback_llm_error_v1"
    else:
        orchestrator = ChatOrchestrator(global_context=global_context_for_orchestrator)
        action_handler_result = orchestrator.execute_action(llm_response_structured)

    if action_handler_result.get("success"):
        handler_source = action_handler_result.get("fuente")
        if handler_source == "pyme_pedido_registrado":
            enriched_payload = _build_pyme_order_success_payload(
                global_context_for_orchestrator,
                action_handler_result,
            )
            # Preserve auxiliary keys that may not be part of the enriched payload
            for key in ("pedir_info", "contexto_actualizado"):
                if key in action_handler_result and key not in enriched_payload:
                    enriched_payload[key] = action_handler_result[key]
            action_handler_result = {**action_handler_result, **enriched_payload}

    # --- 6. Procesar Resultado del Action Handler y Formatear Respuesta ---
    respuesta_final_texto = action_handler_result.get("message_body")
    if not respuesta_final_texto:
        respuesta_final_texto = llm_response_structured.get("message_body", "No estoy seguro de cómo proceder. ¿Podrías intentarlo de nuevo?")

    opciones_finales = (
        action_handler_result.get("options_list")
        or action_handler_result.get("botones")
        or llm_response_structured.get("botones", [])
    )
    if not opciones_finales:
        opciones_finales = []
    pedir_info_final = action_handler_result.get("pedir_info") or llm_response_structured.get("pedir_info")

    # Actualizar estado de conversación en pyme_ctx_actual (que es global_context_for_orchestrator[CONTEXTO_PYME])
    if action_handler_result.get("success") and not pedir_info_final:
        if pyme_ctx_actual.get("estado_conversacion") not in [None, PymeConversationState.IDLE.name]:
            logger_actual.info(f"Acción PYME '{llm_response_structured.get('accion_backend')}' exitosa y sin pedir_info. Limpiando estado PYME.")
            preserved = {
                key: pyme_ctx_actual[key]
                for key in [
                    "static_data_cache",
                    "rubro_slug",
                    "nombre_pyme_cache",
                    "ultima_ubicacion_usuario",
                    "ultimo_adjunto",
                    "datos_interpretados_archivo",
                ]
                if key in pyme_ctx_actual
            }
            pyme_ctx_actual.clear() # Limpia el sub-diccionario
            pyme_ctx_actual.update(preserved)
    elif pedir_info_final:
        # Mapear pedir_info_final a un PymeConversationState
        # Esta lógica de mapeo es crucial y debe ser exhaustiva.
        estado_objetivo_str_pyme = None
        if pedir_info_final == "productos_del_pedido": estado_objetivo_str_pyme = PymeConversationState.ESPERANDO_PRODUCTO.name
        elif pedir_info_final == "nombre_cliente_pedido": estado_objetivo_str_pyme = PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE.name
        # ... más mapeos para PYME ...
        if estado_objetivo_str_pyme:
            pyme_ctx_actual["estado_conversacion"] = estado_objetivo_str_pyme
        else:
            logger_actual.warning(f"No se pudo mapear pedir_info_pyme '{pedir_info_final}' a PymeConversationState.")

    # Guardar historial de chat_db_context con respuesta final
    chat_db_context.context_data["mensajes_previos_llm_formato"].append({"role": "model", "parts": [{"text": respuesta_final_texto}]})
    if len(chat_db_context.context_data["mensajes_previos_llm_formato"]) > 20:
        chat_db_context.context_data["mensajes_previos_llm_formato"] = chat_db_context.context_data["mensajes_previos_llm_formato"][-20:]

    # --- Lógica de sugerencia de registro PROACTIVA (simplificada, similar a municipio) ---
    if not viewer_user and anon_id and current_app and \
       action_handler_result.get("fuente","") != "sugerencia_registro_pyme_v2": # No sugerir si ya es una sugerencia
        
        estado_actual_str_sug_pyme = pyme_ctx_actual.get("estado_conversacion")
        estado_sug_pyme_enum = None
        if estado_actual_str_sug_pyme:
            try: estado_sug_pyme_enum = PymeConversationState[estado_actual_str_sug_pyme]
            except KeyError: pass
        
        estados_evitar_sug_pyme = [ # Definir estados donde no se debe interrumpir con sugerencia
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE, PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO,
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION, PymeConversationState.ESPERANDO_DATOS_CLIENTE_EMAIL,
            PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS, PymeConversationState.CONFIRMANDO_PEDIDO
        ]
        if not estado_sug_pyme_enum or estado_sug_pyme_enum not in estados_evitar_sug_pyme:
            interacciones_anon_pyme = pyme_ctx_actual.get("interacciones_anon_sesion", 0)
            if len(pregunta_str.split()) > 1 or pregunta_str.lower() not in ["si", "no", "ok"]:
                interacciones_anon_pyme += 1
            pyme_ctx_actual["interacciones_anon_sesion"] = interacciones_anon_pyme
            umbral_sug_pyme = current_app.config.get("PYME_UMBRAL_SUGERENCIA_REGISTRO", 3)

            if umbral_sug_pyme > 0 and interacciones_anon_pyme >= umbral_sug_pyme and \
               not pyme_ctx_actual.get("sugerencia_registro_emitida_ronda", False):
                logger_actual.info(f"Anon {anon_id} (PYME) alcanzó umbral. Añadiendo sugerencia de registro.")
                pyme_ctx_actual["sugerencia_registro_emitida_ronda"] = True
                sug_obj_pyme = construir_respuesta_sugerir_registro("Para una mejor experiencia de compra.", "pyme", channel)
                respuesta_final_texto += f"\n\n{sug_obj_pyme['respuesta']}"
                opciones_finales.extend(sug_obj_pyme.get('botones',[]))
    
    # Serializar y guardar contexto PYME final
    estado_final_pyme_str = pyme_ctx_actual.get("estado_conversacion")
    if isinstance(estado_final_pyme_str, PymeConversationState):
        pyme_ctx_actual["estado_conversacion"] = estado_final_pyme_str.name
    elif estado_final_pyme_str is None:
        pyme_ctx_actual.pop("estado_conversacion", None)

    session_state.save()
    pyme_ctx_actual[session_state.STORAGE_KEY] = session_state.raw
    contexto_pyme_serializado_para_db = serializar_enum(pyme_ctx_actual)
    chat_db_context.context_data[CONTEXTO_PYME] = contexto_pyme_serializado_para_db
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    # Formatear respuesta
    message_type_pyme = action_handler_result.get("message_type") or "text"
    if channel and str(channel).lower() == "whatsapp":
        message_type_pyme = "text"
    elif message_type_pyme == "text" and opciones_finales:
        num_opt = len(opciones_finales)
        if 0 < num_opt <= 3:
            message_type_pyme = "interactive_buttons"
        elif num_opt > 3:
            message_type_pyme = "interactive_list"

    if pyme_ctx_actual.get("saludo_audio_pendiente") and respuesta_final_texto:
        respuesta_final_texto = f"Hola, soy el asistente de {nombre_pyme_display}. {respuesta_final_texto}"
        pyme_ctx_actual["saludo_audio_pendiente"] = False

    final_response_dict = {
        "message_body": respuesta_final_texto, "options_list": opciones_finales,
        "message_type": message_type_pyme,
        "contexto_actualizado": {CONTEXTO_PYME: contexto_pyme_serializado_para_db},
        "ticket_id": action_handler_result.get("data", {}).get("pedido_id"), # o ticket_id si es un reclamo pyme
        "fuente": action_handler_result.get("fuente") or llm_response_structured.get("accion_backend", "pyme_general_v4"),
        "adjuntos": [] # Manejar adjuntos si es necesario
    }

    if action_handler_result.get("data"):
        final_response_dict["data"] = action_handler_result["data"]
    if action_handler_result.get("delayed_payload"):
        final_response_dict["delayed_payload"] = action_handler_result["delayed_payload"]

    # Log de conversación (Legacy)
    if anon_id and not viewer_user:
        try:
            db.session.add(Conversacion(
                session_id=kwargs.get("chat_session_uuid") or anon_id, pregunta=pregunta_str,
                respuesta=final_response_dict["message_body"], fuente=final_response_dict["fuente"],
                rubro=global_context_for_orchestrator["rubro_nombre"], user_id=None, pyme_id=global_context_for_orchestrator["user_id"]
            ))
            db.session.commit()
        except Exception as e_conv_pyme_final:
            logger_actual.error(f"Error guardando Conversacion final (PYME): {e_conv_pyme_final}", exc_info=True)
            db.session.rollback()

    # --- Persistence: Save Chat History for Ticket (Pyme) ---
    try:
        # Check if an active ticket exists in the context (set by HumanHandler)
        active_ticket_id = pyme_ctx_actual.get("ultimo_ticket_creado")
        # Or if one was created in this turn
        if final_response_dict.get("ticket_id"):
            active_ticket_id = final_response_dict["ticket_id"]

        if active_ticket_id:
            # 1. Save User Message
            servicio_tickets.crear_comentario(
                ticket_id=active_ticket_id,
                tipo_ticket="pyme",
                comentario_data={
                    "comentario": pregunta_str, # Original user input
                    "user_id": getattr(viewer_user, "id", None),
                    "es_admin": False,
                    "origen": "chat_persistence"
                }
            )
            # 2. Save Bot Response
            bot_text = final_response_dict.get("message_body", "")
            if bot_text:
                servicio_tickets.crear_comentario(
                    ticket_id=active_ticket_id,
                    tipo_ticket="pyme",
                    comentario_data={
                        "comentario": bot_text,
                        "user_id": getattr(owner_user, "id", None), # Bot acts on behalf of owner
                        "es_admin": True,
                        "origen": "chat_persistence"
                    }
                )
            logger_actual.info(f"Persisted Pyme chat messages to Ticket ID {active_ticket_id}")

            # --- Update context for Voice Status Handlers (Pyme) ---
            if final_response_dict.get("success") and final_response_dict.get("data", {}).get("nro_pedido"):
                ticket_data = final_response_dict["data"]
                chat_db_context.context_data["latest_ticket_id"] = ticket_data.get("pedido_id")
                chat_db_context.context_data["latest_ticket_nro"] = ticket_data.get("nro_pedido")
                # Try to extract tracking link from body or build it
                import re
                tracking_links = re.findall(r"https?://\S+/pyme/pedidos\S*", final_response_dict.get("message_body", ""))
                if tracking_links:
                    chat_db_context.context_data["latest_tracking_url"] = tracking_links[0]

                flag_modified(chat_db_context, "context_data")

    except Exception as e_persist:
        logger_actual.warning(f"Failed to persist Pyme chat messages: {e_persist}")

    # Proactive suggestions (Intelligent)
    pyme_ctx_actual["turn_counter"] = int(pyme_ctx_actual.get("turn_counter", 0) or 0) + 1

    cart_summary_now = cart_service.get_cart_summary(
        global_context_for_orchestrator.get("chat_db_context_data", {}).get(cart_service.SESSION_CARTS_KEY, {}),
        getattr(owner_user, "id", None),
        getattr(viewer_user, "id", None),
    )

    sugerencia_proactiva = sugerir_productos_relacionados(
        historial_chat_llm,
        pyme_id=getattr(owner_user, "id", 0),
        rubro_nombre=global_context_for_orchestrator.get("rubro_nombre", "general"),
        nombre_pyme=nombre_pyme_display,
        cart_summary=cart_summary_now,
        last_intent=llm_response_structured.get("accion_backend") if isinstance(llm_response_structured, dict) else None,
        pyme_ctx=pyme_ctx_actual,
        channel=channel,
    )

    if sugerencia_proactiva and final_response_dict.get("success", True):
        final_response_dict["message_body"] += f"\n\n{sugerencia_proactiva}"

    logger.info(f"[RESPONDER_PYME_END_V4 - {request_id}] Respuesta: '{final_response_dict['message_body'][:100]}...', Fuente: {final_response_dict['fuente']}")
    return final_response_dict

def _trim_to_words(text: str, max_words: int) -> str:
    words = (text or "").strip().split()
    if len(words) <= max_words:
        return " ".join(words).strip()
    return " ".join(words[:max_words]).strip().rstrip(".,;:!?") + "…"

def _is_low_signal_message(msg: str) -> bool:
    m = (msg or "").strip().lower()
    return m in {"ok", "dale", "gracias", "👍", "si", "no", "bien"} or len(m) < 3

def _pick_seed_product_from_cart(cart_summary: Optional[dict]) -> Optional[str]:
    if not cart_summary or not isinstance(cart_summary.get("items_detalle"), list):
        return None
    for it in cart_summary["items_detalle"]:
        if not isinstance(it, dict):
            continue
        nombre = (it.get("nombre_producto") or it.get("nombre") or "").strip()
        if nombre:
            return nombre
    return None

def _pick_last_user_message(historial_chat: list) -> str:
    if not historial_chat:
        return ""
    for msg in reversed(historial_chat):
        if msg.get("role") == "user":
            parts = msg.get("parts") or []
            if parts and isinstance(parts, list):
                txt = (parts[0] or {}).get("text", "")
                return txt or ""
    return ""

def sugerir_productos_relacionados(
    historial_chat: list,
    pyme_id: int,
    rubro_nombre: str,
    *,
    nombre_pyme: str = "la tienda",
    cart_summary: Optional[dict] = None,
    last_intent: Optional[str] = None,
    pyme_ctx: Optional[dict] = None,
    channel: str = "web",
    max_words: int = 16,
) -> Optional[str]:
    """
    Sugerencia pro:
    1) Intenta cross-sell REAL con Qdrant (catálogo).
    2) Si no hay, intenta LLM (JSON estricto) pero SIN inventar productos.
    3) Rate limit + evita momentos sensibles del flujo.
    """

    if not pyme_id or not historial_chat:
        return None

    # ------------- Rate limit / timing -------------
    pyme_ctx = pyme_ctx if isinstance(pyme_ctx, dict) else {}
    turns = int(pyme_ctx.get("turn_counter", 0) or 0)  # si no lo tenés, lo podés incrementar en responder_pyme
    last_turn = int(pyme_ctx.get("cross_sell_last_turn", -9999) or -9999)

    # Evitar spamear: 1 sugerencia cada 4 turnos
    if turns - last_turn < 4:
        return None

    # Evitar cuando el usuario está dejando datos / confirmando
    estado = str(pyme_ctx.get("estado_conversacion") or "")
    estados_sensibles = {
        PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE.name,
        PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO.name,
        PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION.name,
        PymeConversationState.ESPERANDO_DATOS_CLIENTE_EMAIL.name,
        PymeConversationState.CONFIRMANDO_PEDIDO.name,
        PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS.name,
    }
    if estado in estados_sensibles:
        return None

    last_user_message = _pick_last_user_message(historial_chat)
    if not last_user_message or _is_low_signal_message(last_user_message):
        return None

    # Si la intención es “hablar agente” o similar, no metas cross-sell
    li = (last_intent or "").lower()
    if any(x in li for x in ["hablar", "agente", "humano", "reclamo", "ticket"]):
        return None

    # ------------- 1) QDRANT FIRST (SUGERENCIA REAL) -------------
    seed = _pick_seed_product_from_cart(cart_summary)
    if not seed:
        # intento simple desde el último mensaje (sin NLP pesado)
        seed = last_user_message.strip()[:60]

    try:
        query = f"complemento para {seed}"
        hits = buscar_catalogo_qdrant(
            pyme_id,
            query,
            rubro_nombre or "general",
            3,
            CATALOGO_PYME,
        )
        # elegí el primer hit decente
        for hit in hits or []:
            payload = getattr(hit, "payload", {}) or {}
            nombre = (payload.get("nombre") or "").strip()
            if not nombre:
                continue
            # Evitar sugerir exactamente lo mismo
            if nombre.lower() in (seed or "").lower():
                continue

            # opcional: precio si está
            precio_txt = (payload.get("precio_str") or "").strip()
            sugerencia = f"Sugerencia: ¿Querés sumar {nombre} para completar tu compra?"
            if precio_txt and len(precio_txt) < 20:
                sugerencia = f"Sugerencia: ¿Sumamos {nombre} ({precio_txt}) para completar?"

            # guardar rate limit
            pyme_ctx["cross_sell_last_turn"] = turns
            return _trim_to_words(sugerencia, max_words)
    except Exception as e:
        logger.warning(f"[cross_sell_qdrant] error: {e}")

    # ------------- 2) LLM FALLBACK (JSON ESTRICTO, SIN INVENTAR) -------------
    # Si no tenés catálogo o Qdrant no devolvió nada, el LLM puede sugerir un "extra" genérico
    # (ej: "¿Querés coordinar envío o retiro?") pero sin inventar productos.
    prompt = f"""
Sos un asistente comercial de {nombre_pyme}.
Objetivo: aumentar el ticket sin ser insistente.

REGLAS:
- NO inventes productos que no estén confirmados en el mensaje del usuario.
- Si no hay una sugerencia clara, devolvé: {{ "suggestion": null }}
- Si sí hay, devolvé JSON estricto: {{ "suggestion": "..." }}
- 1 sola oración, tono profesional, máximo {max_words} palabras.
- Sin emojis.

Contexto:
- Rubro: {rubro_nombre or "general"}
- Último mensaje del usuario: "{last_user_message}"

Salida JSON:
""".strip()

    try:
        llm_out, _ = llamar_llm_con_fallback(
            app=current_app,
            mensaje_usuario=prompt,
            usuario={"nombre": "system", "tipo_entidad": "pyme"},
            historial=[],
            chat_session_id=None,
        )
        suggestion = None
        if isinstance(llm_out, dict):
            suggestion = llm_out.get("suggestion") or llm_out.get("respuesta") or llm_out.get("message_body")
        elif isinstance(llm_out, str):
            suggestion = llm_out

        if not suggestion:
            return None

        suggestion = str(suggestion).strip().strip('"').strip()
        if suggestion.lower() in {"skip", "null", "none"}:
            return None

        pyme_ctx["cross_sell_last_turn"] = turns
        return _trim_to_words(f"Sugerencia: {suggestion}", max_words)
    except Exception as e:
        logger.warning(f"[cross_sell_llm] error: {e}")
        return None

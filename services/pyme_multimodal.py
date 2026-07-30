"""Helpers for the multimodal PYME experience.

This module centralises the lightweight keyword dispatcher, cart helpers and
media utilities that allow PYME conversations to reuse the same multimodal
pipeline already available for municipality chats.  The goal is to keep the
logic side-effect free so it can be unit tested without network access.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import secrets
from dataclasses import dataclass, field
from datetime import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

from flask import current_app
from sqlalchemy.exc import IntegrityError

from models import CatalogoItem, ChatSessionContext, PedidoConversacional, PymePedido, TenantProfile, TenantTicket, db
from services.domain_effect_gate import (
    DomainEffectOutboxConfigurationError,
    resolve_domain_effect_outbox_policy,
)
from services.multimodal_analyzer import analizar_imagen_con_fallback
from services.order_attachment_preview import build_crm_order_draft
from services.order_idempotency import (
    existing_order_payload_matches,
    order_payload_hash,
)
from services.pyme_menu import get_pyme_menu_payload
from services.config_loader import cargar_configuracion_pyme
from services.document_processing_service import document_processing_service
from services.marketplace_analytics import track_marketplace_event
from services.qdrant_search import buscar_catalogo_qdrant, CATALOGO_PYME
from utils.money_ar import format_ars, parse_ars

logger = logging.getLogger(__name__)


def _format_money(value: object, currency: str = "ARS") -> str:
    if currency != "ARS":
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)
    try:
        # Convert float to string to avoid precision issues before Decimal
        dec_value = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return str(value)
    decimals = 0 if dec_value == dec_value.to_integral_value() else 2
    return format_ars(dec_value, decimals=decimals)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


@dataclass
class PymeCartItem:
    sku: str
    title: str
    qty: int
    unit_price: float
    currency: str = "ARS"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sku": self.sku,
            "title": self.title,
            "qty": self.qty,
            "unitPrice": self.unit_price,
            "currency": self.currency,
            "metadata": self.metadata,
        }


class PymeSessionState:
    """Utility wrapper stored under ``context_data[CONTEXTO_PYME]``.

    The state is persisted as a regular dictionary so it survives SQLAlchemy's
    JSON serialisation.  Consumers must call :meth:`save` when changes occur.
    """

    STORAGE_KEY = "multimodal_state_v1"

    def __init__(self, backing_store: Dict[str, Any], pyme_id: Optional[int]):
        self._store = backing_store.setdefault(self.STORAGE_KEY, {})
        self.pyme_id = pyme_id
        self.cart: Dict[str, Any] = self._store.setdefault(
            "cart",
            {
                "items": [],
                "currency": "ARS",
                "subtotal": 0.0,
                "total": 0.0,
                "promotions": [],
            },
        )
        self.delivery: Dict[str, Any] = self._store.setdefault("delivery", {})
        self.last_intent: Optional[str] = self._store.get("last_intent")

    @property
    def raw(self) -> Dict[str, Any]:
        return self._store

    def save(self) -> None:
        self._store["cart"] = self.cart
        self._store["delivery"] = self.delivery
        if self.last_intent:
            self._store["last_intent"] = self.last_intent
        else:
            self._store.pop("last_intent", None)


# ---------------------------------------------------------------------------
# Catalog utilities
# ---------------------------------------------------------------------------


def _parse_price(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        # Use localized parser to handle '5.207' as 5207.0, not 5.207
        return float(parse_ars(value))
    except Exception:
        return 0.0


def _serialise_item(item: CatalogoItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "catalogo_item_id": item.id,
        "sku": item.sku or item.nombre,
        "nombre": item.nombre,
        "descripcion": item.descripcion or item.descripcion_corta,
        "precio": _parse_price(item.precio),
        "moneda": "ARS",
        "presentacion": item.cantidad or "unidad",
        "marca": item.marca,
        "categoria": item.categoria,
        "talles": item.extra_metadata.get("talles") if item.extra_metadata else None,
        "colores": item.extra_metadata.get("colores") if item.extra_metadata else None,
    }


def load_catalog(owner_user_id: Optional[int], rubro_slug: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    if owner_user_id:
        query = CatalogoItem.query.filter_by(user_id=owner_user_id)
        for db_item in query.limit(200):
            items.append(_serialise_item(db_item))

    if not items:
        fallback = cargar_configuracion_pyme(rubro_slug, "catalogo_destacado.json")
        if not fallback:
            fallback = cargar_configuracion_pyme("default", "catalogo_destacado.json")
        if isinstance(fallback, list):
            for raw in fallback:
                if isinstance(raw, dict):
                    items.append(
                        {
                            "sku": raw.get("sku") or raw.get("nombre"),
                            "nombre": raw.get("nombre"),
                            "descripcion": raw.get("descripcion"),
                            "precio": _parse_price(raw.get("precio")),
                            "moneda": raw.get("moneda") or "ARS",
                            "presentacion": raw.get("presentacion"),
                        }
                    )

    return items


def _normalise(text: str) -> str:
    return " ".join(text.lower().strip().split())


def match_catalog_items(
    query: str, catalog: Iterable[Dict[str, Any]], max_results: int = 3
) -> List[Dict[str, Any]]:
    if not query:
        return []

    tokens = [_normalise(token) for token in query.split() if token]
    results: List[Tuple[int, Dict[str, Any]]] = []

    for item in catalog:
        haystack = " ".join(
            _normalise(str(item.get(key, "")))
            for key in ("sku", "nombre", "descripcion", "presentacion", "categoria", "marca")
        )
        score = 0
        for token in tokens:
            if token and token in haystack:
                score += 1
        if score:
            results.append((score, item))

    results.sort(key=lambda pair: pair[0], reverse=True)

    # Log top matches for observability
    top_matches = results[:max_results]
    if top_matches:
        logger.info(
            f"[CATALOG_MATCH] Query: '{query}' | Tokens: {tokens} | Top {len(top_matches)} matches:"
        )
        for score, item in top_matches:
            logger.info(f"  - Score: {score} | Item: {item.get('nombre')} (SKU: {item.get('sku')})")
    else:
        logger.info(f"[CATALOG_MATCH] Query: '{query}' | No matches found.")

    return [item for _, item in top_matches]


def add_items_to_cart(
    state: PymeSessionState, items: List[Tuple[Dict[str, Any], int]]
) -> List[Dict[str, Any]]:
    cart_items = state.cart.setdefault("items", [])
    lookup: Dict[str, Dict[str, Any]] = {item.get("sku"): item for item in cart_items if item.get("sku")}
    operations: List[Dict[str, Any]] = []

    for catalog_item, qty in items:
        if qty <= 0:
            qty = 1
        sku = catalog_item.get("sku") or catalog_item.get("nombre")
        unit_price = _parse_price(catalog_item.get("precio"))
        currency = catalog_item.get("moneda") or state.cart.get("currency", "ARS")

        existing = lookup.get(sku)
        if existing:
            existing["qty"] += qty
            operations.append(
                {
                    "sku": sku,
                    "qty_delta": qty,
                    "mode": "increment",
                    "result_qty": existing["qty"],
                }
            )
        else:
            entry = {
                "sku": sku,
                "title": catalog_item.get("nombre") or sku,
                "qty": qty,
                "unitPrice": unit_price,
                "currency": currency,
                "metadata": {
                    "presentacion": catalog_item.get("presentacion"),
                    "descripcion": catalog_item.get("descripcion"),
                },
            }
            cart_items.append(entry)
            lookup[sku] = entry
            operations.append(
                {
                    "sku": sku,
                    "qty_delta": qty,
                    "mode": "new",
                    "result_qty": qty,
                }
            )

    subtotal = sum(item["qty"] * item.get("unitPrice", 0.0) for item in cart_items)
    state.cart["subtotal"] = subtotal
    state.cart["total"] = subtotal  # promos will adjust later
    return operations


def render_cart_summary(state: PymeSessionState) -> str:
    items = state.cart.get("items", [])
    if not items:
        return "Tu carrito está vacío por ahora."

    lines = ["🧺 **Resumen de tu carrito:**"]
    for item in items:
        title = item.get("title") or item.get("sku")
        qty = item.get("qty", 1)
        price = item.get("unitPrice", 0.0)
        total = qty * price
        presentacion = item.get("metadata", {}).get("presentacion")
        currency = item.get("currency", "ARS")
        detail = f"- {qty} x {title}"
        if presentacion:
            detail += f" ({presentacion})"
        detail += f" — ${_format_money(total, currency)}"
        lines.append(detail)

    subtotal = state.cart.get("subtotal", 0.0)
    envio = state.delivery.get("shipping_total")
    total = state.cart.get("total", subtotal)

    lines.append(f"Subtotal productos: ${_format_money(subtotal)}")
    if envio is not None:
        lines.append(f"Envío estimado: ${_format_money(envio)}")
        total = subtotal + envio
    lines.append(f"Total estimado: ${_format_money(total)}")
    return "\n".join(lines)


_QUANTITY_RE = re.compile(
    r"(?:^|\b)(\d{1,3})\s*(?:x|unid(?:ad(?:es)?)?|caja(?:s)?|botellas?|pack|packs)\b|(?:^|\b)(\d{1,3})(?=\s+[A-Za-zÀ-ÿ])",
    re.IGNORECASE,
)


def _extract_quantity_from_text(text: str) -> int:
    match = _QUANTITY_RE.search(text)
    if not match:
        return 1
    try:
        value = int(match.group(1) or match.group(2))
        return max(1, value)
    except Exception:  # pragma: no cover - defensive
        return 1


def _match_catalog_from_free_text(
    text: str, catalog: Iterable[Dict[str, Any]], max_results: int = 3
) -> List[Tuple[Dict[str, Any], int]]:
    if not text:
        return []

    aggregated: Dict[str, Tuple[Dict[str, Any], int]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matches = match_catalog_items(line, catalog, max_results=max_results)
        if not matches:
            continue
        qty = _extract_quantity_from_text(line)
        top_item = matches[0]
        sku = top_item.get("sku") or top_item.get("nombre")
        if not sku:
            continue
        stored_item, current_qty = aggregated.get(sku, (top_item, 0))
        aggregated[sku] = (stored_item, current_qty + qty)

    return [(item, qty) for item, qty in aggregated.values() if qty > 0]


# ---------------------------------------------------------------------------
# Keyword dispatcher
# ---------------------------------------------------------------------------


KEYWORD_MAP = {
    "confirmar": {"confirmar", "finalizar", "cerrar"},
    "ver_catalogo": {"catalogo", "catálogo", "menu", "menú"},
    "precios": {"precio", "precios", "lista", "tarifa"},
    "pedido": {"pedido", "comprar", "agregar", "sumar", "quiero", "necesito", "busco"},
    "presupuesto": {"presupuesto", "cotizacion", "cotización", "quote"},
    "delivery": {"delivery", "envio", "envío", "reparto", "entrega"},
    "ubicacion": {"ubicacion", "ubicación", "direccion", "dirección"},
    "pago": {"pagar", "pago", "cobro"},
    "menu_fijo": {"menu", "menú", "inicio"},
}

INTENT_PRIORITY = {
    "confirmar": 0,
    "delivery": 1,
    "pedido": 2,
    "presupuesto": 3,
    "ver_catalogo": 4,
    "precios": 5,
    "ubicacion": 6,
    "menu_fijo": 7,
    "pago": 8,
}


def detect_intent_from_text(text: str) -> Optional[str]:
    normalised = _normalise(text)
    if not normalised:
        return None

    scores: Dict[str, int] = {}
    for intent, keywords in KEYWORD_MAP.items():
        score = sum(1 for keyword in keywords if keyword in normalised)
        if score:
            scores[intent] = score

    if not scores:
        logger.info(f"[INTENT_DETECT] No keywords matched for text: '{text}'")
        return None

    max_score = max(scores.values())
    candidates = [intent for intent, score in scores.items() if score == max_score]
    candidates.sort(key=lambda intent: INTENT_PRIORITY.get(intent, 99))

    winner = candidates[0]
    logger.info(f"[INTENT_DETECT] Winner: '{winner}' | Scores: {scores}")
    return winner


@dataclass
class PymeFlowResult:
    message_body: str
    message_type: str = "text"
    options_list: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "pyme_multimodal_v1"
    data: Dict[str, Any] = field(default_factory=dict)
    delayed_payload: Optional[Dict[str, Any]] = None
    delay_seconds: Optional[int] = None
    audio_url: Optional[str] = None
    audio_text: Optional[str] = None
    generar_audio: Optional[bool] = None
    menu_audio_enabled: Optional[bool] = None
    tts_cache_text: Optional[str] = None
    tts_cache_namespace: Optional[str] = None
    audio_cache_policy: Optional[Dict[str, Any]] = None
    tts_voice: Optional[str] = None
    tts_model: Optional[str] = None
    tts_style: Optional[str] = None
    tts_speed: Optional[float] = None


def _menu_response(state: PymeSessionState, context: Dict[str, Any], channel: str) -> PymeFlowResult:
    payload = get_pyme_menu_payload(
        {
            "nombre_pyme": context.get("nombre_pyme"),
            "rubro_slug": context.get("rubro_slug"),
            "rubro_nombre": context.get("rubro_nombre"),
        },
        channel=channel,
    )
    return PymeFlowResult(
        message_body=payload.get("message_body", ""),
        message_type=payload.get("message_type", "interactive_menu"),
        options_list=payload.get("options_list", []),
        source="pyme_menu_keywords",
        data={"menu": payload.get("data")},
    )


def handle_keyword_intent(
    *,
    intent: str,
    text: str,
    state: PymeSessionState,
    owner_user_id: Optional[int],
    rubro_slug: str,
    context: Dict[str, Any],
    channel: str,
    parsed_items: Optional[List[Dict[str, Any]]] = None,
    request_id: Optional[str] = None,
) -> Optional[PymeFlowResult]:
    logger.info(
        f"[HANDLE_INTENT] Intent: '{intent}' | Text: '{text}' | Parsed Items: {parsed_items} | Request ID: {request_id}"
    )
    catalog = load_catalog(owner_user_id, rubro_slug)

    if intent in {"ver_catalogo", "precios"}:
        if not catalog:
            return PymeFlowResult(
                message_body="Todavía no tengo el catálogo cargado, ¿querés que te contacte un asesor?",
                source="pyme_catalogo_vacio",
            )
        destacados = catalog[:3]
        lines = ["Estos son algunos destacados:"]
        for item in destacados:
            lines.append(
                f"• {item.get('nombre')} — ${_format_money(_parse_price(item.get('precio')))} ({item.get('presentacion')})"
            )
        return PymeFlowResult(
            message_body="\n".join(lines),
            source="pyme_catalogo_destacado",
            options_list=[
                {"texto": "Ver más opciones", "action_id": "ver_catalogo_completo"},
                {"texto": "Armar presupuesto", "action_id": "armar_presupuesto"},
            ],
        )

    if intent == "pedido":
        matches: List[Tuple[Dict[str, Any], int]] = []
        # Explicit items (e.g. "2 cajas de Malbec")
        if parsed_items:
            for parsed in parsed_items:
                nombre = parsed.get("nombre")
                cantidad = int(parsed.get("cantidad") or 1)
                if not nombre:
                    continue
                catalog_match = match_catalog_items(nombre, catalog, max_results=1)
                if catalog_match:
                    matches.append((catalog_match[0], cantidad))

        # If explicit matches found, add to cart
        if matches:
            logger.info(
                "[PYME_FLOW] catalog_hit",
                extra={
                    "request_id": request_id,
                    "intent": "pedido",
                    "matches": [
                        {"sku": item.get("sku"), "qty": qty} for item, qty in matches if item.get("sku")
                    ],
                },
            )
            cart_updates = add_items_to_cart(state, matches)
            if cart_updates:
                logger.info(
                    "[PYME_FLOW] cart_updated",
                    extra={
                        "request_id": request_id,
                        "updates": cart_updates,
                        "subtotal": state.cart.get("subtotal"),
                    },
                )
            state.last_intent = "pedido"
            body = render_cart_summary(state)
            return PymeFlowResult(
                message_body=body,
                source="pyme_item_agregado",
                options_list=[
                    {"texto": "Pedir presupuesto", "action_id": "armar_presupuesto"},
                    {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
                ],
            )

        # Fallback: treat as SEARCH if no explicit quantity/item found
        # User said "quiero comprar malbec" -> Show list of malbecs
        fallback_matches = match_catalog_items(text, catalog, max_results=5)
        if not fallback_matches:
            return PymeFlowResult(
                message_body=(
                    "No encontré productos que coincidan con tu búsqueda. "
                    "Podés ver el catálogo completo o probar con otro nombre."
                ),
                source="pyme_catalogo_sin_match",
                options_list=[
                    {"texto": "Ver catálogo", "action_id": "ver_catalogo"},
                ]
            )

        lines = ["Encontré estos productos:"]
        for item in fallback_matches:
            lines.append(
                f"• {item.get('nombre')} — ${_format_money(_parse_price(item.get('precio')))} ({item.get('presentacion')})"
            )
        lines.append("\nRespondé con el nombre o cantidad para agregarlos al carrito.")

        return PymeFlowResult(
            message_body="\n".join(lines),
            source="pyme_catalogo_busqueda",
            options_list=[
                {"texto": "Ver más opciones", "action_id": "ver_catalogo_completo"},
            ],
        )

    if intent == "presupuesto":
        if not state.cart.get("items"):
            return PymeFlowResult(
                message_body="Tu carrito está vacío. Decime qué producto querés y lo agrego.",
                source="pyme_presupuesto_vacio",
            )
        summary = render_cart_summary(state)
        state.last_intent = "presupuesto"
        logger.info(
            "[PYME_FLOW] quote_generated",
            extra={
                "request_id": request_id,
                "items": [
                    {"sku": item.get("sku"), "qty": item.get("qty"), "unitPrice": item.get("unitPrice")}
                    for item in state.cart.get("items", [])
                ],
                "subtotal": state.cart.get("subtotal"),
                "total": state.cart.get("total"),
            },
        )
        return PymeFlowResult(
            message_body=summary,
            source="pyme_presupuesto_resumen",
            options_list=[
                {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
                {"texto": "Agregar otro producto", "action_id": "agregar_producto"},
            ],
            data={"cart": state.cart},
        )

    if intent == "delivery":
        state.last_intent = "delivery"
        body = "Perfecto, ¿me compartís la ubicación para estimar el envío?"
        return PymeFlowResult(
            message_body=body,
            source="pyme_delivery_solicitud_ubicacion",
            options_list=[
                {"texto": "Compartir mi ubicación", "action_id": "enviar_ubicacion"},
                {"texto": "Retiro en tienda", "action_id": "retiro_tienda"},
            ],
        )

    if intent in {"ubicacion", "menu_fijo"}:
        return _menu_response(state, context, channel)

    if intent == "pago":
        body = "Podés abonar por transferencia, Mercado Pago o efectivo contra entrega."
        return PymeFlowResult(message_body=body, source="pyme_info_pago")

    if intent == "confirmar":
        if not state.cart.get("items"):
            return PymeFlowResult(
                message_body="Todavía no cargaste productos al carrito.",
                source="pyme_confirmar_sin_items",
            )
        pedido = persist_order(
            state,
            owner_user_id,
            context.get("viewer_user_id"),
            context=context,
            request_id=request_id,
        )
        if not pedido:
            return PymeFlowResult(
                message_body="No pude registrar tu pedido en este momento. Probá nuevamente o hablá con un asesor.",
                source="pyme_pedido_error",
                options_list=[
                    {"texto": "🤝 Hablar con un asesor", "action_id": "pyme_hablar_agente"},
                    {"texto": "🛒 Ver catálogo", "action_id": "pyme_productos_stock"},
                ],
            )
        state.last_intent = "confirmar"
        numero = getattr(pedido, "nro_pedido", None)

        items_detalle: List[Dict[str, Any]] = []
        subtotal = 0.0
        currency = state.cart.get("currency", "ARS")
        for item in state.cart.get("items", []):
            qty_raw = item.get("qty", 0)
            try:
                qty = int(qty_raw)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            unit_price = float(item.get("unitPrice") or 0.0)
            line_total = unit_price * qty
            subtotal += line_total
            metadata = item.get("metadata") or {}
            items_detalle.append(
                {
                    "nombre_producto": item.get("title") or item.get("sku") or "Producto",
                    "cantidad": qty,
                    "precio_unitario_original": unit_price,
                    "subtotal_con_descuento": line_total,
                    "moneda": item.get("currency") or currency,
                    "presentacion": metadata.get("presentacion"),
                    "sku": item.get("sku"),
                }
            )

        envio_total = state.delivery.get("shipping_total")
        total_estimado = subtotal + (envio_total or 0.0)
        cart_summary = {
            "items_detalle": items_detalle,
            "total_original_calculado": subtotal,
            "total_final_con_descuento": total_estimado,
        }
        if envio_total is not None:
            cart_summary["envio_estimado"] = envio_total

        resumen_texto = render_cart_summary(state)

        cliente_payload = {
            "nombre": context.get("nombre_cliente"),
            "telefono": context.get("telefono_cliente"),
            "email": context.get("email_cliente"),
            "direccion": state.delivery.get("address"),
        }
        cliente_payload = {k: v for k, v in cliente_payload.items() if v}

        data_payload: Dict[str, Any] = {
            "nro_pedido": numero,
            "pedido_id": getattr(pedido, "id", None),
            "monto_total": total_estimado,
            "cart_summary": cart_summary,
            "cliente": cliente_payload,
            "order_summary_text": resumen_texto,
        }
        if getattr(pedido, "consulta_pin", None):
            data_payload["consulta_pin"] = getattr(pedido, "consulta_pin")

        data_payload["pedido"] = {
            "id": getattr(pedido, "id", None),
            "nro_pedido": numero,
            "total": total_estimado,
            "cart_summary": cart_summary,
            "cliente": cliente_payload,
        }

        # Limpiar carrito y entrega después de confirmar
        state.cart["items"] = []
        state.cart["subtotal"] = 0.0
        state.cart["total"] = 0.0
        state.delivery.clear()

        opciones = [
            {"texto": "Compartir comprobante", "action_id": "enviar_comprobante"},
            {"texto": "Ver catálogo", "action_id": "ver_catalogo"},
        ]

        return PymeFlowResult(
            message_body=resumen_texto,
            source="pyme_pedido_registrado",
            data=data_payload,
            options_list=opciones,
        )

    return None


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _resolve_base_location(config: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    for ubicacion in (config.get("ubicaciones") or []):
        coords = ubicacion.get("coordenadas") or {}
        lat = coords.get("lat") or coords.get("latitude")
        lon = coords.get("lon") or coords.get("lng") or coords.get("longitude")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    return None


def estimate_shipping(config: Dict[str, Any], destination: Dict[str, Any]) -> Dict[str, Any]:
    base_coords = _resolve_base_location(config) or (-34.6037, -58.3816)  # Buenos Aires fallback
    dest_lat = float(destination.get("lat") or destination.get("latitude"))
    dest_lon = float(destination.get("lon") or destination.get("lng") or destination.get("longitude"))
    distance_km = haversine_distance_km(base_coords[0], base_coords[1], dest_lat, dest_lon)
    base_rate = config.get("tarifa_base_envio", 1800)
    per_km = config.get("tarifa_por_km", 120)
    total = base_rate + per_km * distance_km
    eta_hours = max(1, distance_km / 40)
    return {
        "distance_km": distance_km,
        "shipping_total": round(total, 2),
        "eta_hours": round(eta_hours, 1),
    }


def handle_location_payload(
    state: PymeSessionState, config: Dict[str, Any], location_payload: Dict[str, Any]
) -> PymeFlowResult:
    estimate = estimate_shipping(config, location_payload)
    state.delivery.update({
        "lat": location_payload.get("lat") or location_payload.get("latitude"),
        "lng": location_payload.get("lon") or location_payload.get("lng") or location_payload.get("longitude"),
        "address": location_payload.get("address") or location_payload.get("descripcion"),
        "shipping_total": estimate["shipping_total"],
    })
    body = (
        "Gracias por la ubicación. Estimo un costo de envío de ${:.2f} "
        "con entrega en ~{} horas ({} km)."
    ).format(estimate["shipping_total"], estimate["eta_hours"], round(estimate["distance_km"], 1))
    return PymeFlowResult(
        message_body=body,
        source="pyme_delivery_estimate",
        data={"delivery": state.delivery},
        options_list=[
            {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
            {"texto": "Editar dirección", "action_id": "editar_direccion"},
        ],
    )


# ---------------------------------------------------------------------------
# Order persistence helpers
# ---------------------------------------------------------------------------


def persist_order(
    state: PymeSessionState,
    owner_user_id: Optional[int],
    viewer_user_id: Optional[int],
    *,
    context: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> Optional[PymePedido]:
    if not state.cart.get("items") or not owner_user_id:
        return None

    context = context or {}
    currency = state.cart.get("currency", "ARS")
    detalles_items: List[Dict[str, Any]] = []
    for item in state.cart.get("items", []):
        if not isinstance(item, dict):
            continue
        qty = item.get("qty") or item.get("cantidad") or 1
        unit_price = item.get("unitPrice") or item.get("precio_unitario") or 0.0
        try:
            qty_int = max(1, int(float(qty)))
        except (TypeError, ValueError):
            qty_int = 1
        try:
            unit_price_float = float(unit_price)
        except (TypeError, ValueError):
            unit_price_float = 0.0
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        detalles_items.append(
            {
                "nombre": item.get("title") or item.get("nombre") or item.get("sku") or "Producto",
                "nombre_producto": item.get("title") or item.get("nombre") or item.get("sku") or "Producto",
                "sku": item.get("sku"),
                "cantidad": qty_int,
                "precio_unitario": unit_price_float,
                "precio_unitario_original": unit_price_float,
                "subtotal": unit_price_float * qty_int,
                "subtotal_con_descuento": unit_price_float * qty_int,
                "moneda": item.get("currency") or currency,
                "presentacion": metadata.get("presentacion"),
            }
        )

    total = state.cart.get("total", state.cart.get("subtotal", 0.0))
    direccion = state.delivery.get("address") or context.get("direccion_cliente") or context.get("direccion")
    tenant_id = context.get("tenant_id")
    idempotency_key = str(context.get("idempotency_key") or "").strip() or None
    if idempotency_key and (len(idempotency_key) > 128 or not tenant_id):
        logger.error(
            "[PYME_FLOW] invalid_order_idempotency_context",
            extra={"request_id": request_id, "has_tenant": bool(tenant_id)},
        )
        return None

    pedido_payload = {
        "pyme_id": owner_user_id,
        "tenant_id": tenant_id,
        "asunto": "Pedido generado desde el asistente",
        "detalles": json.dumps(detalles_items, ensure_ascii=False),
        "monto_total": total,
        "moneda": currency,
        "user_id": viewer_user_id,
        "nombre_cliente": context.get("nombre_cliente") or context.get("nombre"),
        "email_cliente": context.get("email_cliente") or context.get("email"),
        "telefono_cliente": context.get("telefono_cliente") or context.get("telefono"),
        "direccion": direccion,
        "latitud": state.delivery.get("lat"),
        "longitud": state.delivery.get("lng"),
        "estado": "pendiente",
    }

    # Queue-canary confirmations enter the canonical order service so the
    # legacy order, both CRM projections and every provider intent share one
    # transaction. Non-canary tenants intentionally keep this flow's historic
    # behavior below: persist only PymePedido and do not dispatch or project.
    try:
        from flask import has_app_context

        if has_app_context():
            mode = str(
                current_app.config.get("DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
                or "legacy"
            ).strip().lower()
            if not tenant_id and mode == "queue":
                raise DomainEffectOutboxConfigurationError(
                    "domain_effect_tenant_invalid"
                )
            policy = (
                resolve_domain_effect_outbox_policy(
                    current_app.config,
                    tenant_id=tenant_id,
                )
                if tenant_id
                else None
            )
            if policy and policy.enabled:
                from services.pedido_service import PedidoService

                return PedidoService().crear_nuevo_pedido(
                    {
                        **pedido_payload,
                        "rubro": (
                            context.get("rubro")
                            or context.get("rubro_nombre")
                            or "general_pyme"
                        ),
                        "idempotency_key": idempotency_key,
                        "channel": context.get("channel") or "whatsapp",
                    }
                )
    except DomainEffectOutboxConfigurationError as exc:
        logger.error(
            "[PYME_FLOW] order_outbox_configuration_rejected",
            extra={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "reason_code": str(exc),
            },
        )
        return None

    expected_payload_hash = (
        order_payload_hash(pedido_payload) if idempotency_key else None
    )

    def accept_existing(existing: PymePedido, *, raced: bool = False) -> Optional[PymePedido]:
        matches, legacy_contract = existing_order_payload_matches(
            existing,
            expected_payload_hash or "",
        )
        if not matches:
            logger.warning(
                "[PYME_FLOW] order_idempotency_payload_conflict",
                extra={
                    "pedido_id": existing.id,
                    "tenant_id": existing.tenant_id,
                    "legacy_contract": legacy_contract,
                    "raced": raced,
                },
            )
            return None
        logger.info(
            "[PYME_FLOW] order_idempotent_replay",
            extra={
                "pedido_id": existing.id,
                "tenant_id": existing.tenant_id,
                "legacy_contract": legacy_contract,
                "raced": raced,
            },
        )
        return existing

    if idempotency_key:
        existing = PymePedido.query.filter_by(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return accept_existing(existing)

    pedido = PymePedido(
        owner_user_id,
        pedido_payload["asunto"],
        pedido_payload["detalles"],
        monto_total=total,
        moneda=currency,
        user_id=viewer_user_id,
        nombre_cliente=pedido_payload["nombre_cliente"],
        email_cliente=pedido_payload["email_cliente"],
        telefono_cliente=pedido_payload["telefono_cliente"],
        direccion=direccion,
        latitud=state.delivery.get("lat"),
        longitud=state.delivery.get("lng"),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        idempotency_payload_hash=expected_payload_hash,
    )
    db.session.add(pedido)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if tenant_id and idempotency_key:
            existing = PymePedido.query.filter_by(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                return accept_existing(existing, raced=True)
        raise
    logger.info(
        "[PYME_FLOW] order_created",
        extra={
            "pedido_id": getattr(pedido, "id", None),
            "tenant_id": getattr(pedido, "tenant_id", None),
            "item_count": len(detalles_items),
            "idempotency_bound": bool(expected_payload_hash),
        },
    )
    return pedido


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------


ASSISTED_REQUEST_CONTRACT_VERSION = "marketplace.assisted_request.v1"
ASSISTED_INTAKE_CONTRACT_VERSION = "marketplace.assisted_intake_experience.v1"


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _assisted_channel(channel: Optional[str]) -> str:
    normalized = (channel or "web").strip().lower()
    if normalized in {"whatsapp", "wa", "twilio_whatsapp"}:
        return "whatsapp"
    if normalized in {"widget", "chat_widget", "web_widget"}:
        return "chat_widget"
    return normalized or "web"


def _assisted_contact_payload(context: Optional[Dict[str, Any]], anon_id: Optional[str]) -> Dict[str, str]:
    context = context or {}
    contact = {
        "name": _clean_text(context.get("nombre_cliente") or context.get("nombre") or context.get("profile_name")),
        "phone": _clean_text(context.get("telefono_cliente") or context.get("telefono") or anon_id),
        "email": _clean_text(context.get("email_cliente") or context.get("email")),
        "address": _clean_text(context.get("direccion_cliente") or context.get("direccion")),
    }
    return {key: value for key, value in contact.items() if value}


def _assisted_source_payload(
    *,
    source_type: str,
    channel: Optional[str],
    attachment_info: Dict[str, Any],
    text_preview: Optional[str] = None,
) -> Dict[str, Any]:
    attachment_payload = {
        "id": attachment_info.get("id"),
        "url": attachment_info.get("url"),
        "name": attachment_info.get("name") or attachment_info.get("filename"),
        "filename": attachment_info.get("filename") or attachment_info.get("name"),
        "mimeType": attachment_info.get("mime_type") or attachment_info.get("mimeType"),
        "mime_type": attachment_info.get("mime_type") or attachment_info.get("mimeType"),
        "size": attachment_info.get("size") or attachment_info.get("file_size_bytes"),
        "source": "pyme_multimodal",
    }
    attachment_payload = {key: value for key, value in attachment_payload.items() if value}
    source = {
        "channel": _assisted_channel(channel),
        "input_type": source_type,
        "archivo_url": attachment_info.get("url"),
        "archivo_nombre": attachment_info.get("name") or attachment_info.get("filename"),
        "attachment_id": attachment_info.get("id"),
        "mime_type": attachment_info.get("mime_type") or attachment_info.get("mimeType"),
    }
    if attachment_payload.get("url") or attachment_payload.get("id"):
        source["attachmentInfo"] = attachment_payload
        source["attachment_info"] = attachment_payload
        source["source_attachment"] = attachment_payload
    if text_preview:
        source["text_preview"] = text_preview[:500]
    return {key: value for key, value in source.items() if value}


def _catalog_match_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    product_id = item.get("catalogo_item_id") or item.get("id") or item.get("product_id")
    return {
        "catalogo_item_id": product_id,
        "product_id": product_id,
        "sku": item.get("sku"),
        "nombre": item.get("nombre") or item.get("title"),
        "name": item.get("nombre") or item.get("title"),
        "precio": item.get("precio"),
        "price": _parse_price(item.get("precio")),
        "moneda": item.get("moneda") or item.get("currency") or "ARS",
        "presentacion": item.get("presentacion"),
        "descripcion": item.get("descripcion"),
    }


def _is_placeholder_item(item: Dict[str, Any]) -> bool:
    sku = str(item.get("sku") or "")
    return sku.startswith("GENERIC_") or sku.startswith("PDF_")


def _assisted_items_from_matches(
    matches: List[Tuple[Dict[str, Any], int]]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    detected_items: List[Dict[str, Any]] = []
    unmatched_rows: List[Dict[str, Any]] = []
    catalog_candidates: List[Dict[str, Any]] = []
    for item, qty in matches:
        name = item.get("nombre") or item.get("title") or item.get("sku") or "Producto"
        row = {
            "nombre": name,
            "cantidad": max(1, int(qty or 1)),
            "sku": item.get("sku"),
            "descripcion": item.get("descripcion"),
            "catalog_match": None,
        }
        if _is_placeholder_item(item):
            unmatched_rows.append({key: value for key, value in row.items() if key != "catalog_match" and value})
        else:
            row["catalog_match"] = _catalog_match_payload(item)
            catalog_candidates.append(
                {
                    "item": name,
                    "row": {"nombre": name, "cantidad": row["cantidad"], "sku": item.get("sku")},
                    "candidates": [row["catalog_match"]],
                }
            )
        detected_items.append(row)
    return detected_items, unmatched_rows, catalog_candidates


def _attachment_text_preview(attachment_info: Dict[str, Any], *extra_parts: Any) -> Optional[str]:
    parts: List[str] = []
    for value in (
        attachment_info.get("caption"),
        attachment_info.get("texto_extraido"),
        attachment_info.get("text"),
        attachment_info.get("description"),
        *extra_parts,
    ):
        cleaned = _clean_text(value)
        if cleaned:
            parts.append(cleaned)
    if not parts:
        return None
    return "\n".join(parts)[:500]


def _manual_review_unmatched_rows(
    attachment_info: Dict[str, Any],
    *,
    default_name: str,
    text_preview: Optional[str] = None,
) -> List[Dict[str, Any]]:
    name = _clean_text(
        attachment_info.get("name")
        or attachment_info.get("filename")
        or attachment_info.get("id")
        or default_name
    ) or default_name
    description = _clean_text(text_preview) or "Archivo recibido para revision manual."
    return [
        {
            "nombre": name,
            "cantidad": 1,
            "descripcion": description[:280],
            "status": "needs_operator_review",
        }
    ]


def _assisted_intake_experience(
    *,
    source_channel: str,
    matched_count: int,
    unmatched_count: int,
    detected_count: int,
    extraction_error: Optional[str] = None,
) -> Dict[str, Any]:
    needs_review = unmatched_count > 0 or matched_count == 0 or bool(extraction_error)
    return {
        "contract_version": ASSISTED_INTAKE_CONTRACT_VERSION,
        "render_as": "anonymous_assisted_marketplace_intake",
        "title": "Pedido asistido por foto, papel o texto",
        "summary": "Fotos, PDFs o notas de pedido quedan convertidas en una solicitud lista para revisar.",
        "anonymous_intake": True,
        "source_channel": source_channel,
        "catalog_matching": True,
        "needs_operator_review": needs_review,
        "pipeline": [
            {
                "id": "capture",
                "label": "Archivo recibido",
                "description": "Entrada desde WhatsApp, widget o marketplace.",
                "status": "done",
            },
            {
                "id": "ai_parse",
                "label": "Lectura automatica",
                "description": "La imagen o documento se transforma en renglones de pedido.",
                "status": "warning" if extraction_error else ("done" if detected_count else "pending_review"),
            },
            {
                "id": "catalog_match",
                "label": "Cruce con catalogo",
                "description": "Propone productos del catalogo, alternativas y faltantes.",
                "status": "done" if matched_count else "pending_review",
            },
            {
                "id": "crm_handoff",
                "label": "Listo para el equipo",
                "description": "El equipo recibe tareas, contexto y proximo paso sugerido.",
                "status": "pending_review" if needs_review else "ready",
            },
        ],
        "crm_handoff": {
            "label": "Solicitud lista para seguimiento",
            "recommended_next_action": "revisar_y_responder" if needs_review else "confirmar_stock_precio_y_enviar",
            "channels": ["whatsapp", "chat_widget", "email", "phone", "crm"],
        },
        "frontend_contract": {
            "render_as": "marketplace_assisted_intake",
            "primary_cta": "Subir foto o papel",
            "secondary_cta": "Escribir pedido",
            "show_on_empty_catalog": True,
        },
    }


def _assisted_next_actions(
    *,
    pedido_id: int,
    tenant_slug: Optional[str],
    matched_count: int,
    unmatched_count: int,
) -> List[Dict[str, Any]]:
    tenant_path = f"/t/{tenant_slug}" if tenant_slug else ""
    actions = [
        {
            "id": "operator_review",
            "label": "Revisar en panel",
            "type": "crm",
            "description": "Validar lectura, stock, precio y datos de contacto.",
            "enabled": True,
        },
        {
            "id": "complete_by_chat",
            "label": "Responder por canal",
            "type": "handoff",
            "description": "Continuar por WhatsApp, widget, email o telefono.",
            "enabled": True,
        },
        {
            "id": "open_marketplace",
            "label": "Abrir marketplace",
            "type": "link",
            "href": f"{tenant_path}/market" if tenant_path else "/market",
            "description": "Mostrar catalogo para completar o reemplazar articulos.",
            "enabled": True,
        },
        {
            "id": "tracking",
            "label": "Seguimiento",
            "type": "reference",
            "reference": f"pedido:{pedido_id}",
            "description": "Referencia para historial y seguimiento por canal.",
            "enabled": True,
        },
    ]
    if unmatched_count:
        actions.insert(
            1,
            {
                "id": "review_unmatched_items",
                "label": "Resolver faltantes",
                "type": "crm_task",
                "description": "Hay articulos que no quedaron asociados con seguridad al catalogo.",
                "enabled": True,
            },
        )
    return actions


def _assisted_public_follow_up(
    *,
    pedido_id: int,
    tenant_slug: Optional[str],
    request_kind_label: str,
    customer_message: str,
) -> Dict[str, Any]:
    tracking_code = f"pc-{pedido_id}"
    tracking_token = secrets.token_urlsafe(24)
    tracking_path = f"/tracking/order/{quote_plus(tracking_code)}"
    tracking_api = f"/api/public/tracking/experience?kind=order&code={quote_plus(tracking_code)}"
    if tenant_slug:
        encoded_tenant = quote_plus(str(tenant_slug))
        tracking_path = f"{tracking_path}?tenant_slug={encoded_tenant}#token={quote_plus(tracking_token)}"
        tracking_api = f"{tracking_api}&tenant_slug={encoded_tenant}"
    else:
        tracking_path = f"{tracking_path}#token={quote_plus(tracking_token)}"
    return {
        "contract_version": "marketplace.assisted_followup.v1",
        "kind": "order",
        "title": f"Seguimiento de {request_kind_label}",
        "summary": customer_message,
        "tracking": {
            "kind": "order",
            "code": tracking_code,
            "raw_code": tracking_code,
            "pedido_id": pedido_id,
            "path": tracking_path,
            "api_endpoint": tracking_api,
            "token": tracking_token,
            "token_required": True,
            "access": "signed_link",
            "credential_transport": "x-tracking-token-header",
            "label": "Seguimiento publico",
        },
        "channels": [
            {
                "id": "tracking_page",
                "label": "Abrir seguimiento",
                "type": "link",
                "href": tracking_path,
                "description": "Consultar estado y continuidad de la solicitud.",
                "enabled": True,
            }
        ],
    }


def _assisted_ticket_fingerprint(
    *,
    tenant_id: Optional[int],
    pedido_id: Optional[int],
    request_id: Optional[str],
    source_channel: str,
) -> Optional[str]:
    if not tenant_id:
        return None
    seed = f"{tenant_id}:{request_id or f'pedido:{pedido_id}'}:{source_channel or 'web'}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:18]
    return f"pyme_assisted_upload:{tenant_id}:{digest}"[:120]


def _materialize_assisted_intake_ticket(
    *,
    pedido: PedidoConversacional,
    tenant_id: Optional[int],
    user_id: Optional[int],
    source_channel: str,
    source: Dict[str, Any],
    contact: Dict[str, Any],
    match_summary: Dict[str, Any],
    crm_order_draft: Dict[str, Any],
    review_context: Dict[str, Any],
    intake_experience: Dict[str, Any],
    operator_pack: Dict[str, Any],
    public_follow_up: Dict[str, Any],
    request_kind_label: str,
    request_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    fingerprint = _assisted_ticket_fingerprint(
        tenant_id=tenant_id,
        pedido_id=getattr(pedido, "id", None),
        request_id=request_id,
        source_channel=source_channel,
    )
    if not tenant_id or not fingerprint:
        return None

    existing = TenantTicket.query.filter_by(tenant_id=tenant_id, fingerprint=fingerprint).first()
    if existing:
        return {
            "kind": "tenant_ticket",
            "target_module": "orders",
            "id": existing.id,
            "ticket_id": existing.id,
            "ticket_type": "tenant_ticket",
            "status": existing.estado,
            "category": existing.categoria,
            "admin_thread_binding": "tenant_ticket_id",
            "fingerprint": existing.fingerprint,
            "reused": True,
        }

    detected = int(match_summary.get("detected") or 0)
    matched = int(match_summary.get("matched") or 0)
    unmatched = int(match_summary.get("unmatched") or 0)
    needs_review = bool(match_summary.get("needs_operator_review"))
    source_name = source.get("archivo_nombre") or source.get("name") or source.get("filename")
    title = f"{request_kind_label.capitalize()} desde {source_channel.replace('_', ' ')}"
    description_parts = [
        title,
        f"Estado: {'requiere revision' if needs_review else 'listo para confirmar'}",
        f"Lectura: {detected} detectado(s), {matched} con match, {unmatched} para revisar.",
    ]
    if source_name:
        description_parts.append(f"Origen: {source_name}")
    if contact.get("name") or contact.get("phone") or contact.get("email"):
        description_parts.append(
            "Contacto: "
            + ", ".join(str(value) for value in (contact.get("name"), contact.get("phone"), contact.get("email")) if value)
        )
    source_attachment = (
        source.get("attachmentInfo")
        or source.get("attachment_info")
        or source.get("source_attachment")
        or None
    )
    initial_comments: List[Dict[str, Any]] = []
    if isinstance(source_attachment, dict):
        attachment_name = (
            source_attachment.get("name")
            or source_attachment.get("filename")
            or source_name
            or "archivo recibido"
        )
        initial_comments.append(
            {
                "id": 1,
                "body": f"Adjunto recibido para revisar pedido: {attachment_name}",
                "visibility": "public",
                "author_user_id": user_id,
                "created_at": datetime.utcnow().isoformat(),
                "attachmentInfo": source_attachment,
            }
        )

    ticket = TenantTicket(
        tenant_id=tenant_id,
        user_id=user_id,
        categoria="marketplace_assisted_order",
        descripcion="\n".join(description_parts),
        estado="nuevo",
        origen=str(source_channel or "web")[:20],
        fingerprint=fingerprint,
        datos_extra={
            "contract_version": "marketplace.commerce_intake_ticket.v1",
            "title": title,
            "type": "commerce_assisted_intake",
            "priority": operator_pack.get("priority") or ("high" if needs_review else "normal"),
            "channel": source_channel,
            "source": "pyme_multimodal",
            "pedido_conversacional_id": getattr(pedido, "id", None),
            "pedido_reference": f"pedido:{getattr(pedido, 'id', None)}",
            "request_kind": "order_note",
            "request_kind_label": request_kind_label,
            "target_module": "orders",
            "crm_state": "pending_operator_review" if needs_review else "ready_for_confirmation",
            "contact": contact,
            "lead_profile": {
                "contact_state": "available" if contact.get("phone") or contact.get("email") else "missing",
                "contact_channels": [
                    channel for channel in ("phone", "email") if contact.get(channel)
                ],
                "anon_id": source.get("anon_id"),
                "request_id": request_id,
            },
            "match_summary": match_summary,
            "crm_order_draft": crm_order_draft,
            "review_context": review_context,
            "intake_experience": intake_experience,
            "operator_pack": operator_pack,
            "public_follow_up": public_follow_up,
            "source_attachment": source.get("attachmentInfo")
            or source.get("attachment_info")
            or source.get("source_attachment"),
            "attachmentInfo": source_attachment,
            "attachment_info": source_attachment,
            "attachments": [source_attachment] if isinstance(source_attachment, dict) else [],
            "comments": initial_comments,
        },
    )
    db.session.add(ticket)
    db.session.flush()
    return {
        "kind": "tenant_ticket",
        "target_module": "orders",
        "id": ticket.id,
        "ticket_id": ticket.id,
        "ticket_type": "tenant_ticket",
        "status": ticket.estado,
        "category": ticket.categoria,
        "admin_thread_binding": "tenant_ticket_id",
        "fingerprint": ticket.fingerprint,
    }


def _operator_pack(
    *,
    record_id: Optional[int],
    request_kind_label: str,
    contact: Dict[str, Any],
    match_summary: Dict[str, Any],
    unmatched_items: List[Any],
    extraction_error: Optional[str],
) -> Dict[str, Any]:
    try:
        from services.commerce_unified import _build_assisted_operator_pack

        return _build_assisted_operator_pack(
            record_id=record_id,
            request_kind_label=request_kind_label,
            contact=contact,
            match_summary=match_summary,
            unmatched_items=unmatched_items,
            extraction_error=extraction_error,
        )
    except Exception:
        priority = "high" if match_summary.get("needs_operator_review") else "normal"
        return {
            "priority": priority,
            "reference": f"pedido:{record_id}" if record_id else None,
            "needs_human_review": priority == "high",
            "suggested_reply": "Recibimos tu pedido por archivo. Lo revisamos y te respondemos por este canal.",
            "suggested_tasks": [],
            "contact_links": [],
        }


def _persist_assisted_intake_request(
    *,
    state: PymeSessionState,
    owner_user_id: Optional[int],
    viewer_user_id: Optional[int],
    tenant_id: Optional[int],
    tenant_slug: Optional[str],
    channel: Optional[str],
    source_type: str,
    attachment_info: Dict[str, Any],
    detected_items: List[Dict[str, Any]],
    unmatched_rows: List[Dict[str, Any]],
    catalog_candidates: List[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
    anon_id: Optional[str] = None,
    request_id: Optional[str] = None,
    extraction_error: Optional[str] = None,
    text_preview: Optional[str] = None,
) -> Dict[str, Any]:
    matched_count = sum(1 for item in detected_items if item.get("catalog_match"))
    unmatched_count = len(unmatched_rows)
    detected_count = len(detected_items)
    source_channel = _assisted_channel(channel)
    contact = _assisted_contact_payload(context, anon_id)
    source = _assisted_source_payload(
        source_type=source_type,
        channel=source_channel,
        attachment_info=attachment_info,
        text_preview=text_preview,
    )
    match_summary = {
        "matched": matched_count,
        "unmatched": unmatched_count,
        "detected": detected_count,
        "needs_operator_review": unmatched_count > 0 or matched_count == 0 or bool(extraction_error),
    }
    unmatched_labels = [
        str(row.get("nombre") or row.get("sku") or row.get("descripcion") or row)
        for row in unmatched_rows
        if row
    ]
    intake_experience = _assisted_intake_experience(
        source_channel=source_channel,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        detected_count=detected_count,
        extraction_error=extraction_error,
    )
    request_kind_label = "pedido por adjunto"
    crm_state = "pending_operator_review" if match_summary["needs_operator_review"] else "ready_for_confirmation"
    review_context = {
        "summary": "Solicitud creada desde archivo o imagen enviada por el cliente.",
        "recommended_channels": ["whatsapp", "chat_widget", "email", "phone", "crm"],
        "operator_goal": "convertir_a_pedido_o_cotizacion",
        "needs_operator_review": match_summary["needs_operator_review"],
    }
    customer_message = (
        f"Recibimos tu {request_kind_label}. Detectamos {detected_count} renglon(es): "
        f"{matched_count} asociado(s) al catalogo y {unmatched_count} para revisar."
    )
    customer_next_steps = [
        {
            "id": "operator_review",
            "label": "Revision del equipo",
            "description": "Un operador valida faltantes, stock y precio antes de responder.",
            "status": "pending_review" if match_summary["needs_operator_review"] else "ready",
        },
        {
            "id": "reply",
            "label": "Respuesta por canal",
            "description": "La respuesta puede continuar por WhatsApp, widget, email o telefono.",
            "status": "pending",
        },
    ]
    next_actions = _assisted_next_actions(
        pedido_id=0,
        tenant_slug=tenant_slug,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
    )
    operator_pack = _operator_pack(
        record_id=None,
        request_kind_label=request_kind_label,
        contact=contact,
        match_summary=match_summary,
        unmatched_items=unmatched_labels,
        extraction_error=extraction_error,
    )
    crm_order_draft = build_crm_order_draft(
        request_kind="order_note",
        request_kind_label=request_kind_label,
        source=source,
        contact=contact,
        matched_items=[item for item in detected_items if item.get("catalog_match")],
        unmatched_items=unmatched_rows,
        catalog_candidates=catalog_candidates,
        match_summary=match_summary,
    )
    assisted_request = {
        "contract_version": ASSISTED_REQUEST_CONTRACT_VERSION,
        "mode": "order_note_upload",
        "request_kind": "order_note",
        "request_kind_label": request_kind_label,
        "crm_state": crm_state,
        "source": source,
        "contact": contact,
        "match_summary": match_summary,
        "crm_order_draft": crm_order_draft,
        "review_context": review_context,
        "detected_items": detected_items,
        "unmatched_items": unmatched_labels,
        "raw_unmatched_rows": unmatched_rows,
        "catalog_candidates": catalog_candidates,
        "customer_message": customer_message,
        "customer_next_steps": customer_next_steps,
        "intake_experience": intake_experience,
        "operator_pack": operator_pack,
        "next_actions": next_actions,
        "request_id": request_id,
    }

    if not owner_user_id or not tenant_id:
        assisted_request["persistence_skipped"] = "missing_owner_or_tenant"
        return assisted_request

    record_payload = {
        "archivo_url": source.get("archivo_url"),
        "archivo_nombre": source.get("archivo_nombre"),
        "request_kind": "order_note",
        "request_kind_label": request_kind_label,
        "contact": contact,
        "items_detectados": detected_items,
        "no_encontrados": unmatched_rows,
        "no_encontrados_labels": unmatched_labels,
        "catalog_candidates": catalog_candidates,
        "origen": source_channel,
        "contract_version": ASSISTED_REQUEST_CONTRACT_VERSION,
        "review_context": review_context,
        "match_summary": match_summary,
        "crm_order_draft": crm_order_draft,
        "customer_message": customer_message,
        "customer_next_steps": customer_next_steps,
        "intake_experience": intake_experience,
        "operator_pack": operator_pack,
        "extraction_error": extraction_error,
        "request_id": request_id,
    }
    pedido = PedidoConversacional(
        tenant_id=tenant_id,
        user_id=viewer_user_id or owner_user_id,
        tipo="nota_de_pedido",
        estado="nuevo",
        items=[record_payload],
        monto_monetario=state.cart.get("total") or state.cart.get("subtotal") or 0,
        monto_puntos=0,
        anon_id=anon_id,
        origen=source_channel,
        metadata_payload={
            "contract_version": ASSISTED_REQUEST_CONTRACT_VERSION,
            "mode": "order_note_upload",
            "request_kind": "order_note",
            "request_kind_label": request_kind_label,
            "crm_state": crm_state,
            "source": source,
            "contact": contact,
            "match_summary": match_summary,
            "crm_order_draft": crm_order_draft,
            "review_context": review_context,
            "catalog_candidates": catalog_candidates,
            "customer_next_steps": customer_next_steps,
            "intake_experience": intake_experience,
            "operator_pack": operator_pack,
            "next_actions": next_actions,
            "request_id": request_id,
        },
    )
    db.session.add(pedido)
    db.session.flush()
    public_follow_up = _assisted_public_follow_up(
        pedido_id=pedido.id,
        tenant_slug=tenant_slug,
        request_kind_label=request_kind_label,
        customer_message=customer_message,
    )
    for action in next_actions:
        if action.get("id") == "tracking":
            action["reference"] = f"pedido:{pedido.id}"
            action["type"] = "link"
            action["href"] = public_follow_up["tracking"]["path"]
            action["tracking_code"] = public_follow_up["tracking"]["code"]
            action["tracking_kind"] = public_follow_up["tracking"]["kind"]
    operator_pack = _operator_pack(
        record_id=pedido.id,
        request_kind_label=request_kind_label,
        contact=contact,
        match_summary=match_summary,
        unmatched_items=unmatched_labels,
        extraction_error=extraction_error,
    )
    crm_order_draft = {
        **crm_order_draft,
        "pedido_id": pedido.id,
        "lead_id": pedido.id,
        "reference": f"pedido:{pedido.id}",
    }
    linked_record = _materialize_assisted_intake_ticket(
        pedido=pedido,
        tenant_id=tenant_id,
        user_id=viewer_user_id or owner_user_id,
        source_channel=source_channel,
        source=source,
        contact=contact,
        match_summary=match_summary,
        crm_order_draft=crm_order_draft,
        review_context=review_context,
        intake_experience=intake_experience,
        operator_pack=operator_pack,
        public_follow_up=public_follow_up,
        request_kind_label=request_kind_label,
        request_id=request_id,
    )
    assisted_request.update(
        {
            "pedido_id": pedido.id,
            "lead_id": pedido.id,
            "operator_pack": operator_pack,
            "next_actions": next_actions,
            "crm_order_draft": crm_order_draft,
            "public_follow_up": public_follow_up,
        }
    )
    if linked_record:
        assisted_request.update(
            {
                "linked_record": linked_record,
                "intake_ticket_id": linked_record.get("id"),
                "ticket_id": linked_record.get("id"),
                "ticket_type": "tenant_ticket",
                "crm_state": "materialized_ticket_pending_review"
                if match_summary["needs_operator_review"]
                else "materialized_ticket_ready_for_confirmation",
            }
        )
    pedido.metadata_payload = {
        **(pedido.metadata_payload or {}),
        "operator_pack": operator_pack,
        "next_actions": next_actions,
        "crm_order_draft": crm_order_draft,
        "public_follow_up": public_follow_up,
    }
    if linked_record:
        pedido.metadata_payload = {
            **(pedido.metadata_payload or {}),
            "linked_record": linked_record,
            "intake_ticket_id": linked_record.get("id"),
            "crm_state": assisted_request["crm_state"],
        }
    pedido.items = [
        {
            **record_payload,
            "operator_pack": operator_pack,
            "crm_order_draft": crm_order_draft,
            "public_follow_up": public_follow_up,
            **({"linked_record": linked_record} if linked_record else {}),
        },
        *pedido.items[1:],
    ]
    db.session.commit()
    if tenant_id:
        tenant = db.session.get(TenantProfile, tenant_id)
        if tenant:
            track_marketplace_event(
                tenant,
                "assisted_multimodal_intake_submitted",
                {
                    "source": "pyme_multimodal",
                    "assisted_request_contract_version": ASSISTED_REQUEST_CONTRACT_VERSION,
                    "intake_ticket_contract_version": "marketplace.commerce_intake_ticket.v1"
                    if linked_record
                    else None,
                    "mode": "order_note_upload",
                    "request_kind": "order_note",
                    "request_kind_label": request_kind_label,
                    "target_module": "orders",
                    "input_type": source_type,
                    "has_file": bool(source.get("archivo_url") or source.get("attachment_id")),
                    "matched_count": matched_count,
                    "unmatched_count": unmatched_count,
                    "detected_count": detected_count,
                    "needs_operator_review": match_summary.get("needs_operator_review"),
                    "crm_state": pedido.metadata_payload.get("crm_state"),
                    "linked_record_type": linked_record.get("kind") if linked_record else None,
                    "linked_record_id": linked_record.get("id") if linked_record else None,
                },
                channel=source_channel,
                anon_id=anon_id,
                entity_ref=f"pedido:{pedido.id}",
                user_id=viewer_user_id or owner_user_id,
            )
    return assisted_request


def analyse_image_for_products(image_url: str) -> List[Dict[str, Any]]:
    if not image_url:
        return []
    try:
        prompt = (
            "Analiza esta imagen. Si es una lista de pedido manuscrita o impresa, extrae los productos y cantidades en JSON. "
            "Si es una etiqueta de producto, extrae nombre, marca y detalles. "
            "Si es un documento médico o técnico, extrae el concepto principal. "
            "Formato: {\"items\": [{\"nombre\": \"...\", \"cantidad\": 1, \"descripcion\": \"...\"}]}"
        )
        result = analizar_imagen_con_fallback(
            image_url,
            prompt,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("OpenAI vision fallback failed: %s", exc)
        return []

    if not isinstance(result, dict):
        return []
    items = result.get("items") or result.get("productos") or []
    normalised: List[Dict[str, Any]] = []
    for raw in items:
        if isinstance(raw, dict):
            normalised.append(raw)
    return normalised


def handle_image_payload(
    state: PymeSessionState,
    image_info: Dict[str, Any],
    catalog: Iterable[Dict[str, Any]],
    *,
    request_id: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    viewer_user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    tenant_slug: Optional[str] = None,
    channel: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    anon_id: Optional[str] = None,
) -> Optional[PymeFlowResult]:
    image_url = image_info.get("url")
    detected = analyse_image_for_products(image_url)

    if not detected:
        text_preview = _attachment_text_preview(image_info)
        unmatched_rows = _manual_review_unmatched_rows(
            image_info,
            default_name="Imagen de pedido para revisar",
            text_preview=text_preview,
        )
        assisted_request = _persist_assisted_intake_request(
            state=state,
            owner_user_id=owner_user_id or state.pyme_id,
            viewer_user_id=viewer_user_id,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            channel=channel or "web",
            source_type="image_manual_review",
            attachment_info=image_info,
            detected_items=[],
            unmatched_rows=unmatched_rows,
            catalog_candidates=[],
            context=context,
            anon_id=anon_id,
            request_id=request_id,
            extraction_error="imagen_sin_lectura",
            text_preview=text_preview,
        )
        return PymeFlowResult(
            message_body="Recibí la imagen pero no pude leer el contenido claramente. ¿Podrías decirme qué necesitás?",
            source="pyme_imagen_revision_manual",
            data={
                "assisted_request": assisted_request,
                "pedido_id": assisted_request.get("pedido_id"),
                "lead_id": assisted_request.get("lead_id"),
            },
        )

    matched: List[Tuple[Dict[str, Any], int]] = []
    items_to_confirm: List[str] = []

    for candidate in detected:
        raw_name = candidate.get("nombre") or candidate.get("sku") or "Producto desconocido"
        qty = int(candidate.get("cantidad") or candidate.get("qty") or 1)

        # 1. Try deterministic/exact match first (fastest/safest)
        catalog_match = match_catalog_items(raw_name, catalog, max_results=1)

        # 2. If no exact match, try Semantic Vector Search (Qdrant)
        if not catalog_match and state.pyme_id:
            try:
                hits = buscar_catalogo_qdrant(
                    user_id=state.pyme_id,
                    pregunta=raw_name,
                    limite=1,
                    coleccion=CATALOGO_PYME
                )
                if hits:
                    payload = getattr(hits[0], "payload", {})
                    # Convert payload back to serialised format used by this module
                    item_qdrant = {
                        "sku": payload.get("sku") or payload.get("nombre"),
                        "nombre": payload.get("nombre"),
                        "descripcion": payload.get("descripcion"),
                        "precio": _parse_price(payload.get("precio_str") or payload.get("precio")),
                        "moneda": "ARS",
                        "presentacion": payload.get("cantidad") or "unidad",
                        # We trust semantic match score > 0.8 usually, but here we take top 1
                    }
                    catalog_match = [item_qdrant]
                    logger.info(f"[PYME_MULTIMODAL] Qdrant match for '{raw_name}' -> '{item_qdrant.get('nombre')}'")
            except Exception as e:
                logger.warning(f"[PYME_MULTIMODAL] Qdrant search failed for '{raw_name}': {e}")

        if catalog_match:
            item = catalog_match[0]
            matched.append((item, qty))
            items_to_confirm.append(f"- {qty} x {item.get('nombre')} (${_format_money(_parse_price(item.get('precio')))})")
        else:
            # 3. Item truly not found: add as generic placeholder
            placeholder_item = {
                "sku": f"GENERIC_{_normalise(raw_name)[:10].replace(' ', '_')}",
                "nombre": raw_name,
                "precio": 0.0,
                "moneda": "ARS",
                "descripcion": candidate.get("descripcion", "Item detectado en imagen"),
                "presentacion": "unidad"
            }
            matched.append((placeholder_item, qty))
            items_to_confirm.append(f"- {qty} x {raw_name} (Precio a confirmar)")

    if not matched:
        return PymeFlowResult(
            message_body="Entendí la lista pero no encontré coincidencias exactas en el catálogo. ¿Te gustaría que lo revise un humano?",
            source="pyme_imagen_catalogo_sin_match",
        )

    # Add to cart tentatively
    logger.info(
        "[PYME_FLOW] image_order_extracted",
        extra={
            "request_id": request_id,
            "intent": "imagen_pedido",
            "matches": [
                {"sku": item.get("sku"), "qty": qty} for item, qty in matched
            ],
        },
    )

    add_items_to_cart(state, matched)
    detected_items, unmatched_rows, catalog_candidates = _assisted_items_from_matches(matched)
    assisted_request = _persist_assisted_intake_request(
        state=state,
        owner_user_id=owner_user_id or state.pyme_id,
        viewer_user_id=viewer_user_id,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        channel=channel or "web",
        source_type="image_order_note",
        attachment_info=image_info,
        detected_items=detected_items,
        unmatched_rows=unmatched_rows,
        catalog_candidates=catalog_candidates,
        context=context,
        anon_id=anon_id,
        request_id=request_id,
    )

    msg = "Leí tu pedido de la imagen:\n\n" + "\n".join(items_to_confirm) + "\n\n¿Es correcto? Confirmame para procesarlo."

    return PymeFlowResult(
        message_body=msg,
        source="pyme_imagen_items_agregados",
        data={
            "assisted_request": assisted_request,
            "pedido_id": assisted_request.get("pedido_id"),
            "lead_id": assisted_request.get("lead_id"),
        },
        options_list=[
            {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
            {"texto": "Modificar", "action_id": "ver_carrito_pyme"},
        ],
    )


def handle_pdf_payload(
    state: PymeSessionState,
    pdf_info: Dict[str, Any],
    catalog: Iterable[Dict[str, Any]],
    *,
    request_id: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    viewer_user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    tenant_slug: Optional[str] = None,
    channel: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    anon_id: Optional[str] = None,
) -> PymeFlowResult:
    extracted_items: List[Dict[str, Any]] = []
    text_blocks: List[str] = []

    analysis_data = pdf_info.get("analysis") if isinstance(pdf_info.get("analysis"), dict) else {}
    if analysis_data:
        extracted_items.extend(analysis_data.get("items") or analysis_data.get("productos") or [])
        texto = analysis_data.get("texto_extraido") or analysis_data.get("text")
        if texto:
            text_blocks.append(texto)

    processing_result: Optional[Dict[str, Any]] = None
    if not analysis_data and pdf_info.get("id"):
        try:
            processing_result = document_processing_service.process_document_by_id(pdf_info["id"])
        except Exception as exc:  # pragma: no cover - logged for observability
            logger.warning("Error procesando PDF %s: %s", pdf_info.get("id"), exc)
        else:
            if isinstance(processing_result, dict):
                data = (
                    processing_result.get("extracted_data")
                    or processing_result.get("data")
                    or processing_result
                )
                if isinstance(data, dict):
                    extracted_items.extend(data.get("items") or data.get("productos") or [])
                    texto = data.get("texto_extraido") or data.get("text")
                    if texto:
                        text_blocks.append(texto)

    if pdf_info.get("texto_extraido") and isinstance(pdf_info.get("texto_extraido"), str):
        text_blocks.append(pdf_info["texto_extraido"])

    matches: List[Tuple[Dict[str, Any], int]] = []
    items_to_confirm: List[str] = []

    # First pass: structured items from PDF analysis
    for raw_item in extracted_items:
        if not isinstance(raw_item, dict):
            continue
        raw_name = raw_item.get("nombre") or raw_item.get("sku")
        if not raw_name:
            continue
        qty = int(raw_item.get("cantidad") or raw_item.get("qty") or 1)

        catalog_match = match_catalog_items(raw_name, catalog, max_results=1)
        if catalog_match:
            item = catalog_match[0]
            matches.append((item, max(1, qty)))
            items_to_confirm.append(f"- {qty} x {item.get('nombre')} (${_format_money(_parse_price(item.get('precio')))})")
        else:
             # Add generic placeholder for unknown items in PDF
            placeholder_item = {
                "sku": f"PDF_{raw_name[:10].replace(' ', '_')}",
                "nombre": raw_name,
                "precio": 0.0,
                "moneda": "ARS",
                "descripcion": "Item detectado en PDF",
                "presentacion": "unidad"
            }
            matches.append((placeholder_item, qty))
            items_to_confirm.append(f"- {qty} x {raw_name} (Precio a confirmar)")

    # Second pass: Free text matching if structured extraction failed
    if not matches and text_blocks:
        for block in text_blocks:
            found = _match_catalog_from_free_text(block, catalog)
            for item, qty in found:
                matches.append((item, qty))
                items_to_confirm.append(f"- {qty} x {item.get('nombre')}")

    if not matches:
        text_preview = _attachment_text_preview(pdf_info, "\n".join(text_blocks))
        unmatched_rows = _manual_review_unmatched_rows(
            pdf_info,
            default_name="Archivo de pedido para revisar",
            text_preview=text_preview,
        )
        extraction_error = "pdf_sin_contenido" if not text_blocks and not extracted_items else "pdf_sin_match"
        assisted_request = _persist_assisted_intake_request(
            state=state,
            owner_user_id=owner_user_id or state.pyme_id,
            viewer_user_id=viewer_user_id,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            channel=channel or "web",
            source_type="pdf_manual_review",
            attachment_info=pdf_info,
            detected_items=[],
            unmatched_rows=unmatched_rows,
            catalog_candidates=[],
            context=context,
            anon_id=anon_id,
            request_id=request_id,
            extraction_error=extraction_error,
            text_preview=text_preview,
        )
        if not text_blocks and not extracted_items:
            return PymeFlowResult(
                message_body=(
                    "Recibí el archivo pero no pude leer el contenido. Si tenés otra versión "
                    "podés reenviarla o contarme qué necesitás."
                ),
                source="pyme_pdf_revision_manual",
                data={
                    "assisted_request": assisted_request,
                    "pedido_id": assisted_request.get("pedido_id"),
                    "lead_id": assisted_request.get("lead_id"),
                },
            )
        return PymeFlowResult(
            message_body=(
                "Analicé el documento pero no encontré coincidencias exactas en el catálogo. "
                "¿Me confirmás qué productos te interesan?"
            ),
            source="pyme_pdf_revision_manual",
            data={
                "assisted_request": assisted_request,
                "pedido_id": assisted_request.get("pedido_id"),
                "lead_id": assisted_request.get("lead_id"),
            },
        )

    logger.info(
        "[PYME_FLOW] pdf_order_extracted",
        extra={
            "request_id": request_id,
            "intent": "pdf_pedido",
            "matches": [
                {"sku": item.get("sku"), "qty": qty} for item, qty in matches if item.get("sku")
            ],
        },
    )
    add_items_to_cart(state, matches)
    detected_items, unmatched_rows, catalog_candidates = _assisted_items_from_matches(matches)
    assisted_request = _persist_assisted_intake_request(
        state=state,
        owner_user_id=owner_user_id or state.pyme_id,
        viewer_user_id=viewer_user_id,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        channel=channel or "web",
        source_type="pdf_order_note",
        attachment_info=pdf_info,
        detected_items=detected_items,
        unmatched_rows=unmatched_rows,
        catalog_candidates=catalog_candidates,
        context=context,
        anon_id=anon_id,
        request_id=request_id,
        text_preview="\n".join(text_blocks),
    )

    msg = "Procesé el pedido del archivo:\n\n" + "\n".join(items_to_confirm) + "\n\n¿Confirmamos?"

    return PymeFlowResult(
        message_body=msg,
        source="pyme_pdf_items_agregados",
        data={
            "assisted_request": assisted_request,
            "pedido_id": assisted_request.get("pedido_id"),
            "lead_id": assisted_request.get("lead_id"),
        },
        options_list=[
            {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
            {"texto": "Modificar", "action_id": "ver_carrito_pyme"},
        ],
    )


def build_default_context(owner_user: Any, rubro_slug: str) -> Dict[str, Any]:
    nombre_pyme = getattr(owner_user, "nombre_empresa", None) or getattr(owner_user, "name", None)
    config = cargar_configuracion_pyme(rubro_slug, "config.json") or {}
    if config.get("nombre_pyme"):
        nombre_pyme = config["nombre_pyme"]
    return {
        "nombre_pyme": nombre_pyme,
        "rubro_slug": rubro_slug,
        "config": config,
    }


def ensure_session_context(session: Optional[ChatSessionContext]) -> ChatSessionContext:
    if session is None:
        session = ChatSessionContext(context_data={})
    if session.context_data is None:
        session.context_data = {}
    return session


__all__ = [
    "PymeSessionState",
    "PymeFlowResult",
    "detect_intent_from_text",
    "handle_keyword_intent",
    "handle_location_payload",
    "persist_order",
    "render_cart_summary",
    "load_catalog",
    "handle_image_payload",
    "handle_pdf_payload",
    "build_default_context",
    "ensure_session_context",
]

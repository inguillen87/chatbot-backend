"""Helpers for the multimodal PYME experience.

This module centralises the lightweight keyword dispatcher, cart helpers and
media utilities that allow PYME conversations to reuse the same multimodal
pipeline already available for municipality chats.  The goal is to keep the
logic side-effect free so it can be unit tested without network access.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import current_app

from models import CatalogoItem, ChatSessionContext, PymePedido, db
from services.multimodal_analyzer import analizar_imagen_con_fallback
from services.pyme_menu import get_pyme_menu_payload
from services.config_loader import cargar_configuracion_pyme
from services.document_processing_service import document_processing_service

logger = logging.getLogger(__name__)


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
        cleaned = str(value).strip().replace("$", "").replace(",", "")
        return float(cleaned)
    except Exception:
        return 0.0


def _serialise_item(item: CatalogoItem) -> Dict[str, Any]:
    return {
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
    return [item for _, item in results[:max_results]]


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
        detail = f"- {qty} x {title}"
        if presentacion:
            detail += f" ({presentacion})"
        detail += f" — ${total:,.2f}"
        lines.append(detail)

    subtotal = state.cart.get("subtotal", 0.0)
    envio = state.delivery.get("shipping_total")
    total = state.cart.get("total", subtotal)

    lines.append(f"Subtotal productos: ${subtotal:,.2f}")
    if envio is not None:
        lines.append(f"Envío estimado: ${envio:,.2f}")
        total = subtotal + envio
    lines.append(f"Total estimado: ${total:,.2f}")
    return "\n".join(lines)


_QUANTITY_RE = re.compile(
    r"(?:^|\b)(\d{1,4})(?:\s*(?:x|unid(?:ad(?:es)?)?|caja(?:s)?|botellas?|pack|packs))?",
    re.IGNORECASE,
)


def _extract_quantity_from_text(text: str) -> int:
    match = _QUANTITY_RE.search(text)
    if not match:
        return 1
    try:
        value = int(match.group(1))
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
        return None

    max_score = max(scores.values())
    candidates = [intent for intent, score in scores.items() if score == max_score]
    candidates.sort(key=lambda intent: INTENT_PRIORITY.get(intent, 99))
    return candidates[0]


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
                f"• {item.get('nombre')} — ${_parse_price(item.get('precio')):,.2f} ({item.get('presentacion')})"
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
        if parsed_items:
            for parsed in parsed_items:
                nombre = parsed.get("nombre")
                cantidad = int(parsed.get("cantidad") or 1)
                if not nombre:
                    continue
                catalog_match = match_catalog_items(nombre, catalog, max_results=1)
                if catalog_match:
                    matches.append((catalog_match[0], cantidad))
        if not matches:
            fallback_matches = match_catalog_items(text, catalog)
            matches.extend((item, 1) for item in fallback_matches)
        if not matches:
            return PymeFlowResult(
                message_body=(
                    "No reconocí el producto en el catálogo. Podés pedirme, por ejemplo, "
                    "'Agregar 2 cajas de Malbec Reserva'."
                ),
                source="pyme_catalogo_sin_match",
            )
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
    request_id: Optional[str] = None,
) -> Optional[PymePedido]:
    if not state.cart.get("items") or not owner_user_id:
        return None

    detalles = {
        "items": state.cart.get("items"),
        "currency": state.cart.get("currency", "ARS"),
        "subtotal": state.cart.get("subtotal", 0.0),
        "total": state.cart.get("total", state.cart.get("subtotal", 0.0)),
        "delivery": state.delivery,
    }

    total = detalles["total"]

    pedido = PymePedido(
        owner_user_id,
        "Pedido generado desde el asistente",
        json.dumps(detalles, ensure_ascii=False),
        monto_total=total,
        user_id=viewer_user_id,
        direccion=state.delivery.get("address"),
        latitud=state.delivery.get("lat"),
        longitud=state.delivery.get("lng"),
    )
    db.session.add(pedido)
    db.session.commit()
    logger.info(
        "[PYME_FLOW] order_created",
        extra={
            "request_id": request_id,
            "pedido_id": getattr(pedido, "id", None),
            "nro_pedido": getattr(pedido, "nro_pedido", None),
            "total": total,
            "items": [
                {"sku": item.get("sku"), "qty": item.get("qty"), "unitPrice": item.get("unitPrice")}
                for item in state.cart.get("items", [])
            ],
        },
    )
    return pedido


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------


def analyse_image_for_products(image_url: str) -> List[Dict[str, Any]]:
    if not image_url:
        return []
    try:
        result = analizar_imagen_con_fallback(
            image_url,
            "Detecta productos de catálogo o etiquetas legibles. Devuelve JSON con 'items'.",
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
) -> Optional[PymeFlowResult]:
    image_url = image_info.get("url")
    detected = analyse_image_for_products(image_url)
    if not detected:
        return PymeFlowResult(
            message_body="No pude reconocer el producto de la foto. ¿Me confirmás el nombre?",
            source="pyme_imagen_sin_match",
        )

    matched: List[Tuple[Dict[str, Any], int]] = []
    for candidate in detected:
        sku = candidate.get("sku") or candidate.get("nombre")
        qty = int(candidate.get("qty") or candidate.get("cantidad") or 1)
        if not sku:
            continue
        catalog_match = match_catalog_items(sku, catalog, max_results=1)
        if catalog_match:
            matched.append((catalog_match[0], qty))

    if not matched:
        return PymeFlowResult(
            message_body="Recibí la foto pero no encuentro coincidencias en el catálogo. ¿La cargamos manualmente?",
            source="pyme_imagen_catalogo_sin_match",
        )

    logger.info(
        "[PYME_FLOW] catalog_hit",
        extra={
            "request_id": request_id,
            "intent": "imagen_catalogo",
            "matches": [
                {"sku": item.get("sku"), "qty": qty} for item, qty in matched if item.get("sku")
            ],
        },
    )
    cart_updates = add_items_to_cart(state, matched)
    if cart_updates:
        logger.info(
            "[PYME_FLOW] cart_updated",
            extra={
                "request_id": request_id,
                "updates": cart_updates,
                "subtotal": state.cart.get("subtotal"),
            },
        )
    summary = render_cart_summary(state)
    return PymeFlowResult(
        message_body=summary,
        source="pyme_imagen_items_agregados",
        options_list=[
            {"texto": "Pedir presupuesto", "action_id": "armar_presupuesto"},
            {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
        ],
    )


def handle_pdf_payload(
    state: PymeSessionState,
    pdf_info: Dict[str, Any],
    catalog: Iterable[Dict[str, Any]],
    *,
    request_id: Optional[str] = None,
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
    for raw_item in extracted_items:
        if not isinstance(raw_item, dict):
            continue
        sku = raw_item.get("sku") or raw_item.get("nombre")
        if not sku:
            continue
        qty = int(raw_item.get("qty") or raw_item.get("cantidad") or 1)
        catalog_match = match_catalog_items(sku, catalog, max_results=1)
        if catalog_match:
            matches.append((catalog_match[0], max(1, qty)))

    if not matches and text_blocks:
        for block in text_blocks:
            matches.extend(_match_catalog_from_free_text(block, catalog))

    if not matches:
        if not text_blocks and not extracted_items:
            return PymeFlowResult(
                message_body=(
                    "Recibí el archivo pero no pude leer el contenido. Si tenés otra versión "
                    "podés reenviarla o contarme qué necesitás."
                ),
                source="pyme_pdf_sin_contenido",
            )
        return PymeFlowResult(
            message_body=(
                "Analicé la lista de precios pero no encontré coincidencias exactas. ¿Me confirmás "
                "qué producto te interesa?"
            ),
            source="pyme_pdf_sin_match",
        )

    logger.info(
        "[PYME_FLOW] catalog_hit",
        extra={
            "request_id": request_id,
            "intent": "pdf_catalogo",
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

    summary = render_cart_summary(state)
    return PymeFlowResult(
        message_body=summary,
        source="pyme_pdf_items_agregados",
        options_list=[
            {"texto": "Pedir presupuesto", "action_id": "armar_presupuesto"},
            {"texto": "Confirmar pedido", "action_id": "confirmar_pedido"},
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


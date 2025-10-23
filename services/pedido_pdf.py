"""Utilities to generate printable order summaries for PyME pedidos."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # pragma: no cover - optional dependency handled at runtime
    from fpdf import FPDF
except Exception:  # pragma: no cover - optional dependency handled at runtime
    FPDF = None


logger = logging.getLogger(__name__)


def _to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort conversion to ``float`` with graceful fallback."""

    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive path
        return default


def _iter_detalles(detalles: Any) -> Iterable[Dict[str, Any]]:
    """Yield dict entries from ``detalles`` regardless of structure."""

    if isinstance(detalles, list):
        for item in detalles:
            if isinstance(item, dict):
                yield item
    elif isinstance(detalles, dict):
        # Some payloads wrap items in {"items": [...]}
        maybe_items: Sequence[Any] = []
        if "items" in detalles and isinstance(detalles["items"], list):
            maybe_items = detalles["items"]
        elif "detalles" in detalles and isinstance(detalles["detalles"], list):
            maybe_items = detalles["detalles"]
        else:
            maybe_items = list(detalles.values())

        for item in maybe_items:
            if isinstance(item, dict):
                yield item


def extraer_items_pedido(pedido) -> List[Dict[str, Any]]:
    """Return a normalized list of items for a ``PymePedido``-like object.

    The ``pedido.detalles`` field is expected to contain a JSON string with a
    list (or dict) of items. Each item is normalized to include ``nombre``,
    ``cantidad``, ``precio_unitario`` and ``subtotal`` keys, filling sensible
    defaults when the source data is incomplete.
    """

    raw = getattr(pedido, "detalles", None)
    if not raw:
        return []

    detalles_obj: Any
    try:
        detalles_obj = json.loads(raw)
    except (TypeError, ValueError):
        logger.debug("No se pudo parsear detalles del pedido como JSON.")
        return []

    items: List[Dict[str, Any]] = []
    for item in _iter_detalles(detalles_obj):
        nombre = (
            str(
                item.get("nombre")
                or item.get("producto")
                or item.get("descripcion")
                or item.get("sku")
                or "Item"
            )
        )
        cantidad = _to_float(
            item.get("cantidad")
            or item.get("qty")
            or item.get("cantidad_producto")
            or 1,
            1.0,
        )
        precio_unitario = _to_float(
            item.get("precio_unitario")
            or item.get("precio")
            or item.get("precioUnitario")
            or item.get("monto_unitario")
            or 0.0,
            0.0,
        )
        subtotal = item.get("subtotal")
        subtotal_float = _to_float(subtotal, precio_unitario * cantidad)

        items.append(
            {
                "nombre": nombre,
                "cantidad": cantidad,
                "precio_unitario": precio_unitario,
                "subtotal": subtotal_float,
            }
        )

    return items


def generar_pdf_nota_pedido(pedido, empresa_info: Optional[Dict[str, Any]] = None) -> bytes:
    """Generate a branded PDF summarizing a PyME order."""

    if FPDF is None:
        raise RuntimeError("fpdf2 library is required to generar_pdf_nota_pedido")

    items = extraer_items_pedido(pedido)
    total_calculado = sum(item["subtotal"] for item in items)
    monto_total = getattr(pedido, "monto_total", None)
    if monto_total is None or monto_total == 0:
        monto_total = total_calculado

    empresa_info = empresa_info or {}
    empresa_nombre = empresa_info.get("nombre") or getattr(pedido, "pyme_nombre", "")
    if not empresa_nombre:
        empresa_nombre = "Pedido"

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, empresa_nombre, ln=True)

    pdf.set_font("Helvetica", size=11)
    if empresa_info.get("direccion"):
        pdf.cell(0, 6, str(empresa_info["direccion"]), ln=True)
    contacto_line = []
    if empresa_info.get("telefono"):
        contacto_line.append(f"Tel: {empresa_info['telefono']}")
    if empresa_info.get("email"):
        contacto_line.append(f"Email: {empresa_info['email']}")
    if contacto_line:
        pdf.cell(0, 6, " ".join(contacto_line), ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, f"Nota de pedido #{getattr(pedido, 'nro_pedido', '')}", ln=True)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(
        0,
        6,
        f"Fecha: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}",
        ln=True,
    )

    nombre_cliente = getattr(pedido, "nombre_cliente", None)
    if nombre_cliente:
        pdf.cell(0, 6, f"Cliente: {nombre_cliente}", ln=True)
    if getattr(pedido, "email_cliente", None):
        pdf.cell(0, 6, f"Email cliente: {pedido.email_cliente}", ln=True)
    if getattr(pedido, "telefono_cliente", None):
        pdf.cell(0, 6, f"Teléfono cliente: {pedido.telefono_cliente}", ln=True)
    if getattr(pedido, "direccion", None):
        pdf.cell(0, 6, f"Dirección de entrega: {pedido.direccion}", ln=True)

    pdf.ln(4)
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(80, 8, "Producto", 1, 0, "C", True)
    pdf.cell(25, 8, "Cant.", 1, 0, "C", True)
    pdf.cell(35, 8, "Precio", 1, 0, "C", True)
    pdf.cell(40, 8, "Subtotal", 1, 1, "C", True)

    pdf.set_font("Helvetica", size=11)
    for item in items:
        if pdf.get_y() > 260:
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(80, 8, "Producto", 1, 0, "C", True)
            pdf.cell(25, 8, "Cant.", 1, 0, "C", True)
            pdf.cell(35, 8, "Precio", 1, 0, "C", True)
            pdf.cell(40, 8, "Subtotal", 1, 1, "C", True)
            pdf.set_font("Helvetica", size=11)

        nombre = item.get("nombre", "")[:40]
        cantidad = item.get("cantidad", 0)
        precio_unit = item.get("precio_unitario", 0)
        subtotal = item.get("subtotal", 0)

        pdf.cell(80, 8, nombre, 1)
        pdf.cell(25, 8, f"{cantidad:g}", 1, 0, "R")
        pdf.cell(35, 8, f"${precio_unit:,.2f}", 1, 0, "R")
        pdf.cell(40, 8, f"${subtotal:,.2f}", 1, 1, "R")

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 10, f"Total: ${monto_total:,.2f}", ln=True)

    notas = getattr(pedido, "asunto", None)
    if notas:
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 6, f"Notas: {notas}")

    return pdf.output(dest="S").encode("latin1")


__all__ = ["extraer_items_pedido", "generar_pdf_nota_pedido", "FPDF"]


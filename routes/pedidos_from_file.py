import io
import logging
from typing import Dict, List

import pandas as pd
from flask import Blueprint, jsonify, request, session, g
from sqlalchemy import func

from database import db
from models import CatalogoItem
from routes.catalogo import _formatear_producto
from routes.productos import _resolve_public_owner
from services.cart import _get_pyme_cart
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file

logger = logging.getLogger(__name__)

pedidos_from_file_bp = Blueprint("pedidos_from_file_bp", __name__, url_prefix="/api/pedidos")

_PROMPT = """
Identificá productos y cantidades del documento. Devuelve JSON {"items": [{"sku": "...", "nombre": "...", "cantidad": 1}]}.
"""


def _normalize_items(owner_id: int, tenant_id: int, rows: List[dict]):
    pyme_carts_data: Dict[int, list] = session.get("carritos_pymes", {})
    cart = _get_pyme_cart(pyme_carts_data, tenant_id)
    not_found: List[dict] = []

    query = CatalogoItem.query.filter(
        CatalogoItem.user_id == owner_id,
        func.coalesce(CatalogoItem.tenant_id, tenant_id) == tenant_id,
    )
    for row in rows:
        cantidad = int(row.get("cantidad") or 1)
        sku = row.get("sku")
        nombre = row.get("nombre")
        match = None
        if sku:
            match = query.filter(func.lower(CatalogoItem.sku) == sku.lower()).first()
        if not match and nombre:
            match = query.filter(func.lower(CatalogoItem.nombre) == nombre.lower()).first()
        if match:
            cart.append({"catalogo_item_id": match.id, "cantidad": cantidad})
        else:
            not_found.append(row)
    session["carritos_pymes"] = pyme_carts_data
    session.modified = True
    return cart, not_found


@pedidos_from_file_bp.route("/from-file", methods=["POST"])
def pedidos_desde_archivo():
    archivo = request.files.get("archivo")
    if not archivo:
        return jsonify({"error": "Archivo requerido"}), 400

    tenant_slug = request.headers.get("X-Tenant") or request.args.get("tenant")
    try:
        tenant, owner, _ = resolve_tenant_and_user(
            tenant_slug=tenant_slug, current_user=getattr(g, "user", None)
        )
    except TenantResolutionError:
        tenant, owner = _resolve_public_owner()
        if not tenant or not owner:
            return jsonify({"error": "Tenant no encontrado"}), 404

    contenido = archivo.read()
    rows = extract_table_from_file(contenido, _PROMPT)
    if rows is None:
        try:
            df = pd.read_excel(io.BytesIO(contenido))
        except Exception:
            df = pd.read_csv(io.BytesIO(contenido))
        rows = df.to_dict(orient="records")

    cart, not_found = _normalize_items(owner.id, tenant.id, rows or [])

    # Enriquecer respuesta reutilizando formateador existente
    item_ids = [it.get("catalogo_item_id") for it in cart if it.get("catalogo_item_id")]
    enriched = []
    if item_ids:
        items = CatalogoItem.query.filter(CatalogoItem.id.in_(item_ids)).all()
        mapping = {item.id: item for item in items}
        for it in cart:
            item = mapping.get(it.get("catalogo_item_id"))
            if not item:
                continue
            formatted = _formatear_producto(
                {
                    "nombre": item.nombre,
                    "descripcion": item.descripcion,
                    "precio_str": item.precio,
                    "precio_float": float(item.precio_monetario) if item.precio_monetario is not None else None,
                    "sku": item.sku,
                    "unidad": item.unidad,
                    "imagen_url": item.imagen_url,
                }
            )
            enriched.append({"catalogo_item_id": item.id, "cantidad": it.get("cantidad", 1), **formatted})

    return jsonify({"tenant_id": tenant.id, "items": enriched, "no_encontrados": not_found})


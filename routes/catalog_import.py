import io
import logging
from typing import List

import pandas as pd
from flask import Blueprint, jsonify, request, g

from database import db
from models import CatalogoItem
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file

logger = logging.getLogger(__name__)

catalog_import_bp = Blueprint("catalog_import_bp", __name__, url_prefix="/api/admin/catalogo")


_PROMPT = """
Extraé las filas del archivo y devolvélas como JSON {"items": [...]}.
Campos esperados por item: sku, nombre, descripcion, precio_unitario, precio_caja, unidad_por_caja, moneda, imagen_url.
Si el archivo tiene columnas BRAND y VARIETAL, concatena para nombre. $ BOTTLE = precio_unitario, $ BOX = precio_caja, UNIT/BOX = unidad_por_caja.
"""


def _persist_rows(owner_id: int, tenant_id: int, rows: List[dict]):
    created = 0
    for row in rows:
        nombre = row.get("nombre")
        if not nombre:
            continue
        sku = row.get("sku")
        item = None
        if sku:
            item = CatalogoItem.query.filter_by(user_id=owner_id, sku=sku).first()
        if not item:
            item = CatalogoItem(user_id=owner_id, tenant_id=tenant_id, nombre=nombre)
        item.descripcion = row.get("descripcion")
        item.sku = sku
        item.precio = row.get("precio_unitario") or row.get("precio")
        item.precio_monetario = row.get("precio_unitario")
        item.precio_por_caja = row.get("precio_caja")
        item.unidad_por_caja = row.get("unidad_por_caja")
        item.modalidad = row.get("modalidad") or "venta"
        item.moneda = row.get("moneda") or "ARS"
        item.precio_puntos = row.get("precio_puntos")
        item.imagen_url = row.get("imagen_url")
        item.metadata = row
        db.session.add(item)
        created += 1
    db.session.commit()
    return created


@catalog_import_bp.route("/importar", methods=["POST"])
def importar_catalogo():
    archivo = request.files.get("archivo")
    if not archivo:
        return jsonify({"error": "Archivo requerido"}), 400

    try:
        tenant, owner, _ = resolve_tenant_and_user(
            tenant_slug=request.headers.get("X-Tenant"), current_user=getattr(g, "user", None)
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    contenido = archivo.read()
    filas = extract_table_from_file(contenido, _PROMPT)
    if filas is None:
        try:
            df = pd.read_excel(io.BytesIO(contenido))
        except Exception:
            df = pd.read_csv(io.BytesIO(contenido))
        filas = df.to_dict(orient="records")

    creados = _persist_rows(owner.id, tenant.id, filas or [])
    return jsonify({"ok": True, "items_importados": creados})


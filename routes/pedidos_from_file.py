import io
import io
import logging
from typing import Dict, List

import logging

import pandas as pd
from flask import Blueprint, jsonify, request, session, g
from flask_cors import cross_origin
from sqlalchemy import func

from database import db
from models import CatalogoItem, PedidoConversacional
from routes.catalogo import _formatear_producto
from routes.productos import _resolve_public_owner
from services.cart import _get_pyme_cart
from services.gcs_service import upload_to_gcs
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file
from config import ALLOWED_ORIGINS

logger = logging.getLogger(__name__)

_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]

pedidos_from_file_bp = Blueprint("pedidos_from_file_bp", __name__, url_prefix="/api/pedidos")

_PROMPT = """
Identificá productos y cantidades del documento. Devuelve JSON {"items": [{"sku": "...", "nombre": "...", "cantidad": 1}]}.
"""


def _json_error(status_code: int, code: str, message: str):
    response = jsonify({"codigo": code, "mensaje": message})
    response.status_code = status_code
    return response


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


def _extract_rows(contenido: bytes) -> List[dict]:
    try:
        rows = extract_table_from_file(contenido, _PROMPT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error al extraer items de nota de pedido", exc_info=exc)
        raise ValueError("No se pudo procesar el archivo subido") from exc

    if rows:
        return rows

    try:
        df = pd.read_excel(io.BytesIO(contenido))
    except Exception as excel_exc:  # noqa: BLE001
        try:
            df = pd.read_csv(io.BytesIO(contenido))
        except Exception as csv_exc:  # noqa: BLE001
            logger.exception("Formato de nota de pedido no soportado", exc_info=csv_exc)
            raise ValueError("Formato de archivo no soportado o dañado") from csv_exc
    return df.to_dict(orient="records")


@pedidos_from_file_bp.route("/from-file", methods=["POST", "OPTIONS"])
@cross_origin(
    origins=ALLOWED_ORIGINS,
    supports_credentials=True,
    allow_headers=_CORS_ALLOWED_HEADERS,
    methods=["POST", "OPTIONS"],
)
def pedidos_desde_archivo():
    if request.method == "OPTIONS":
        return "", 204

    archivo = request.files.get("archivo")
    if not archivo:
        return _json_error(400, "archivo_requerido", "Archivo requerido")
    if not archivo.filename:
        return _json_error(400, "archivo_sin_nombre", "Archivo sin nombre")

    extension = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
    allowed = {"pdf", "xls", "xlsx", "csv", "png", "jpg", "jpeg", "webp"}
    if extension not in allowed:
        return _json_error(400, "formato_no_permitido", "Formato no permitido. Usa PDF, Excel o imagen.")

    tenant_slug = request.headers.get("X-Tenant") or request.args.get("tenant") or request.args.get("tenant_slug")
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    user = getattr(g, "user", None)
    owner = None
    try:
        tenant, user, _ = resolve_tenant_and_user(
            tenant_slug=tenant_slug,
            tenant_id=tenant_id,
            current_user=user,
            widget_token=request.headers.get("X-Widget-Token") or request.args.get("widget_token"),
        )
    except TenantResolutionError:
        tenant, owner = _resolve_public_owner()
        if not tenant or not owner:
            return _json_error(404, "tenant_no_encontrado", "Tenant no encontrado")

    if not owner:
        owner = getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)

    try:
        contenido = archivo.read()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error al leer archivo de nota de pedido", exc_info=exc)
        return _json_error(400, "archivo_ilegible", "No se pudo leer el archivo subido")

    if not contenido:
        return _json_error(400, "archivo_vacio", "El archivo está vacío")

    upload_meta = upload_to_gcs(archivo)
    if not upload_meta or not upload_meta.get("public_url"):
        return _json_error(500, "upload_fallido", "No se pudo guardar el archivo")

    try:
        rows = _extract_rows(contenido)
    except ValueError as exc:
        return _json_error(400, "formato_no_soportado", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error inesperado procesando nota de pedido", exc_info=exc)
        return _json_error(500, "error_interno", "Error interno al procesar el archivo")

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

    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=getattr(user, "id", None) or owner.id,
        tipo="nota_de_pedido",
        estado="confirmado",
        items=[
            {
                "archivo_url": upload_meta.get("public_url"),
                "archivo_nombre": upload_meta.get("original_name") or archivo.filename,
                "items_detectados": enriched,
                "no_encontrados": not_found,
                "origen": request.headers.get("X-Checkout-Origin")
                or request.args.get("origen")
                or "web",
            }
        ],
        monto_monetario=0,
        monto_puntos=0,
        anon_id=getattr(user, "anon_id", None),
    )
    db.session.add(pedido)
    db.session.commit()

    return (
        jsonify(
            {
                "tenant_id": tenant.id,
                "items": enriched,
                "no_encontrados": not_found,
                "pedido_id": pedido.id,
                "archivo_url": upload_meta.get("public_url"),
                "tipo": pedido.tipo,
            }
        ),
        201,
    )


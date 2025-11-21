import io
import logging
from typing import List

import pandas as pd
from flask import Blueprint, jsonify, request, g
from werkzeug.exceptions import HTTPException

from database import db
from models import CatalogoItem
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file

logger = logging.getLogger(__name__)

catalog_import_bp = Blueprint("catalog_import_bp", __name__, url_prefix="/api/admin/catalogo")


def _json_error(status_code: int, code: str, message: str):
    response = jsonify({"codigo": code, "mensaje": message})
    response.status_code = status_code
    return response


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
        item.extra_metadata = row
        db.session.add(item)
        created += 1
    db.session.commit()
    return created


def _parse_rows(contenido: bytes) -> List[dict]:
    """Parsea filas de catálogo desde un archivo enviado por el usuario.

    Primero intenta usar el extractor de tablas (OpenAI/LLM) y, si no produce
    resultados, recurre a pandas para leer Excel o CSV. Siempre devuelve una
    lista de diccionarios o genera un ValueError con un mensaje apto para UI.
    """

    try:
        filas = extract_table_from_file(contenido, _PROMPT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error al extraer tabla con LLM", exc_info=exc)
        raise ValueError("No se pudo extraer información del archivo") from exc

    if filas:
        return filas

    try:
        df = pd.read_excel(io.BytesIO(contenido))
    except Exception as excel_exc:  # noqa: BLE001
        try:
            df = pd.read_csv(io.BytesIO(contenido))
        except Exception as csv_exc:  # noqa: BLE001
            logger.exception("Error al leer archivo de catálogo", exc_info=csv_exc)
            raise ValueError("Formato de archivo no soportado o archivo dañado") from csv_exc
        else:
            logger.info("Archivo de catálogo procesado como CSV")
    else:
        logger.info("Archivo de catálogo procesado como Excel")

    return df.to_dict(orient="records")


@catalog_import_bp.route("/importar", methods=["POST"])
def importar_catalogo():
    archivo = request.files.get("archivo")
    if not archivo:
        return _json_error(400, "archivo_requerido", "Archivo requerido")

    try:
        tenant, owner, _ = resolve_tenant_and_user(
            tenant_slug=request.headers.get("X-Tenant"), current_user=getattr(g, "user", None)
        )
    except TenantResolutionError as exc:
        return _json_error(404, "tenant_no_encontrado", str(exc))

    try:
        contenido = archivo.read()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error al leer archivo subido", exc_info=exc)
        return _json_error(400, "archivo_ilegible", "No se pudo leer el archivo subido")

    try:
        filas = _parse_rows(contenido)
    except ValueError as exc:
        return _json_error(400, "formato_no_soportado", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado importando catálogo", exc_info=exc)
        return _json_error(500, "error_interno", "Error interno al importar el catálogo")

    creados = _persist_rows(owner.id, tenant.id, filas or [])
    return jsonify({"ok": True, "items_importados": creados})


@catalog_import_bp.route("/importar", methods=["OPTIONS"])
def importar_catalogo_options():
    """Responde el preflight CORS con JSON para evitar HTML inesperado."""

    return jsonify({"ok": True})


@catalog_import_bp.app_errorhandler(HTTPException)
def _http_error_handler(exc: HTTPException):
    """Garantiza respuestas JSON para errores HTTP en el blueprint."""

    if not request.path.startswith("/api/admin/catalogo"):
        return exc

    logger.exception("Error HTTP en importar catálogo", exc_info=exc)
    status_code = exc.code or 500
    message = exc.description or exc.name
    return _json_error(status_code, exc.name.lower().replace(" ", "_"), message)


@catalog_import_bp.app_errorhandler(Exception)
def _unhandled_error_handler(exc: Exception):  # noqa: BLE001
    if not request.path.startswith("/api/admin/catalogo"):
        return exc

    logger.exception("Error no controlado en importar catálogo", exc_info=exc)
    return _json_error(500, "error_interno", "Error interno al importar el catálogo")


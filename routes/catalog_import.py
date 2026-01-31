import io
import json
import logging
from typing import Dict, List, Optional

import pandas as pd
from flask import Blueprint, jsonify, request, g
from werkzeug.exceptions import HTTPException

from database import db
from models import CatalogoItem
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file

logger = logging.getLogger(__name__)

catalog_import_bp = Blueprint("catalog_import_bp", __name__, url_prefix="/api/admin/catalogo")


def _with_cors_headers(response):
    """Adjunta cabeceras CORS explícitas para evitar HTML en navegadores."""

    origin = request.headers.get("Origin") or "*"
    response.headers.setdefault("Access-Control-Allow-Origin", origin)
    response.headers.setdefault(
        "Access-Control-Allow-Headers",
        "Origin, X-Requested-With, Content-Type, Accept, Authorization, X-Tenant",
    )
    response.headers.setdefault("Access-Control-Allow-Methods", "POST, OPTIONS")
    response.headers.setdefault("Access-Control-Allow-Credentials", "true")
    return response


def _json_error(status_code: int, code: str, message: str):
    response = jsonify({"codigo": code, "mensaje": message})
    response.status_code = status_code
    return _with_cors_headers(response)


def _parse_column_map(raw: Optional[str]) -> Optional[Dict[str, str]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:  # noqa: BLE001
        raise ValueError("column_map debe ser JSON válido") from exc
    if not isinstance(parsed, dict):
        raise ValueError("column_map debe ser un objeto JSON")
    return {str(k): str(v) for k, v in parsed.items()}


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


def _apply_column_map(rows: List[dict], column_map: Optional[Dict[str, str]]):
    if not column_map:
        return rows

    renamed_rows: List[dict] = []
    for row in rows:
        updated = dict(row)
        for source, target in column_map.items():
            if source in row and target:
                updated[target] = row.get(source)
        renamed_rows.append(updated)
    return renamed_rows


def _save_template(tenant, template_name: Optional[str], column_map: Optional[Dict[str, str]]):
    if not tenant or not template_name or not column_map:
        return

    cfg = tenant.configuracion or {}
    templates = cfg.get("catalogo_import_templates")
    if not isinstance(templates, dict):
        templates = {}

    templates[template_name] = column_map
    cfg["catalogo_import_templates"] = templates
    tenant.configuracion = cfg
    try:
        db.session.add(tenant)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("No se pudo guardar la plantilla de importación", exc_info=exc)


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

    filename = archivo.filename or ""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in {"pdf", "xlsx", "xls", "csv", "txt"}:
        return _json_error(400, "formato_no_soportado", "Formato de archivo no soportado")

    template_name = request.form.get("plantilla") or request.form.get("template_name")
    raw_column_map = request.form.get("column_map") or request.form.get("mapa_columnas")
    save_template = str(request.form.get("guardar_plantilla", "")).lower() in {"1", "true", "yes", "on"}
    preview_mode = str(request.form.get("preview", "")).lower() in {"1", "true", "yes", "on"}
    try:
        provided_column_map = _parse_column_map(raw_column_map)
    except ValueError as exc:
        return _json_error(400, "column_map_invalido", str(exc))

    try:
        tenant, owner, _ = resolve_tenant_and_user(
            tenant_slug=request.headers.get("X-Tenant"), current_user=getattr(g, "user", None)
        )
    except TenantResolutionError as exc:
        return _json_error(404, "tenant_no_encontrado", str(exc))

    column_map = provided_column_map
    if not column_map and template_name:
        templates = None
        cfg = tenant.configuracion or {}
        if isinstance(cfg, dict):
            templates = cfg.get("catalogo_import_templates")
        if isinstance(templates, dict):
            column_map = templates.get(template_name)

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

    filas = _apply_column_map(filas or [], column_map)

    if preview_mode:
        return _with_cors_headers(
            jsonify(
                {
                    "ok": True,
                    "preview": True,
                    "items": filas[:100],  # Return first 100 for preview
                    "total_detected": len(filas),
                    "plantilla_aplicada": template_name if column_map else None,
                }
            )
        )

    creados = _persist_rows(owner.id, tenant.id, filas)

    if save_template:
        _save_template(tenant, template_name, column_map)

    return _with_cors_headers(
        jsonify(
            {
                "ok": True,
                "items_importados": creados,
                "plantilla_aplicada": template_name if column_map else None,
            }
        )
    )


@catalog_import_bp.route("/importar", methods=["OPTIONS"])
def importar_catalogo_options():
    """Responde el preflight CORS con JSON para evitar HTML inesperado."""

    return _with_cors_headers(jsonify({"ok": True}))


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


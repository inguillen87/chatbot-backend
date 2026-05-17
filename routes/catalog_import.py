from flask import Blueprint, request, jsonify, g, current_app
from models import db, CatalogUpload, CatalogoItem, TenantCatalogMapping, TenantProfile
from middleware.tenant_context import require_tenant
from utils.auth_helpers import obtener_token, token_requerido, user_from_token
from werkzeug.utils import secure_filename
from services.tenant_resolver import resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file
from services.catalog_inventory import (
    inventory_columns_contract,
    inventory_contract,
    new_catalog_version,
    stock_value_from_row,
)
from sqlalchemy.orm.attributes import flag_modified
import os
import io
import json
import logging
import uuid
import hashlib
import re
import pandas as pd
from datetime import datetime, timezone

UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")
logger = logging.getLogger(__name__)

catalog_import_bp = Blueprint('catalog_import_bp', __name__)


def _catalog_import_error(codigo: str, mensaje: str, status: int):
    response = jsonify({"codigo": codigo, "mensaje": mensaje})
    response.status_code = status
    return response


def _coerce_dataframe_rows(frame) -> list[dict]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = frame.to_dict(orient="records")
    return [
        {str(key): value for key, value in row.items() if value is not None}
        for row in rows
    ]


def _parse_column_map(raw_value) -> dict:
    if not raw_value:
        return {}
    if isinstance(raw_value, dict):
        return raw_value
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _apply_column_map(rows: list[dict], column_map: dict) -> list[dict]:
    if not column_map:
        return rows
    mapped_rows: list[dict] = []
    for row in rows:
        mapped = dict(row)
        for source, target in column_map.items():
            if source in row and target:
                mapped[str(target)] = row[source]
                if target != source:
                    mapped.pop(source, None)
        mapped_rows.append(mapped)
    return mapped_rows


_IMAGE_KEYS = (
    "imagen_url",
    "image_url",
    "foto",
    "foto_url",
    "photo",
    "photo_url",
    "thumbnail",
    "thumbnail_url",
)

_GALLERY_KEYS = (
    "gallery_urls",
    "imagenes",
    "images",
    "image_urls",
    "fotos",
    "photos",
)


def _split_image_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, dict):
        raw_values = list(value.values())
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                raw_values = parsed if isinstance(parsed, list) else [text]
            except Exception:
                raw_values = [text]
        else:
            raw_values = re.split(r"[\n;,|]+", text)

    urls: list[str] = []
    for raw in raw_values:
        url = str(raw or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls[:12]


def _product_images_from_row(row: dict) -> tuple[str | None, list[str]]:
    primary = None
    for key in _IMAGE_KEYS:
        if row.get(key):
            primary = str(row.get(key)).strip()
            break

    gallery: list[str] = []
    for key in _GALLERY_KEYS:
        gallery.extend(_split_image_values(row.get(key)))
    if primary and primary not in gallery:
        gallery.insert(0, primary)
    if not primary and gallery:
        primary = gallery[0]
    return primary, list(dict.fromkeys(gallery))[:12]


def _normalize_rows_for_images(rows: list[dict]) -> tuple[list[dict], dict]:
    normalized: list[dict] = []
    with_images = 0
    missing_images = 0
    for row in rows:
        mapped = dict(row)
        primary, gallery = _product_images_from_row(mapped)
        if primary:
            mapped["imagen_url"] = primary
            with_images += 1
        else:
            missing_images += 1
        if gallery:
            mapped["gallery_urls"] = gallery
        normalized.append(mapped)

    return normalized, {
        "contract_version": "catalog.import_images.v1",
        "with_images": with_images,
        "missing_images": missing_images,
        "accepted_columns": [*_IMAGE_KEYS, *_GALLERY_KEYS],
        "embedded_pdf_image_extraction": "best_effort_pending",
    }


def _guess_catalog_column_type(key: str, values: list) -> str:
    normalized = str(key or "").strip().lower()
    if normalized in {"precio", "price", "precio_monetario", "stock"}:
        return "number"
    if normalized in {"imagen_url", "image_url", "foto", "gallery_urls", "imagenes", "images"}:
        return "image"
    if normalized in {"external_url", "url", "link"}:
        return "url"
    if normalized in {"disponible", "published", "activo"}:
        return "boolean"
    numeric = 0
    for value in values:
        try:
            float(str(value).replace(",", ".").strip())
            numeric += 1
        except (TypeError, ValueError):
            pass
    if values and numeric >= max(1, len(values) // 2):
        return "number"
    return "text"


def _catalog_columns(rows: list[dict]) -> list[dict]:
    keys: list[str] = []
    for row in rows:
        for key in row.keys():
            key_text = str(key)
            if key_text not in keys:
                keys.append(key_text)
    columns = []
    for key in keys:
        values = [row.get(key) for row in rows if row.get(key) not in (None, "")]
        columns.append(
            {
                "key": key,
                "label": key.replace("_", " ").strip().title(),
                "type": _guess_catalog_column_type(key, values[:20]),
                "confidence": 0.9 if values else 0.4,
                "sample_values": [str(value) for value in values[:3]],
            }
        )
    return columns


def _catalog_quality_summary(rows: list[dict]) -> dict:
    total = len(rows)
    without_price = 0
    without_stock = 0
    without_image = 0
    without_short_description = 0
    ready = 0
    for row in rows:
        price = row.get("precio") or row.get("price") or row.get("precio_monetario")
        stock = row.get("stock") or row.get("existencias") or row.get("cantidad")
        description = row.get("descripcion") or row.get("description") or row.get("descripcion_corta")
        image, gallery = _product_images_from_row(row)
        missing_price = price in (None, "")
        missing_stock = stock in (None, "")
        missing_image = not image and not gallery
        missing_description = not str(description or "").strip()
        without_price += int(missing_price)
        without_stock += int(missing_stock)
        without_image += int(missing_image)
        without_short_description += int(missing_description)
        if not missing_price and not missing_image and not missing_description:
            ready += 1
    return {
        "total_rows": total,
        "ready_to_publish": ready,
        "without_price": without_price,
        "without_stock": without_stock,
        "without_image": without_image,
        "without_short_description": without_short_description,
    }


def _catalog_inventory_summary(rows: list[dict]) -> dict:
    with_stock = 0
    out_of_stock = 0
    low_stock = 0
    unknown_stock = 0
    for row in rows:
        raw_stock = stock_value_from_row(row)
        status = inventory_contract(raw_stock).get("stock_status")
        if status == "stock_unknown":
            unknown_stock += 1
        else:
            with_stock += 1
        if status == "out_of_stock":
            out_of_stock += 1
        if status == "low_stock":
            low_stock += 1
    return {
        "contract_version": "catalog.inventory_summary.v1",
        "total_rows": len(rows),
        "with_stock": with_stock,
        "stock_unknown": unknown_stock,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "columns": inventory_columns_contract(),
    }


def _catalog_rows_sample(rows: list[dict], warnings: list | None = None) -> list[dict]:
    warnings = warnings or []
    sample = []
    for index, row in enumerate(rows[:25]):
        row_warnings = []
        if not (row.get("nombre") or row.get("titulo") or row.get("title") or row.get("producto")):
            row_warnings.append("missing_name")
        if not (row.get("precio") or row.get("price") or row.get("precio_monetario")):
            row_warnings.append("missing_price")
        image, gallery = _product_images_from_row(row)
        if not image and not gallery:
            row_warnings.append("missing_image")
        sample.append({"row_index": index, "cells": row, "warnings": row_warnings})
    if not sample and warnings:
        sample.append({"row_index": None, "cells": {}, "warnings": warnings[:5]})
    return sample


def _catalog_suggested_actions(quality_summary: dict, image_summary: dict) -> list[dict]:
    actions = []
    if quality_summary.get("without_image"):
        actions.append({"id": "complete_images", "label": "Completar imagenes", "reason_code": "missing_images"})
    if quality_summary.get("without_price"):
        actions.append({"id": "review_prices", "label": "Revisar precios", "reason_code": "missing_prices"})
    if quality_summary.get("without_stock"):
        actions.append({"id": "review_stock", "label": "Revisar stock", "reason_code": "missing_stock"})
    if quality_summary.get("ready_to_publish"):
        actions.append({"id": "publish_ready", "label": "Publicar listos", "reason_code": "ready_to_publish"})
    if not actions:
        actions.append({"id": "review_rows", "label": "Revisar filas", "reason_code": "manual_review"})
    return actions


def _catalog_import_preview_contract(upload: CatalogUpload, *, request_id: str | None = None) -> dict:
    base = upload.to_dict()
    preview = base.get("preview_data") if isinstance(base.get("preview_data"), dict) else {}
    rows = preview.get("items") or preview.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    image_summary = preview.get("image_summary") if isinstance(preview.get("image_summary"), dict) else None
    if image_summary is None:
        rows, image_summary = _normalize_rows_for_images(rows)
    quality_summary = _catalog_quality_summary(rows)
    pages_count = (
        preview.get("pages")
        or preview.get("page_count")
        or preview.get("pages_count")
        or preview.get("total_pages")
    )
    try:
        pages_count = int(pages_count) if pages_count is not None else None
    except (TypeError, ValueError):
        pages_count = None
    source_size = None
    upload_path = os.path.join(UPLOAD_FOLDER, f"{upload.filename}")
    if os.path.exists(upload_path):
        try:
            source_size = os.path.getsize(upload_path)
        except OSError:
            source_size = None
    base.update(
        {
            "contract_version": "catalog.import_preview.v1",
            "upload_id": upload.id,
            "request_id": request_id or request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}",
            "source_file": {
                "name": upload.filename,
                "type": upload.mime_type,
                "pages": pages_count,
                "rows": len(rows),
                "size": source_size,
                "status": upload.status,
                "processor": upload.processor_slug,
                "engine": upload.engine_used,
            },
            "columns": _catalog_columns(rows),
            "rows_sample": _catalog_rows_sample(rows, base.get("warnings") or base.get("errors")),
            "image_summary": image_summary,
            "quality_summary": quality_summary,
            "inventory_summary": _catalog_inventory_summary(rows),
            "suggested_actions": _catalog_suggested_actions(quality_summary, image_summary),
            "commit_endpoint": f"/api/admin/catalog/import/{upload.id}/commit",
            "publish_policy": "manual_commit_required",
            "frontend_contract": {
                "render_as": "catalog_import_preview",
                "editable_rows": True,
                "publish_requires_admin_confirmation": True,
            },
        }
    )
    return base


def _persist_rows(owner_id: int, tenant_id: int, rows: list[dict]) -> int:
    count = 0
    for row in rows:
        title = row.get("nombre") or row.get("titulo") or row.get("title") or row.get("producto")
        if not title:
            continue
        primary_image, gallery_urls = _product_images_from_row(row)
        metadata = row.get("extra_metadata") if isinstance(row.get("extra_metadata"), dict) else {}
        metadata = dict(metadata)
        if gallery_urls:
            metadata["gallery_urls"] = gallery_urls
            metadata["image_status"] = "ready"
        else:
            metadata["image_status"] = "missing"
        item = CatalogoItem(
            user_id=owner_id,
            tenant_id=tenant_id,
            sku=str(row.get("sku") or row.get("codigo") or f"IMP-{uuid.uuid4().hex[:8]}"),
            nombre=str(title),
            descripcion=str(row.get("descripcion") or row.get("description") or "") or None,
            precio=str(row.get("precio") or row.get("price") or ""),
            cantidad=str(stock_value_from_row(row)) if stock_value_from_row(row) is not None else None,
            precio_monetario=0.0,
            categoria=row.get("categoria") or row.get("category"),
            marca=row.get("marca") or row.get("brand"),
            imagen_url=primary_image,
            extra_metadata=metadata,
            disponible=True,
            modalidad="venta",
        )
        db.session.add(item)
        count += 1
    if count:
        db.session.commit()
    return count


def _legacy_catalog_current_user():
    if current_app.config.get("TESTING"):
        return None
    token = obtener_token()
    user = user_from_token(token) if token else None
    return user


@catalog_import_bp.route('/api/admin/catalogo/importar', methods=['GET', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def legacy_catalog_import_method_not_allowed():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    return _catalog_import_error(
        "method_not_allowed",
        "Method not allowed. Use POST para importar un catalogo.",
        405,
    )


@catalog_import_bp.route('/api/admin/catalogo/importar', methods=['POST'])
def legacy_catalog_import():
    current_user = _legacy_catalog_current_user()
    if not current_app.config.get("TESTING") and not current_user:
        return _catalog_import_error("token_missing", "Token de autenticacion requerido.", 401)

    upload = request.files.get("archivo") or request.files.get("file")
    if not upload:
        return _catalog_import_error("archivo_requerido", "Tenes que adjuntar un archivo.", 400)

    filename = secure_filename(upload.filename or "")
    ext = os.path.splitext(filename.lower())[1]
    content = upload.read()
    rows: list[dict] = []

    try:
        if ext in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
            rows = extract_table_from_file(content, "Extrae productos o servicios del catalogo.") or []
        elif ext in {".xlsx", ".xls"}:
            rows = _coerce_dataframe_rows(pd.read_excel(io.BytesIO(content)))
        elif ext == ".csv":
            rows = _coerce_dataframe_rows(pd.read_csv(io.BytesIO(content)))
        else:
            try:
                rows = extract_table_from_file(content, "Extrae productos o servicios del catalogo.") or []
            except Exception:
                rows = []
            if not rows:
                try:
                    rows = _coerce_dataframe_rows(pd.read_excel(io.BytesIO(content)))
                except Exception:
                    rows = []
            if not rows:
                try:
                    rows = _coerce_dataframe_rows(pd.read_csv(io.BytesIO(content)))
                except Exception:
                    rows = []
    except Exception as exc:
        logger.warning("Legacy catalog import parse failed: %s", exc)
        return _catalog_import_error(
            "formato_no_soportado",
            "No se pudo interpretar el archivo. Proba con PDF, Excel o CSV.",
            400,
        )

    if not rows:
        return _catalog_import_error(
            "formato_no_soportado",
            "No se pudo interpretar el formato del archivo. Proba con PDF, Excel o CSV.",
            400,
        )

    try:
        tenant_slug = request.form.get("tenant") or request.form.get("tenant_slug")
        tenant, owner, _ = resolve_tenant_and_user(tenant_slug=tenant_slug, current_user=current_user)
    except Exception as exc:
        logger.warning("Legacy catalog import tenant resolution failed: %s", exc)
        return _catalog_import_error(
            "tenant_no_resuelto",
            "No se pudo resolver el tenant para importar el catalogo.",
            400,
        )

    plantilla = (request.form.get("plantilla") or "").strip() or None
    column_map = _parse_column_map(request.form.get("column_map"))
    if not column_map and plantilla:
        templates = ((getattr(tenant, "configuracion", None) or {}).get("catalogo_import_templates") or {})
        column_map = templates.get(plantilla) or {}

    rows = _apply_column_map(rows, column_map)
    rows, image_summary = _normalize_rows_for_images(rows)

    if plantilla and column_map and str(request.form.get("guardar_plantilla", "")).lower() in {"1", "true", "si", "sÃ­"}:
        config = getattr(tenant, "configuracion", None)
        if not isinstance(config, dict):
            config = {}
        templates = config.setdefault("catalogo_import_templates", {})
        templates[plantilla] = column_map
        tenant.configuracion = config
        try:
            db.session.add(tenant)
            db.session.commit()
        except Exception:
            db.session.rollback()

    count = _persist_rows(getattr(owner, "id", None), getattr(tenant, "id", None), rows)
    return jsonify({
        "ok": True,
        "importados": count,
        "plantilla_aplicada": plantilla,
        "filas_detectadas": len(rows),
        "image_summary": image_summary,
        "imagenes_detectadas": image_summary.get("with_images", 0),
    })

@catalog_import_bp.route('/api/admin/catalog/import', methods=['POST'])
@token_requerido
@require_tenant
def create_import_session(current_user):
    tenant = g.tenant_profile
    file = request.files.get('file')

    if not file:
        return jsonify({"error": "No file"}), 400

    filename = secure_filename(file.filename)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    path = os.path.join(UPLOAD_FOLDER, f"{tenant.slug}_{filename}")
    file.save(path)

    # Calculate hash
    hasher = hashlib.md5()
    with open(path, 'rb') as f:
        buf = f.read()
        hasher.update(buf)
    file_hash = hasher.hexdigest()

    # Create Upload Record
    upload = CatalogUpload(
        tenant_id=tenant.id,
        filename=filename,
        mime_type=file.mimetype,
        status="processing",
        processor_slug="generic_v2",
        file_hash=file_hash
    )
    db.session.add(upload)
    db.session.commit()

    # Trigger Processing
    try:
        from services.catalog_pipeline import CatalogPipeline
        pipeline = CatalogPipeline()

        rubro_slug = "generic"
        if tenant.municipio and tenant.municipio.rubro:
             rubro_slug = tenant.municipio.rubro.nombre
        elif tenant.pyme and tenant.pyme.rubro:
             rubro_slug = tenant.pyme.rubro.nombre

        # Process upload
        extraction_result = pipeline.process_upload_preview(upload.id, path, file.mimetype, rubro_slug)

        items = extraction_result.get('items') or []
        if isinstance(items, list):
            normalized_items, image_summary = _normalize_rows_for_images(items)
            extraction_result['items'] = normalized_items
            extraction_result['image_summary'] = image_summary
            items = normalized_items
        else:
            image_summary = {"with_images": 0, "missing_images": 0}

        # Store full result
        upload.preview_data = extraction_result
        upload.warnings = extraction_result.get('warnings', [])
        upload.engine_used = extraction_result.get('engine', 'unknown')
        upload.stats = {
            "confidence": extraction_result.get('confidence', 0),
            "total_rows": len(items),
            "with_images": image_summary.get("with_images", 0),
            "missing_images": image_summary.get("missing_images", 0),
        }

        # Validation Logic
        if not items:
             upload.status = "failed"
             upload.errors = extraction_result.get('errors', []) + ["No structured data found"]
             if not upload.errors:
                 upload.errors = ["No se encontraron productos válidos."]
        else:
             upload.status = "ready_to_commit"

        db.session.commit()

    except Exception as e:
        logger.error(f"Import failed: {e}", exc_info=True)
        upload.status = "failed"
        upload.errors = [str(e)]
        db.session.commit()
        return jsonify({"error": "Processing failed", "details": str(e)}), 500

    resp = _catalog_import_preview_contract(upload)
    return jsonify(resp)

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>', methods=['OPTIONS'])
def options_import_session(upload_id):
    return jsonify({'status': 'ok'})

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>', methods=['GET'])
@token_requerido
@require_tenant
def get_import_session(current_user, upload_id):
    tenant = g.tenant_profile
    upload = CatalogUpload.query.filter_by(id=upload_id, tenant_id=tenant.id).first()
    if not upload:
        return jsonify({"error": "Not found"}), 404

    return jsonify(_catalog_import_preview_contract(upload))

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>', methods=['PUT'])
@token_requerido
@require_tenant
def update_import_preview(current_user, upload_id):
    tenant = g.tenant_profile
    upload = CatalogUpload.query.filter_by(id=upload_id, tenant_id=tenant.id).first()
    if not upload:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    if 'preview_data' in data:
        # User is manually correcting the data
        preview = data['preview_data'] or {}
        items = preview.get("items") or preview.get("rows") or []
        if isinstance(items, list):
            normalized_items, image_summary = _normalize_rows_for_images(items)
            if "rows" in preview and "items" not in preview:
                preview["rows"] = normalized_items
            else:
                preview["items"] = normalized_items
            preview["image_summary"] = image_summary
        upload.preview_data = preview
        upload.status = "ready_to_commit"

    db.session.commit()
    return jsonify(_catalog_import_preview_contract(upload))

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>/commit', methods=['POST'])
@token_requerido
@require_tenant
def commit_import_session(current_user, upload_id):
    tenant = g.tenant_profile
    upload = CatalogUpload.query.filter_by(id=upload_id, tenant_id=tenant.id).first()
    if not upload:
        return jsonify({"error": "Not found"}), 404

    if upload.status != "ready_to_commit":
        return jsonify({"error": "Upload not ready"}), 400

    preview = upload.preview_data or {}
    items = preview.get('items') or preview.get('rows') or []
    if isinstance(items, list):
        items, image_summary = _normalize_rows_for_images(items)
        preview["items"] = items
        preview["image_summary"] = image_summary
        upload.preview_data = preview
    else:
        image_summary = {"with_images": 0, "missing_images": 0}
    count = 0

    from services.qdrant_service import index_catalog_item
    from services.embedding_service import embed_textos_llm

    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "").strip().lower()
    if not mode:
        mode = "replace" if body.get("replace", True) else "upsert"
    if mode not in {"replace", "upsert", "stock_only"}:
        return jsonify({"error": "Invalid import mode", "allowed_modes": ["replace", "upsert", "stock_only"]}), 400

    replace = mode == "replace"
    stock_only = mode == "stock_only"
    created_count = 0
    updated_count = 0
    stock_updated_count = 0
    skipped_rows: list[dict] = []

    if replace:
        CatalogoItem.query.filter_by(tenant_id=tenant.id).delete()

    for item in items:
        # Flexible key access
        sku = item.get('sku') or item.get('SKU')
        title = item.get('title') or item.get('nombre') or item.get('Producto')
        price = item.get('price') or item.get('precio') or item.get('Precio')
        raw_stock = stock_value_from_row(item)

        if not sku:
             sku = f"GEN-{uuid.uuid4().hex[:8]}"

        if not title and not stock_only:
            continue # minimal requirement for product upsert/replace

        item_obj = None
        existing = CatalogoItem.query.filter_by(tenant_id=tenant.id, sku=sku).first()
        if not existing and tenant.pyme_id:
             existing = CatalogoItem.query.filter_by(user_id=tenant.pyme_id, sku=sku).first()

        if existing:
            item_obj = existing
            updated_count += 1
        else:
            if stock_only:
                skipped_rows.append(
                    {
                        "sku": sku,
                        "reason_code": "sku_not_found_for_stock_only_import",
                    }
                )
                continue
            new_item = CatalogoItem(
                user_id=tenant.pyme_id or current_user.id,
                tenant_id=tenant.id,
                sku=str(sku),
                nombre=str(title),
                precio=str(price),
                precio_monetario=0.0,
                modalidad="venta",
                disponible=True
            )
            db.session.add(new_item)
            item_obj = new_item
            created_count += 1

        if not title:
            title = item_obj.nombre

        if not stock_only:
            item_obj.nombre = str(title)
            item_obj.precio = str(price)
            try:
                 import re
                 clean_price = re.sub(r'[^\d\.,]', '', str(price))
                 clean_price = clean_price.replace(',', '.')
                 item_obj.precio_monetario = float(clean_price)
            except:
                 item_obj.precio_monetario = 0.0

            item_obj.categoria = item.get('category') or item.get('categoria')
            item_obj.moneda = item.get('currency') or item.get('moneda') or 'ARS'
            item_obj.marca = item.get('brand') or item.get('marca')
            item_obj.descripcion = item.get('description') or item.get('descripcion') or item_obj.descripcion
            primary_image, gallery_urls = _product_images_from_row(item)
            if primary_image is not None:
                item_obj.imagen_url = primary_image
            metadata = item_obj.extra_metadata if isinstance(item_obj.extra_metadata, dict) else {}
            metadata = dict(metadata)
            if gallery_urls:
                metadata["gallery_urls"] = gallery_urls
                metadata["image_status"] = "ready"
            elif not item_obj.imagen_url:
                metadata["image_status"] = "missing"
            item_obj.extra_metadata = metadata

        if raw_stock is not None:
            item_obj.cantidad = str(raw_stock)
            stock_updated_count += 1
            metadata = item_obj.extra_metadata if isinstance(item_obj.extra_metadata, dict) else {}
            metadata = dict(metadata)
            metadata["inventory_source"] = "catalog_import"
            metadata["stock_updated_at"] = datetime.now(timezone.utc).isoformat()
            item_obj.extra_metadata = metadata

        db.session.flush()

        try:
            text_to_embed = f"{item_obj.nombre} {item_obj.categoria or ''} {item_obj.precio}"
            embedding_list = embed_textos_llm([text_to_embed])
            if embedding_list and embedding_list[0]:
                rubro_nombre = "general"
                if tenant.pyme and tenant.pyme.rubro:
                    rubro_nombre = tenant.pyme.rubro.nombre

                owner_user_id = None
                if tenant.pyme:
                    owner_user_id = tenant.pyme.id
                elif tenant.municipio:
                    owner_user_id = tenant.municipio.id

                item_data = {
                    "id": item_obj.id,
                    "nombre": item_obj.nombre,
                    "descripcion": item_obj.descripcion or "",
                    "precio": float(item_obj.precio_monetario or 0),
                    "rubro": rubro_nombre,
                    "stock": item_obj.cantidad,
                    "user_id": owner_user_id or tenant.id,
                    "tenant_id": tenant.id,
                    "imagen_url": item_obj.imagen_url,
                }
                index_catalog_item(tenant.id, item_data, embedding_list[0])

        except Exception as e:
            logger.warning(f"Failed to index item {sku}: {e}")

        count += 1

    upload.status = "committed"
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    catalog_version = new_catalog_version(tenant.id)
    cfg["catalog_version"] = catalog_version
    cfg["catalog_last_import_id"] = upload.id
    cfg["catalog_last_import_mode"] = mode
    cfg["catalog_last_inventory_update_at"] = catalog_version
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")
    db.session.commit()

    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    return jsonify(
        {
            "ok": True,
            "success": True,
            "contract_version": "catalog.import_commit.v1",
            "request_id": request_id,
            "upload_id": upload.id,
            "mode": mode,
            "count": count,
            "created": created_count,
            "updated": updated_count,
            "stock_updated": stock_updated_count,
            "skipped_rows": skipped_rows[:50],
            "summary": {
                "created": created_count,
                "updated": updated_count,
                "skipped": len(skipped_rows),
                "errors": 0,
            },
            "warnings": [],
            "catalog_version": catalog_version,
            "image_summary": image_summary,
            "inventory_summary": _catalog_inventory_summary(items),
        }
    )

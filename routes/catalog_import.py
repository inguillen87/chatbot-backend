from flask import Blueprint, request, jsonify, g, current_app
from models import db, CatalogUpload, CatalogoItem, TenantCatalogMapping, TenantProfile
from middleware.tenant_context import require_tenant
from utils.auth_helpers import obtener_token, token_requerido, user_from_token
from werkzeug.utils import secure_filename
from services.tenant_resolver import resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file
import os
import io
import json
import logging
import uuid
import hashlib
import re
import pandas as pd

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


@catalog_import_bp.route('/api/admin/catalogo/importar', methods=['GET'])
def legacy_catalog_import_method_not_allowed():
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

        # Store full result
        upload.preview_data = extraction_result
        upload.warnings = extraction_result.get('warnings', [])
        upload.engine_used = extraction_result.get('engine', 'unknown')
        upload.stats = {
            "confidence": extraction_result.get('confidence', 0),
            "total_rows": len(extraction_result.get('items', []))
        }

        # Validation Logic
        items = extraction_result.get('items') or []
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

    resp = upload.to_dict()
    resp['upload_id'] = resp['id']
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

    resp = upload.to_dict()
    resp['upload_id'] = resp['id']
    return jsonify(resp)

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>', methods=['PUT'])
@token_requerido
@require_tenant
def update_import_preview(current_user, upload_id):
    tenant = g.tenant_profile
    upload = CatalogUpload.query.filter_by(id=upload_id, tenant_id=tenant.id).first()
    if not upload:
        return jsonify({"error": "Not found"}), 404

    data = request.json
    if 'preview_data' in data:
        # User is manually correcting the data
        upload.preview_data = data['preview_data']
        upload.status = "ready_to_commit"

    db.session.commit()
    return jsonify(upload.to_dict())

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
    count = 0

    from services.qdrant_service import index_catalog_item
    from services.embedding_service import embed_textos_llm

    replace = request.json.get('replace', True) if request.json else True

    if replace:
        CatalogoItem.query.filter_by(tenant_id=tenant.id).delete()

    for item in items:
        # Flexible key access
        sku = item.get('sku') or item.get('SKU')
        title = item.get('title') or item.get('nombre') or item.get('Producto')
        price = item.get('price') or item.get('precio') or item.get('Precio')

        if not title: continue # minimal requirement

        if not sku:
             sku = f"GEN-{uuid.uuid4().hex[:8]}"

        item_obj = None
        existing = CatalogoItem.query.filter_by(tenant_id=tenant.id, sku=sku).first()
        if not existing and tenant.pyme_id:
             existing = CatalogoItem.query.filter_by(user_id=tenant.pyme_id, sku=sku).first()

        if existing:
            item_obj = existing
        else:
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
                    "descripcion": "",
                    "precio": float(item_obj.precio_monetario or 0),
                    "rubro": rubro_nombre,
                    "stock": 0,
                    "user_id": owner_user_id or tenant.id,
                    "tenant_id": tenant.id,
                }
                index_catalog_item(tenant.id, item_data, embedding_list[0])

        except Exception as e:
            logger.warning(f"Failed to index item {sku}: {e}")

        count += 1

    upload.status = "committed"
    db.session.commit()

    return jsonify({"success": True, "count": count})

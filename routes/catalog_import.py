from flask import Blueprint, request, jsonify, g
from models import db, CatalogUpload, CatalogoItem, TenantCatalogMapping, TenantProfile
from middleware.tenant_context import require_tenant
from utils.auth_helpers import token_requerido
from werkzeug.utils import secure_filename
import os
import json
import logging
import uuid
import hashlib

UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")
logger = logging.getLogger(__name__)

catalog_import_bp = Blueprint('catalog_import_bp', __name__)

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

                item_data = {
                    "id": item_obj.id,
                    "nombre": item_obj.nombre,
                    "descripcion": "",
                    "precio": float(item_obj.precio_monetario or 0),
                    "rubro": rubro_nombre,
                    "stock": 0
                }
                index_catalog_item(tenant.id, item_data, embedding_list[0])

        except Exception as e:
            logger.warning(f"Failed to index item {sku}: {e}")

        count += 1

    upload.status = "committed"
    db.session.commit()

    return jsonify({"success": True, "count": count})

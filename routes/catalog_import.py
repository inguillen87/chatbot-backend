from flask import Blueprint, request, jsonify, g
from models import db, CatalogUpload, CatalogoItem, TenantCatalogMapping, TenantProfile
from middleware.tenant_context import require_tenant
from utils.auth_helpers import token_requerido
from werkzeug.utils import secure_filename
import os
import json
import logging

# Re-using upload folder logic
UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")
logger = logging.getLogger(__name__)

# This replaces/extends catalog_routes.py logic for the new flow
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

    # Create Upload Record
    upload = CatalogUpload(
        tenant_id=tenant.id,
        filename=filename,
        mime_type=file.mimetype,
        status="processing",
        processor_slug="generic_v2"
    )
    db.session.add(upload)
    db.session.commit()

    # Trigger Processing
    try:
        from services.catalog.pipeline import CatalogPipeline
        pipeline = CatalogPipeline()

        # Determine rubro for extraction heuristics
        rubro_slug = "generic"
        if tenant.municipio and tenant.municipio.rubro:
             rubro_slug = tenant.municipio.rubro.nombre
        elif tenant.pyme and tenant.pyme.rubro:
             rubro_slug = tenant.pyme.rubro.nombre

        # Process upload (synchronous for P0 MVP)
        # This returns a dict with 'items', 'warnings', etc.
        extraction_result = pipeline.process_upload_preview(upload.id, path, file.mimetype, rubro_slug)

        upload.preview_data = extraction_result.get('items', [])
        upload.warnings = extraction_result.get('warnings', [])
        upload.status = "ready_to_commit"
        db.session.commit()

    except Exception as e:
        logger.error(f"Import failed: {e}")
        upload.status = "failed"
        db.session.commit()
        return jsonify({"error": "Processing failed"}), 500

    return jsonify(upload.to_dict())

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>', methods=['GET'])
@token_requerido
@require_tenant
def get_import_session(current_user, upload_id):
    tenant = g.tenant_profile
    upload = CatalogUpload.query.filter_by(id=upload_id, tenant_id=tenant.id).first()
    if not upload:
        return jsonify({"error": "Not found"}), 404

    return jsonify(upload.to_dict())

@catalog_import_bp.route('/api/admin/catalog/import/<int:upload_id>', methods=['PUT'])
@token_requerido
@require_tenant
def update_import_preview(current_user, upload_id):
    """
    Allow frontend to fix/edit preview data before commit.
    """
    tenant = g.tenant_profile
    upload = CatalogUpload.query.filter_by(id=upload_id, tenant_id=tenant.id).first()
    if not upload:
        return jsonify({"error": "Not found"}), 404

    data = request.json
    if 'preview_data' in data:
        upload.preview_data = data['preview_data']
        # Recalculate warnings if needed
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

    items = upload.preview_data or []
    count = 0

    # Bulk Upsert Logic
    for item in items:
        sku = item.get('sku')
        if not sku: continue

        # Check existing
        existing = CatalogoItem.query.filter_by(tenant_id=tenant.id, sku=sku).first()
        # Fallback to user_id ownership if tenant_id is ambiguous (legacy)
        if not existing and tenant.pyme_id:
             existing = CatalogoItem.query.filter_by(user_id=tenant.pyme_id, sku=sku).first()

        if existing:
            existing.nombre = item.get('title', existing.nombre)
            existing.precio = str(item.get('price', existing.precio)) # Legacy uses string
            existing.precio_monetario = float(item.get('price', 0))
            # Update other fields
        else:
            new_item = CatalogoItem(
                user_id=tenant.pyme_id or current_user.id, # Fallback
                tenant_id=tenant.id,
                sku=sku,
                nombre=item.get('title'),
                precio=str(item.get('price')),
                precio_monetario=float(item.get('price', 0)),
                categoria=item.get('category'),
                modalidad="venta",
                disponible=True
            )
            db.session.add(new_item)

        count += 1

    upload.status = "committed"
    db.session.commit()

    return jsonify({"success": True, "count": count})

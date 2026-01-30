from flask import Blueprint, request, jsonify, g
from extensions import db
from models import CatalogUpload, User
from services.catalog.pipeline import CatalogPipeline
from werkzeug.utils import secure_filename
import os

catalog_bp = Blueprint("catalog_bp", __name__)
pipeline = CatalogPipeline()

UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")

@catalog_bp.route("/api/catalog/upload", methods=["POST"])
def upload_catalog_preview():
    user = g.get("current_user")
    if not user: return jsonify({"error": "Unauthorized"}), 401

    file = request.files.get("file")
    if not file: return jsonify({"error": "No file"}), 400

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = secure_filename(file.filename)
    path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(path)

    rubro_slug = "generic"
    if user.rubro: rubro_slug = user.rubro.nombre

    upload_rec = CatalogUpload(
        tenant_id=user.tenant_id or 1,
        filename=filename,
        mime_type=file.mimetype,
        status="pending"
    )
    db.session.add(upload_rec)
    db.session.commit()

    result = pipeline.process_upload_preview(upload_rec.id, path, file.mimetype, rubro_slug)
    return jsonify(result)

@catalog_bp.route("/api/catalog/confirm", methods=["POST"])
def confirm_catalog_upload():
    user = g.get("current_user")
    if not user: return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    upload_token = data.get("upload_token")
    mapping = data.get("mapping_override")

    rubro_slug = "generic"
    if user.rubro: rubro_slug = user.rubro.nombre

    result = pipeline.confirm_upload(upload_token, user.id, rubro_slug, mapping)
    return jsonify(result)

from flask import Blueprint, request, jsonify, g
from extensions import db
from models import CatalogUpload, User
from services.catalog.pipeline import CatalogPipeline
from werkzeug.utils import secure_filename
import os
import requests
import mimetypes

catalog_bp = Blueprint("catalog_bp", __name__)
pipeline = CatalogPipeline()

UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")

@catalog_bp.route("/api/catalog/upload", methods=["POST"])
def upload_catalog_preview():
    user = g.get("current_user")
    if not user: return jsonify({"error": "Unauthorized"}), 401

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    file = request.files.get("file")
    file_url = request.form.get("file_url") or (request.json and request.json.get("file_url"))

    path = None
    filename = None
    mime_type = None

    if file:
        filename = secure_filename(file.filename)
        path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(path)
        mime_type = file.mimetype
    elif file_url:
        try:
            response = requests.get(file_url, stream=True)
            response.raise_for_status()

            # Try to guess filename from URL or headers
            if "Content-Disposition" in response.headers:
                # Basic extraction, can be improved
                import re
                fname = re.findall("filename=(.+)", response.headers["Content-Disposition"])
                if fname:
                    filename = secure_filename(fname[0].strip('"'))

            if not filename:
                filename = secure_filename(os.path.basename(file_url.split("?")[0])) or "downloaded_catalog.pdf"

            path = os.path.join(UPLOAD_FOLDER, filename)
            with open(path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            mime_type = response.headers.get("Content-Type") or mimetypes.guess_type(path)[0] or "application/octet-stream"
        except Exception as e:
            return jsonify({"error": f"Failed to download file from URL: {str(e)}"}), 400
    else:
        return jsonify({"error": "No file or file_url provided"}), 400

    rubro_slug = "generic"
    # Safely access rubro.nombre if rubro exists, otherwise handle it (e.g. rubro_id)
    if user.rubro:
        if isinstance(user.rubro, str): # Legacy check
             rubro_slug = user.rubro
        else:
             rubro_slug = getattr(user.rubro, 'clave', None) or getattr(user.rubro, 'nombre', 'generic')

    upload_rec = CatalogUpload(
        tenant_id=user.tenant_id or 1,
        filename=filename,
        mime_type=mime_type,
        status="pending"
    )
    db.session.add(upload_rec)
    db.session.commit()

    result = pipeline.process_upload_preview(upload_rec.id, path, mime_type, rubro_slug)
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

from flask import Blueprint, jsonify, request
from routes.auth import token_requerido
from services.pyme_catalog_mapping_service import pyme_catalog_mapping_service
from werkzeug.utils import secure_filename
import os

pyme_catalog_mappings_bp = Blueprint('pyme_catalog_mappings', __name__, url_prefix='/pymes/<int:pyme_id>/catalog-mappings')

@pyme_catalog_mappings_bp.route('', methods=['POST'])
@token_requerido
def upload_catalog_mapping(user, pyme_id):
    if 'file' not in request.files:
        return jsonify({"error": "No se encontró el archivo"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No se seleccionó ningún archivo"}), 400

    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join('/tmp', filename)
        file.save(filepath)

        try:
            pyme_catalog_mapping_service.process_catalog(filepath, pyme_id)
            return jsonify({"mensaje": "Catálogo subido y procesándose."}), 202
        except Exception as e:
            return jsonify({"error": f"Error al procesar el catálogo: {e}"}), 500

@pyme_catalog_mappings_bp.route('', methods=['GET'])
@token_requerido
def get_catalog_mappings(user, pyme_id):
    # This is a simplified implementation. A real implementation would need to
    # fetch the catalog mappings from the database.
    return jsonify([])

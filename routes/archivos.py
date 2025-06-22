from flask import Blueprint, request, jsonify, current_app
import os
from werkzeug.utils import secure_filename
from datetime import datetime
from routes.auth import token_requerido

archivos_bp = Blueprint('archivos_bp', __name__, url_prefix='/archivos')

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf', 'xlsx', 'xls', 'csv', 'docx', 'txt'}


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@archivos_bp.route('/subir', methods=['POST'])
@token_requerido
def subir_archivo(current_user):
    if 'archivo' not in request.files:
        return jsonify({'error': 'No se envió archivo.'}), 400
    file = request.files['archivo']
    if file.filename == '':
        return jsonify({'error': 'Nombre de archivo vacío.'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(f"{current_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}")
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        file.save(save_path)
        current_app.logger.info(f"Archivo subido por user {current_user.id}: {filename}")
        return jsonify({'mensaje': 'Archivo subido', 'filename': filename, 'url': f'/uploads/{filename}'}), 200
    return jsonify({'error': 'Formato no permitido.'}), 400

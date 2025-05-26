import os
import logging
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from flask import current_app as app
from flask_login import login_required
from extensions import db
from models import CatalogoItem, User
import pandas as pd
import uuid
import mimetypes

upload_bp = Blueprint("upload", __name__)

ALLOWED_EXTENSIONS = {'.pdf', '.xlsx', '.xls', '.csv'}
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def extension_valida(nombre):
    return os.path.splitext(nombre)[1].lower() in ALLOWED_EXTENSIONS


def procesar_archivo(path, user_id):
    registros = []
    try:
        ext = os.path.splitext(path)[1].lower()

        if ext in ['.xlsx', '.xls', '.csv']:
            if ext == '.csv':
                df = pd.read_csv(path)
            else:
                df = pd.read_excel(path)

            for _, row in df.iterrows():
                texto = " - ".join([str(val) for val in row.values if pd.notna(val)])
                if texto:
                    registros.append(CatalogoItem(user_id=user_id, texto=texto))

        # Future: Agregar soporte a PDF con pdfplumber

        db.session.bulk_save_objects(registros)
        db.session.commit()
        return len(registros)

    except Exception as e:
        logging.error(f"❌ Error al procesar archivo: {e}")
        return 0


@upload_bp.route("/subir_catalogo", methods=["POST"])
@login_required
def subir_catalogo():
    user: User = request.user
    if 'file' not in request.files:
        return jsonify({"error": "No se adjuntó ningún archivo"}), 400

    archivo = request.files['file']
    if archivo.filename == '':
        return jsonify({"error": "Nombre de archivo vacío"}), 400

    if not extension_valida(archivo.filename):
        return jsonify({"error": "Formato de archivo no permitido"}), 400

    nombre_seguro = secure_filename(f"{user.nombre_empresa}_{uuid.uuid4().hex}{os.path.splitext(archivo.filename)[1]}").replace(" ", "")
    ruta = os.path.join(UPLOAD_FOLDER, nombre_seguro)
    archivo.save(ruta)

    cantidad = procesar_archivo(ruta, user.id)
    if cantidad == 0:
        return jsonify({"error": "Error procesando el archivo. Asegurate que tenga contenido legible."}), 500

    return jsonify({"mensaje": f"✅ Catálogo procesado con {cantidad} productos."})

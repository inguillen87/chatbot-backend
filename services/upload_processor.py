import os
import uuid
import logging
import traceback

from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoEmbedding, User
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google
from services.procesar_catalogo_excel import procesar_catalogo_excel

upload_bp = Blueprint("upload_bp", __name__)
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def extension_valida(nombre_archivo):
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS


def procesar_y_embedear_catalogo(path, user_id):
    try:
        ext = os.path.splitext(path)[1].lower()
        logging.info(f"📥 Archivo recibido: {path}")
        logging.info(f"📦 Extensión: {ext} | 👤 User ID: {user_id}")

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError("❌ Formato de archivo no soportado")

        # Leer y procesar registros
        if ext == ".pdf":
            registros = procesar_catalogo_pdf_google(path)
        else:
            registros = procesar_catalogo_excel(path)

        if not registros:
            raise ValueError("⚠️ No se extrajo contenido útil del archivo")

        # Validación de campos requeridos
        registros_filtrados = []
        for i, r in enumerate(registros):
            if not all(k in r and r[k] for k in ("nombre", "descripcion", "precio", "cantidad")):
                logging.warning(f"⚠️ Registro inválido (índice {i}): {r}")
                continue
            registros_filtrados.append(r)

        if not registros_filtrados:
            raise ValueError("⚠️ Todos los registros estaban incompletos")

        # Generar texto para vectores
        textos = [
            f"{r['nombre']}. {r['descripcion']}. Precio: {r['precio']}. Cantidad: {r['cantidad']}."
            for r in registros_filtrados
        ]

        vectores = embed_textos(textos)
        if not vectores or len(vectores) != len(registros_filtrados):
            raise ValueError(f"❌ Fallo en generación de vectores ({len(vectores)} / {len(registros_filtrados)})")

        # Guardar en DB
        items = [
            CatalogoEmbedding(
                user_id=user_id,
                nombre=r["nombre"],
                descripcion=r["descripcion"],
                precio=r["precio"],
                cantidad=r["cantidad"],
                embedding_vector=vec
            )
            for r, vec in zip(registros_filtrados, vectores)
        ]

        db.session.bulk_save_objects(items)
        db.session.commit()
        logging.info(f"✅ {len(items)} ítems embebidos para user_id={user_id}")
        return len(items)

    except Exception:
        logging.exception("❌ Error inesperado procesando catálogo")
        return 0


@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
    try:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token no proporcionado"}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido o expirado"}), 401

        archivo = request.files.get("file")
        if not archivo or archivo.filename == "":
            return jsonify({"error": "Archivo no válido o no presente"}), 400

        if not extension_valida(archivo.filename):
            return jsonify({"error": "Formato de archivo no permitido"}), 400

        nombre_seguro = secure_filename(
            f"{user.nombre_empresa}_{uuid.uuid4().hex}{os.path.splitext(archivo.filename)[1]}"
        )
        ruta = os.path.join(UPLOAD_FOLDER, nombre_seguro)
        archivo.save(ruta)

        cantidad = procesar_y_embedear_catalogo(ruta, user.id)
        if cantidad == 0:
            return jsonify({"error": "No se procesó ningún ítem válido"}), 500

        return jsonify({"mensaje": f"✅ Catálogo procesado con {cantidad} productos."})
    except Exception:
        logging.exception("❌ Error inesperado en el endpoint /subir_catalogo")
        return jsonify({"error": "Error interno al procesar el catálogo"}), 500

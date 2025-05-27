import os
import uuid
import logging
import traceback
import pandas as pd

from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoEmbedding, User
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google

upload_bp = Blueprint("upload_bp", __name__)
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def extension_valida(nombre_archivo):
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS


def procesar_y_embedear_catalogo(path, user_id):
    try:
        ext = os.path.splitext(path)[1].lower()
        print("📥 Archivo recibido:", path)
        print("📦 Extensión:", ext)
        print("👤 User ID:", user_id)

        registros = []

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError("❌ Formato no soportado")

        # 📄 PDF: usar Google Document AI
        if ext == ".pdf":
            registros = procesar_catalogo_pdf_google(path)

        # 📊 CSV / Excel
        elif ext in [".csv", ".xlsx", ".xls"]:
            df = pd.read_csv(path) if ext == ".csv" else pd.read_excel(path)
            if df.empty:
                raise ValueError("⚠️ El archivo está vacío")

            for _, row in df.iterrows():
                nombre = str(row.get("nombre", "")).strip()
                descripcion = str(row.get("descripcion", "")).strip()
                precio = str(row.get("precio", "")).replace("$", "").replace(",", ".").strip()
                cantidad = str(row.get("cantidad", "1")).strip()

                if not nombre and not descripcion:
                    continue

                registros.append({
                    "nombre": nombre[:50],
                    "descripcion": descripcion or nombre,
                    "precio": precio or "-",
                    "cantidad": cantidad or "1"
                })

        if not registros:
            raise ValueError("⚠️ No se extrajo contenido útil del archivo")

        # 🧠 Generar texto para embebido
        textos = [
            f"{r['nombre']}. {r['descripcion']}. Precio: {r['precio']}. Cantidad: {r['cantidad']}."
            for r in registros
        ]

        vectores = embed_textos(textos)
        if not vectores:
            raise ValueError("❌ No se generaron vectores")

        items = [
            CatalogoEmbedding(
                user_id=user_id,
                nombre=r["nombre"],
                descripcion=r["descripcion"],
                precio=r["precio"],
                embedding_vector=vec
            )
            for r, vec in zip(registros, vectores)
        ]

        db.session.bulk_save_objects(items)
        db.session.commit()
        logging.info(f"✅ {len(items)} ítems embebidos (user_id={user_id})")
        return len(items)

    except Exception as e:
        print("❌ ERROR al procesar catálogo:")
        traceback.print_exc()
        logging.exception("❌ Error inesperado procesando catálogo:")
        return 0


@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
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

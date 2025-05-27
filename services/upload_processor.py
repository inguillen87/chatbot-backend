import os
import uuid
import logging
import pandas as pd
import pdfplumber
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from flask_login import login_required, current_user
from extensions import db
from models import CatalogoEmbedding
from services.cohere_ai import embed_textos
from models import User

upload_bp = Blueprint("upload_bp", __name__)
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def extension_valida(nombre_archivo):
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS


def procesar_y_embedear_catalogo(path, user_id):
    try:
        ext = os.path.splitext(path)[1].lower()
        textos = []
        registros = []

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError("❌ Formato no soportado")

        # CSV / Excel
        if ext in [".csv", ".xlsx", ".xls"]:
            df = pd.read_csv(path) if ext == ".csv" else pd.read_excel(path)
            if df.empty:
                return 0

            for _, row in df.iterrows():
                nombre = str(row.get("nombre", "")).strip()
                descripcion = str(row.get("descripcion", "")).strip()
                precio = str(row.get("precio", "")).strip()

                if not nombre and not descripcion:
                    continue

                texto = f"{nombre}. {descripcion}. Precio: {precio}."
                textos.append(texto)
                registros.append({
                    "nombre": nombre,
                    "descripcion": descripcion,
                    "precio": precio
                })

        # PDF
        elif ext == ".pdf":
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    table = page.extract_table()
                    if table and len(table[0]) >= 2:
                        headers = [h.lower() for h in table[0]]
                        for row in table[1:]:
                            row_dict = dict(zip(headers, row))
                            nombre = row_dict.get("nombre", row[0]) or ""
                            descripcion = row_dict.get("descripcion", "") or ""
                            precio = row_dict.get("precio", "") or ""
                            texto = f"{nombre}. {descripcion}. Precio: {precio}."
                            textos.append(texto)
                            registros.append({
                                "nombre": nombre.strip()[:50],
                                "descripcion": descripcion.strip(),
                                "precio": precio.strip()
                            })
                    else:
                        text = page.extract_text()
                        if text:
                            for line in text.split("\n"):
                                texto = line.strip()
                                if texto:
                                    textos.append(texto)
                                    registros.append({
                                        "nombre": texto[:50],
                                        "descripcion": texto,
                                        "precio": "-"
                                    })

        if not textos:
            raise ValueError("⚠️ No se extrajo contenido útil")

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
        logging.error(f"❌ Error al procesar catálogo: {e}")
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


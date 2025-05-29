import os
import uuid
import logging
import traceback

from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User  # <--- Ya no uses CatalogoEmbedding
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google
from services.procesar_catalogo_excel import procesar_catalogo_excel
from services.qdrant_utils import get_qdrant_client  # Importá tu función de conexión Qdrant

upload_bp = Blueprint("upload_bp", __name__)
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def extension_valida(nombre_archivo):
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id, textos, vectores):
    qdrant = get_qdrant_client()
    try:
        # Crea la colección si no existe (idempotente)
        qdrant.recreate_collection(
            collection_name="catalogos",
            vectors_config={"size": 768, "distance": "Cosine"}
        )
    except Exception as e:
        logging.info(f"Qdrant: la colección ya existe o fue creada. {e}")

    # Guardar cada texto como un punto en Qdrant
    for idx, (texto, vector) in enumerate(zip(textos, vectores)):
        qdrant.upsert(
            collection_name="catalogos",
            points=[{
                "id": f"{user_id}_{idx}",
                "vector": vector,
                "payload": {
                    "texto": texto,
                    "user_id": user_id
                }
            }]
        )
    logging.info(f"✅ {len(textos)} ítems guardados en Qdrant para user_id={user_id}")

def procesar_y_embedear_catalogo(path, user_id):
    try:
        ext = os.path.splitext(path)[1].lower()
        logging.info(f"\n📥 Archivo recibido: {path} ({os.path.getsize(path)} bytes)")
        logging.info(f"📦 Extensión: {ext} | 👤 User ID: {user_id}")

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"❌ Formato de archivo no soportado: {ext}")

        # Procesar archivo
        if ext == ".pdf":
            logging.info("🔍 Usando Google Document AI para procesar PDF...")
            registros = procesar_catalogo_pdf_google(path)
        else:
            logging.info("📊 Usando pandas para procesar Excel...")
            registros = procesar_catalogo_excel(path)

        logging.info(f"🔎 REGISTROS EXTRAIDOS (primeros 3): {registros[:3]} | TOTAL: {len(registros)}")

        if not registros:
            raise ValueError(f"⚠️ No se extrajo contenido útil del archivo {path}")

        # Filtrar y validar estructura mínima
        registros_filtrados = []
        for i, r in enumerate(registros):
            if not all(k in r and r[k] for k in ("nombre", "descripcion", "precio", "cantidad")):
                logging.warning(f"⚠️ Registro inválido (índice {i}): {r}")
                continue
            registros_filtrados.append(r)

        logging.info(f"🟢 Registros válidos para embedding: {len(registros_filtrados)}")

        if not registros_filtrados:
            raise ValueError("⚠️ Todos los registros estaban incompletos")

        # Embedding
        textos = [
            f"{r['nombre']}. {r['descripcion']}. Precio: {r['precio']}. Cantidad: {r['cantidad']}."
            for r in registros_filtrados
        ]
        logging.info(f"🧠 Textos a embebear (primeros 3): {textos[:3]} | TOTAL: {len(textos)}")

        logging.info("🧬 Generando vectores de embedding con Cohere...")
        vectores = embed_textos(textos)
        logging.info(f"🧬 Vectores generados: {len(vectores)} (esperados: {len(registros_filtrados)})")

        if not vectores or len(vectores) != len(registros_filtrados):
            raise ValueError(f"❌ Fallo en generación de vectores ({len(vectores)} / {len(registros_filtrados)})")

        # Guardar en Qdrant
        guardar_en_qdrant(user_id, textos, vectores)

        # (Opcional) Guardar los items claros en la base (sin embedding)
        items_claros = [
            CatalogoItem(
                user_id=user_id,
                nombre=r["nombre"],
                descripcion=r["descripcion"],
                precio=r["precio"],
                cantidad=r["cantidad"],
                categoria=r.get("categoria", ""),
                unidad=r.get("unidad", ""),
                texto=f"{r['nombre']}. {r['descripcion']}. Precio: {r['precio']}. Cantidad: {r['cantidad']}."
            )
            for r in registros_filtrados
        ]
        db.session.bulk_save_objects(items_claros)
        db.session.commit()

        logging.info(f"✅ {len(textos)} ítems procesados y guardados (Qdrant y base) para user_id={user_id}")
        return len(textos)

    except Exception as e:
        logging.error(f"❌ Excepción no controlada: {str(e)}")
        traceback.print_exc()
        raise ValueError(f"❌ Error procesando catálogo: {e}")

# El endpoint /subir_catalogo queda igual, solo cambia el procesamiento interno

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
        logging.info(f"📂 Archivo guardado temporalmente en: {ruta}")

        cantidad = procesar_y_embedear_catalogo(ruta, user.id)
        logging.info(f"🎉 Proceso completado: {cantidad} productos embebidos")
        return jsonify({"mensaje": f"✅ Catálogo procesado con {cantidad} productos."})

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 500
    except Exception as e:
        logging.exception("❌ Error inesperado en el endpoint /subir_catalogo")
        return jsonify({"error": f"Error inesperado: {str(e)}"}), 500

import os
import uuid
import logging
import traceback
from collections import Counter
import re # Necesario para re.sub en nombre_empresa_seguro y limpiar_texto

from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google
from services.procesar_catalogo_excel import procesar_catalogo_excel
from services.qdrant_utils import get_qdrant_client
from qdrant_client import models as qdrant_models

upload_bp = Blueprint("upload_bp", __name__)

UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}

# --- FUNCIÓN DE UTILIDAD (AHORA DEFINIDA AQUÍ) ---
def limpiar_texto(texto: str) -> str:
    """Limpia espacios extra y caracteres problemáticos comunes."""
    if not texto:
        return ""
    # Eliminar múltiples espacios, tabulaciones, y saltos de línea residuales
    texto_limpio = re.sub(r'\s+', ' ', texto).strip()
    # Puedes añadir más reglas de limpieza aquí si es necesario
    return texto_limpio
# --- FIN FUNCIÓN DE UTILIDAD ---

def extension_valida(nombre_archivo):
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id: int, productos_estructurados: list, vectores: list):
    qdrant = get_qdrant_client()
    puntos = []
    if len(productos_estructurados) != len(vectores):
        logging.error(f"Error Qdrant: Productos ({len(productos_estructurados)}) vs Vectores ({len(vectores)}).")
        raise ValueError("Discrepancia entre productos y vectores al guardar en Qdrant.")

    for producto_dict, vector in zip(productos_estructurados, vectores):
        payload = {
            "user_id": user_id,
            "nombre": producto_dict.get("nombre", "S/N"),
            "descripcion": producto_dict.get("descripcion", ""),
            "precio_str": producto_dict.get("precio_str", ""),
            "precio_float": producto_dict.get("precio_float"),
            "moneda": producto_dict.get("moneda", "ARS"),
            "categoria_qdrant": producto_dict.get("categoria", ""),
            "unidad": producto_dict.get("unidad", ""),
            "texto_original_para_embedding": producto_dict.get("texto_para_embedding", "")
        }
        payload_limpio = {k: v for k, v in payload.items() if v is not None and v != ""} # No guardar None ni vacíos

        puntos.append({"id": str(uuid.uuid4()), "vector": vector, "payload": payload_limpio})

    if puntos:
        qdrant.upsert(collection_name="catalogos", points=puntos, wait=True)
        logging.info(f"✅ {len(puntos)} ítems guardados/actualizados en Qdrant para user_id={user_id}")
    else:
        logging.warning(f"No se prepararon puntos para Qdrant para user_id={user_id}")


def procesar_y_embedear_catalogo(path: str, user_id: int, pyme_rubro_nombre: str = "generico"):
    try:
        ext = os.path.splitext(path)[1].lower()
        base_filename = os.path.basename(path)
        logging.info(f"📥 Archivo: {base_filename}, Tamaño: {os.path.getsize(path)} bytes")
        logging.info(f"📦 Ext: {ext}, User ID: {user_id}, Rubro: {pyme_rubro_nombre}")

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"❌ Formato no soportado: {ext}")

        registros_estructurados = []
        if ext == ".pdf":
            logging.info("🔍 Procesando PDF con Google Document AI...")
            registros_estructurados = procesar_catalogo_pdf_google(path, tipo_catalogo=pyme_rubro_nombre, pyme_user_id=user_id)
        else:
            logging.info("📊 Procesando Excel/CSV con pandas...")
            registros_estructurados = procesar_catalogo_excel(path)

        logging.info(f"🔎 REGISTROS EXTRAÍDOS (Total: {len(registros_estructurados)}). Primeros 3: {registros_estructurados[:3]}")

        if not registros_estructurados:
            raise ValueError(f"⚠️ No se extrajo contenido estructurado de {base_filename}")

        textos_para_embedding = []
        productos_finales_para_qdrant = []

        for prod_dict in registros_estructurados:
            if not isinstance(prod_dict, dict):
                logging.warning(f"Registro no es dict, se omite: {prod_dict}")
                continue

            nombre = prod_dict.get("nombre", "")
            descripcion = prod_dict.get("descripcion", "")
            precio_str = prod_dict.get("precio_str", "")
            categoria = prod_dict.get("categoria", "")

            # Construir texto para embedding de forma más selectiva
            partes_texto_embed = [f"Producto: {nombre}" if nombre else "Producto"]
            if categoria: partes_texto_embed.append(f"Categoría: {categoria}")
            if descripcion and limpiar_texto(descripcion) != limpiar_texto(nombre): partes_texto_embed.append(f"Detalles: {descripcion}")
            if precio_str: partes_texto_embed.append(f"Precio: {precio_str}")
            
            texto_embed_limpio = limpiar_texto(" | ".join(partes_texto_embed)) # Limpiar el texto final

            if texto_embed_limpio and len(texto_embed_limpio) > 10:
                textos_para_embedding.append(texto_embed_limpio)
                prod_dict_copy = prod_dict.copy()
                prod_dict_copy["texto_para_embedding"] = texto_embed_limpio
                productos_finales_para_qdrant.append(prod_dict_copy)
            else:
                logging.warning(f"Se omitió registro por texto de embedding corto/vacío: {prod_dict}. Texto generado: '{texto_embed_limpio}'")
        
        if not textos_para_embedding:
            raise ValueError("No se generaron textos válidos para embedding.")

        logging.info(f"🧠 Textos para embedding (Total: {len(textos_para_embedding)}). Primeros 3: {textos_para_embedding[:3]}")
        logging.info("🧬 Generando vectores con Cohere...")
        vectores = embed_textos(textos_para_embedding) # Esta es tu función de services.cohere_ai
        logging.info(f"🧬 Vectores generados: {len(vectores)}")

        if not vectores or len(vectores) != len(productos_finales_para_qdrant):
            logging.error(f"Error: Vectores ({len(vectores)}) vs Productos ({len(productos_finales_para_qdrant)})")
            raise ValueError("Fallo en generación de vectores o desajuste con productos.")

        guardar_en_qdrant(user_id, productos_finales_para_qdrant, vectores)

        items_para_db = []
        for prod_dict in productos_finales_para_qdrant:
            items_para_db.append(
                CatalogoItem(
                    user_id=user_id,
                    nombre=prod_dict.get("nombre", "")[:255],
                    descripcion=prod_dict.get("descripcion", "")[:1024],
                    precio=str(prod_dict.get("precio_str", prod_dict.get("precio", "")))[:50],
                    cantidad=str(prod_dict.get("cantidad", "1"))[:50],
                    categoria=prod_dict.get("categoria", "")[:100],
                    unidad=prod_dict.get("unidad", "")[:50],
                    texto=prod_dict.get("texto_para_embedding", "")
                )
            )
        
        if items_para_db:
            try:
                CatalogoItem.query.filter_by(user_id=user_id).delete()
                # db.session.commit() # Cometer la eliminación por separado podría ser más seguro, o hacerla parte de la misma transacción
                db.session.bulk_save_objects(items_para_db)
                db.session.commit()
                logging.info(f"✅ {len(items_para_db)} ítems guardados en DB relacional para user_id={user_id}")
            except Exception as e_db:
                db.session.rollback()
                logging.error(f"❌ Error guardando en DB relacional: {e_db}", exc_info=True)

        logging.info(f"🎉 Proceso de catálogo completado: {len(productos_finales_para_qdrant)} ítems procesados para user_id={user_id}")
        return len(productos_finales_para_qdrant)

    except ValueError as ve:
        logging.error(f"❌ Error de Valor en procesar_y_embedear_catalogo ({os.path.basename(path)}): {str(ve)}", exc_info=True) # Añadido exc_info
        raise
    except Exception as e:
        logging.error(f"❌ Excepción no controlada en procesar_y_embedear_catalogo ({os.path.basename(path)}): {str(e)}")
        traceback.print_exc()
        raise ValueError(f"Error interno grave al procesar el catálogo: {str(e)}")


# ... (tu Blueprint upload_bp, UPLOAD_FOLDER, ALLOWED_EXTENSIONS, limpiar_texto, extension_valida, 

@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
    user = None
    try:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token: return jsonify({"error": "Token no proporcionado"}), 401
        user = User.query.filter_by(token=token).first()
        if not user: return jsonify({"error": "Token inválido o expirado"}), 401
        archivo = request.files.get("file")
        if not archivo or not archivo.filename: return jsonify({"error": "Archivo no válido o no presente"}), 400
        if not extension_valida(archivo.filename): return jsonify({"error": "Formato de archivo no permitido. Permitidos: " + ", ".join(ALLOWED_EXTENSIONS)}), 400
        
        nombre_empresa_seguro = re.sub(r'\W+', '_', user.nombre_empresa) if user.nombre_empresa else "empresa_desconocida"
        nombre_seguro = secure_filename(
            f"{nombre_empresa_seguro}_{user.id}_{uuid.uuid4().hex[:8]}{os.path.splitext(archivo.filename)[1]}"
        )
        
        upload_dir = os.path.abspath(UPLOAD_FOLDER)
        os.makedirs(upload_dir, exist_ok=True)
        ruta_guardado = os.path.join(upload_dir, nombre_seguro)
        
        archivo.save(ruta_guardado)
        logging.info(f"📂 Archivo '{archivo.filename}' guardado en: {ruta_guardado} para User ID: {user.id}")

        pyme_rubro_nombre = "generico"
        if user.rubro_id:
            rubro_obj = Rubro.query.get(user.rubro_id)
            if rubro_obj: pyme_rubro_nombre = rubro_obj.nombre.lower().strip()

        # --- INICIO: BORRAR CATÁLOGO ANTERIOR EN QDRANT ---
        logging.info(f"Intentando eliminar catálogo anterior en Qdrant para user_id={user.id}...")
        try:
            client_qdrant = get_qdrant_client()
            client_qdrant.delete(
                collection_name="catalogos", # El nombre de tu colección
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="user_id", # El campo en tu payload para filtrar
                                match=qdrant_models.MatchValue(value=user.id)
                            )
                        ]
                    )
                )
            )
            logging.info(f"✅ Catálogo anterior en Qdrant para user_id={user.id} eliminado (o intento realizado).")
        except Exception as e_delete_qdrant:
            # Si falla la eliminación, podría ser porque no había nada o un error de Qdrant.
            # Es importante loguearlo, pero podrías decidir continuar con la subida del nuevo catálogo.
            logging.error(f"⚠️ Error al intentar eliminar catálogo anterior en Qdrant para user_id={user.id}: {e_delete_qdrant}", exc_info=True)
        # --- FIN: BORRAR CATÁLOGO ANTERIOR EN QDRANT ---
        
        cantidad_procesada = procesar_y_embedear_catalogo(ruta_guardado, user.id, pyme_rubro_nombre=pyme_rubro_nombre)
        
        # ... (resto del endpoint, incluyendo el os.remove opcional) ...

        return jsonify({"mensaje": f"✅ Catálogo procesado con {cantidad_procesada} ítems."}), 200

    except ValueError as ve:
        logging.error(f"Error de Valor en /subir_catalogo: {str(ve)}", exc_info=True if "procesar el catálogo" in str(ve).lower() else False)
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        logging.error(f"❌ Error inesperado en /subir_catalogo: {str(e)}", exc_info=True)
        return jsonify({"error": "Error interno inesperado al procesar el catálogo."}), 500
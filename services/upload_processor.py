# services/upload_processor.py
import os
import uuid
import logging
# traceback no es necesario importarlo explícitamente si usas logger.error con exc_info=True
# from collections import Counter # No se usa, se puede quitar
import re 
# import json # No se usa json directamente aquí

from flask import Blueprint, request, jsonify # NO import current_app aquí arriba
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro 
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google 
from services.procesar_catalogo_excel import procesar_catalogo_excel 

# Importar limpiar_texto_base de utils
from .utils import limpiar_texto_base 

from services.qdrant_utils import get_qdrant_client
from qdrant_client import models as qdrant_models # Para PointStruct y Filter
from typing import List, Dict, Any, Optional # Tipado

upload_bp = Blueprint("upload_bp", __name__)
logger = logging.getLogger(__name__) 

# --- CORRECCIÓN DEFINITIVA PARA UPLOAD_FOLDER ---
# Se define relativo al directorio de trabajo actual (donde corre Gunicorn).
# El directorio se creará dentro del endpoint si no existe.
UPLOAD_FOLDER_NAME = "temp_uploads" 
# La ruta completa se construirá dinámicamente dentro del endpoint
# --- FIN CORRECCIÓN ---

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}

def extension_valida(nombre_archivo: str) -> bool:
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id: int, productos_estructurados: List[Dict[str, Any]], vectores: List[List[float]]):
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error(f"[QDRANT_SAVE] No se pudo obtener cliente Qdrant para user_id={user_id}.")
        raise ConnectionError("No se pudo conectar a Qdrant para guardar los datos.")

    puntos_para_insertar: List[qdrant_models.PointStruct] = []
    
    if len(productos_estructurados) != len(vectores):
        logger.error(f"[QDRANT_SAVE] Discrepancia crítica: {len(productos_estructurados)} productos vs {len(vectores)} vectores para user_id={user_id}.")
        raise ValueError("Discrepancia crítica entre número de productos y vectores al preparar datos para Qdrant.")

    for producto_dict, vector in zip(productos_estructurados, vectores):
        payload = {
            "user_id": user_id,
            "nombre": producto_dict.get("nombre", "Producto Sin Nombre"),
            "descripcion": producto_dict.get("descripcion", ""),
            "precio_str": str(producto_dict.get("precio_str", "")),
            "precio_float": producto_dict.get("precio_float"),
            "moneda": producto_dict.get("moneda", "ARS"),
            "categoria_qdrant": producto_dict.get("categoria_qdrant", producto_dict.get("categoria", "")),
            "unidad": producto_dict.get("unidad", ""),
            "marca": producto_dict.get("marca", ""),
            "sku": producto_dict.get("sku", ""),
            "texto_original_para_embedding": producto_dict.get("texto_para_embedding", "")
        }
        payload_limpio = {k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip() != "")}
        
        if not vector or not isinstance(vector, list) or not all(isinstance(num, (int, float)) for num in vector):
            logger.warning(f"[QDRANT_SAVE] Vector inválido o vacío para producto '{payload.get('nombre')}', user_id={user_id}. Saltando.")
            continue

        puntos_para_insertar.append(qdrant_models.PointStruct(
            id=str(uuid.uuid4()), 
            vector=vector, 
            payload=payload_limpio
        ))

    if puntos_para_insertar:
        try:
            logger.info(f"[QDRANT_SAVE] Intentando upsert de {len(puntos_para_insertar)} puntos para user_id={user_id}...")
            qdrant_cli.upsert(collection_name="catalogos", points=puntos_para_insertar, wait=True)
            logger.info(f"✅ {len(puntos_para_insertar)} ítems guardados/actualizados en Qdrant para user_id={user_id}")
        except Exception as e_qdrant_upsert:
            logger.error(f"❌ Error durante upsert a Qdrant para user_id={user_id}: {e_qdrant_upsert}", exc_info=True)
            raise ValueError(f"Fallo al guardar datos en Qdrant: {str(e_qdrant_upsert)}")
    else:
        logger.warning(f"[QDRANT_SAVE] No se prepararon puntos válidos para Qdrant para user_id={user_id}.")


def procesar_y_embedear_catalogo(path_archivo: str, user_id: int, pyme_rubro_nombre: str = "generico") -> int:
    logger.info(f"[UPLOAD_PROC] Iniciando procesamiento y embedding de catálogo: '{os.path.basename(path_archivo)}' para user_id={user_id}, rubro Pyme='{pyme_rubro_nombre}'")
    registros_estructurados: List[Dict[str, Any]] = []

    try:
        _, extension_archivo = os.path.splitext(path_archivo)
        extension_archivo = extension_archivo.lower()

        if extension_archivo == ".pdf":
            logger.info(f"[UPLOAD_PROC] Procesando PDF con Google DocAI: {os.path.basename(path_archivo)}")
            registros_estructurados = procesar_catalogo_pdf_google(path_archivo, user_id, pyme_rubro_nombre) 
        elif extension_archivo in [".xlsx", ".xls", ".csv"]:
            logger.info(f"[UPLOAD_PROC] Procesando EXCEL/CSV: {os.path.basename(path_archivo)}")
            registros_estructurados = procesar_catalogo_excel(path_archivo, user_id, pyme_rubro_nombre)
        else:
            logger.error(f"[UPLOAD_PROC] Tipo de archivo no soportado: {extension_archivo}")
            raise ValueError(f"Tipo de archivo no soportado: {extension_archivo}")

        if not isinstance(registros_estructurados, list):
             logger.error(f"[UPLOAD_PROC] Procesador no devolvió lista para '{os.path.basename(path_archivo)}'. Devolvió: {type(registros_estructurados)}")
             registros_estructurados = [] 

        if not registros_estructurados:
            logger.warning(f"[UPLOAD_PROC] Procesamiento de '{os.path.basename(path_archivo)}' no devolvió registros.")
            return 0 

        logger.info(f"📄 {len(registros_estructurados)} registros extraídos. Ejemplo: {registros_estructurados[0] if registros_estructurados else 'N/A'}")

        textos_para_embedding: List[str] = []
        productos_finales_para_qdrant_y_db: List[Dict[str, Any]] = []

        for i, prod_dict in enumerate(registros_estructurados):
            if not isinstance(prod_dict, dict):
                logger.warning(f"[UPLOAD_PROC] Ítem {i} no es dict, saltando: {prod_dict}"); continue
            nombre = str(prod_dict.get("nombre", "")).strip(); descripcion = str(prod_dict.get("descripcion", "")).strip()
            categoria = str(prod_dict.get("categoria_qdrant", prod_dict.get("categoria", pyme_rubro_nombre))).strip()
            marca = str(prod_dict.get("marca", "")).strip(); sku = str(prod_dict.get("sku", "")).strip(); unidad = str(prod_dict.get("unidad", "")).strip()
            
            partes_texto_embed = [f"Producto: {nombre}" if nombre else "Producto"]
            if marca: partes_texto_embed.append(f"Marca: {marca}")
            if categoria: partes_texto_embed.append(f"Categoría: {categoria}")
            if unidad: partes_texto_embed.append(f"Presentación: {unidad}")
            if sku: partes_texto_embed.append(f"Código/SKU: {sku}")
            descripcion_limpia = limpiar_texto_base(descripcion); nombre_limpio = limpiar_texto_base(nombre)
            if descripcion_limpia and descripcion_limpia != nombre_limpio: partes_texto_embed.append(f"Detalles: {descripcion}")
            texto_combinado = " | ".join(filter(None, partes_texto_embed)).strip()
            
            if texto_combinado and len(texto_combinado) >= 10:
                textos_para_embedding.append(texto_combinado); prod_dict["texto_para_embedding"] = texto_combinado
                productos_finales_para_qdrant_y_db.append(prod_dict)
            else: logger.warning(f"[UPLOAD_PROC] Texto para embedding corto/vacío para '{nombre}' (Índice: {i}), saltando. Texto: '{texto_combinado}'")
        
        if not productos_finales_para_qdrant_y_db: logger.warning("[UPLOAD_PROC] No textos válidos para embedding."); return 0
        logger.info(f"🧠 Textos para embedding: {len(textos_para_embedding)}. Primeros 3: {textos_para_embedding[:3]}"); logger.info("🧬 Generando vectores..."); 
        vectores = embed_textos(textos_para_embedding, input_type="search_document")
        
        if not vectores or len(vectores) != len(productos_finales_para_qdrant_y_db): 
            logger.error(f"[UPLOAD_PROC] Error vectores. Esperados {len(productos_finales_para_qdrant_y_db)}, obtenidos {len(vectores if vectores else [])}.")
            raise ValueError("Fallo generación/desajuste vectores.")
        logger.info(f"🧬 Vectores generados: {len(vectores)}. Dim primer vector: {len(vectores[0]) if vectores and isinstance(vectores[0], list) else 'N/A'}")

        guardar_en_qdrant(user_id, productos_finales_para_qdrant_y_db, vectores)
        
        items_para_db_sql: List[CatalogoItem] = [CatalogoItem(user_id=user_id, nombre=str(p.get("nombre", "S/N"))[:255], descripcion=str(p.get("descripcion", ""))[:1024], precio=str(p.get("precio_str", ""))[:50], cantidad=str(p.get("cantidad_disponible", "1"))[:50], categoria=str(p.get("categoria_qdrant", p.get("categoria", "")))[:100], unidad=str(p.get("unidad", ""))[:50], sku=str(p.get("sku", ""))[:100], marca=str(p.get("marca", ""))[:100], texto_embedding=p.get("texto_para_embedding", "")) for p in productos_finales_para_qdrant_y_db]
        
        if items_para_db_sql:
            try: CatalogoItem.query.filter_by(user_id=user_id).delete(); db.session.bulk_save_objects(items_para_db_sql); db.session.commit(); logger.info(f"✅ {len(items_para_db_sql)} ítems guardados en DB relacional user_id={user_id}")
            except Exception as e_db: db.session.rollback(); logger.error(f"❌ Error guardando en DB relacional user_id={user_id}: {e_db}", exc_info=True); raise ValueError(f"Error al guardar catálogo en DB: {str(e_db)}")
        
        logger.info(f"🎉 Proceso catálogo completado: {len(productos_finales_para_qdrant_y_db)} ítems procesados/guardados para user_id={user_id}")
        return len(productos_finales_para_qdrant_y_db)
    except ValueError as ve: logger.warning(f"[UPLOAD_PROC] Error Valor procesar_y_embedear_catalogo user_id={user_id}: {str(ve)}"); raise
    except Exception as e_inesperado: logger.error(f"❌ [UPLOAD_PROC] Error Genérico Severo procesar_y_embedear_catalogo user_id={user_id}: {str(e_inesperado)}", exc_info=True); raise ValueError(f"Error interno grave al procesar catálogo.")


@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
    user: Optional[User] = None 
    ruta_guardado_temporal: Optional[str] = None 
    # Obtener UPLOAD_FOLDER basado en el directorio de trabajo actual
    # Esto es seguro porque se ejecuta dentro del contexto de la solicitud
    directorio_base_para_uploads = os.getcwd() 
    upload_folder_path = os.path.join(directorio_base_para_uploads, UPLOAD_FOLDER_NAME) # UPLOAD_FOLDER_NAME es "temp_uploads"

    try:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token: return jsonify({"error": "Token no proporcionado."}), 401
        user = User.query.filter_by(token=token).first()
        if not user: return jsonify({"error": "Token inválido."}), 401
        if 'file' not in request.files: return jsonify({"error": "No se encontró archivo."}), 400
        archivo = request.files.get("file")
        if not archivo or not archivo.filename: return jsonify({"error": "Archivo no válido."}), 400
        if not extension_valida(archivo.filename): return jsonify({"error": "Formato no permitido. Aceptados: " + ", ".join(ALLOWED_EXTENSIONS)}), 400
        
        # Asegurar que el directorio UPLOAD_FOLDER exista
        if not os.path.exists(upload_folder_path):
            try:
                os.makedirs(upload_folder_path, exist_ok=True)
                logger.info(f"Directorio UPLOAD_FOLDER '{upload_folder_path}' creado.")
            except OSError as e:
                logger.error(f"Error creando UPLOAD_FOLDER '{upload_folder_path}': {e}")
                return jsonify({"error": "Error interno al crear directorio de subida."}), 500

        nombre_empresa_seguro = limpiar_texto_base(user.nombre_empresa if user.nombre_empresa else "pyme").replace(" ", "_")
        _, extension_archivo_segura = os.path.splitext(secure_filename(archivo.filename))
        nombre_archivo_unico = f"user_{user.id}_{nombre_empresa_seguro[:15]}_{uuid.uuid4().hex[:6]}{extension_archivo_segura}"
        ruta_guardado_temporal = os.path.join(upload_folder_path, nombre_archivo_unico) # Usar la ruta construida
        
        archivo.save(ruta_guardado_temporal)
        logger.info(f"📂 Archivo '{archivo.filename}' (guardado como '{nombre_archivo_unico}') en: {ruta_guardado_temporal} para User ID: {user.id}")

        pyme_rubro_nombre = "generico"
        if user.rubro_id:
            rubro_obj = db.session.get(Rubro, user.rubro_id)
            if rubro_obj and rubro_obj.nombre: pyme_rubro_nombre = rubro_obj.nombre.lower().strip()
        logger.info(f"[UPLOAD_PROC] Rubro Pyme para procesamiento: {pyme_rubro_nombre}")

        logger.info(f"Intentando eliminar catálogo anterior en Qdrant para user_id={user.id}...")
        try:
            qdrant_cli = get_qdrant_client()
            if qdrant_cli:
                qdrant_cli.delete(
                    collection_name="catalogos",
                    points_selector=qdrant_models.FilterSelector(
                        filter=qdrant_models.Filter(must=[qdrant_models.FieldCondition(key="user_id", match=qdrant_models.MatchValue(value=user.id))])
                    ), wait=True
                )
                logger.info(f"✅ Intento de eliminación Qdrant para user_id={user.id} completado.")
            else: logger.error(f"⚠️ No cliente Qdrant para eliminar puntos de user_id={user.id}.")
        except Exception as e_del_q: logger.error(f"⚠️ Error eliminando en Qdrant para user_id={user.id}: {e_del_q}", exc_info=True)
        
        cantidad_procesada = procesar_y_embedear_catalogo(ruta_guardado_temporal, user.id, pyme_rubro_nombre=pyme_rubro_nombre)
        
        mensaje_exito = f"✅ Catálogo procesado. Se { 'han' if cantidad_procesada != 1 else 'ha'} encontrado e indexado {cantidad_procesada} { 'producto' if cantidad_procesada == 1 else 'productos'}."
        if cantidad_procesada == 0: mensaje_exito = "⚠️ El archivo fue procesado, pero no se encontraron productos válidos. Revisa el formato y contenido de tu archivo."
        return jsonify({"mensaje": mensaje_exito, "items_procesados": cantidad_procesada}), 200
    except ValueError as ve: 
        logger.warning(f"[UPLOAD_PROC] Error de Valor en /subir_catalogo (user {getattr(user, 'id', 'N/A')}): {str(ve)}")
        return jsonify({"error": f"Error al procesar el catálogo: {str(ve)}"}), 400
    except Exception as e_global: 
        logger.error(f"❌ [UPLOAD_PROC] Error inesperado severo en /subir_catalogo (user {getattr(user, 'id', 'N/A')}): {str(e_global)}", exc_info=True)
        return jsonify({"error": "Error interno inesperado. Por favor, intenta más tarde."}), 500
    finally:
        if ruta_guardado_temporal and os.path.exists(ruta_guardado_temporal):
            try: os.remove(ruta_guardado_temporal); logger.info(f"🗑️ Archivo temporal '{ruta_guardado_temporal}' eliminado.")
            except Exception as e_rem: logger.error(f"🔥 Error eliminando temporal '{ruta_guardado_temporal}': {e_rem}", exc_info=True)
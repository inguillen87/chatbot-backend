import os
import uuid
import logging
import traceback
import re
import shutil
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro, ArchivoAdjunto
from services.embedding_service import embed_textos_gemini as embed_textos

from services.google_docai import procesar_catalogo_pdf_google, procesar_catalogo_imagen_google
from services.procesar_catalogo_excel import procesar_catalogo_excel

from .common_utils import limpiar_texto_base # Changed from .utils

from services.qdrant_utils import (
    get_qdrant_client,
    verificar_y_crear_coleccion_qdrant,
)
from services.qdrant_search import CATALOGO_PYME, CATALOGO_MUNICIPIO
from services.logic import es_rubro_publico
from qdrant_client import models as qdrant_models
from typing import List, Dict, Any, Optional

upload_bp = Blueprint("upload_bp", __name__)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".txt"}
UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")  # Esto anda en cualquier entorno
CATALOGO_FOLDER = os.path.join("data", "catalogos")

def extension_valida(nombre_archivo: str) -> bool:
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id: int, productos_estructurados: List[Dict[str, Any]], vectores: List[List[float]], coleccion: str):
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error(f"[QDRANT_SAVE] No se pudo obtener cliente Qdrant para user_id={user_id}.")
        raise ConnectionError("No se pudo conectar a Qdrant para guardar los datos.")

    vector_dim = len(vectores[0]) if vectores else 1024
    if not verificar_y_crear_coleccion_qdrant(coleccion, vector_dim, create_indexes=True):
        raise ConnectionError("No se pudo inicializar la colección en Qdrant.")

    puntos_para_insertar: List[qdrant_models.PointStruct] = []

    if len(productos_estructurados) != len(vectores):
        logger.error(f"[QDRANT_SAVE] Discrepancia crítica: {len(productos_estructurados)} productos vs {len(vectores)} vectores para user_id={user_id}.")
        raise ValueError("Discrepancia crítica entre número de productos y vectores al preparar datos para Qdrant.")

    for producto_dict, vector in zip(productos_estructurados, vectores):
        # Normalizar categoria_producto antes de usarla
        categoria_norm = limpiar_texto_base(
            str(producto_dict.get("categoria_producto", producto_dict.get("categoria", ""))) # Prioriza categoria_producto
        ).lower() or pyme_rubro_nombre # Fallback al rubro de la pyme si no hay categoría específica

        payload = {
            "user_id": user_id,
            "nombre": producto_dict.get("nombre", "Producto Sin Nombre"),
            "descripcion": producto_dict.get("descripcion", ""), # Descripción larga
            "descripcion_corta": producto_dict.get("descripcion_corta", ""),
            "precio_str": str(producto_dict.get("precio_str", "")),
            "precio_float": producto_dict.get("precio_float"),
            "moneda": producto_dict.get("moneda", "ARS"),
            "categoria_qdrant": categoria_norm, # Categoría normalizada
            "unidad_original": producto_dict.get("unidad", ""), # Original string e.g. "Caja x 6 botellas"
            "unidad_descripcion": producto_dict.get("unidad_parsed", producto_dict.get("unidad", "")), # Parsed e.g. "Caja botellas" or fallback
            "cantidad_empaque": producto_dict.get("cantidad_empaque"), # Parsed e.g. 6 or None
            "marca": producto_dict.get("marca", ""),
            "sku": producto_dict.get("sku", ""),
            "stock": producto_dict.get("stock", ""), # Puede ser numérico o texto como "disponible"
            "promocion_texto": producto_dict.get("promocion_texto", ""),
            "talles": producto_dict.get("talles", ""),
            "colores": producto_dict.get("colores", ""),
            "texto_original_para_embedding": producto_dict.get("texto_para_embedding", ""),
            "db_id": producto_dict.get("db_id") # Ensure this is passed in producto_dict
        }
        payload_limpio = {k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip() != "")}
        if not payload_limpio.get("db_id"): # Critical: db_id must be present
            logger.error(f"[QDRANT_SAVE] Producto '{payload.get('nombre')}' no tiene db_id. Saltando.")
            continue

        if not vector or not isinstance(vector, list) or not all(isinstance(num, (float, int)) for num in vector):
            logger.warning(f"[QDRANT_SAVE] Vector inválido o vacío para producto '{payload.get('nombre')}', user_id={user_id}. Saltando este punto.")
            continue

        puntos_para_insertar.append(qdrant_models.PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload=payload_limpio
        ))

    if puntos_para_insertar:
        try:
            logger.info(f"[QDRANT_SAVE] Intentando upsert de {len(puntos_para_insertar)} puntos para user_id={user_id}...")
            qdrant_cli.upsert(collection_name=coleccion, points=puntos_para_insertar, wait=True)
            logger.info(f"✅ {len(puntos_para_insertar)} ítems guardados/actualizados en Qdrant para user_id={user_id}")
        except Exception as e_qdrant_upsert:
            logger.error(f"❌ Error durante upsert a Qdrant para user_id={user_id}: {e_qdrant_upsert}", exc_info=True)
            raise ValueError(f"Fallo al guardar datos en Qdrant: {str(e_qdrant_upsert)}")
    else:
        logger.warning(f"[QDRANT_SAVE] No se prepararon puntos válidos para Qdrant para user_id={user_id}. Ningún ítem fue enviado.")

def procesar_y_embedear_catalogo(path_archivo: str, user_id: int, pyme_rubro_nombre: str = "generico", coleccion: str = CATALOGO_PYME) -> int:
    logger.info(f"[UPLOAD_PROC] Iniciando procesamiento y embedding de catálogo: '{os.path.basename(path_archivo)}' para user_id={user_id}, rubro Pyme='{pyme_rubro_nombre}'")
    registros_estructurados: List[Dict[str, Any]] = []

    try:
        _, extension_archivo = os.path.splitext(path_archivo)
        extension_archivo = extension_archivo.lower()

        if extension_archivo in [".xlsx", ".xls", ".csv"]:
            registros_estructurados = procesar_catalogo_excel(path_archivo)
        elif extension_archivo == ".pdf":
            registros_estructurados = procesar_catalogo_pdf_google(path_archivo, user_id)
        elif extension_archivo in [".png", ".jpg", ".jpeg"]:
            registros_estructurados = procesar_catalogo_imagen_google(path_archivo, user_id)
        else:
            # Fallback a genérico para .doc, .docx, .txt
            from services.generic_file_processor import procesar_archivo_generico
            from mimetypes import guess_type
            mime_type, _ = guess_type(path_archivo)
            if mime_type:
                resultado_generico = procesar_archivo_generico(path_archivo, mime_type)
                if resultado_generico and resultado_generico.get("analisis_gemini"):
                    registros_estructurados = resultado_generico["analisis_gemini"]
                    if isinstance(registros_estructurados, dict) and "productos" in registros_estructurados:
                        registros_estructurados = registros_estructurados["productos"]
                else:
                    registros_estructurados = []
            else:
                 raise ValueError(f"Tipo de archivo no soportado: {extension_archivo}")

        if not isinstance(registros_estructurados, list):
            logger.error(f"[UPLOAD_PROC] El procesador de archivos no devolvió una lista para '{os.path.basename(path_archivo)}'. Devolvió: {type(registros_estructurados)}")
            registros_estructurados = []

        if not registros_estructurados:
            logger.warning(f"[UPLOAD_PROC] El procesamiento del archivo '{os.path.basename(path_archivo)}' no devolvió registros estructurados o la lista está vacía.")
            return 0

        logger.info(f"📄 {len(registros_estructurados)} registros extraídos del archivo. Ejemplo primer registro (si existe): {registros_estructurados[0] if registros_estructurados else 'N/A'}")

        textos_para_embedding: List[str] = []
        productos_finales_para_qdrant_y_db: List[Dict[str, Any]] = []

        for i, prod_dict in enumerate(registros_estructurados):
            if not isinstance(prod_dict, dict):
                logger.warning(f"[UPLOAD_PROC] Ítem {i} no es un diccionario, saltando: {prod_dict}")
                continue

            nombre = str(prod_dict.get("nombre", "")).strip()
            descripcion = str(prod_dict.get("descripcion", "")).strip()
            categoria = str(prod_dict.get("categoria_qdrant", prod_dict.get("categoria", pyme_rubro_nombre))).strip()
            marca = str(prod_dict.get("marca", "")).strip()
            sku = str(prod_dict.get("sku", "")).strip()
            
            # Get original and parsed unit information
            unidad_original = str(prod_dict.get("unidad", "")).strip() # e.g., "Caja x 6 botellas"
            unidad_parsed_desc = str(prod_dict.get("unidad_parsed", "")).strip() # e.g., "Caja botellas"
            cantidad_empaque_val = prod_dict.get("cantidad_empaque") # e.g., 6 or None

            talles = str(prod_dict.get("talles", "")).strip()
            colores = str(prod_dict.get("colores", "")).strip()

            partes_texto_embed = []
            if nombre: partes_texto_embed.append(f"Producto: {nombre}")
            else: continue # Skip if no name

            if marca: partes_texto_embed.append(f"Marca: {marca}")
            if categoria: partes_texto_embed.append(f"Categoría: {categoria}")
            
            # Construct a descriptive presentacion_texto for embedding
            presentacion_texto_para_embed = unidad_original # Default to original string
            if unidad_parsed_desc and cantidad_empaque_val is not None and cantidad_empaque_val > 0:
                presentacion_texto_para_embed = f"{unidad_parsed_desc} (empaque de {cantidad_empaque_val})"
            elif unidad_parsed_desc: # Only parsed description, no quantity (or quantity is 1 or None)
                presentacion_texto_para_embed = unidad_parsed_desc
            
            if presentacion_texto_para_embed: # Use the constructed text
                partes_texto_embed.append(f"Presentación: {presentacion_texto_para_embed}")
            elif unidad_original: # Fallback if somehow presentacion_texto_para_embed is empty but original is not
                partes_texto_embed.append(f"Presentación: {unidad_original}")

            if talles: partes_texto_embed.append(f"Talles: {talles}")
            if colores: partes_texto_embed.append(f"Colores: {colores}")
            if sku: partes_texto_embed.append(f"Código/SKU: {sku}")

            descripcion_limpia = limpiar_texto_base(descripcion)
            nombre_limpio = limpiar_texto_base(nombre)
            if descripcion_limpia and descripcion_limpia != nombre_limpio:
                partes_texto_embed.append(f"Detalles: {descripcion}")

            texto_combinado = " | ".join(filter(None, partes_texto_embed)).strip()

            if texto_combinado and len(texto_combinado) >= 10:
                textos_para_embedding.append(texto_combinado)
                prod_dict["texto_para_embedding"] = texto_combinado
                productos_finales_para_qdrant_y_db.append(prod_dict)
            else:
                logger.warning(f"[UPLOAD_PROC] Texto para embedding demasiado corto o vacío para producto '{nombre}' (Índice: {i}), saltando. Texto generado: '{texto_combinado}'")

        if not productos_finales_para_qdrant_y_db:
            logger.warning("[UPLOAD_PROC] No se generaron textos válidos para embedding después de procesar todos los registros.")
            return 0

        logger.info(f"🧠 Textos para embedding preparados (Total: {len(textos_para_embedding)}). Primeros 3 (si hay): {textos_para_embedding[:3]}")
        logger.info("🧬 Generando vectores con Cohere...")
        vectores = embed_textos(textos_para_embedding, input_type="search_document")

        if not vectores or len(vectores) != len(productos_finales_para_qdrant_y_db):
            logger.error(f"[UPLOAD_PROC] Error en generación de vectores. Se esperaban {len(productos_finales_para_qdrant_y_db)} vectores, se obtuvieron {len(vectores if vectores else [])}.")
            raise ValueError("Fallo en la generación de vectores o desajuste con productos.")

        logger.info(f"🧬 Vectores generados: {len(vectores)}. Dimensión del primer vector (si existe): {len(vectores[0]) if vectores and isinstance(vectores[0], list) else 'N/A'}")

        guardar_en_qdrant(user_id, productos_finales_para_qdrant_y_db, vectores, coleccion)

        items_para_db_sql: List[CatalogoItem] = []
        for prod_dict_final in productos_finales_para_qdrant_y_db:
            items_para_db_sql.append(
                CatalogoItem(
                    user_id=user_id,
                    nombre=str(prod_dict_final.get("nombre", "S/N"))[:255],
                    descripcion=str(prod_dict_final.get("descripcion", ""))[:1024], # Descripcion larga
                    descripcion_corta=str(prod_dict_final.get("descripcion_corta", ""))[:512], # Nuevo campo
                    promocion_info=str(prod_dict_final.get("promocion_texto", ""))[:255], # Nuevo campo
                    precio=str(prod_dict_final.get("precio_str", ""))[:50],
                    cantidad=str(prod_dict_final.get("stock", "0"))[:50], # Mapea 'stock' a 'cantidad'
                    categoria=str(prod_dict_final.get("categoria_qdrant", pyme_rubro_nombre))[:100],
                    unidad=str(prod_dict_final.get("unidad", ""))[:50],
                    sku=str(prod_dict_final.get("sku", ""))[:100],
                    marca=str(prod_dict_final.get("marca", ""))[:100],
                    texto=prod_dict_final.get("texto_para_embedding", "")
                )
            )

        if items_para_db_sql:
            try:
                # Importar la función de resumen aquí para evitar importación circular si llm_utils importa algo de upload_processor indirectamente
                from services.llm_utils import resumir_descripcion_producto_llm

                # Procesar descripciones cortas ANTES de bulk_save_objects
                for item_dict in productos_finales_para_qdrant_y_db: # Necesitamos iterar sobre los diccionarios originales
                    desc_larga = str(item_dict.get("descripcion", "")) 
                    desc_corta_extraida = str(item_dict.get("descripcion_corta", ""))
                    
                    if not desc_corta_extraida and desc_larga:
                        desc_corta_generada = resumir_descripcion_producto_llm(desc_larga)
                        item_dict["descripcion_corta_final_para_db"] = desc_corta_generada # Guardar en el dict para usarla abajo
                    else:
                        item_dict["descripcion_corta_final_para_db"] = desc_corta_extraida

                # Reconstruir items_para_db_sql con la descripción corta posiblemente generada
                items_para_db_sql_actualizados: List[CatalogoItem] = []
                for prod_dict_final_actualizado in productos_finales_para_qdrant_y_db:
                    items_para_db_sql_actualizados.append(
                        CatalogoItem(
                            user_id=user_id,
                            nombre=str(prod_dict_final_actualizado.get("nombre", "S/N"))[:255],
                            descripcion=str(prod_dict_final_actualizado.get("descripcion", ""))[:1024],
                            descripcion_corta=str(prod_dict_final_actualizado.get("descripcion_corta_final_para_db", ""))[:512], # Usar el campo actualizado
                            promocion_info=str(prod_dict_final_actualizado.get("promocion_texto", ""))[:255],
                            precio=str(prod_dict_final_actualizado.get("precio_str", ""))[:50],
                            cantidad=str(prod_dict_final_actualizado.get("stock", "0"))[:50],
                            categoria=str(prod_dict_final_actualizado.get("categoria_qdrant", pyme_rubro_nombre))[:100],
                            unidad=str(prod_dict_final_actualizado.get("unidad", ""))[:50],
                            sku=str(prod_dict_final_actualizado.get("sku", ""))[:100],
                            marca=str(prod_dict_final_actualizado.get("marca", ""))[:100],
                            texto=prod_dict_final_actualizado.get("texto_para_embedding", "")
                        )
                    )
                
                CatalogoItem.query.filter_by(user_id=user_id).delete()
                db.session.bulk_save_objects(items_para_db_sql_actualizados)
                db.session.commit()
                logger.info(f"✅ {len(items_para_db_sql_actualizados)} ítems guardados en DB relacional para user_id={user_id} (desc. cortas procesadas).")
            except Exception as e_db_relacional:
                db.session.rollback()
                logger.error(f"❌ Error guardando en DB relacional para user_id={user_id}: {e_db_relacional}", exc_info=True)
                raise ValueError(f"Error al guardar el catálogo en la base de datos principal: {str(e_db_relacional)}")

        logger.info(f"🎉 Proceso de catálogo completado: {len(productos_finales_para_qdrant_y_db)} ítems procesados y guardados para user_id={user_id}")
        return len(productos_finales_para_qdrant_y_db)

    except ValueError as ve:
        logger.warning(f"[UPLOAD_PROC] Error de Valor en procesar_y_embedear_catalogo para user_id={user_id} (archivo: {os.path.basename(path_archivo)}): {str(ve)}")
        raise
    except Exception as e_inesperado:
        logger.error(f"❌ [UPLOAD_PROC] Excepción Genérica Severa en procesar_y_embedear_catalogo para user_id={user_id} (archivo: {os.path.basename(path_archivo)}): {str(e_inesperado)}", exc_info=True)
        raise ValueError(f"Error interno grave al procesar el catálogo. Por favor, contacta a soporte si el problema persiste.")

@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
    user: Optional[User] = None
    ruta_guardado_temporal: Optional[str] = None

    try:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token no proporcionado. Por favor, inicia sesión de nuevo."}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido o sesión expirada. Por favor, inicia sesión de nuevo."}), 401

        if 'file' not in request.files:
            return jsonify({"error": "No se encontró el archivo en la solicitud."}), 400

        archivo = request.files.get("file")
        if not archivo or not archivo.filename:
            return jsonify({"error": "Archivo no válido o no presente."}), 400

        if not extension_valida(archivo.filename):
            return jsonify({"error": "Formato de archivo no permitido. Solo se aceptan: " + ", ".join(ALLOWED_EXTENSIONS)}), 400

        nombre_empresa_seguro = limpiar_texto_base(user.nombre_empresa if user.nombre_empresa else "pyme").replace(" ", "_")
        nombre_base_seguro, extension_archivo_segura = os.path.splitext(secure_filename(archivo.filename))
        nombre_archivo_unico = f"user_{user.id}_{nombre_empresa_seguro[:15]}_{uuid.uuid4().hex[:6]}{extension_archivo_segura}"

        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        ruta_guardado_temporal = os.path.join(UPLOAD_FOLDER, nombre_archivo_unico)

        archivo.save(ruta_guardado_temporal)
        logger.info(f"📂 Archivo '{archivo.filename}' (guardado como '{nombre_archivo_unico}') en: {ruta_guardado_temporal} para User ID: {user.id}")

        pyme_rubro_nombre = "generico"
        if user.rubro_id:
            rubro_obj = db.session.get(Rubro, user.rubro_id)
            if rubro_obj and rubro_obj.nombre:
                pyme_rubro_nombre = rubro_obj.nombre.lower().strip()
        logger.info(f"[UPLOAD_PROC] Rubro de la Pyme para procesamiento: {pyme_rubro_nombre}")

        coleccion = (
            CATALOGO_MUNICIPIO if user.municipio_id or es_rubro_publico(user.rubro)
            else CATALOGO_PYME
        )
        if not verificar_y_crear_coleccion_qdrant(coleccion, 1024, create_indexes=True):
            logger.error("[UPLOAD_PROC] No se pudo preparar la colección en Qdrant")
            return jsonify({"error": "Error de infraestructura al preparar el catálogo."}), 500

        logger.info(f"Intentando eliminar catálogo anterior en Qdrant para user_id={user.id}...")
        try:
            qdrant_cli = get_qdrant_client()
            if qdrant_cli:
                qdrant_cli.delete(
                    collection_name=coleccion,
                    points_selector=qdrant_models.FilterSelector(
                        filter=qdrant_models.Filter(
                            must=[qdrant_models.FieldCondition(key="user_id", match=qdrant_models.MatchValue(value=user.id))]
                        )
                    ),
                    wait=True
                )
                logger.info(f"✅ Intento de eliminación de catálogo anterior en Qdrant para user_id={user.id} completado.")
            else:
                logger.error(f"⚠️ No se pudo obtener cliente Qdrant para eliminar puntos de user_id={user.id}.")
        except Exception as e_delete_qdrant:
            logger.error(f"⚠️ Error al intentar eliminar catálogo anterior en Qdrant para user_id={user.id}: {e_delete_qdrant}", exc_info=True)

        cantidad_procesada = procesar_y_embedear_catalogo(
            ruta_guardado_temporal,
            user.id,
            pyme_rubro_nombre=pyme_rubro_nombre,
            coleccion=coleccion,
        )

        # Guardar el archivo original para descargas futuras
        os.makedirs(CATALOGO_FOLDER, exist_ok=True)
        ruta_final = os.path.join(CATALOGO_FOLDER, nombre_archivo_unico)
        shutil.move(ruta_guardado_temporal, ruta_final)
        ruta_guardado_temporal = None
        tamano = os.path.getsize(ruta_final)
        url = f"/catalogo/archivo/{nombre_archivo_unico}"
        db.session.add(
            ArchivoAdjunto(
                user_id=user.id,
                filename=nombre_archivo_unico,
                nombre_original=archivo.filename,
                mime=archivo.mimetype,
                tamano=tamano,
                tipo="catalogo",
                url=url,
            )
        )
        db.session.commit()

        mensaje_exito = f"✅ Catálogo procesado. Se { 'han' if cantidad_procesada != 1 else 'ha'} encontrado e indexado {cantidad_procesada} { 'producto' if cantidad_procesada == 1 else 'productos'}."
        if cantidad_procesada == 0:
            mensaje_exito = "⚠️ El archivo fue procesado, pero no se encontraron productos válidos. Revisa el formato y contenido de tu archivo. Asegúrate que tenga encabezados claros como 'Nombre', 'Precio', 'Descripción', etc., y que los productos tengan nombre."

        return jsonify({"mensaje": mensaje_exito, "items_procesados": cantidad_procesada}), 200

    except ValueError as ve:
        logger.warning(f"[UPLOAD_PROC] Error de Valor en /subir_catalogo para user {getattr(user, 'id', 'N/A')}: {str(ve)}")
        return jsonify({"error": f"Error al procesar el catálogo: {str(ve)}"}), 400
    except Exception as e_global:
        logger.error(f"❌ [UPLOAD_PROC] Error inesperado severo en /subir_catalogo para user {getattr(user, 'id', 'N/A')}: {str(e_global)}", exc_info=True)
        return jsonify({"error": "Error interno inesperado al procesar el catálogo. Por favor, intenta más tarde o contacta a soporte."}), 500
    finally:
        if ruta_guardado_temporal and os.path.exists(ruta_guardado_temporal):
            try:
                os.remove(ruta_guardado_temporal)
                logger.info(f"🗑️ Archivo temporal '{ruta_guardado_temporal}' eliminado.")
            except Exception as e_remove:
                logger.error(f"🔥 Error al eliminar archivo temporal '{ruta_guardado_temporal}': {e_remove}", exc_info=True)

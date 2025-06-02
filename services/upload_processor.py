# services/upload_processor.py
import os
import uuid
import logging
# traceback no es necesario importarlo explícitamente si usas logger.error con exc_info=True
# from collections import Counter # No se usa, se puede quitar
import re 
# import json # No se usa json directamente aquí

# NO importes current_app aquí arriba si no lo vas a usar a nivel de módulo
from flask import Blueprint, request, jsonify 
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro 
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google 
from services.procesar_catalogo_excel import procesar_catalogo_excel 
from .utils import limpiar_texto_base 
from services.qdrant_utils import get_qdrant_client
from qdrant_client import models as qdrant_models
from typing import List, Dict, Any, Optional # Asegurar que Optional y otros estén aquí

upload_bp = Blueprint("upload_bp", __name__)
logger = logging.getLogger(__name__) 

# --- CORRECCIÓN DEFINITIVA PARA UPLOAD_FOLDER ---
# Esta es la forma que NO da error al iniciar la app.
# os.getcwd() te da el directorio desde donde se corre Gunicorn (usualmente la raíz de tu proyecto).
UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")
# --- FIN CORRECCIÓN ---

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
# ... EL RESTO DE TU ARCHIVO upload_processor.py (las funciones extension_valida, guardar_en_qdrant, 
#     procesar_y_embedear_catalogo, y el endpoint subir_catalogo) SE MANTIENE IGUAL
#     a la última versión completa que te pasé en la respuesta @‶gANVneHZLGe...
#     Solo asegúrate de que la creación de UPLOAD_FOLDER con os.makedirs esté DENTRO de subir_catalogo().

# Usar una carpeta temporal dentro de la instancia de la app o una carpeta designada
# Esto es más seguro y estándar para Flask. 'temp_uploads' en la raíz del proyecto.
UPLOAD_FOLDER = os.path.join(current_app.root_path, "temp_uploads") 
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}

def extension_valida(nombre_archivo: str) -> bool:
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id: int, productos_estructurados: List[Dict[str, Any]], vectores: List[List[float]]):
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error(f"[QDRANT_SAVE] No se pudo obtener cliente Qdrant para user_id={user_id}.")
        # Considerar no levantar ValueError aquí, sino loguear y continuar (quizás solo guardar en DB relacional)
        # o devolver un estado para que el endpoint principal lo maneje.
        # Por ahora, mantenemos el raise para indicar un fallo crítico en esta etapa.
        raise ConnectionError("No se pudo conectar a Qdrant para guardar los datos.")

    puntos_para_insertar: List[qdrant_models.PointStruct] = []
    
    if len(productos_estructurados) != len(vectores):
        logger.error(f"[QDRANT_SAVE] Discrepancia crítica: {len(productos_estructurados)} productos vs {len(vectores)} vectores para user_id={user_id}.")
        # Esto es un error grave de lógica interna, debería detener el proceso.
        raise ValueError("Discrepancia crítica entre número de productos y vectores al preparar datos para Qdrant.")

    for producto_dict, vector in zip(productos_estructurados, vectores):
        # Construir el payload para Qdrant con los campos que quieres que sean buscables o filtrables
        # y que se devuelvan en los resultados de búsqueda.
        payload = {
            "user_id": user_id, # Esencial para filtrar por usuario
            "nombre": producto_dict.get("nombre", "Producto Sin Nombre"),
            "descripcion": producto_dict.get("descripcion", ""),
            "precio_str": str(producto_dict.get("precio_str", "")), # Asegurar que sea string
            "precio_float": producto_dict.get("precio_float"), # Puede ser None
            "moneda": producto_dict.get("moneda", "ARS"),
            "categoria_qdrant": producto_dict.get("categoria_qdrant", producto_dict.get("categoria", "")), # Usar categoria_qdrant si existe
            "unidad": producto_dict.get("unidad", ""),
            "marca": producto_dict.get("marca", ""),
            "sku": producto_dict.get("sku", ""),
            "texto_original_para_embedding": producto_dict.get("texto_para_embedding", "") # Guardar el texto que se usó
        }
        # Eliminar claves con valores None o string vacío del payload para Qdrant
        payload_limpio = {k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip() != "")}
        
        # Validar el vector
        if not vector or not isinstance(vector, list) or not all(isinstance(num, (int, float)) for num in vector):
            logger.warning(f"[QDRANT_SAVE] Vector inválido o vacío para producto '{payload.get('nombre')}', user_id={user_id}. Saltando este punto.")
            continue # Saltar este producto si el vector no es válido

        puntos_para_insertar.append(qdrant_models.PointStruct(
            id=str(uuid.uuid4()), # Generar un ID único para cada punto
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
            raise ValueError(f"Fallo al guardar datos en Qdrant: {str(e_qdrant_upsert)}") # Relanzar para que el endpoint lo maneje
    else:
        logger.warning(f"[QDRANT_SAVE] No se prepararon puntos válidos para Qdrant para user_id={user_id}. Ningún ítem fue enviado.")


def procesar_y_embedear_catalogo(path_archivo: str, user_id: int, pyme_rubro_nombre: str = "generico") -> int:
    logger.info(f"[UPLOAD_PROC] Iniciando procesamiento y embedding de catálogo: '{os.path.basename(path_archivo)}' para user_id={user_id}, rubro Pyme='{pyme_rubro_nombre}'")
    registros_estructurados: List[Dict[str, Any]] = []

    try:
        _, extension_archivo = os.path.splitext(path_archivo)
        extension_archivo = extension_archivo.lower()

        if extension_archivo == ".pdf":
            logger.info(f"[UPLOAD_PROC] Procesando PDF con Google DocAI: {os.path.basename(path_archivo)}")
            # Pasar user_id y pyme_rubro_nombre a la función de procesamiento
            registros_estructurados = procesar_catalogo_pdf_google(path_archivo, user_id, pyme_rubro_nombre) 
        elif extension_archivo in [".xlsx", ".xls", ".csv"]:
            logger.info(f"[UPLOAD_PROC] Procesando EXCEL/CSV: {os.path.basename(path_archivo)}")
            registros_estructurados = procesar_catalogo_excel(path_archivo, user_id, pyme_rubro_nombre)
        else:
            logger.error(f"[UPLOAD_PROC] Tipo de archivo no soportado: {extension_archivo}")
            raise ValueError(f"Tipo de archivo no soportado: {extension_archivo}")

        if not isinstance(registros_estructurados, list):
             logger.error(f"[UPLOAD_PROC] El procesador de archivos no devolvió una lista para '{os.path.basename(path_archivo)}'. Devolvió: {type(registros_estructurados)}")
             registros_estructurados = [] 

        if not registros_estructurados:
            logger.warning(f"[UPLOAD_PROC] El procesamiento del archivo '{os.path.basename(path_archivo)}' no devolvió registros estructurados o la lista está vacía.")
            return 0 

        logger.info(f"📄 {len(registros_estructurados)} registros extraídos del archivo. Ejemplo primer registro (si existe): {registros_estructurados[0] if registros_estructurados else 'N/A'}")

        textos_para_embedding: List[str] = []
        productos_finales_para_qdrant_y_db: List[Dict[str, Any]] = [] # Productos que tienen texto válido para embedding

        for i, prod_dict in enumerate(registros_estructurados):
            if not isinstance(prod_dict, dict):
                logger.warning(f"[UPLOAD_PROC] Ítem {i} no es un diccionario, saltando: {prod_dict}")
                continue

            # --- Construcción Mejorada del Texto para Embedding ---
            nombre = str(prod_dict.get("nombre", "")).strip()
            descripcion = str(prod_dict.get("descripcion", "")).strip()
            # Usar categoria_qdrant si está, sino categoria, sino el rubro de la pyme
            categoria = str(prod_dict.get("categoria_qdrant", prod_dict.get("categoria", pyme_rubro_nombre))).strip()
            marca = str(prod_dict.get("marca", "")).strip()
            sku = str(prod_dict.get("sku", "")).strip()
            unidad = str(prod_dict.get("unidad", "")).strip()
            # No incluir precio en el embedding por defecto, ya que puede cambiar frecuentemente
            # y la búsqueda semántica se basa más en descripción/nombre/categoría.
            
            partes_texto_embed = []
            if nombre: partes_texto_embed.append(f"Producto: {nombre}")
            else: continue # Si no hay nombre, es difícil hacer un embedding útil
            
            if marca: partes_texto_embed.append(f"Marca: {marca}")
            if categoria: partes_texto_embed.append(f"Categoría: {categoria}")
            if unidad: partes_texto_embed.append(f"Presentación: {unidad}")
            if sku: partes_texto_embed.append(f"Código/SKU: {sku}")
            
            # Añadir descripción solo si es informativa y diferente del nombre
            descripcion_limpia = limpiar_texto_base(descripcion)
            nombre_limpio = limpiar_texto_base(nombre)
            if descripcion_limpia and descripcion_limpia != nombre_limpio: 
                partes_texto_embed.append(f"Detalles: {descripcion}") # Usar descripción original para el embedding
            
            texto_combinado = " | ".join(filter(None, partes_texto_embed)).strip()
            
            if texto_combinado and len(texto_combinado) >= 10: # Umbral mínimo para un texto útil
                textos_para_embedding.append(texto_combinado)
                # Guardar el texto exacto que se usó para el embedding en el diccionario del producto
                prod_dict["texto_para_embedding"] = texto_combinado 
                productos_finales_para_qdrant_y_db.append(prod_dict)
            else:
                logger.warning(f"[UPLOAD_PROC] Texto para embedding demasiado corto o vacío para producto '{nombre}' (Índice: {i}), saltando. Texto generado: '{texto_combinado}'")
        
        if not productos_finales_para_qdrant_y_db: # Si después de filtrar, no queda nada
            logger.warning("[UPLOAD_PROC] No se generaron textos válidos para embedding después de procesar todos los registros.")
            return 0

        logger.info(f"🧠 Textos para embedding preparados (Total: {len(textos_para_embedding)}). Primeros 3 (si hay): {textos_para_embedding[:3]}")
        logger.info("🧬 Generando vectores con Cohere...")
        vectores = embed_textos(textos_para_embedding, input_type="search_document") # input_type para documentos del catálogo
        
        if not vectores or len(vectores) != len(productos_finales_para_qdrant_y_db):
            logger.error(f"[UPLOAD_PROC] Error en generación de vectores. Se esperaban {len(productos_finales_para_qdrant_y_db)} vectores, se obtuvieron {len(vectores if vectores else [])}.")
            raise ValueError("Fallo en la generación de vectores o desajuste con productos.")
        
        logger.info(f"🧬 Vectores generados: {len(vectores)}. Dimensión del primer vector (si existe): {len(vectores[0]) if vectores and isinstance(vectores[0], list) else 'N/A'}")

        # Guardar en Qdrant
        guardar_en_qdrant(user_id, productos_finales_para_qdrant_y_db, vectores)
        
        # Guardar en Base de Datos Relacional (SQLAlchemy)
        items_para_db_sql: List[CatalogoItem] = []
        for prod_dict_final in productos_finales_para_qdrant_y_db:
            items_para_db_sql.append(
                CatalogoItem(
                    user_id=user_id,
                    nombre=str(prod_dict_final.get("nombre", "S/N"))[:255], # S/N = Sin Nombre
                    descripcion=str(prod_dict_final.get("descripcion", ""))[:1024],
                    precio=str(prod_dict_final.get("precio_str", ""))[:50], # Guardar el string del precio
                    # podrías añadir precio_float y moneda a CatalogoItem si lo necesitas
                    cantidad=str(prod_dict_final.get("cantidad_disponible", "1"))[:50], # Usar cantidad_disponible
                    categoria=str(prod_dict_final.get("categoria_qdrant", prod_dict_final.get("categoria", "")))[:100],
                    unidad=str(prod_dict_final.get("unidad", ""))[:50],
                    sku=str(prod_dict_final.get("sku", ""))[:100],
                    marca=str(prod_dict_final.get("marca", ""))[:100],
                    texto_embedding=prod_dict_final.get("texto_para_embedding", "") # Guardar el texto usado para el embedding
                )
            )
        
        if items_para_db_sql:
            try:
                # Borrar ítems anteriores del catálogo para este usuario en la DB relacional
                CatalogoItem.query.filter_by(user_id=user_id).delete()
                db.session.bulk_save_objects(items_para_db_sql)
                db.session.commit()
                logger.info(f"✅ {len(items_para_db_sql)} ítems guardados en DB relacional para user_id={user_id}")
            except Exception as e_db_relacional:
                db.session.rollback()
                logger.error(f"❌ Error guardando en DB relacional para user_id={user_id}: {e_db_relacional}", exc_info=True)
                raise ValueError(f"Error al guardar el catálogo en la base de datos principal: {str(e_db_relacional)}")

        logger.info(f"🎉 Proceso de catálogo completado: {len(productos_finales_para_qdrant_y_db)} ítems procesados y guardados para user_id={user_id}")
        return len(productos_finales_para_qdrant_y_db)

    except ValueError as ve: # Errores controlados que queremos mostrar al usuario
        logger.warning(f"[UPLOAD_PROC] Error de Valor en procesar_y_embedear_catalogo para user_id={user_id} (archivo: {os.path.basename(path_archivo)}): {str(ve)}")
        raise # Relanzar para que el endpoint lo capture y devuelva el mensaje de error al usuario
    except Exception as e_inesperado: # Otros errores no esperados
        logger.error(f"❌ [UPLOAD_PROC] Excepción Genérica Severa en procesar_y_embedear_catalogo para user_id={user_id} (archivo: {os.path.basename(path_archivo)}): {str(e_inesperado)}", exc_info=True)
        # Para errores muy inesperados, es mejor dar un mensaje genérico al usuario y loguear el detalle.
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
        
        # Usar una forma más segura de generar el nombre de empresa para el archivo
        nombre_empresa_seguro = limpiar_texto_base(user.nombre_empresa if user.nombre_empresa else "pyme").replace(" ", "_")
        nombre_base_seguro, extension_archivo_segura = os.path.splitext(secure_filename(archivo.filename))
        # Crear un nombre de archivo único más corto
        nombre_archivo_unico = f"user_{user.id}_{nombre_empresa_seguro[:15]}_{uuid.uuid4().hex[:6]}{extension_archivo_segura}"
        
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        ruta_guardado_temporal = os.path.join(UPLOAD_FOLDER, nombre_archivo_unico)
        
        archivo.save(ruta_guardado_temporal)
        logger.info(f"📂 Archivo '{archivo.filename}' (guardado como '{nombre_archivo_unico}') en: {ruta_guardado_temporal} para User ID: {user.id}")

        pyme_rubro_nombre = "generico" # Default
        if user.rubro_id:
            rubro_obj = db.session.get(Rubro, user.rubro_id)
            if rubro_obj and rubro_obj.nombre: 
                pyme_rubro_nombre = rubro_obj.nombre.lower().strip()
        logger.info(f"[UPLOAD_PROC] Rubro de la Pyme para procesamiento: {pyme_rubro_nombre}")

        logger.info(f"Intentando eliminar catálogo anterior en Qdrant para user_id={user.id}...")
        try:
            qdrant_cli = get_qdrant_client()
            if qdrant_cli: # Solo intentar si el cliente se obtuvo correctamente
                qdrant_cli.delete(
                    collection_name="catalogos",
                    points_selector=qdrant_models.FilterSelector(
                        filter=qdrant_models.Filter(
                            must=[qdrant_models.FieldCondition(key="user_id", match=qdrant_models.MatchValue(value=user.id))]
                        )
                    ),
                    wait=True # Esperar a que la operación se complete
                )
                logger.info(f"✅ Intento de eliminación de catálogo anterior en Qdrant para user_id={user.id} completado.")
            else:
                logger.error(f"⚠️ No se pudo obtener cliente Qdrant para eliminar puntos de user_id={user.id}.")
        except Exception as e_delete_qdrant:
            logger.error(f"⚠️ Error al intentar eliminar catálogo anterior en Qdrant para user_id={user.id}: {e_delete_qdrant}", exc_info=True)
        
        cantidad_procesada = procesar_y_embedear_catalogo(ruta_guardado_temporal, user.id, pyme_rubro_nombre=pyme_rubro_nombre)
        
        mensaje_exito = f"✅ Catálogo procesado. Se { 'han' if cantidad_procesada != 1 else 'ha'} encontrado e indexado {cantidad_procesada} { 'producto' if cantidad_procesada == 1 else 'productos'}."
        if cantidad_procesada == 0:
            mensaje_exito = "⚠️ El archivo fue procesado, pero no se encontraron productos válidos. Revisa el formato y contenido de tu archivo. Asegúrate que tenga encabezados claros como 'Nombre', 'Precio', 'Descripción', etc., y que los productos tengan nombre."
        
        return jsonify({"mensaje": mensaje_exito, "items_procesados": cantidad_procesada}), 200

    except ValueError as ve: # Capturar ValueErrors que se relanzan desde procesar_y_embedear_catalogo
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
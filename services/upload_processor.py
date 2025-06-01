# upload_processor.py
import os
import uuid
import logging
import traceback # Aunque no se use explícitamente traceback.print_exc(), logging.error con exc_info=True lo hace.
from collections import Counter # No se usa actualmente, se podría quitar si no es necesario.
import re 
import json # Importado para el placeholder, pero no se usa directamente en este archivo.

from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro # Asegúrate que Rubro esté importado si se usa aquí
from services.cohere_ai import embed_textos

# Estas son TUS funciones que deben existir y funcionar correctamente:
from services.google_docai import procesar_catalogo_pdf_google 
from services.procesar_catalogo_excel import procesar_catalogo_excel 

# DEBES ASEGURARTE DE TENER ESTA FUNCIÓN EN ALGÚN LADO E IMPORTARLA CORRECTAMENTE:
# Ejemplo: from services.utils import limpiar_texto_base 
# Si no la tienes, esta función placeholder básica se usará:
def limpiar_texto_base(texto: str) -> str:
    """
    Placeholder para limpiar_texto_base. Reemplaza con tu import real si tienes una mejor.
    Limpia y normaliza el texto.
    """
    if not texto:
        return ""
    texto_limpio = str(texto).lower() # Asegurar que sea string
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio) 
    texto_limpio = texto_limpio.strip()
    return texto_limpio
# FIN PLACEHOLDER limpiar_texto_base

from services.qdrant_utils import get_qdrant_client
from qdrant_client import models as qdrant_models

upload_bp = Blueprint("upload_bp", __name__)
logger = logging.getLogger(__name__) 

UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads") # Guardar en un directorio temporal en la raíz del proyecto
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}

def extension_valida(nombre_archivo: str) -> bool:
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id: int, productos_estructurados: list, vectores: list):
    qdrant_client = get_qdrant_client()
    puntos = []
    if len(productos_estructurados) != len(vectores):
        logger.error(f"Error Qdrant: Discrepancia en cantidad - Productos ({len(productos_estructurados)}) vs Vectores ({len(vectores)}) para user_id={user_id}.")
        raise ValueError("Discrepancia crítica entre número de productos y vectores al preparar datos para Qdrant.")

    for producto_dict, vector in zip(productos_estructurados, vectores):
        payload = {
            "user_id": user_id,
            "nombre": producto_dict.get("nombre", "Producto Sin Nombre"),
            "descripcion": producto_dict.get("descripcion", ""),
            "precio_str": str(producto_dict.get("precio_str", "")),
            "precio_float": producto_dict.get("precio_float"),
            "moneda": producto_dict.get("moneda", "ARS"),
            "categoria_qdrant": producto_dict.get("categoria", ""),
            "unidad": producto_dict.get("unidad", ""),
            "texto_original_para_embedding": producto_dict.get("texto_para_embedding", "")
        }
        payload_limpio = {k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip() != "")}
        
        if not vector or not hasattr(vector, '__iter__') or not all(isinstance(num, (int, float)) for num in vector):
            logger.warning(f"Vector inválido o vacío para producto '{payload.get('nombre')}', user_id={user_id}. Saltando este punto.")
            continue

        puntos.append(qdrant_models.PointStruct(
            id=str(uuid.uuid4()), 
            vector=vector, 
            payload=payload_limpio
        ))

    if puntos:
        try:
            qdrant_client.upsert(collection_name="catalogos", points=puntos, wait=True)
            logger.info(f"✅ {len(puntos)} ítems guardados/actualizados en Qdrant para user_id={user_id}")
        except Exception as e_qdrant_upsert:
            logger.error(f"❌ Error durante upsert a Qdrant para user_id={user_id}: {e_qdrant_upsert}", exc_info=True)
            raise ValueError(f"Fallo al guardar datos en Qdrant: {str(e_qdrant_upsert)}")
    else:
        logger.warning(f"No se prepararon puntos válidos para Qdrant para user_id={user_id}")


def procesar_y_embedear_catalogo(path_archivo: str, user_id: int, pyme_rubro_nombre: str = "generico"):
    logger.info(f"Iniciando procesamiento de catálogo: '{os.path.basename(path_archivo)}' para user_id={user_id}, rubro Pyme='{pyme_rubro_nombre}'")
    registros_estructurados = [] # Inicializar para asegurar que siempre esté definida

    try:
        _, extension_archivo = os.path.splitext(path_archivo)
        extension_archivo = extension_archivo.lower()

        # --- Llamadas a TUS funciones de procesamiento ---
        if extension_archivo == ".pdf":
            logger.info(f"Procesando PDF con Google DocAI: {os.path.basename(path_archivo)}")
            # Esta es TU función. Debe devolver: list[dict]
            registros_estructurados = procesar_catalogo_pdf_google(path_archivo, pyme_rubro_nombre) 
        elif extension_archivo in [".xlsx", ".xls", ".csv"]:
            logger.info(f"Procesando EXCEL/CSV: {os.path.basename(path_archivo)}")
            # Esta es TU función. Debe devolver: list[dict]
            registros_estructurados = procesar_catalogo_excel(path_archivo, pyme_rubro_nombre)
        else:
            raise ValueError(f"Tipo de archivo no soportado internamente: {extension_archivo}")
        # --- Fin llamadas ---

        if not isinstance(registros_estructurados, list):
             logger.error(f"El procesador de archivos no devolvió una lista para '{os.path.basename(path_archivo)}'. Devolvió: {type(registros_estructurados)}")
             registros_estructurados = [] # Forzar a lista vacía para evitar más errores

        if not registros_estructurados:
            logger.warning(f"El procesamiento del archivo '{os.path.basename(path_archivo)}' no devolvió registros estructurados o la lista está vacía.")
            return 0 

        logger.info(f"📄 {len(registros_estructurados)} registros extraídos del archivo. Ejemplo primer registro (si existe): {registros_estructurados[0] if registros_estructurados else 'N/A'}")

        textos_para_embedding = []
        productos_finales_para_qdrant = []

        for i, prod_dict in enumerate(registros_estructurados):
            if not isinstance(prod_dict, dict):
                logger.warning(f"Item {i} en registros_estructurados no es un diccionario, saltando: {prod_dict}")
                continue

            nombre = str(prod_dict.get("nombre", "")).strip()
            descripcion = str(prod_dict.get("descripcion", "")).strip()
            precio_str = str(prod_dict.get("precio_str", prod_dict.get("precio", ""))).strip()
            categoria = str(prod_dict.get("categoria", "")).strip()
            unidad = str(prod_dict.get("unidad", "")).strip()

            partes_texto_embed = []
            if nombre: partes_texto_embed.append(f"Producto: {nombre}")
            else: partes_texto_embed.append("Producto") 

            if categoria: partes_texto_embed.append(f"Categoría: {categoria}")
            
            descripcion_limpia = limpiar_texto_base(descripcion)
            nombre_limpio = limpiar_texto_base(nombre)
            if descripcion_limpia and descripcion_limpia != nombre_limpio: 
                partes_texto_embed.append(f"Detalles: {descripcion}") # Usar descripción original para el embedding
            
            if precio_str: partes_texto_embed.append(f"Precio: {precio_str}")
            if unidad: partes_texto_embed.append(f"Presentación: {unidad}")
            
            texto_combinado = " | ".join(partes_texto_embed)
            texto_embed_limpio = limpiar_texto_base(texto_combinado) # Limpiar el texto combinado final

            if texto_embed_limpio and len(texto_embed_limpio) >= 10:
                textos_para_embedding.append(texto_embed_limpio)
                prod_dict_qdrant = prod_dict.copy()
                prod_dict_qdrant["texto_para_embedding"] = texto_embed_limpio
                productos_finales_para_qdrant.append(prod_dict_qdrant)
            else:
                logger.warning(f"Texto para embedding demasiado corto o vacío para producto '{nombre}' (Índice: {i}), saltando. Texto generado: '{texto_embed_limpio}'")
        
        if not textos_para_embedding:
            logger.warning("No se generaron textos válidos para embedding después de procesar todos los registros.")
            return 0

        logger.info(f"🧠 Textos para embedding preparados (Total: {len(textos_para_embedding)}). Primeros 3: {textos_para_embedding[:3]}")
        logger.info("🧬 Generando vectores con Cohere...")
        vectores = embed_textos(textos_para_embedding) 
        
        if not vectores:
            logger.error("La función embed_textos no devolvió vectores.")
            raise ValueError("Fallo en la generación de vectores: no se obtuvieron resultados.")
        
        logger.info(f"🧬 Vectores generados: {len(vectores)}. Dimensión del primer vector: {len(vectores[0]) if vectores and isinstance(vectores[0], list) else 'N/A'}")

        if len(vectores) != len(productos_finales_para_qdrant):
            logger.error(f"Error crítico: Desajuste en cantidad de Vectores ({len(vectores)}) vs Productos para Qdrant ({len(productos_finales_para_qdrant)})")
            raise ValueError("Desajuste entre número de productos y vectores generados.")

        guardar_en_qdrant(user_id, productos_finales_para_qdrant, vectores)
        
        items_para_db = []
        for prod_dict_final in productos_finales_para_qdrant:
            items_para_db.append(
                CatalogoItem(
                    user_id=user_id,
                    nombre=str(prod_dict_final.get("nombre", "S/N"))[:255],
                    descripcion=str(prod_dict_final.get("descripcion", ""))[:1024],
                    precio=str(prod_dict_final.get("precio_str", prod_dict_final.get("precio", "")))[:50],
                    cantidad=str(prod_dict_final.get("cantidad", "1"))[:50],
                    categoria=str(prod_dict_final.get("categoria", ""))[:100],
                    unidad=str(prod_dict_final.get("unidad", ""))[:50],
                    texto=prod_dict_final.get("texto_para_embedding", "")
                )
            )
        
        if items_para_db:
            try:
                CatalogoItem.query.filter_by(user_id=user_id).delete()
                db.session.bulk_save_objects(items_para_db)
                db.session.commit()
                logger.info(f"✅ {len(items_para_db)} ítems guardados en DB relacional para user_id={user_id}")
            except Exception as e_db_relacional:
                db.session.rollback()
                logger.error(f"❌ Error guardando en DB relacional para user_id={user_id}: {e_db_relacional}", exc_info=True)
                raise ValueError(f"Error al guardar el catálogo en la base de datos principal: {str(e_db_relacional)}")

        logger.info(f"🎉 Proceso de catálogo completado: {len(productos_finales_para_qdrant)} ítems procesados y guardados para user_id={user_id}")
        return len(productos_finales_para_qdrant)

    except ValueError as ve:
        logger.error(f"❌ Error de Valor en procesar_y_embedear_catalogo para user_id={user_id} (archivo: {os.path.basename(path_archivo)}): {str(ve)}", exc_info=True)
        raise 
    except Exception as e_inesperado:
        logger.error(f"❌ Excepción Genérica Severa en procesar_y_embedear_catalogo para user_id={user_id} (archivo: {os.path.basename(path_archivo)}): {str(e_inesperado)}", exc_info=True)
        raise ValueError(f"Error interno grave al procesar el catálogo: {str(e_inesperado)}")


@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
    user = None 
    ruta_guardado_temporal = None 

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
        
        nombre_empresa_seguro = re.sub(r'[\W_]+', '_', user.nombre_empresa) if user.nombre_empresa else "empresa_desconocida"
        nombre_base_seguro, extension_archivo_segura = os.path.splitext(secure_filename(archivo.filename))
        nombre_archivo_unico = f"user_{user.id}_{nombre_empresa_seguro}_{uuid.uuid4().hex[:8]}{extension_archivo_segura}"
        
        # Crear UPLOAD_FOLDER si no existe
        os.makedirs(UPLOAD_FOLDER, exist_ok=True) # Se crea en la raíz del proyecto ahora
        ruta_guardado_temporal = os.path.join(UPLOAD_FOLDER, nombre_archivo_unico)
        
        archivo.save(ruta_guardado_temporal)
        logger.info(f"📂 Archivo '{archivo.filename}' (guardado como '{nombre_archivo_unico}') en: {ruta_guardado_temporal} para User ID: {user.id}")

        pyme_rubro_nombre = "generico"
        if user.rubro_id:
            rubro_obj = db.session.get(Rubro, user.rubro_id)
            if rubro_obj: 
                pyme_rubro_nombre = rubro_obj.nombre.lower().strip()

        logger.info(f"Intentando eliminar catálogo anterior en Qdrant para user_id={user.id}...")
        try:
            qdrant_client = get_qdrant_client()
            qdrant_client.delete(
                collection_name="catalogos",
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="user_id", 
                                match=qdrant_models.MatchValue(value=user.id)
                            )
                        ]
                    )
                ),
                wait=True
            )
            logger.info(f"✅ Intento de eliminación de catálogo anterior en Qdrant para user_id={user.id} completado.")
        except Exception as e_delete_qdrant:
            logger.error(f"⚠️ Error al intentar eliminar catálogo anterior en Qdrant para user_id={user.id}: {e_delete_qdrant}", exc_info=True)
        
        cantidad_procesada = procesar_y_embedear_catalogo(ruta_guardado_temporal, user.id, pyme_rubro_nombre=pyme_rubro_nombre)
        
        mensaje_exito = f"✅ Catálogo procesado exitosamente. Se { 'han' if cantidad_procesada != 1 else 'ha'} encontrado e indexado {cantidad_procesada} { 'ítem' if cantidad_procesada == 1 else 'ítems'}."
        if cantidad_procesada == 0:
            mensaje_exito = "⚠️ El archivo fue procesado, pero no se encontraron ítems válidos para añadir al catálogo. Revisa el formato y contenido de tu archivo, o asegúrate de que contenga al menos un producto con nombre."
        
        return jsonify({"mensaje": mensaje_exito}), 200

    except ValueError as ve: 
        logger.warning(f"Error de Valor en /subir_catalogo para user {getattr(user, 'id', 'N/A')}: {str(ve)}")
        # Devolver el mensaje de error específico de la excepción ValueError
        return jsonify({"error": f"Error al procesar el catálogo: {str(ve)}"}), 400
    except Exception as e_global: 
        logger.error(f"❌ Error inesperado severo en /subir_catalogo para user {getattr(user, 'id', 'N/A')}: {str(e_global)}", exc_info=True)
        return jsonify({"error": "Error interno inesperado al procesar el catálogo. Por favor, intenta más tarde."}), 500
    finally:
        if ruta_guardado_temporal and os.path.exists(ruta_guardado_temporal):
            try:
                os.remove(ruta_guardado_temporal)
                logger.info(f"🗑️ Archivo temporal '{ruta_guardado_temporal}' eliminado.")
            except Exception as e_remove:
                logger.error(f"🔥 Error al eliminar archivo temporal '{ruta_guardado_temporal}': {e_remove}", exc_info=True)
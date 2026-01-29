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
from services.embedding_service import embed_textos_llm as embed_textos

from services.intelligent_catalog_processor import IntelligentCatalogProcessor
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
        ).lower() or "" # Fallback al rubro de la pyme si no hay categoría específica

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
        # db_id is not strictly required for saving to Qdrant if we generate UUID, but good practice

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

    # Delegate to IntelligentCatalogProcessor which uses OpenAI/OpenSource
    processor = IntelligentCatalogProcessor(user_id=user_id)

    # process_file returns True/False and saves to DB.
    # But this function (procesar_y_embedear_catalogo) is expected to return int (count)
    # and also handle embedding logic which might not be fully inside process_file if we want to use specific embedding logic here.
    # However, IntelligentCatalogProcessor saves to DB but maybe doesn't embed to Qdrant?
    # Let's check IntelligentCatalogProcessor.process_file again.
    # It calls _save_catalog_items -> saves to SQL DB.
    # It does NOT seem to call embedding service or Qdrant.

    # So we need to:
    # 1. Use processor to extract structured data (we need to slightly modify usage or extract logic)
    #    Actually processor.process_file saves to DB. We can read back from DB?
    #    Or better, reuse the logic inside processor to just GET the data here.
    #    processor._process_pdf/image/excel return structured data!

    # Let's instantiate processor and use its internal methods which return data,
    # avoiding the full process_file pipeline that saves to DB immediately if we want to keep logic here.
    # OR better: Let process_file do its job (save to SQL), and then we read from SQL to embed to Qdrant?
    # That seems cleaner but might be slower.

    # Alternative: Refactor this function to call specific processor methods based on file type
    # similar to how it was doing before but calling processor._process_X instead of google methods.

    registros_estructurados: List[Dict[str, Any]] = []

    _, extension_archivo = os.path.splitext(path_archivo)
    extension_archivo = extension_archivo.lower()

    try:
        extracted_text_ignored = None
        if extension_archivo in [".xlsx", ".xls", ".csv"]:
            # Keep existing robust excel logic via processor wrapper or direct
            registros_estructurados = processor._process_excel(path_archivo)
        elif extension_archivo == ".pdf":
            extracted_text_ignored, registros_estructurados = processor._process_pdf(path_archivo)
        elif extension_archivo in [".doc", ".docx"]:
            extracted_text_ignored, registros_estructurados = processor._process_word(path_archivo)
        elif extension_archivo in [".png", ".jpg", ".jpeg"]:
            extracted_text_ignored, registros_estructurados = processor._process_image(path_archivo)
        elif extension_archivo == ".txt":
             # Fallback generic
             from services.generic_file_processor import procesar_archivo_generico
             res = procesar_archivo_generico(path_archivo, 'text/plain')
             if res:
                 registros_estructurados = processor._get_structured_data_from_llm(res['texto_extraido'])
                 registros_estructurados = processor._normalize_data(registros_estructurados)

        if not isinstance(registros_estructurados, list):
            registros_estructurados = []

        if not registros_estructurados:
            logger.warning(f"[UPLOAD_PROC] El procesamiento del archivo '{os.path.basename(path_archivo)}' no devolvió registros estructurados o la lista está vacía.")
            return 0

        logger.info(f"📄 {len(registros_estructurados)} registros extraídos del archivo.")

        textos_para_embedding: List[str] = []
        productos_finales_para_qdrant_y_db: List[Dict[str, Any]] = []

        for i, prod_dict in enumerate(registros_estructurados):
            if not isinstance(prod_dict, dict):
                continue

            nombre = str(prod_dict.get("nombre", "")).strip()
            descripcion = str(prod_dict.get("descripcion", "")).strip()
            categoria = str(prod_dict.get("categoria", pyme_rubro_nombre)).strip() # Normalized key from processor is 'categoria'
            marca = str(prod_dict.get("marca", "")).strip()
            sku = str(prod_dict.get("sku", "")).strip()
            
            # IntelligentProcessor normalize returns keys: 'nombre', 'descripcion', 'precio', 'cantidad', 'sku', 'marca', 'categoria', 'unidad', 'imagen_url'
            unidad = str(prod_dict.get("unidad", "")).strip()
            cantidad = str(prod_dict.get("cantidad", "")).strip()
            talles = str(prod_dict.get("talles", "")).strip()
            colores = str(prod_dict.get("colores", "")).strip()

            partes_texto_embed = []
            if nombre: partes_texto_embed.append(f"Producto: {nombre}")
            else: continue

            if marca: partes_texto_embed.append(f"Marca: {marca}")
            if categoria: partes_texto_embed.append(f"Categoría: {categoria}")
            
            if unidad:
                partes_texto_embed.append(f"Presentación: {unidad}")
            
            if talles: partes_texto_embed.append(f"Talles: {talles}")
            if colores: partes_texto_embed.append(f"Colores: {colores}")
            if sku: partes_texto_embed.append(f"Código/SKU: {sku}")

            if descripcion and descripcion != nombre:
                partes_texto_embed.append(f"Detalles: {descripcion}")

            texto_combinado = " | ".join(filter(None, partes_texto_embed)).strip()

            if texto_combinado and len(texto_combinado) >= 10:
                textos_para_embedding.append(texto_combinado)
                prod_dict["texto_para_embedding"] = texto_combinado

                # Map processor keys to what Qdrant logic expects if different
                prod_dict["categoria_qdrant"] = categoria
                prod_dict["stock"] = cantidad
                prod_dict["precio_str"] = str(prod_dict.get("precio", ""))
                # precio_float parsing is tricky without helper, lets assume string for now or parse
                from services.common_utils import parse_precio_flexible
                _, p_float, p_currency = parse_precio_flexible(prod_dict["precio_str"])
                prod_dict["precio_float"] = p_float
                prod_dict["moneda"] = p_currency or "ARS"

                productos_finales_para_qdrant_y_db.append(prod_dict)

        if not productos_finales_para_qdrant_y_db:
            return 0

        logger.info("🧬 Generando vectores con Cohere...")
        vectores = embed_textos(textos_para_embedding, input_type="search_document")

        guardar_en_qdrant(user_id, productos_finales_para_qdrant_y_db, vectores, coleccion)

        # Save to SQL DB
        items_para_db_sql: List[CatalogoItem] = []
        for prod_dict_final in productos_finales_para_qdrant_y_db:
            # We assume description is already enriched by IntelligentProcessor if needed
            items_para_db_sql.append(
                CatalogoItem(
                    user_id=user_id,
                    nombre=str(prod_dict_final.get("nombre", "S/N"))[:255],
                    descripcion=str(prod_dict_final.get("descripcion", ""))[:1024],
                    descripcion_corta=str(prod_dict_final.get("descripcion_corta", ""))[:512],
                    promocion_info=str(prod_dict_final.get("promocion_texto", ""))[:255],
                    precio=str(prod_dict_final.get("precio_str", ""))[:50],
                    cantidad=str(prod_dict_final.get("stock", "0"))[:50],
                    categoria=str(prod_dict_final.get("categoria_qdrant", pyme_rubro_nombre))[:100],
                    unidad=str(prod_dict_final.get("unidad", ""))[:50],
                    sku=str(prod_dict_final.get("sku", ""))[:100],
                    marca=str(prod_dict_final.get("marca", ""))[:100],
                    texto=prod_dict_final.get("texto_para_embedding", ""),
                    imagen_url=prod_dict_final.get("imagen_url")
                )
            )

        if items_para_db_sql:
            CatalogoItem.query.filter_by(user_id=user_id).delete()
            db.session.bulk_save_objects(items_para_db_sql_actualizados if 'items_para_db_sql_actualizados' in locals() else items_para_db_sql)
            db.session.commit()

        logger.info(f"🎉 Proceso de catálogo completado: {len(productos_finales_para_qdrant_y_db)} ítems procesados y guardados para user_id={user_id}")
        return len(productos_finales_para_qdrant_y_db)

    except ValueError as ve:
        logger.warning(f"[UPLOAD_PROC] Error de Valor: {str(ve)}")
        raise
    except Exception as e_inesperado:
        logger.error(f"❌ [UPLOAD_PROC] Excepción Genérica: {str(e_inesperado)}", exc_info=True)
        raise ValueError(f"Error interno grave al procesar el catálogo.")

@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo(current_user=None):
    user: Optional[User] = current_user
    ruta_guardado_temporal: Optional[str] = None

    try:
        if user is None:
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

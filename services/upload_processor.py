import os
import uuid
import logging
import traceback
from collections import Counter
import re
from flask import Blueprint, request, jsonify # Flask ya está importado
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro # Añadido Rubro si quieres usarlo para tipo_catalogo
from services.cohere_ai import embed_textos
from services.google_docai import procesar_catalogo_pdf_google
from services.procesar_catalogo_excel import procesar_catalogo_excel
from services.qdrant_utils import get_qdrant_client

upload_bp = Blueprint("upload_bp", __name__) # Ya lo tienes
UPLOAD_FOLDER = os.path.join("static", "uploads") # O usa app.config['UPLOAD_FOLDER']
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
# os.makedirs(UPLOAD_FOLDER, exist_ok=True) # Mejor hacerlo en create_app o al inicio

def extension_valida(nombre_archivo):
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS

def guardar_en_qdrant(user_id: int, productos_estructurados: list, vectores: list):
    """
    Guarda los productos con sus campos estructurados y vectores en Qdrant.
    """
    qdrant = get_qdrant_client()
    puntos = []

    if len(productos_estructurados) != len(vectores):
        logging.error(f"Error: El número de productos ({len(productos_estructurados)}) no coincide con el número de vectores ({len(vectores)}). No se guardará en Qdrant.")
        raise ValueError("Discrepancia entre productos y vectores al guardar en Qdrant.")

    for producto_dict, vector in zip(productos_estructurados, vectores):
        # 'producto_dict' es el diccionario que viene de procesar_catalogo_pdf_google o procesar_catalogo_excel
        # Debería tener claves como 'nombre', 'descripcion', 'precio_str', 'precio_float', etc.
        
        # El 'texto_para_embedding' ya se usó para generar el 'vector'.
        # Ahora construimos un payload rico con los campos estructurados.
        payload = {
            "user_id": user_id,
            "nombre": producto_dict.get("nombre", "Nombre no disponible"),
            "descripcion": producto_dict.get("descripcion", ""),
            "precio_str": producto_dict.get("precio_str", ""), # Precio como string para mostrar
            "precio_float": producto_dict.get("precio_float"), # Precio como float para cálculos (puede ser None)
            "moneda": producto_dict.get("moneda", "ARS"),
            # Puedes añadir más campos estructurados que extraigas:
            "categoria_qdrant": producto_dict.get("categoria", ""), # Renombrado para evitar colisión si 'categoria' es palabra clave
            "unidad": producto_dict.get("unidad", ""),
            "texto_original_para_embedding": producto_dict.get("texto_para_embedding", "") # Guardar el texto que se embebió
        }
        # Limpiar valores None del payload para evitar problemas con Qdrant si no los maneja bien
        payload_limpio = {k: v for k, v in payload.items() if v is not None}

        puntos.append({
            "id": str(uuid.uuid4()),
            "vector": vector,
            "payload": payload_limpio
        })

    if puntos:
        qdrant.upsert(
            collection_name="catalogos", # Asegúrate que esta colección exista y tenga el vector_size correcto
            points=puntos,
            wait=True # Esperar a que la operación se complete
        )
        logging.info(f"✅ {len(puntos)} ítems guardados/actualizados en Qdrant para user_id={user_id}")
    else:
        logging.warning(f"No se prepararon puntos para guardar en Qdrant para user_id={user_id}")


def limpiar_textos_para_embedding(textos_originales: list[str]) -> list[str]:
    """Limpia una lista de strings que se usarán para embedding."""
    textos_limpios = [t.strip() for t in textos_originales if t and len(t.strip()) > 10] # Mínimo 10 caracteres
    
    # Eliminar duplicados exactos que podrían ser headers/footers muy repetitivos
    # La lógica de Counter(textos) < 3 podría ser demasiado simple si hay productos legítimamente repetidos.
    # Una mejor aproximación es eliminar duplicados exactos para la lista de embedding.
    # O si se usa Counter, que sea sobre los textos ya procesados.
    
    # Eliminación de duplicados exactos para embedding
    textos_unicos_para_embedding = sorted(list(set(textos_limpios)), key=textos_limpios.index)

    # Opcional: Lógica de Counter si es realmente necesaria para eliminar headers/footers
    # if len(textos_unicos_para_embedding) > 20: # Aplicar solo si hay muchos textos
    #     conteo = Counter(textos_unicos_para_embedding)
    #     textos_filtrados_por_conteo = [t for t in textos_unicos_para_embedding if conteo[t] < 4] # Umbral ajustable
    #     if len(textos_filtrados_por_conteo) > 0 : # Solo usar si no elimina todo
    #         logging.info(f"Textos después de filtro por conteo: {len(textos_filtrados_por_conteo)}")
    #         return textos_filtrados_por_conteo
            
    return textos_unicos_para_embedding


def procesar_y_embedear_catalogo(path: str, user_id: int, pyme_rubro_nombre: str = "generico"):
    """
    Procesa el archivo de catálogo, extrae datos estructurados,
    genera embeddings y los guarda en Qdrant y opcionalmente en DB relacional.
    """
    try:
        ext = os.path.splitext(path)[1].lower()
        logging.info(f"📥 Archivo recibido: {os.path.basename(path)} ({os.path.getsize(path)} bytes)")
        logging.info(f"📦 Extensión: {ext} | 👤 User ID (PYME): {user_id} | Rubro Sugerido: {pyme_rubro_nombre}")

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"❌ Formato de archivo no soportado: {ext}")

        registros_estructurados = [] # Debería ser una lista de diccionarios
        if ext == ".pdf":
            logging.info("🔍 Usando Google Document AI para procesar PDF...")
            # Pasamos el user_id de la PYME a la función de procesamiento por si es útil para logging o contexto
            registros_estructurados = procesar_catalogo_pdf_google(path, tipo_catalogo=pyme_rubro_nombre, pyme_user_id=user_id)
        else: # .csv, .xlsx, .xls
            logging.info("📊 Usando pandas para procesar Excel/CSV...")
            registros_estructurados = procesar_catalogo_excel(path) # Esta función ya devuelve List[dict]

        logging.info(f"🔎 REGISTROS ESTRUCTURADOS EXTRAIDOS (primeros 3 de {len(registros_estructurados)}): {registros_estructurados[:3]}")

        if not registros_estructurados:
            raise ValueError(f"⚠️ No se extrajo contenido estructurado útil del archivo {os.path.basename(path)}")

        # Preparar textos para embedding y mantener los dicts estructurados
        textos_para_embedding = []
        productos_finales_para_qdrant = [] # Lista de dicts estructurados

        for prod_dict in registros_estructurados:
            if not isinstance(prod_dict, dict):
                logging.warning(f"Registro no es un diccionario, se omite: {prod_dict}")
                continue

            # Crear el texto para embedding (puedes refinar qué campos incluir aquí para mejor semántica)
            # Es importante que refleje lo que un usuario podría preguntar.
            nombre = prod_dict.get("nombre", "")
            descripcion = prod_dict.get("descripcion", "")
            precio = prod_dict.get("precio_str", "") # Usar precio_str para el texto de embedding
            categoria = prod_dict.get("categoria", "") # Si lo extraes

            texto_embed = f"Producto: {nombre}"
            if categoria: texto_embed += f" | Categoría: {categoria}"
            if descripcion and descripcion != nombre: texto_embed += f" | Descripción: {descripcion}" # Evitar redundancia
            if precio: texto_embed += f" | Precio: {precio}"
            # Considera añadir más campos relevantes aquí si los extraes (ej. marca, material para indumentaria)
            
            if texto_embed.strip() and len(texto_embed.strip()) > 10: # Chequeo de calidad
                textos_para_embedding.append(limpiar_texto(texto_embed))
                # Añadir el 'texto_para_embedding' al dict para referencia futura si es necesario
                prod_dict_copy = prod_dict.copy() # Trabajar con una copia
                prod_dict_copy["texto_para_embedding"] = limpiar_texto(texto_embed)
                productos_finales_para_qdrant.append(prod_dict_copy)
            else:
                logging.warning(f"Se omitió un registro por texto de embedding vacío o muy corto: {prod_dict}")
        
        if not textos_para_embedding:
            raise ValueError("No se generaron textos válidos para embedding después del procesamiento.")

        # La función limpiar_textos_para_embedding podría no ser necesaria si ya limpiamos al crear texto_embed
        # y si la lista productos_finales_para_qdrant se alinea con textos_para_embedding.
        # Por ahora, la mantenemos para una limpieza general de los textos que se van a embeber.
        # ¡IMPORTANTE! Si limpias/filtras 'textos_para_embedding' aquí, debes asegurar que
        # 'productos_finales_para_qdrant' se filtre de la misma manera para mantener la correspondencia.
        # Es más seguro filtrar ANTES de crear las dos listas separadas o aplicar el mismo filtro a ambas.

        # Simplificación: Asumimos que textos_para_embedding y productos_finales_para_qdrant tienen el mismo orden y cantidad.
        logging.info(f"🧠 Textos preparados para embedding (primeros 3 de {len(textos_para_embedding)}): {textos_para_embedding[:3]}")

        logging.info("🧬 Generando vectores de embedding con Cohere...")
        vectores = embed_textos(textos_para_embedding)
        logging.info(f"🧬 Vectores generados: {len(vectores)} (esperados: {len(textos_para_embedding)})")

        if not vectores or len(vectores) != len(productos_finales_para_qdrant): # Comparar con productos_finales...
            logging.error(f"Error: Discrepancia en cantidad de vectores ({len(vectores)}) y productos procesados ({len(productos_finales_para_qdrant)})")
            raise ValueError("Fallo en generación de vectores o desajuste con productos.")

        # Guardar en Qdrant usando los productos estructurados
        guardar_en_qdrant(user_id, productos_finales_para_qdrant, vectores)

        # Guardar en la base de datos relacional (CatalogoItem)
        # Esta parte ya espera un diccionario, así que debería funcionar bien si
        # procesar_catalogo_pdf_google y procesar_catalogo_excel devuelven la estructura esperada.
        items_para_db = []
        for prod_dict in productos_finales_para_qdrant: # Usar los mismos productos que se guardaron en Qdrant
            items_para_db.append(
                CatalogoItem( # Asegúrate que los campos de CatalogoItem coincidan
                    user_id=user_id,
                    nombre=prod_dict.get("nombre", "")[:255], # Ajustar longitudes si es necesario
                    descripcion=prod_dict.get("descripcion", "")[:1024],
                    precio=str(prod_dict.get("precio_str", prod_dict.get("precio", "")))[:50], # Convertir a string para DB si es necesario
                    # 'cantidad' en tu modelo CatalogoItem es string, ¿de dónde vendría?
                    # Si 'procesar_catalogo_pdf_google' lo extrae, úsalo.
                    cantidad=str(prod_dict.get("cantidad", "1"))[:50], 
                    categoria=prod_dict.get("categoria", "")[:100],
                    unidad=prod_dict.get("unidad", "")[:50],
                    texto=prod_dict.get("texto_para_embedding", "") # Texto que se embebió
                )
            )
        
        if items_para_db:
            try:
                # Borrar catálogo anterior para este user_id en la DB relacional antes de guardar el nuevo
                CatalogoItem.query.filter_by(user_id=user_id).delete()
                db.session.commit() # Cometer la eliminación
                logging.info(f"Catálogo relacional anterior para user_id={user_id} eliminado.")

                db.session.bulk_save_objects(items_para_db)
                db.session.commit()
                logging.info(f"✅ {len(items_para_db)} ítems guardados en DB relacional para user_id={user_id}")
            except Exception as e_db:
                db.session.rollback()
                logging.error(f"❌ Error guardando ítems en DB relacional: {e_db}", exc_info=True)
                # Decide si esto debe ser un error fatal o solo una advertencia

        logging.info(f"🎉 Proceso de catálogo completado: {len(productos_finales_para_qdrant)} ítems procesados para user_id={user_id}")
        return len(productos_finales_para_qdrant)

    except ValueError as ve: # Errores esperados durante el procesamiento
        logging.error(f"❌ Error de Valor procesando catálogo ({os.path.basename(path)}): {str(ve)}")
        # No relanzar aquí para que el endpoint pueda devolver un error 500 específico de ValueError
        raise # Relanzar para que el endpoint lo capture y devuelva 500 con el mensaje de ve
    except Exception as e: # Errores inesperados
        logging.error(f"❌ Excepción no controlada en procesar_y_embedear_catalogo ({os.path.basename(path)}): {str(e)}")
        traceback.print_exc() # Loguear el traceback completo
        raise ValueError(f"Error interno grave al procesar el catálogo: {str(e)}") # Relanzar como ValueError


@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo():
    user = None # Definir user fuera del try para usarlo en el nombre de archivo si es necesario
    try:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token no proporcionado"}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido o expirado"}), 401

        archivo = request.files.get("file")
        if not archivo or not archivo.filename: # Chequeo más robusto
            return jsonify({"error": "Archivo no válido o no presente"}), 400

        if not extension_valida(archivo.filename):
            return jsonify({"error": "Formato de archivo no permitido. Permitidos: " + ", ".join(ALLOWED_EXTENSIONS)}), 400

        # Usar un nombre de empresa más genérico si user.nombre_empresa es None o vacío
        nombre_empresa_seguro = re.sub(r'\W+', '_', user.nombre_empresa) if user.nombre_empresa else "empresa_desconocida"
        
        nombre_seguro = secure_filename(
            f"{nombre_empresa_seguro}_{user.id}_{uuid.uuid4().hex[:8]}{os.path.splitext(archivo.filename)[1]}"
        )
        # Asegurarse que UPLOAD_FOLDER exista (aunque es mejor en create_app)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        ruta_guardado = os.path.join(UPLOAD_FOLDER, nombre_seguro)
        archivo.save(ruta_guardado)
        logging.info(f"📂 Archivo '{archivo.filename}' guardado temporalmente en: {ruta_guardado} para User ID: {user.id}")

        # Determinar el rubro_nombre de la PYME para pasarlo a procesar_catalogo_pdf_google
        pyme_rubro_nombre = "generico"
        if user.rubro_id:
            rubro_obj = Rubro.query.get(user.rubro_id)
            if rubro_obj:
                pyme_rubro_nombre = rubro_obj.nombre.lower().strip()
        
        # Antes de procesar, podrías querer borrar el catálogo anterior de Qdrant para este user_id
        # client_qdrant = get_qdrant_client()
        # client_qdrant.delete(
        #     collection_name="catalogos",
        #     points_selector=models.FilterSelector(
        #         filter=models.Filter(
        #             must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user.id))]
        #         )
        #     )
        # )
        # logging.info(f"Catálogo anterior en Qdrant para user_id={user.id} eliminado (o intento realizado).")


        cantidad_procesada = procesar_y_embedear_catalogo(ruta_guardado, user.id, pyme_rubro_nombre=pyme_rubro_nombre)
        
        # Opcional: Eliminar el archivo subido después de procesarlo si ya no se necesita
        # try:
        #     os.remove(ruta_guardado)
        #     logging.info(f"Archivo temporal {ruta_guardado} eliminado.")
        # except OSError as e_remove:
        #     logging.error(f"Error eliminando archivo temporal {ruta_guardado}: {e_remove}")

        logging.info(f"🎉 Proceso de subida de catálogo completado: {cantidad_procesada} ítems procesados para user_id={user.id}")
        return jsonify({"mensaje": f"✅ Catálogo procesado con {cantidad_procesada} ítems."}), 200

    except ValueError as ve: # Capturar los ValueErrors que relanzamos desde procesar_y_embedear_catalogo
        logging.error(f"Error de Valor en /subir_catalogo: {str(ve)}", exc_info=True) # Loguear con traceback
        return jsonify({"error": str(ve)}), 400 # Usar 400 para errores de datos del cliente o procesamiento
    except Exception as e:
        logging.error(f"❌ Error inesperado en el endpoint /subir_catalogo: {str(e)}", exc_info=True) # Loguear con traceback
        return jsonify({"error": f"Error interno inesperado en el servidor al procesar el catálogo."}), 500
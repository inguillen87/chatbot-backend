# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional 

from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

# --- Carga de Credenciales (mantenida como la última versión que te di) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
# ... (tu lógica de carga de credenciales que ya funcionaba) ...
try:
    ruta_cred_render = "/etc/secrets/google_service_key.json"
    ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json")
    ruta_cred_final = None
    if os.path.exists(ruta_cred_render): ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local): ruta_cred_final = ruta_cred_local
    
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True
        logger.info(f"✅ Credenciales de Google cargadas exitosamente desde: {ruta_cred_final}")
    else:
        logger.error(f"❌ Archivo de credenciales de Google NO encontrado. Rutas: '{ruta_cred_render}', '{ruta_cred_local}'.")
except Exception as e:
    logger.error(f"❌ Error crítico al cargar credenciales de Google: {e}", exc_info=True)
# --- Fin Carga de Credenciales ---

def _get_text_from_layout_segments(text_anchor: documentai.Document.TextAnchor, full_doc_text: str) -> str:
    # ... (mantenida como antes) ...
    response = ""
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start_index = int(segment.start_index); end_index = int(segment.end_index)
            response += full_doc_text[start_index:end_index]
    return limpiar_texto_base(response)

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    # ... (mantenida como la última versión que te di, con chequeo de variables de entorno y process_options) ...
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("Imposible procesar PDF: Credenciales de Google no están cargadas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v for v,k in [("PROJECT_ID",project_id),("LOCATION",location),("PROCESSOR_ID",processor_id)] if not k]
            logger.error(f"❌ Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
            return None
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)
        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        process_options = documentai.ProcessOptions(
            table_extraction_params=documentai.ProcessOptions.TableExtractionParams(enabled=True, model_version="builtin/stable"),
            ocr_config=documentai.OcrConfig(enable_native_pdf_parsing=True, premium_features=documentai.OcrConfig.PremiumFeatures(compute_style_info=False, enable_math_ocr=False)) # Ajusta según necesites
        )
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document, process_options=process_options, skip_human_review=True)
        logger.info(f"Enviando '{os.path.basename(pdf_path)}' a Document AI...")
        result = client.process_document(request=request_doc_ai)
        if result and result.document:
            if not result.document.text and not result.document.tables:
                 logger.warning(f"DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas.")
                 return None
            logger.info(f"Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas: {len(result.document.tables)}")
            return result.document
        logger.error(f"Document AI no devolvió un resultado válido para '{os.path.basename(pdf_path)}'.")
        return None
    except Exception as e:
        logger.error(f"❌ Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    productos_de_tablas: List[Dict[str, Any]] = []
    if not document.tables:
        logger.info("No se encontraron tablas en el documento PDF para procesar mediante Document AI Tables.")
        return productos_de_tablas

    logger.info(f"Detectadas {len(document.tables)} tablas en el PDF. Iniciando extracción estructurada de tablas...")
    full_doc_text = document.text # El texto completo del documento para resolver los TextAnchor

    for table_idx, table_obj in enumerate(document.tables):
        logger.info(f"Procesando Tabla {table_idx + 1} de {len(document.tables)}...")
        if not table_obj.header_rows or not table_obj.body_rows:
            logger.warning(f"Tabla {table_idx + 1} no tiene filas de encabezado o cuerpo suficientes. Se omite.")
            continue

        # --- 1. Extraer y Mapear Encabezados (CRÍTICO: ADAPTAR A TUS PDFs) ---
        # Esta lista de listas de encabezados puede manejar múltiples filas de encabezado
        header_rows_texts: List[List[str]] = []
        for hr_idx, header_row_obj in enumerate(table_obj.header_rows):
            current_header_row_texts = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in header_row_obj.cells]
            header_rows_texts.append(current_header_row_texts)
            logger.debug(f"  Encabezados crudos (fila {hr_idx+1}) de Tabla {table_idx + 1}: {current_header_row_texts}")
        
        if not header_rows_texts or not header_rows_texts[0]: # Si no hay encabezados extraídos
            logger.warning(f"No se pudieron extraer encabezados para la Tabla {table_idx + 1}. Se omite esta tabla.")
            continue

        # TU LÓGICA DE MAPEADO DE ENCABEADOS:
        # Define qué nombres de columna en tus PDFs corresponden a qué campos de producto.
        # Este es un EJEMPLO MUY AMPLIO basado en tus archivos y comunes. Debes refinarlo.
        HEADER_MAP_PDF: Dict[str, List[str]] = {
            "sku": ["código", "cod.", "codigo", "art.", "articulo", "ref.", "referencia", "item", "id"],
            "nombre": ["producto", "descripción", "descripcion", "detalle", "nombre del producto", "articulo", "item name", "designacion"],
            "marca": ["marca", "bodega", "fabricante", "brand"],
            "categoria": ["categoría", "categoria", "linea", "línea", "rubro", "tipo", "variedad", "varietal"], # 'varietal' como categoría si no hay columna específica
            "precio_str": ["precio", "valor", "importe", "pvp", "p.v.p", "contado", "lista", "unitario", "distribuidor", "mayorista", "minorista", "final"],
            "unidad": ["unidad", "presentación", "presentacion", "empaque", "formato", "u/m", "caja x", "pack x", "unid."],
            "stock": ["stock", "disponible", "cantidad", "cant.", "disponibilidad", "existencia"],
            # Añade más campos que necesites y sus posibles nombres de columna
        }
        
        # Intentar encontrar el mapeo de índice de columna a campo de producto
        column_to_field_map: Dict[int, str] = {}
        # Usar la primera fila de encabezados para el mapeo principal
        # Podrías tener lógica más compleja si tus encabezados ocupan varias filas.
        main_headers = header_rows_texts[0] 
        for col_idx, header_text_raw in enumerate(main_headers):
            header_text = limpiar_texto_base(header_text_raw) # Limpiar el encabezado antes de comparar
            for field_name, possible_headers in HEADER_MAP_PDF.items():
                if any(ph.lower() in header_text.lower() for ph in possible_headers):
                    if col_idx not in column_to_field_map: 
                        column_to_field_map[col_idx] = field_name
                        logger.debug(f"    Mapeo Tabla {table_idx+1}: Col Idx {col_idx} ('{header_text_raw}') -> Campo '{field_name}'")
                        break 
        
        if not column_to_field_map or not any(f in ["nombre", "sku"] for f in column_to_field_map.values()): # Requiere al menos nombre o SKU
            logger.warning(f"Tabla {table_idx + 1}: Mapeo de columnas insuficiente (falta 'nombre' o 'sku' obligatorios) o no se pudieron leer encabezados. Se omite tabla.")
            continue
        logger.info(f"  Mapeo de columnas para Tabla {table_idx+1}: {column_to_field_map}")

        # --- 2. Iterar por Filas del Cuerpo de la Tabla ---
        for row_idx, body_row_obj in enumerate(table_obj.body_rows):
            producto_candidato: Dict[str, Any] = {"user_id": pyme_user_id, "categoria": pyme_rubro_nombre} # Default categoria
            
            celdas_fila_actual = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in body_row_obj.cells]
            # logger.debug(f"    Procesando fila {row_idx+1} de tabla {table_idx+1}: {celdas_fila_actual}")

            for cell_idx, cell_text_raw in enumerate(celdas_fila_actual):
                field_name_mapped = column_to_field_map.get(cell_idx)
                if field_name_mapped:
                    cell_text_limpio = limpiar_texto_base(cell_text_raw)
                    # Si el campo ya existe por una celda anterior (ej. celdas combinadas), concatenar con cuidado
                    if field_name_mapped in producto_candidato and producto_candidato[field_name_mapped] and cell_text_limpio:
                        producto_candidato[field_name_mapped] += " | " + cell_text_limpio 
                    elif cell_text_limpio: # Solo asignar si hay texto limpio
                        producto_candidato[field_name_mapped] = cell_text_limpio
            
            # --- 3. Validar y Procesar Datos Extraídos de la Fila ---
            nombre_tabla = limpiar_texto_base(str(producto_candidato.get("nombre", "")))
            if not nombre_tabla or len(nombre_tabla) < 3:
                # logger.debug(f"    Fila {row_idx+1} (Tabla {table_idx+1}) omitida: nombre ausente o muy corto tras extracción de tabla.")
                continue 
            
            producto_final_tabla = {"user_id": pyme_user_id}
            producto_final_tabla["nombre"] = nombre_tabla[:250]
            producto_final_tabla["descripcion"] = limpiar_texto_base(str(producto_candidato.get("descripcion", "")))[:1000]
            if producto_final_tabla["descripcion"] == producto_final_tabla["nombre"]: producto_final_tabla["descripcion"] = ""

            precio_crudo_tabla = producto_candidato.get("precio_str", "") # Usar 'precio_str' si se mapeó así
            precio_s, precio_f, moneda = parse_precio_flexible(precio_crudo_tabla)
            producto_final_tabla["precio_str"] = precio_s if precio_s else ""
            producto_final_tabla["precio_float"] = precio_f
            producto_final_tabla["moneda"] = moneda if moneda else "ARS"
            
            producto_final_tabla["sku"] = limpiar_texto_base(str(producto_candidato.get("sku", "")))[:100]
            producto_final_tabla["unidad"] = limpiar_texto_base(str(producto_candidato.get("unidad", "")))[:50]
            producto_final_tabla["categoria"] = limpiar_texto_base(str(producto_candidato.get("categoria", pyme_rubro_nombre)))[:100]
            producto_final_tabla["marca"] = limpiar_texto_base(str(producto_candidato.get("marca", "")))[:100]
            producto_final_tabla["cantidad_disponible"] = limpiar_texto_base(str(producto_candidato.get("stock", "1")))[:50]

            # Condición mínima para considerar un producto válido de tabla
            if producto_final_tabla["nombre"]: # Podrías añadir: and (producto_final_tabla["precio_str"] or producto_final_tabla["precio_float"] is not None)
                productos_de_tablas.append(producto_final_tabla)
            # else:
                # logger.debug(f"    Fila {row_idx+1} (Tabla {table_idx+1}) omitida por falta de datos clave después del procesamiento: {producto_final_tabla}")
        
    if productos_de_tablas:
         logger.info(f"Se extrajeron {len(productos_de_tablas)} productos válidos del análisis de tablas del PDF.")
    return productos_de_tablas


def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # ... (Tu lógica de extraer_info_producto_de_linea_pdf que ya te pasé y ajustaste, 
    #      asegurándote que devuelve el diccionario con los campos esperados:
    #      nombre, descripcion, precio_str, precio_float, moneda, unidad, tipo_precio, codigo_articulo, categoria)
    # (La versión que te pasé en el mensaje anterior con timestamp @‶gANVneHZLGJ...” era bastante completa para esto)
    # Por brevedad, la omito aquí, pero debe ser la versión refinada que ya tienes.
    # Si quieres que la vuelva a incluir, dímelo. La clave es que devuelva un diccionario como este:
    # return {
    #     "nombre": "Nombre Producto Ejemplo", 
    #     "descripcion": "Desc Ejemplo", 
    #     "precio_str": "100.50", 
    #     "precio_float": 100.50, 
    #     "moneda": "ARS", 
    #     "unidad": "kg", 
    #     "tipo_precio": "minorista", 
    #     "codigo_articulo": "SKU123",
    #     "categoria": pyme_rubro_nombre # o una detectada
    # }
    # Esta es solo una simplificación, tu función es más compleja.
    # Copia aquí tu función `extraer_info_producto_de_linea_pdf` ya mejorada.
    # Si no la tienes a mano, dímelo y te reenvío la última versión que trabajamos.
    logger.debug(f"Procesando línea (lógica de `extraer_info_producto_de_linea_pdf` se aplica aquí): '{linea_procesar}'")
    # ... Aquí iría tu lógica completa de extraer_info_producto_de_linea_pdf ...
    # Solo para que no de error, devuelvo None, pero debes poner tu función completa.
    return None # ¡¡¡REEMPLAZA ESTO CON TU FUNCIÓN COMPLETA!!!


def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    document = _obtener_documento_ai(pdf_path)
    if not document:
        return [] 
    
    productos_extraidos_final: List[Dict[str, Any]] = []

    # --- 1. PROCESAMIENTO DE TABLAS (Prioritario) ---
    if document.tables:
        productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
        if productos_de_tablas:
            productos_extraidos_final.extend(productos_de_tablas)
            logger.info(f"Extracción de tablas aportó {len(productos_de_tablas)} productos.")
    else:
        logger.info(f"PDF '{os.path.basename(pdf_path)}' no contiene tablas detectables por Document AI o la lógica de tablas no está activa.")

    # --- 2. PROCESAMIENTO LÍNEA POR LÍNEA (Fallback o Complementario) ---
    # Ejecutar si las tablas no dieron suficientes resultados o como complemento.
    # Considera un umbral: si las tablas dieron > X productos, quizás no procesar líneas.
    procesar_lineas = True # Por defecto, procesar líneas
    if productos_extraidos_final and len(productos_extraidos_final) > 20: # Ejemplo de umbral
        logger.info(f"Se extrajeron suficientes productos de tablas ({len(productos_extraidos_final)}). Se omite el procesamiento línea por línea detallado para este PDF.")
        # procesar_lineas = False # Descomentar para omitir si hay muchos productos de tablas

    if procesar_lineas and document.text:
        logger.info(f"Iniciando procesamiento línea por línea del PDF '{os.path.basename(pdf_path)}'...")
        lineas_del_documento = document.text.split('\n')
        logger.info(f"Procesando {len(lineas_del_documento)} líneas de texto.")
        
        productos_de_lineas_count = 0
        nombres_ya_en_tabla = {p.get("nombre","").lower() for p in productos_extraidos_final if p.get("nombre")}

        for i, linea_cruda in enumerate(lineas_del_documento):
            linea_strip = linea_cruda.strip()
            # Filtros más estrictos para líneas individuales
            if len(linea_strip) < 8 or \
               (linea_strip.isdigit() and len(linea_strip) <= 4) or \
               len(linea_strip.split()) > 30: # Líneas excesivamente largas suelen ser párrafos, no items
                continue
            
            producto_candidato = extraer_info_producto_de_linea_pdf(linea_strip, pyme_rubro_nombre) # TU FUNCIÓN AQUÍ
            if producto_candidato:
                producto_candidato["user_id"] = user_id 
                # De-duplicación simple si ya se extrajo de tablas (basado en nombre)
                nombre_candidato_lower = producto_candidato.get("nombre","").lower()
                if nombre_candidato_lower and nombre_candidato_lower not in nombres_ya_en_tabla:
                    productos_extraidos_final.append(producto_candidato)
                    productos_de_lineas_count +=1
                elif not nombre_candidato_lower: # Si no tiene nombre pero otros datos, añadirlo
                     productos_extraidos_final.append(producto_candidato)
                     productos_de_lineas_count +=1

        logger.info(f"Procesamiento línea por línea añadió {productos_de_lineas_count} productos nuevos.")
    elif not document.text:
        logger.warning(f"Document AI no extrajo texto del PDF '{os.path.basename(pdf_path)}'. No se puede procesar línea por línea.")
            
    if not productos_extraidos_final:
        logger.warning(f"⚠️ No se pudo extraer ningún producto estructurado del PDF: {os.path.basename(pdf_path)}.")
    else:
        logger.info(f"Total de productos estructurados finales del PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        
    return productos_extraidos_final
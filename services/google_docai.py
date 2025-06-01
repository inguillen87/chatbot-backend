# services/google_docai.py

import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account
# Asegúrate que la ruta a utils.py sea correcta desde este archivo
# Si google_docai.py está en /services y utils.py también, entonces .utils está bien.
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

# --- Carga de Credenciales ---
GOOGLE_CREDENTIALS = None
CREDENTIALS_LOADED_SUCCESSFULLY = False
try:
    # Determinar la ruta del archivo de credenciales
    # Priorizar la ruta de Render, luego una ruta local relativa al proyecto.
    # Asumimos que el script corre desde la raíz del proyecto o que 'instance' está en la raíz.
    ruta_cred_render = "/etc/secrets/google_service_key.json"
    ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json")

    if os.path.exists(ruta_cred_render):
        ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local):
        ruta_cred_final = ruta_cred_local
    else:
        ruta_cred_final = None
        logger.error(f"❌ Archivo de credenciales de Google NO encontrado. Rutas verificadas: '{ruta_cred_render}', '{ruta_cred_local}'.")

    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f:
            credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True
        logger.info(f"✅ Credenciales de Google cargadas exitosamente desde: {ruta_cred_final}")

except Exception as e:
    logger.error(f"❌ Error crítico al cargar o parsear credenciales de Google: {e}", exc_info=True)
    # No relanzar RuntimeError aquí para permitir que la app inicie,
    # las funciones que dependen de esto verificarán CREDENTIALS_LOADED_SUCCESSFULLY.
# --- Fin Carga de Credenciales ---

def _obtener_documento_ai(pdf_path: str) -> documentai.Document | None:
    """
    Función interna para enviar el PDF a Document AI y obtener el objeto Document.
    """
    if not CREDENTIALS_LOADED_SUCCESSFULLY or GOOGLE_CREDENTIALS is None:
        logger.error("Imposible llamar a Document AI: Credenciales de Google no cargadas o inválidas.")
        return None

    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        # Ubicación de tu procesador Document AI (ej. "us" o "eu")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")

        if not all([project_id, location, processor_id]):
            logger.error("❌ Faltan variables de entorno para Google Document AI: GOOGLE_PROJECT_ID, GOOGLE_DOCAI_LOCATION, o GOOGLE_DOCAI_PROCESSOR_ID.")
            return None

        # El endpoint regional se construye así para Document AI
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # Considera habilitar opciones de procesamiento específicas si tu procesador las soporta
        # y si mejora la calidad de la extracción para tus documentos.
        # process_options = documentai.ProcessOptions(
        #     # Ejemplo: Habilitar OCR mejorado o extracción de tablas si es relevante
        #     # ocr_config=documentai.OcrConfig(enable_native_pdf_parsing=True),
        #     # table_extraction_params=documentai.TableExtractionParams(enabled=True, model_version="builtin/stable")
        # )
        # request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document, process_options=process_options)
        
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)
        
        logger.info(f"Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id} en {location})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document and result.document.text:
            logger.info(f"Documento procesado por Document AI. Texto extraído (primeros 100 chars): '{result.document.text[:100]}...'")
        elif result and result.document:
            logger.warning(f"Documento procesado por Document AI, pero no se extrajo texto principal (document.text está vacío). Puede que solo haya imágenes o tablas.")
        else:
            logger.error("Document AI no devolvió un resultado de documento válido.")
            return None
            
        return result.document

    except Exception as e:
        logger.error(f"❌ Error en la llamada a la API de Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

def _get_text_from_layout_segments(text_anchor: documentai.Document.TextAnchor, full_doc_text: str) -> str:
    """Helper para extraer texto de segmentos de layout (usado en tablas y entidades)."""
    response = ""
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start_index = int(segment.start_index)
            end_index = int(segment.end_index)
            response += full_doc_text[start_index:end_index]
    return response.strip()


def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> list[dict]:
    """
    Procesa las tablas extraídas por Document AI.
    ESTA ES UNA IMPLEMENTACIÓN ESQUEMÁTICA. DEBES ADAPTARLA PROFUNDAMENTE
    A LA ESTRUCTURA DE LAS TABLAS EN TUS PDFS Y A CÓMO DOCUMENT AI LAS DEVUELVE.
    """
    productos_de_tablas = []
    if not document.tables:
        logger.info("No se encontraron tablas en el documento PDF para procesar.")
        return productos_de_tablas

    logger.info(f"Procesando {len(document.tables)} tablas encontradas en el PDF...")
    full_doc_text = document.text

    for table_idx, table_obj in enumerate(document.tables):
        logger.info(f"Procesando Tabla {table_idx + 1}...")
        
        # 1. Extraer Encabezados (si existen)
        header_texts = []
        if table_obj.header_rows:
            for header_row in table_obj.header_rows:
                row_texts = [limpiar_texto_base(_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text)) for cell in header_row.cells]
                header_texts.append(row_texts)
                logger.debug(f"  Encabezados detectados en tabla {table_idx+1}: {row_texts}")
        
        # 2. Lógica para Mapear Encabezados a tus Campos Esperados (nombre, precio, sku, etc.)
        #    Esta es la parte más compleja y específica de tus PDFs.
        #    Ejemplo MUY simplificado:
        #    col_map = {} # Ej: {"nombre": 0, "precio": 1, "sku": 2}
        #    if header_texts and header_texts[0]:
        #        for col_idx, header_text in enumerate(header_texts[0]):
        #            if "nombre" in header_text.lower() or "producto" in header_text.lower(): col_map["nombre"] = col_idx
        #            elif "precio" in header_text.lower(): col_map["precio"] = col_idx
        #            # ... y así para otros campos ...
        
        # 3. Iterar por Filas del Cuerpo de la Tabla (body_rows)
        for row_idx, body_row in enumerate(table_obj.body_rows):
            row_values = [limpiar_texto_base(_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text)) for cell in body_row.cells]
            # logger.debug(f"  Fila {row_idx+1} de tabla {table_idx+1}: {row_values}")

            # 4. Extraer datos de las celdas usando tu mapeo de columnas (col_map)
            #    y construir el diccionario 'producto_tabla'.
            #    Ejemplo conceptual:
            #    nombre_tabla = row_values[col_map["nombre"]] if "nombre" in col_map and col_map["nombre"] < len(row_values) else None
            #    precio_crudo_tabla = row_values[col_map["precio"]] if "precio" in col_map and col_map["precio"] < len(row_values) else None
            #    precio_str_t, precio_flt_t, moneda_t = parse_precio_flexible(precio_crudo_tabla)
            #    
            #    if nombre_tabla and (precio_str_t or precio_flt_t): # Condición mínima
            #        producto_tabla = {
            #            "user_id": pyme_user_id,
            #            "nombre": nombre_tabla,
            #            "descripcion": "", # Podrías buscar una columna de descripción o construirla
            #            "precio_str": precio_str_t or "",
            #            "precio_float": precio_flt_t,
            #            "moneda": moneda_t or "ARS",
            #            # ... otros campos ...
            #        }
            #        productos_de_tablas.append(producto_tabla)
            pass # FIN DEL EJEMPLO CONCEPTUAL DE PROCESAMIENTO DE FILA

    if productos_de_tablas:
         logger.info(f"Se extrajeron {len(productos_de_tablas)} productos (conceptuales) de las tablas del PDF.")
    else:
        logger.warning("Procesamiento de tablas no implementado en detalle o no arrojó resultados.")
    return productos_de_tablas


def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> dict | None:
    """Intenta extraer nombre, precio, unidad, etc., de una sola línea de texto de un PDF."""
    linea_limpia = limpiar_texto_base(linea_procesar)
    if not linea_limpia or len(linea_limpia) < 4: # Umbral mínimo de longitud
        return None

    # Definición de patrones (manteniendo tu lógica robusta)
    # Regex para precios (puede necesitar ajustes finos según tus PDFs)
    # Intenta capturar algo que parezca un precio, opcionalmente con símbolo y/o moneda.
    precio_regex = r"((?:[\$\€\£]?\s*\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)|(?:\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*[\$\€\£]?))\s*(ARS|USD|EUR|GBP|CLP)?"
    codigo_pattern = re.compile(r"(\b(?:ART|COD|REF|SKU|ID|ITEM|NRO)\.?\s*[:\-]?\s*[\w\d\/-]+)\b|(\b\d{4,}[\w\d-]*\b)", re.IGNORECASE)
    
    texto_principal = linea_limpia
    texto_precio_crudo = None
    
    # Buscar el precio en la línea. Podría estar en cualquier lado.
    # Esta heurística buscará la última aparición de algo que parezca un precio.
    mejor_match_precio = None
    for match_p in re.finditer(precio_regex, linea_limpia, re.IGNORECASE):
        if re.search(r"\d", match_p.group(1)): # Asegurar que la parte numérica tenga dígitos
            mejor_match_precio = match_p
            
    if mejor_match_precio:
        texto_precio_crudo = limpiar_texto_base(mejor_match_precio.group(1)) # Parte numérica
        moneda_explicita = limpiar_texto_base(mejor_match_precio.group(2)) if mejor_match_precio.group(2) else None

        # Intentar separar el texto principal del precio
        inicio_precio_match = mejor_match_precio.start()
        fin_precio_match = mejor_match_precio.end()
        
        parte_antes = linea_limpia[:inicio_precio_match].strip()
        parte_despues = linea_limpia[fin_precio_match:].strip()

        # Heurística para determinar cuál es el texto principal (nombre/desc)
        if len(parte_antes) > len(parte_despues) and len(parte_antes) > 3:
            texto_principal = parte_antes
        elif len(parte_despues) > 3:
            texto_principal = parte_despues
        else: # Si ambos son cortos o uno está vacío, usar el que tenga más contenido no numérico
            texto_principal = parte_antes if len(re.sub(r'\d','', parte_antes)) > len(re.sub(r'\d','', parte_despues)) else parte_despues
            if not texto_principal.strip(): # Si aún así queda vacío
                texto_principal = linea_limpia.replace(mejor_match_precio.group(0), "").strip()


    # Parsear el precio extraído usando la función de utils
    precio_str_final, precio_float_final, moneda_final = parse_precio_flexible(texto_precio_crudo)
    if moneda_explicita and moneda_final == "ARS": # Si se detectó moneda explícita y parse_precio no la tomó
        moneda_final = moneda_explicita.upper()


    if not texto_principal.strip() and precio_float_final is None:
        # logger.debug(f"[DOCAI LINE] Línea parece vacía o solo basura tras intentar extraer precio: '{linea_limpia}'")
        return None
    if not texto_principal.strip() and precio_float_final is not None:
        # logger.debug(f"[DOCAI LINE] Línea parece ser solo un precio ('{texto_precio_crudo}'). Se descarta si no hay nombre asociado.")
        return None # Descartar si solo se encontró un precio sin texto asociado


    # Extraer unidades y tipos de precio del texto_principal restante
    unidad_ext, tipo_precio_ext = extraer_unidades_y_tipos_precio(texto_principal, pyme_rubro_nombre)
    unidad_final = unidad_ext if unidad_ext else ""
    tipo_precio_final = tipo_precio_ext if tipo_precio_ext else ""

    # Limpiar el texto_principal de unidades y tipos de precio
    texto_nombre_candidato = texto_principal
    if unidad_final:
        texto_nombre_candidato = limpiar_texto_base(re.sub(r'(?i)' + re.escape(unidad_final), '', texto_nombre_candidato, 1))
    if tipo_precio_final:
        texto_nombre_candidato = limpiar_texto_base(re.sub(r'(?i)' + re.escape(tipo_precio_final), '', texto_nombre_candidato, 1))
    
    # Extraer código de artículo
    match_codigo = codigo_pattern.search(texto_nombre_candidato)
    codigo_articulo_extraido = ""
    if match_codigo:
        codigo_encontrado = limpiar_texto_base(match_codigo.group(0))
        # Evitar que códigos numéricos largos que podrían ser parte del nombre se tomen como SKU si son muy genéricos
        if not (len(codigo_encontrado) >= 4 and codigo_encontrado.isdigit() and not any(k in codigo_encontrado.upper() for k in ["ART", "COD", "REF", "SKU"])):
             codigo_articulo_extraido = codigo_encontrado
             texto_nombre_candidato = limpiar_texto_base(texto_nombre_candidato.replace(codigo_encontrado, "", 1))
    
    nombre_final_producto = limpiar_texto_base(texto_nombre_candidato)
    
    # Asignar descripción: si el nombre final es diferente del texto_principal (después de quitar precio pero antes de quitar unidad/código), usar texto_principal.
    descripcion_final = ""
    if texto_principal != nombre_final_producto and len(texto_principal) > len(nombre_final_producto) + 3 : # Si hubo una limpieza significativa
        descripcion_final = texto_principal # El texto antes de quitar unidades/códigos podría ser la descripción
        if limpiar_texto_base(descripcion_final) == nombre_final_producto: # Evitar redundancia
            descripcion_final = ""
            
    # Filtros de calidad finales
    if not nombre_final_producto or len(nombre_final_producto) < 3:
        # logger.debug(f"[DOCAI LINE] Descarte final (nombre muy corto/vacío): '{linea_limpia}' -> Nombre final: '{nombre_final_producto}'")
        return None

    # Lista de palabras comunes en encabezados o líneas no deseadas
    palabras_irrelevantes = ["articulo", "descripción", "descripcion", "modelo", "talle", "precio", "marca", "linea", "total", 
                             "codigo", "importe", "cantidad", "lista de precios", "colección", "rubro", 
                             "cliente", "fecha", "página", "pagina", "contacto", "teléfono", "telefono", "email", 
                             "subtotal", "iva", "descuento", "observaciones", "referencia", "unidad", "presentacion", "categoría"]
    
    nombre_eval_lower = nombre_final_producto.lower()
    # Si el nombre consiste mayormente en palabras irrelevantes y no hay precio, es probable que no sea un producto.
    if precio_float_final is None:
        palabras_nombre = nombre_eval_lower.split()
        if len(palabras_nombre) <= 4: # Para nombres cortos
            coincidencias_irrelevantes = sum(1 for palabra_hdr in palabras_irrelevantes if palabra_hdr in nombre_eval_lower)
            if coincidencias_irrelevantes >= len(palabras_nombre) / 2 and coincidencias_irrelevantes > 0 : # Si la mitad o más son irrelevantes
                 logger.debug(f"[DOCAI LINE] Descarte (posible encabezado/info general sin precio): '{linea_limpia}' -> Nombre final: '{nombre_final_producto}'")
                 return None
        if len(nombre_final_producto) < 10 and nombre_final_producto.isdigit(): # Solo números y corto
            return None

    # Evitar que un "nombre" que es claramente un precio (y no se parseó como tal antes) se tome como nombre.
    nombre_como_precio_info = parse_precio_flexible(nombre_final_producto)
    if nombre_como_precio_info[1] is not None and precio_float_final is None: # Si el "nombre" es un precio y no teníamos precio antes
         logger.debug(f"[DOCAI LINE] Descarte (nombre parece ser solo un precio no detectado antes): '{linea_limpia}' -> Nombre final: '{nombre_final_producto}'")
         return None

    producto_dict = {
        "nombre": nombre_final_producto[:250],
        "descripcion": descripcion_final[:500] if descripcion_final else "", # Poner string vacío si no hay desc
        "precio_str": precio_str_final if precio_str_final else "",
        "precio_float": precio_float_final,
        "moneda": moneda_final if moneda_final else "ARS",
        "unidad": unidad_final[:50],
        "tipo_precio": tipo_precio_final[:50], # Guardar el tipo de precio si se extrajo
        "codigo_articulo": codigo_articulo_extraido[:100],
        "categoria": "", # La categoría es difícil de determinar aquí, se podría asignar después.
    }
    # logger.debug(f"[DOCAI LINE] Producto extraído: {producto_dict}")
    return producto_dict


def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> list[dict]:
    """
    Función principal para procesar un catálogo PDF.
    Intenta usar el texto completo y procesar línea por línea. 
    La extracción de tablas requiere una implementación específica y robusta por tu parte.
    """
    document = _obtener_documento_ai(pdf_path)
    if not document:
        return [] # Error ya logueado en _obtener_documento_ai
    
    productos_extraidos_final = []

    # --- PROCESAMIENTO DE TABLAS (REQUIERE TU IMPLEMENTACIÓN DETALLADA) ---
    # if document.tables:
    #     logger.info(f"PDF '{os.path.basename(pdf_path)}' contiene {len(document.tables)} tablas. Intentando procesarlas...")
    #     productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
    #     if productos_de_tablas:
    #         productos_extraidos_final.extend(productos_de_tablas)
    #         logger.info(f"Extraídos {len(productos_de_tablas)} productos del análisis de tablas.")
    # else:
    #     logger.info(f"PDF '{os.path.basename(pdf_path)}' no contiene tablas detectables por Document AI o la lógica de tablas no está activa.")

    # --- PROCESAMIENTO LÍNEA POR LÍNEA (Como fallback o método principal) ---
    # Considera si ejecutar esto solo si productos_extraidos_final está vacío después de tablas.
    if not document.text:
        logger.warning(f"Document AI no extrajo texto del PDF '{os.path.basename(pdf_path)}'. No se puede procesar línea por línea.")
        return productos_extraidos_final # Devolver lo que se haya obtenido de tablas (si se implementó)

    logger.info(f"Iniciando procesamiento línea por línea del texto del PDF '{os.path.basename(pdf_path)}'...")
    lineas_del_documento = document.text.split('\n')
    logger.info(f"Procesando {len(lineas_del_documento)} líneas de texto.")
    
    productos_de_lineas = []
    for i, linea_cruda in enumerate(lineas_del_documento):
        # Ignorar líneas muy cortas o que parezcan ser solo números de página/basura
        linea_eval = linea_cruda.strip()
        if len(linea_eval) < 5 or re.fullmatch(r"[\s\d\W]*\d{1,3}[\s\d\W]*", linea_eval): # Heurística para saltar líneas de paginación o muy cortas
            # logger.debug(f"Línea {i+1} omitida (corta o posible basura): '{linea_eval[:50]}'")
            continue
            
        producto_candidato = extraer_info_producto_de_linea_pdf(linea_eval, pyme_rubro_nombre)
        if producto_candidato:
            producto_candidato["user_id"] = user_id
            productos_de_lineas.append(producto_candidato)
    
    if productos_de_lineas:
        logger.info(f"Se extrajeron {len(productos_de_lineas)} productos potenciales del procesamiento línea por línea.")
        # Aquí podrías añadir lógica de de-duplicación si procesaste tablas ANTES.
        # Por ahora, si la lógica de tablas no añade nada, esto será el resultado.
        if not productos_extraidos_final: # Si no hubo nada de tablas
            productos_extraidos_final = productos_de_lineas
        else: # Si hubo de tablas, podrías intentar un merge inteligente o simplemente añadir (cuidado con duplicados)
            # Esta es una lógica de merge muy simple basada en nombre, puede necesitar mejoras
            nombres_de_tabla = {p.get("nombre","").lower() for p in productos_extraidos_final if p.get("nombre")}
            for p_linea in productos_de_lineas:
                if p_linea.get("nombre","").lower() not in nombres_de_tabla:
                    productos_extraidos_final.append(p_linea)


    if not productos_extraidos_final:
        logger.warning(f"⚠️ No se pudo extraer ningún producto estructurado del PDF '{os.path.basename(pdf_path)}' después de todos los intentos.")
    else:
        logger.info(f"Total de productos estructurados finales del PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
        # logger.debug(f"Ejemplos de productos finales extraídos: {productos_extraidos_final[:2]}") # Loguear algunos para revisión
        
    return productos_extraidos_final
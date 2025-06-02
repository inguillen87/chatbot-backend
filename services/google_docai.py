# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai # IMPORTANTE: Usar documentai en lugar de documentai_v1beta3 directamente si es más reciente
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional 
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

# --- Carga de Credenciales (Misma que tenías) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_cred_render = "/etc/secrets/google_service_key.json"; ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json"); ruta_cred_final = None
    if os.path.exists(ruta_cred_render): ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local): ruta_cred_final = ruta_cred_local
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True; logger.info(f"✅ Credenciales Google cargadas: {ruta_cred_final}")
    else: logger.error(f"❌ Credenciales Google NO encontradas. Rutas: '{ruta_cred_render}', '{ruta_cred_local}'.")
except Exception as e: logger.error(f"❌ Error crítico cargando credenciales Google: {e}", exc_info=True)
# --- Fin Carga ---

def _get_text_from_layout_segments(text_anchor: Optional[documentai.Document.TextAnchor], full_doc_text: str) -> str:
    response = "";
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            # Asegurarse de que start_index y end_index sean válidos
            start = int(segment.start_index)
            end = int(segment.end_index)
            if 0 <= start <= end <= len(full_doc_text):
                 response += full_doc_text[start:end]
            else:
                logger.warning(f"Segmento de texto inválido: start={start}, end={end}, len_text={len(full_doc_text)}")
    return limpiar_texto_base(response)

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("[DOCAI] Imposible procesar PDF: Credenciales Google no cargadas/inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v_name for v_name,val in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not val]
            logger.error(f"❌ [DOCAI] Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
            return None 
            
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # --- CORRECCIÓN PARA TableExtractionParams ---
        # La forma de especificar opciones de procesamiento, especialmente para tablas,
        # puede depender de la versión del SDK y del tipo de procesador.
        # Si tu procesador está configurado en Google Cloud para extraer tablas,
        # a menudo no necesitas especificar 'table_extraction_params' aquí.
        # Si lo necesitas, la estructura puede ser diferente.
        # Vamos a probar con una configuración de OCR y quitando table_extraction_params explícito
        # para evitar el AttributeError.

        # field_mask especifica qué campos quieres en la respuesta. 
        # Si no lo pones, Document AI devuelve todo lo que el procesador está configurado para extraer.
        # Si tu procesador SÍ extrae tablas, deberían venir en el objeto Document.
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document_proto, 
            skip_human_review=True
            # Si quieres forzar OCR (útil para PDFs que son solo imágenes):
            # process_options=documentai.ProcessOptions(
            #     ocr_config=documentai.OcrConfig(
            #         enable_native_pdf_parsing=True, # Intenta usar el texto del PDF si existe
            #         # Opcional: hint_language_codes=["es"] # Para ayudar al OCR
            #     )
            # )
        )
        # --- FIN CORRECCIÓN ---

        logger.info(f"[DOCAI] Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            # Acceso seguro a 'tables'
            num_tablas = 0
            # El atributo 'tables' podría no estar presente si el procesador no las extrajo
            # o si la versión del SDK lo maneja diferente.
            # En lugar de acceder directamente, podemos ver si el procesador indica que las extrajo.
            # Por ahora, verificamos si el atributo existe.
            if hasattr(result.document, 'tables') and result.document.tables is not None:
                num_tablas = len(result.document.tables)
            
            logger.info(f"[DOCAI] Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas (atributo directo): {num_tablas}")
            
            # Otra forma de ver si hay info tabular es iterar por las páginas y buscar entidades de tabla
            # for page_idx, page in enumerate(result.document.pages):
            #     if page.tables:
            #         logger.info(f"[DOCAI] Página {page_idx+1} tiene {len(page.tables)} tablas.")

            if not result.document.text and num_tablas == 0:
                 logger.warning(f"[DOCAI] DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas con la configuración actual.")
                 # Podríamos devolver None, pero si hay páginas con bloques de texto, procesar_catalogo_pdf_google podría intentar usarlos.
            return result.document
        else:
            logger.error(f"[DOCAI] Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI] Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

# --- Tu función procesar_tablas_document_ai ---
# (ASEGÚRATE DE PERSONALIZAR EL HEADER_MAP_PDF Y LA LÓGICA DE EXTRACCIÓN DE CELDAS)
def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    # ... (Pega aquí la función procesar_tablas_document_ai completa que te di en la respuesta @‶gANVneHZLGZj...,
    #      la que tiene el HEADER_MAP_PDF y el bucle para iterar filas y celdas.
    #      Esta función sigue necesitando TU personalización para el mapeo de columnas.)
    # Ejemplo de la estructura que debería tener:
    productos_de_tablas: List[Dict[str, Any]] = []
    if not hasattr(document, 'tables') or not document.tables: # Chequeo seguro
        logger.info("[DOCAI-TABLES] No se encontraron tablas en el documento (via document.tables).")
        return productos_de_tablas
    logger.info(f"[DOCAI-TABLES] Procesando {len(document.tables)} tablas encontradas...")
    full_doc_text = document.text if document.text else ""
    for table_idx, table_obj in enumerate(document.tables):
        # ... (Tu lógica de HEADER_MAP_PDF y extracción de celdas aquí) ...
        logger.warning(f"[DOCAI-TABLES] Tabla {table_idx+1}: Lógica de extracción no implementada en detalle. Se omite.")
    return productos_de_tablas


# --- Tu función extraer_info_producto_de_linea_pdf ---
# (Pega aquí tu función extraer_info_producto_de_linea_pdf completa y ya mejorada de la respuesta @‶gANVneHZLGZj...)
def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # ...
    return None # Placeholder

# --- Tu función procesar_catalogo_pdf_google ---
# (Pega aquí tu función procesar_catalogo_pdf_google completa de la respuesta @‶gANVneHZLGZj...)
def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    logger.info(f"[DOCAI-MAIN] Iniciando procesamiento PDF: {os.path.basename(pdf_path)} para User ID: {user_id}")
    document = _obtener_documento_ai(pdf_path)
    if not document: return [] 
    productos_extraidos_final: List[Dict[str, Any]] = []
    # Priorizar tablas si existen y la lógica está implementada
    if hasattr(document, 'tables') and document.tables:
        productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
        if productos_de_tablas: productos_extraidos_final.extend(productos_de_tablas)
    # Procesar texto línea por línea como fallback o si no hay tablas (o para complementar)
    if not productos_extraidos_final and document.text: # Solo si tablas no dio nada (o ajusta esta condición)
        logger.info(f"[DOCAI-MAIN] No se extrajeron productos de tablas, procesando líneas del PDF...")
        lineas = document.text.split('\n'); count_line_prods = 0
        for linea in lineas:
            if len(linea.strip()) < 5: continue
            prod = extraer_info_producto_de_linea_pdf(linea, pyme_rubro_nombre)
            if prod: prod["user_id"] = user_id; productos_extraidos_final.append(prod); count_line_prods += 1
        logger.info(f"[DOCAI-MAIN] Procesamiento línea por línea añadió {count_line_prods} productos.")
    if not productos_extraidos_final: logger.warning(f"⚠️[DOCAI-MAIN] No se extrajeron productos del PDF: {os.path.basename(pdf_path)}.")
    else: logger.info(f"[DOCAI-MAIN] Total productos finales PDF: {len(productos_extraidos_final)}")
    return productos_extraidos_final
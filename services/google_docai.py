# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai 
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional 
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_render = "/etc/secrets/google_service_key.json"; ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json"); ruta_cred_final = None
    if os.path.exists(ruta_render): ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local): ruta_cred_final = ruta_cred_local
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True; logger.info(f"✅ [DOCAI] Credenciales Google cargadas: {ruta_cred_final}")
    else: logger.error(f"❌ [DOCAI] Credenciales Google NO encontradas. Rutas: '{ruta_cred_render}', '{ruta_cred_local}'.")
except Exception as e: logger.error(f"❌ [DOCAI] Error crítico cargando credenciales Google: {e}", exc_info=True)

def _get_text_from_layout_segments(text_anchor: Optional[documentai.Document.TextAnchor], full_doc_text: str) -> str:
    response = "";
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start = int(segment.start_index); end = int(segment.end_index)
            if 0 <= start <= end <= len(full_doc_text): response += full_doc_text[start:end]
            else: logger.warning(f"[DOCAI] Segmento de texto inválido: start={start}, end={end}, len_text={len(full_doc_text)}")
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
        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # LLAMADA SIMPLIFICADA A ProcessRequest
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document_proto, 
            skip_human_review=True
        )
        logger.info(f"[DOCAI] Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            num_tablas = 0
            if hasattr(result.document, 'tables') and result.document.tables is not None:
                num_tablas = len(result.document.tables)
            logger.info(f"[DOCAI] Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas (attr directo): {num_tablas}")
            if not result.document.text and num_tablas == 0:
                 logger.warning(f"[DOCAI] DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas.")
            return result.document
        else:
            logger.error(f"[DOCAI] Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI] Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    # ... (Tu lógica de procesar_tablas_document_ai con el HEADER_MAP para personalizar)
    # Esta función ES UN ESQUELETO que DEBES COMPLETAR con tu lógica de mapeo de columnas
    # basado en los encabezados de TUS PDFs.
    productos_de_tablas: List[Dict[str, Any]] = []
    if not hasattr(document, 'tables') or not document.tables:
        logger.info("[DOCAI-TABLES] No se encontraron tablas en el objeto Document (document.tables).")
        return productos_de_tablas
    logger.info(f"[DOCAI-TABLES] Procesando {len(document.tables)} tablas encontradas en el PDF...")
    full_doc_text = document.text if document.text else ""
    HEADER_MAP_PDF: Dict[str, List[str]] = { "sku": ["código", "art."], "nombre": ["producto", "descripción"], "precio_str": ["precio", "pvp"], } # EJEMPLO MUY BÁSICO
    for table_idx, table_obj in enumerate(document.tables):
        logger.info(f"[DOCAI-TABLES] Procesando Tabla {table_idx + 1}...")
        # ... (Tu lógica COMPLETA de mapeo de encabezados y extracción de celdas aquí)...
        logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1}: Lógica de extracción detallada NO implementada. Se omite esta tabla.")
    return productos_de_tablas

def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # ... (Tu lógica completa de extraer_info_producto_de_linea_pdf que ya tenías y estaba bastante bien)
    # Por brevedad, no la repito, pero debe ser la versión robusta que habíamos trabajado.
    # Asegúrate que limpie el texto y use parse_precio_flexible, extraer_unidades_y_tipos_precio.
    return None # Placeholder

def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    # ... (Tu lógica de procesar_catalogo_pdf_google, como la tenías, llamando a _obtener_documento_ai, 
    #      luego procesar_tablas_document_ai, y luego extraer_info_producto_de_linea_pdf)
    logger.info(f"[DOCAI-MAIN] Iniciando procesamiento PDF: {os.path.basename(pdf_path)} para User ID: {user_id}")
    document = _obtener_documento_ai(pdf_path)
    if not document: return [] 
    productos_extraidos_final: List[Dict[str, Any]] = []
    if hasattr(document, 'tables') and document.tables:
        productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
        if productos_de_tablas: productos_extraidos_final.extend(productos_de_tablas); logger.info(f"[DOCAI-MAIN] Tablas aportaron {len(productos_de_tablas)} productos.")
    if not productos_extraidos_final and document.text: 
        logger.info(f"[DOCAI-MAIN] No productos de tablas, procesando líneas del PDF...")
        lineas = document.text.split('\n'); count_line_prods = 0
        for linea_idx, linea_cruda in enumerate(lineas):
            linea_strip = linea_cruda.strip()
            if len(linea_strip) < 5: continue
            prod = extraer_info_producto_de_linea_pdf(linea_strip, pyme_rubro_nombre)
            if prod: prod["user_id"] = user_id; productos_extraidos_final.append(prod); count_line_prods += 1
        logger.info(f"[DOCAI-MAIN] Líneas añadieron {count_line_prods} productos.")
    if not productos_extraidos_final: logger.warning(f"⚠️[DOCAI-MAIN] No se extrajeron productos del PDF: {os.path.basename(pdf_path)}.")
    else: logger.info(f"[DOCAI-MAIN] Total productos PDF: {len(productos_extraidos_final)}")
    return productos_extraidos_final
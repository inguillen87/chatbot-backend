# services/google_docai.py
import os
import json
import logging
import re
from google.cloud import documentai 
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional # <--- AÑADIDO
from .utils import limpiar_texto_base, parse_precio_flexible, extraer_unidades_y_tipos_precio

logger = logging.getLogger(__name__)

GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    # CORREGIDO NameError:
    ruta_cred_render_path = "/etc/secrets/google_service_key.json" 
    ruta_local_path = os.path.join(os.getcwd(), "instance", "google-credentials.json")
    ruta_cred_final = None
    if os.path.exists(ruta_cred_render_path): 
        ruta_cred_final = ruta_cred_render_path
    elif os.path.exists(ruta_local_path): 
        ruta_cred_final = ruta_local_path
    
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True; logger.info(f"✅ [DOCAI] Credenciales Google cargadas desde: {ruta_cred_final}")
    else: logger.error(f"❌ [DOCAI] Archivo de credenciales Google NO encontrado. Rutas: '{ruta_cred_render_path}', '{ruta_local_path}'.")
except Exception as e: logger.error(f"❌ [DOCAI] Error crítico cargando credenciales Google: {e}", exc_info=True)

def _get_text_from_layout_segments(text_anchor: Optional[documentai.Document.TextAnchor], full_doc_text: str) -> str:
    # ... (como te lo pasé en @‶gANVneHZLGZt...)
    response = "";
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start = int(segment.start_index); end = int(segment.end_index)
            if 0 <= start <= end <= len(full_doc_text): response += full_doc_text[start:end]
            else: logger.warning(f"[DOCAI-SEG] Segmento de texto inválido: start={start}, end={end}, len_text={len(full_doc_text)}")
    return limpiar_texto_base(response)

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    # ... (como te lo pasé en @‶gANVneHZLGZt..., con la llamada simplificada a process_document)
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS: logger.error("[DOCAI-GET] Imposible procesar PDF: Credenciales Google no cargadas/inválidas."); return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID"); location = os.getenv("GOOGLE_DOCAI_LOCATION", "us"); processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v for v,k in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not k]
            logger.error(f"❌ [DOCAI-GET] Faltan variables de entorno Google DocAI: {', '.join(missing)}."); return None 
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}; client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)
        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document_proto, skip_human_review=True)
        logger.info(f"[DOCAI-GET] Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})..."); result = client.process_document(request=request_doc_ai)
        if result and result.document:
            num_tablas_doc = len(result.document.tables) if hasattr(result.document, 'tables') and result.document.tables else 0
            num_tablas_pag = sum(len(p.tables) for p in result.document.pages if hasattr(p, 'tables') and p.tables) if result.document.pages else 0
            logger.info(f"[DOCAI-GET] Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas (directo): {num_tablas_doc}. Tablas (páginas): {num_tablas_pag}.")
            if not result.document.text and num_tablas_doc == 0 and num_tablas_pag == 0: logger.warning(f"[DOCAI-GET] DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas.")
            return result.document
        else: logger.error(f"[DOCAI-GET] Document AI no devolvió resultado válido para '{os.path.basename(pdf_path)}'."); return None
    except Exception as e: logger.error(f"❌ [DOCAI-GET] Error en llamada API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True); return None

def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    # ... (Pega aquí la función procesar_tablas_document_ai COMPLETA que te di en la respuesta @‶gANVneHZLGZv...)
    # ¡¡RECUERDA PERSONALIZAR EL HEADER_MAP_PDF DENTRO DE ESTA FUNCIÓN!!
    productos_de_tablas: List[Dict[str, Any]] = []
    doc_tables_to_process = []
    if hasattr(document, 'tables') and document.tables: doc_tables_to_process.extend(document.tables)
    if document.pages:
        for page in document.pages:
            if hasattr(page, 'tables') and page.tables: doc_tables_to_process.extend(page.tables)
    if not doc_tables_to_process: logger.info("[DOCAI-TABLES] No se encontraron tablas."); return productos_de_tablas
    logger.info(f"[DOCAI-TABLES] Procesando {len(doc_tables_to_process)} tablas..."); full_doc_text = document.text or ""
    HEADER_MAP_PDF: Dict[str, List[str]] = { "sku": ["código", "art."], "nombre": ["producto", "descripción", "varietal"], "precio_str": ["precio", "pvp", "$ botella", "$ caja"], "unidad": ["un/caja"], "marca": ["marca"]} 
    for table_idx, table_obj in enumerate(doc_tables_to_process):
        logger.info(f"[DOCAI-TABLES] Tabla {table_idx + 1}..."); header_rows_texts_list: List[List[str]] = []
        # ... (resto de la lógica de procesar_tablas_document_ai que te pasé)
        # Esta es una versión simplificada, pega la completa que te di.
        logger.warning(f"[DOCAI-TABLES] Tabla {table_idx + 1}: Lógica de extracción detallada (HEADER_MAP) es placeholder. DEBES PERSONALIZARLA.")
    return productos_de_tablas


def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # ... (Pega aquí tu función extraer_info_producto_de_linea_pdf COMPLETA y corregida que te pasé en @‶gANVneHZLGZv...)
    linea_limpia = limpiar_texto_base(linea_procesar); # ... resto de la lógica de esta función
    return None # Placeholder

def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    # ... (Pega aquí tu función procesar_catalogo_pdf_google COMPLETA que te pasé en @‶gANVneHZLGZv...)
    logger.info(f"[DOCAI-MAIN] Iniciando PDF: {os.path.basename(pdf_path)} para User ID: {user_id}, Rubro: {pyme_rubro_nombre}")
    document = _obtener_documento_ai(pdf_path)
    if not document: return [] 
    productos_extraidos_final: List[Dict[str, Any]] = []
    # ... (resto de la lógica de esta función)
    logger.info(f"[DOCAI-MAIN] Total productos PDF: {len(productos_extraidos_final)}")
    return productos_extraidos_final
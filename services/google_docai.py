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

def _get_text_from_layout_segments(text_anchor: documentai.Document.TextAnchor, full_doc_text: str) -> str:
    response = "";
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            response += full_doc_text[int(segment.start_index):int(segment.end_index)]
    return limpiar_texto_base(response)

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("Imposible procesar PDF: Credenciales Google no están cargadas o son inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v_name for v_name,val in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not val]
            logger.error(f"❌ Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
            return None # Retornar None aquí si faltan variables
            
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)
        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf") # Renombrado para evitar confusión
        
        # CORRECCIÓN: Comentar o ajustar TableExtractionParams
        # El error AttributeError indica que esta forma de configurar no es compatible.
        # A menudo, el procesador en Google Cloud ya está configurado para extraer tablas.
        # O la forma de habilitarlo en el SDK ha cambiado.
        # process_options = documentai.ProcessOptions(
        #     table_extraction_params=documentai.ProcessOptions.TableExtractionParams(enabled=True) # ESTA LÍNEA CAUSABA AttributeError
        # )
        # request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document_proto, process_options=process_options, skip_human_review=True)
        
        # Probar sin process_options explícitos para tablas, o solo con OCR config si es necesario
        process_options = documentai.ProcessOptions(
            ocr_config=documentai.OcrConfig(enable_native_pdf_parsing=True) # Puedes probar con o sin esto
        )
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document_proto, process_options=process_options, skip_human_review=True)
        # O la llamada más simple si el procesador ya está configurado:
        # request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document_proto, skip_human_review=True)


        logger.info(f"Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            if not result.document.text and not result.document.tables:
                 logger.warning(f"DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas.")
                 return None 
            logger.info(f"Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas: {len(result.document.tables) if result.document.tables else 0}")
            return result.document
        logger.error(f"Document AI no devolvió un resultado válido para '{os.path.basename(pdf_path)}'.")
        return None
    except Exception as e:
        logger.error(f"❌ Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

# ... (El resto de tus funciones procesar_tablas_document_ai, extraer_info_producto_de_linea_pdf, y procesar_catalogo_pdf_google
#      deben estar aquí. Asegúrate de que procesar_tablas_document_ai tenga el HEADER_MAP que debes personalizar).
#      Por brevedad, no las repito, pero usa las versiones que te pasé en @‶gANVneHZLGZj...
#      y asegúrate de que cualquier llamada a limpiar_texto_base, etc., sea correcta.

# Placeholder para el resto de las funciones si no las tienes a mano:
def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    logger.warning("[DOCAI-TABLES] Lógica de `procesar_tablas_document_ai` no implementada en detalle. Se devuelve lista vacía. Debes personalizar el HEADER_MAP y la extracción de celdas.")
    return []
def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    logger.debug(f"[DOCAI-LINE] Procesando línea (lógica placeholder): '{linea_procesar}'")
    return None # Implementa tu lógica completa aquí
def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    document = _obtener_documento_ai(pdf_path)
    if not document: return [] 
    productos_extraidos_final: List[Dict[str, Any]] = []
    if document.tables:
        productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
        if productos_de_tablas: productos_extraidos_final.extend(productos_de_tablas); logger.info(f"[DOCAI] Tablas aportaron {len(productos_de_tablas)} productos.")
    if not productos_extraidos_final and document.text: 
        logger.info(f"[DOCAI] Sin productos de tablas, procesando líneas del PDF '{os.path.basename(pdf_path)}'...")
        lineas = document.text.split('\n')
        for linea in lineas:
            if len(linea.strip()) < 5: continue
            prod = extraer_info_producto_de_linea_pdf(linea, pyme_rubro_nombre)
            if prod: prod["user_id"] = user_id; productos_extraidos_final.append(prod)
    if not productos_extraidos_final: logger.warning(f"⚠️[DOCAI] No se extrajeron productos del PDF: {os.path.basename(pdf_path)}.")
    else: logger.info(f"[DOCAI] Total productos PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
    return productos_extraidos_final
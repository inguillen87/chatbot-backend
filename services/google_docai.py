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

# --- Carga de Credenciales (como estaba antes, asegúrate que funcione) ---
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
        logger.error("Imposible procesar PDF: Credenciales Google no cargadas/inválidas.")
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
        
        # CORRECCIÓN: Simplificar process_options o quitarlo si el procesador ya está configurado para tablas.
        # El error "type object 'ProcessOptions' has no attribute 'TableExtractionParams'"
        # indica que esta forma de configurarlo no es correcta para tu versión/procesador.
        # Intenta primero SIN process_options explícitos para tablas aquí,
        # confiando en la configuración de tu procesador en Google Cloud.
        # Si necesitas forzar la extracción de tablas y esto no funciona,
        # deberás consultar la documentación de la versión específica de tu librería google-cloud-documentai
        # para la forma correcta de pasar TableExtractionParams.
        
        # process_options = documentai.ProcessOptions( # COMENTADO TEMPORALMENTE PARA EVITAR AttributeError
        #     table_extraction_params=documentai.ProcessOptions.TableExtractionParams(enabled=True, model_version="builtin/stable") 
        # )
        # request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document, process_options=process_options, skip_human_review=True)
        
        # Usar sin process_options explícitos para TableExtractionParams por ahora:
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document, skip_human_review=True)

        logger.info(f"Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        if result and result.document: # ... (resto de la función _obtener_documento_ai como antes)
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

# ... (El resto de google_docai.py: procesar_tablas_document_ai, extraer_info_producto_de_linea_pdf, 
#      procesar_catalogo_pdf_google se mantienen como en la versión que te pasé en @‶gANVneHZLGZj... 
#      ASEGÚRATE de que procesar_tablas_document_ai tenga el HEADER_MAP que debes personalizar)
# (Pega aquí el resto de esas funciones de mi respuesta anterior)
def procesar_tablas_document_ai(document: documentai.Document, pyme_user_id: int, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    # ... (Pega aquí la función procesar_tablas_document_ai completa que te di, con el HEADER_MAP para personalizar)
    logger.warning("Procesamiento de Tabla: Lógica de extracción de datos de tabla no implementada en detalle. Esta sección es un TODO y requiere tu personalización para el HEADER_MAP.")
    return [] # Devuelve lista vacía hasta que implementes la lógica
    
def extraer_info_producto_de_linea_pdf(linea_procesar: str, pyme_rubro_nombre: str) -> Optional[Dict[str, Any]]:
    # ... (Pega aquí tu función extraer_info_producto_de_linea_pdf completa y ya mejorada)
    return None # Placeholder si no la pegas

def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    # ... (Pega aquí tu función procesar_catalogo_pdf_google completa)
    document = _obtener_documento_ai(pdf_path)
    if not document: return [] 
    productos_extraidos_final: List[Dict[str, Any]] = []
    if document.tables:
        productos_de_tablas = procesar_tablas_document_ai(document, user_id, pyme_rubro_nombre)
        if productos_de_tablas: productos_extraidos_final.extend(productos_de_tablas); logger.info(f"Extracción de tablas aportó {len(productos_de_tablas)} productos.")
    if not productos_extraidos_final and document.text: # Solo si tablas no dio nada (o como complemento con de-duplicación)
        logger.info(f"Iniciando procesamiento línea por línea del PDF '{os.path.basename(pdf_path)}'...")
        # ... (resto de tu lógica de procesamiento línea por línea)
        pass
    if not productos_extraidos_final: logger.warning(f"⚠️ No se pudo extraer ningún producto del PDF: {os.path.basename(pdf_path)}.")
    else: logger.info(f"Total productos finales del PDF '{os.path.basename(pdf_path)}': {len(productos_extraidos_final)}")
    return productos_extraidos_final
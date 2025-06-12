# services/google_docai.py
import os
import json
import logging
import pandas as pd
from google.cloud import documentai 
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional

# Importamos el cerebro y las herramientas de nuestro utils centralizado
from .utils import limpiar_texto_base, parse_precio_flexible, crear_mapa_de_columnas_inteligente

logger = logging.getLogger(__name__)

# --- Carga de Credenciales (Tu código original) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
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
        logger.info(f"✅ [DOCAI] Credenciales Google cargadas desde: {ruta_cred_final}")
    else: logger.error(f"❌ [DOCAI] Archivo de credenciales Google NO encontrado.")
except Exception as e: logger.error(f"❌ [DOCAI] Error crítico cargando credenciales Google: {e}", exc_info=True)


# --- HELPER: Convertir Tabla de DocAI a DataFrame de Pandas ---
def _tabla_docai_a_dataframe(table: documentai.Document.Page.Table, full_doc_text: str) -> pd.DataFrame:
    """Convierte un objeto de tabla de Document AI a un DataFrame de pandas."""
    def _get_text(text_anchor):
        if text_anchor and text_anchor.text_segments:
            return "".join(full_doc_text[int(segment.start_index):int(segment.end_index)] for segment in text_anchor.text_segments)
        return ""
        
    filas_datos = []
    todas_las_filas = list(table.header_rows) + list(table.body_rows)
    for row in todas_las_filas:
        celdas_fila = [_get_text(cell.layout.text_anchor) for cell in row.cells]
        filas_datos.append(celdas_fila)
        
    return pd.DataFrame(filas_datos) if filas_datos else pd.DataFrame()

# --- Función para llamar a la API de Google (Tu código original) ---
def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("[DOCAI-GET] Imposible procesar PDF: Credenciales Google no cargadas/inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            logger.error(f"❌ [DOCAI-GET] Faltan variables de entorno Google DocAI.")
            return None 
            
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)
        with open(pdf_path, "rb") as file: pdf_content = file.read()
        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document_proto, skip_human_review=True)
        
        logger.info(f"[DOCAI-GET] Enviando '{os.path.basename(pdf_path)}' a Document AI...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            logger.info(f"[DOCAI-GET] Documento procesado con éxito.")
            return result.document
        else:
            logger.error(f"[DOCAI-GET] Document AI no devolvió un resultado válido.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI-GET] Error en llamada a API Google Document AI: {e}", exc_info=True)
        return None

# --- FUNCIÓN PRINCIPAL REFACTORIZADA PARA PDF ---
def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """
    Procesa un PDF con Google DocAI y usa el motor de mapeo inteligente para extraer productos de sus tablas.
    """
    base_filename = os.path.basename(pdf_path)
    logger.info(f"[DOCAI_PROC] Iniciando procesamiento PDF con motor inteligente para: {base_filename}")
    
    document = _obtener_documento_ai(pdf_path)
    if not document or not document.text:
        raise ValueError(f"Google DocAI no pudo procesar o no encontró texto en el documento: {base_filename}")

    todas_las_tablas_docai = list(document.tables) + [table for page in document.pages for table in page.tables]
    if not todas_las_tablas_docai:
        logger.warning(f"No se encontraron tablas estructuradas en el PDF '{base_filename}'.")
        return []

    logger.info(f"Se encontraron {len(todas_las_tablas_docai)} tablas en el PDF. Analizando cada una con el cerebro...")
    
    productos_extraidos_final = []
    for i, tabla_docai in enumerate(todas_las_tablas_docai):
        logger.info(f"--- Procesando Tabla PDF #{i+1} ---")
        df_tabla = _tabla_docai_a_dataframe(tabla_docai, document.text)
        if df_tabla.empty:
            logger.warning(f"Tabla PDF #{i+1} estaba vacía o no se pudo convertir. Saltando.")
            continue
            
        resultado_mapeo = crear_mapa_de_columnas_inteligente(df_tabla)
        if not resultado_mapeo:
            logger.warning(f"El cerebro no pudo entender la Tabla PDF #{i+1}. Saltando.")
            continue
            
        mapa_columnas, fila_inicio_datos = resultado_mapeo
        
        df_datos = df_tabla.copy()
        df_datos.columns = df_datos.iloc[fila_inicio_datos - 1].tolist()
        df_datos = df_datos.iloc[fila_inicio_datos:].reset_index(drop=True)
        df_datos.dropna(how='all', inplace=True)
        
        for index, row in df_datos.iterrows():
            try:
                nombre_prod = str(row.get(mapa_columnas.get('nombre', ''), '')).strip()
                precio_crudo = str(row.get(mapa_columnas.get('precio', ''), '')).strip()

                if not nombre_prod or not precio_crudo or len(nombre_prod) < 2: continue
                
                precio_str, precio_float, moneda = parse_precio_flexible(precio_crudo)
                if precio_float is None and not re.search(r'consultar|s/p', precio_str or "", re.IGNORECASE): continue
                
                producto = {
                    "nombre": nombre_prod, "precio_str": precio_str, "precio_float": precio_float, "moneda": moneda,
                    "sku": str(row.get(mapa_columnas.get('sku'), '')).strip(),
                    "descripcion": str(row.get(mapa_columnas.get('descripcion'), '')).strip(),
                    "marca": str(row.get(mapa_columnas.get('marca'), '')).strip(),
                    "categoria_qdrant": str(row.get(mapa_columnas.get('categoria'), pyme_rubro_nombre)).strip(),
                    "unidad": str(row.get(mapa_columnas.get('unidad'), 'unidad')).strip(),
                    "cantidad_disponible": str(row.get(mapa_columnas.get('stock'), '1')).strip(),
                }
                productos_extraidos_final.append(producto)
            except Exception as e_row:
                logger.warning(f"⚠️ Error procesando una fila de la Tabla PDF #{i+1}. Fila: {index}. Error: {e_row}")
                continue

    logger.info(f"✅ Proceso de PDF completado. Total productos finales de todas las tablas: {len(productos_extraidos_final)}")
    return productos_extraidos_final
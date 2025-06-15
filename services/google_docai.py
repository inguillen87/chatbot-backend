# services/google_docai.py
import os
import json
import logging
import pandas as pd
import re
from google.cloud import documentai 
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional

# Importamos nuestro cerebro y herramientas compartidas
from .utils import (
    limpiar_texto_base,
    parse_precio_flexible,
    parse_cantidad_flexible,
    crear_mapa_de_columnas_inteligente,
    safe_row_get,
    extraer_unidades_y_tipos_precio,
)

logger = logging.getLogger(__name__)


# --- 1. Carga de Credenciales (Tu lógica original, intacta) ---
GOOGLE_CREDENTIALS: Optional[service_account.Credentials] = None
CREDENTIALS_LOADED_SUCCESSFULLY: bool = False
try:
    ruta_cred_render = "/etc/secrets/google_service_key.json"
    ruta_cred_local = os.path.join(os.getcwd(), "instance", "google-credentials.json")
    ruta_cred_final = None

    if os.path.exists(ruta_cred_render): 
        ruta_cred_final = ruta_cred_render
    elif os.path.exists(ruta_cred_local): 
        ruta_cred_final = ruta_cred_local
    
    if ruta_cred_final:
        with open(ruta_cred_final, "r", encoding="utf-8") as f: credentials_info = json.load(f)
        GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
        CREDENTIALS_LOADED_SUCCESSFULLY = True
        logger.info(f"✅ [DOCAI] Credenciales Google cargadas desde: {ruta_cred_final}")
    else:
        logger.error(f"❌ [DOCAI] Archivo de credenciales Google NO encontrado en rutas buscadas.")
except Exception as e:
    logger.error(f"❌ [DOCAI] Error crítico cargando credenciales Google: {e}", exc_info=True)


# --- 2. Funciones Auxiliares (Helpers) ---

def _get_text_from_layout_segments(text_anchor: Optional[documentai.Document.TextAnchor], full_doc_text: str) -> str:
    """Extrae texto de los segmentos que provee la API de Google."""
    response = ""
    if text_anchor and text_anchor.text_segments:
        for segment in text_anchor.text_segments:
            start = int(segment.start_index)
            end = int(segment.end_index)
            if 0 <= start <= end <= len(full_doc_text):
                response += full_doc_text[start:end]
    # No usamos limpiar_texto_base aquí para mantener el formato original para el DataFrame
    return response.strip()

def _tabla_docai_a_dataframe(table: documentai.Document.Page.Table, full_doc_text: str) -> pd.DataFrame:
    """Convierte un objeto de tabla de Document AI a un DataFrame de pandas."""
    filas_datos = []
    # Itera por todas las filas (cabeceras y cuerpo) para construir el DataFrame en bruto
    todas_las_filas = list(table.header_rows) + list(table.body_rows)
    for row in todas_las_filas:
        celdas_fila = [_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text) for cell in row.cells]
        filas_datos.append(celdas_fila)
        
    return pd.DataFrame(filas_datos) if filas_datos else pd.DataFrame()

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    """Llama a la API de Google Document AI para procesar un PDF. Tu lógica original, intacta."""
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("[DOCAI-GET] Imposible procesar PDF: Credenciales Google no cargadas/inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")
        if not all([project_id, location, processor_id]):
            missing = [v[0] for v in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not v[1]]
            logger.error(f"❌ [DOCAI-GET] Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
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
            logger.error(f"[DOCAI-GET] Document AI no devolvió un resultado de documento válido.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI-GET] Error en llamada a API Google Document AI: {e}", exc_info=True)
        raise  # Re-lanzamos la excepción para que el procesador principal la capture


def _obtener_precio_desde_fila(row: pd.Series, mapa_columnas: Dict[str, str]) -> tuple[str, Optional[float], str]:
    """Devuelve el precio detectado en una fila, con fallback analizando toda la linea."""
    precio_crudo = str(safe_row_get(row, mapa_columnas.get('precio', ''))).strip()
    precio_str, precio_float, moneda = parse_precio_flexible(precio_crudo)

    if precio_float is None:
        fila_completa = " ".join(str(c) for c in row.tolist())
        precio_str_2, precio_float_2, moneda_2 = parse_precio_flexible(fila_completa)
        if precio_float_2 is not None:
            precio_str, precio_float = precio_str_2, precio_float_2
            if moneda_2:
                moneda = moneda_2
    return precio_str, precio_float, moneda

# --- 3. FUNCIÓN PRINCIPAL REFACTORIZADA PARA PDF ---

def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """
    Procesa un PDF con Google DocAI y usa el motor de mapeo inteligente para extraer productos de sus tablas.
    """
    base_filename = os.path.basename(pdf_path)
    logger.info(f"[DOCAI_PROC] Iniciando NUEVO procesamiento PDF para: {base_filename}")
    
    document = _obtener_documento_ai(pdf_path)
    if not document:
        raise ValueError(f"Google DocAI no pudo procesar el documento: {base_filename}")

    # Forma segura y correcta de recolectar todas las tablas
    todas_las_tablas_docai = []
    if document.pages:
        for page in document.pages:
            if hasattr(page, 'tables') and page.tables:
                todas_las_tablas_docai.extend(page.tables)

    if not todas_las_tablas_docai:
        logger.warning(f"No se encontraron tablas estructuradas en el PDF '{base_filename}'.")
        return []

    logger.info(f"Se encontraron {len(todas_las_tablas_docai)} tablas en el PDF. Analizando cada una con el cerebro...")
    
    productos_extraidos_final: List[Dict[str, Any]] = []
    
    for i, tabla_docai in enumerate(todas_las_tablas_docai):
        logger.info(f"--- Procesando Tabla PDF #{i+1} ---")
        df_tabla = _tabla_docai_a_dataframe(tabla_docai, document.text or "")
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
                nombre_prod = str(safe_row_get(row, mapa_columnas.get('nombre', ''))).strip()
                precio_str, precio_float, moneda = _obtener_precio_desde_fila(row, mapa_columnas)

                if not nombre_prod or len(nombre_prod) < 2:
                    fila_completa = " ".join(str(c) for c in row.tolist())
                    posible_nombre = re.split(r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?", fila_completa, 1)[0].strip()
                    if posible_nombre:
                        nombre_prod = posible_nombre

                if not nombre_prod or precio_float is None:
                    continue

                unidad_detectada, _ = extraer_unidades_y_tipos_precio(" ".join(str(c) for c in row.tolist()), pyme_rubro_nombre)

                producto = {
                    "nombre": nombre_prod,
                    "precio_str": precio_str,
                    "precio_float": precio_float,
                    "moneda": moneda,
                    "sku": str(safe_row_get(row, mapa_columnas.get('sku'))).strip(),
                    "descripcion": str(safe_row_get(row, mapa_columnas.get('descripcion'))).strip(),
                    "marca": str(safe_row_get(row, mapa_columnas.get('marca'))).strip(),
                    "categoria_qdrant": str(safe_row_get(row, mapa_columnas.get('categoria')) or pyme_rubro_nombre).strip(),
                    "unidad": unidad_detectada or str(safe_row_get(row, mapa_columnas.get('unidad')) or 'unidad').strip(),
                    "cantidad_disponible": str(
                        parse_cantidad_flexible(
                            safe_row_get(row, mapa_columnas.get('stock')) or '1'
                        )
                        or '0'
                    ).strip(),
                }
                productos_extraidos_final.append(producto)
            except Exception as e_row:
                logger.warning(
                    f"⚠️ Error procesando una fila de la Tabla PDF #{i+1}. Fila: {index}. Error: {e_row}"
                )
                continue

    logger.info(f"✅ Proceso de PDF completado. Total productos finales de todas las tablas: {len(productos_extraidos_final)}")
    return productos_extraidos_final

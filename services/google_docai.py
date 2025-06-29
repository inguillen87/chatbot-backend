# services/google_docai.py
import os
import json
import logging
import pandas as pd
import re
from google.cloud import documentai 
from google.oauth2 import service_account
from typing import List, Dict, Any, Optional
from .spacy_loader import get_spacy_model

# Importamos nuestro cerebro y herramientas compartidas
from .utils import (
    limpiar_texto_base,
    parse_precio_flexible,
)

logger = logging.getLogger(__name__)
NLP_SPACY = get_spacy_model()

def limpiar_texto_spacy(texto: str) -> str:
    """Normaliza texto usando spaCy para mejorar coincidencias."""
    if not texto or NLP_SPACY is None:
        return str(texto or "").strip()
    doc = NLP_SPACY(texto)
    tokens = [t.text for t in doc if not t.is_space]
    return " ".join(tokens).strip()


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
        celdas_fila = [
            limpiar_texto_spacy(_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text))
            for cell in row.cells
        ]
        filas_datos.append(celdas_fila)
        
    return pd.DataFrame(filas_datos) if filas_datos else pd.DataFrame()


def _consolidar_filas(df: pd.DataFrame) -> pd.DataFrame:
    """Combina filas parciales que DocAI pudo haber separado por error."""
    if df.empty:
        return df

    n_cols = df.shape[1]
    filas: List[List[str]] = []
    actual: List[str] | None = None

    for _, row in df.iterrows():
        celdas = [str(c).strip() for c in row.tolist()]
        if actual is None:
            actual = celdas
            continue

        non_empty = [c for c in celdas if c]
        if len(non_empty) < n_cols / 2:
            for idx, val in enumerate(celdas):
                if val:
                    if not actual[idx]:
                        actual[idx] = val
                    else:
                        actual[idx] = f"{actual[idx]} {val}".strip()
        else:
            filas.append(actual)
            actual = celdas

    if actual is not None:
        filas.append(actual)

    return pd.DataFrame(filas, columns=df.columns)

def _obtener_documento_ai(path: str, mime_type: str = "application/pdf") -> Optional[documentai.Document]:
    """Llama a la API de Google Document AI para procesar un archivo."""
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

        with open(path, "rb") as file:
            file_content = file.read()
        raw_document_proto = documentai.RawDocument(content=file_content, mime_type=mime_type)
        
        request_doc_ai = documentai.ProcessRequest(name=resource_name, raw_document=raw_document_proto, skip_human_review=True)
        
        logger.info(f"[DOCAI-GET] Enviando '{os.path.basename(path)}' a Document AI...")
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




def _procesar_documento_tablas(document: documentai.Document, base_filename: str, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    """Extrae productos de las tablas de un documento procesado."""
    todas_las_tablas_docai = []
    if document.pages:
        for page in document.pages:
            if hasattr(page, "tables") and page.tables:
                todas_las_tablas_docai.extend(page.tables)

    if not todas_las_tablas_docai:
        logger.warning(
            f"No se encontraron tablas estructuradas en el documento '{base_filename}'."
        )
        return []

    logger.info(
        f"Se encontraron {len(todas_las_tablas_docai)} tablas en el documento. Analizando cada una..."
    )

    productos_extraidos_final: List[Dict[str, Any]] = []
    for i, tabla_docai in enumerate(todas_las_tablas_docai):
        logger.info(f"--- Procesando Tabla #{i+1} ---")
        df_tabla = _tabla_docai_a_dataframe(tabla_docai, document.text or "")
        df_tabla = _consolidar_filas(df_tabla)
        if df_tabla.empty:
            logger.warning(f"Tabla #{i+1} estaba vacía o no se pudo convertir. Saltando.")
            continue

        df_data = df_tabla.copy()

        # --- Inicio Mapeo Mejorado de Columnas ---
        if df_data.empty or len(df_data.iloc[0]) == 0:
            logger.warning(f"Tabla #{i+1} parece no tener cabeceras o estar vacía después de la consolidación. Saltando.")
            continue

        cabeceras_originales = [str(c).lower() for c in df_data.iloc[0].tolist()]

        posibles_nombres_columnas = {
            "nombre": ["nombre", "producto", "titulo", "descripción", "descripcion", "detalle"],
            "sku": ["sku", "codigo", "código", "cod", "referencia", "ref", "item no"],
            "precio": ["precio", "valor", "importe", "costo", "pvp"],
            "stock": ["stock", "disponibilidad", "disponible", "cantidad", "cant."],
            "categoria_producto": ["categoria", "categoría", "rubro", "familia", "tipo"],
            "marca": ["marca", "fabricante"],
            "descripcion_corta": ["descripcion corta", "desc. corta", "resumen"],
            "promocion_texto": ["promocion", "promoción", "oferta", "descuento"],
            "unidad": ["unidad", "presentacion", "presentación"],
            # Campos específicos de vino que podrían mantenerse o generalizarse
            "varietal": ["varietal", "uva"],
            "caja": ["caja", "precio por caja"],
            "precio_botella": ["precio botella", "precio individual"],
        }

        columnas_mapeadas = {}
        for nombre_std, alias_list in posibles_nombres_columnas.items():
            for alias_idx, alias in enumerate(alias_list):
                # Intentar coincidencia exacta primero
                if alias in cabeceras_originales:
                    original_header = df_data.iloc[0][cabeceras_originales.index(alias)] # Mantener mayúsculas/minúsculas originales
                    columnas_mapeadas[nombre_std] = original_header
                    break
                # Intentar coincidencia parcial (si es más de una palabra o más de 3 letras)
                elif len(alias) > 3 or ' ' in alias:
                    for col_idx, header_orig_raw in enumerate(df_data.iloc[0].tolist()):
                        header_orig_lower = str(header_orig_raw).lower()
                        if alias in header_orig_lower:
                            columnas_mapeadas[nombre_std] = header_orig_raw
                            break
                    if nombre_std in columnas_mapeadas: # Salir si ya se encontró por coincidencia parcial
                        break

        # Usar cabeceras originales si no hay mapeo, limpiándolas
        df_data.columns = [limpiar_texto_base(c).replace(" ", "_") for c in df_data.iloc[0].tolist()]

        logger.info(f"Tabla #{i+1}: Cabeceras originales detectadas: {cabeceras_originales}")
        logger.info(f"Tabla #{i+1}: Cabeceras mapeadas a estándar: {columnas_mapeadas}")

        df_data = df_data.iloc[1:].reset_index(drop=True)
        df_data.dropna(how="all", inplace=True)
        # --- Fin Mapeo Mejorado de Columnas ---

        for _, row in df_data.iterrows():
            registro = {}
            # Llenar el registro usando las columnas mapeadas y luego las originales si hay conflicto o no mapeo
            for nombre_std, header_original_en_df in columnas_mapeadas.items():
                # Encontrar el nombre de columna real en df_data (que fue limpiado)
                col_limpia_en_df = limpiar_texto_base(str(header_original_en_df)).replace(" ", "_")
                if col_limpia_en_df in df_data.columns:
                    valor_celda = str(row.get(col_limpia_en_df, "")).strip()
                    if valor_celda: # Solo añadir si hay valor
                         registro[nombre_std] = valor_celda

            # Añadir datos de columnas no mapeadas explícitamente pero presentes en el df
            for col_original_df in df_data.columns:
                if col_original_df not in [limpiar_texto_base(str(c)).replace(" ", "_") for c in columnas_mapeadas.values()]:
                    # Evitar sobrescribir si un mapeo ya creó la clave estándar
                    clave_potencial_std = col_original_df.lower() # Podríamos intentar un mapeo inverso simple
                    if clave_potencial_std not in registro:
                        valor_celda = str(row.get(col_original_df, "")).strip()
                        if valor_celda: # Solo añadir si hay valor
                            registro[col_original_df] = valor_celda # Usar el nombre original limpio como clave

            if not registro.get("nombre") and registro.get("producto"): # fallback
                 registro["nombre"] = registro.get("producto")

            # Si 'nombre' sigue faltando después de todo, intentar con otras claves comunes
            if not registro.get("nombre"):
                for key_try in ["titulo", "descripcion", "detalle"]:
                    if registro.get(key_try):
                        registro["nombre"] = registro.get(key_try)
                        break

            # Si después de todos los intentos, no hay 'nombre', es un registro inválido.
            if not registro.get("nombre"):
                logger.debug(f"Registro descartado por falta de 'nombre': {registro}")
                continue

            if not any(registro.values()): # Si todos los valores son vacíos
                logger.debug(f"Registro descartado por estar completamente vacío: {row.to_dict()}")
                continue

            if registro.get("precio"):
                precio_str, precio_float, moneda = parse_precio_flexible(registro.get("precio"))
                registro["precio_str"] = precio_str
                registro["precio_float"] = precio_float
                registro["moneda"] = moneda

            # Asegurar que los campos clave para Qdrant tengan un valor default si no se extrajeron
            registro.setdefault("sku", "")
            registro.setdefault("stock", "") # Podría ser "Consultar" o un número
            registro.setdefault("categoria_producto", pyme_rubro_nombre) # Default a rubro de la pyme
            registro.setdefault("marca", "")
            registro.setdefault("descripcion_corta", "")
            registro.setdefault("promocion_texto", "")
            registro.setdefault("unidad", "")

            productos_extraidos_final.append(registro)

    logger.info(
        f"✅ Proceso de documento completado. Total productos finales de todas las tablas: {len(productos_extraidos_final)}. Ejemplo: {productos_extraidos_final[0] if productos_extraidos_final else 'N/A'}"
    )
    return productos_extraidos_final

# --- 3. FUNCIÓN PRINCIPAL REFACTORIZADA PARA PDF ---

def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Procesa un PDF para extraer productos."""
    base_filename = os.path.basename(pdf_path)
    logger.info(f"[DOCAI_PROC] Iniciando NUEVO procesamiento PDF para: {base_filename}")

    document = _obtener_documento_ai(pdf_path, mime_type="application/pdf")
    if not document:
        raise ValueError(f"Google DocAI no pudo procesar el documento: {base_filename}")

    return _procesar_documento_tablas(document, base_filename, pyme_rubro_nombre)


def procesar_catalogo_imagen_google(image_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Procesa una imagen (PNG o JPG) para extraer productos usando DocAI."""
    base_filename = os.path.basename(image_path)
    logger.info(f"[DOCAI_PROC] Iniciando procesamiento de imagen para: {base_filename}")
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"

    document = _obtener_documento_ai(image_path, mime_type=mime)
    if not document:
        raise ValueError(f"Google DocAI no pudo procesar la imagen: {base_filename}")

    return _procesar_documento_tablas(document, base_filename, pyme_rubro_nombre)


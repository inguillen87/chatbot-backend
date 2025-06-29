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
    crear_mapa_de_columnas_inteligente, # Importante
    KEYWORD_MAP # Usaremos el mismo KEYWORD_MAP global
)

logger = logging.getLogger(__name__)
NLP_SPACY = get_spacy_model()

def limpiar_texto_spacy(texto: str) -> str:
    """Normaliza texto usando spaCy para mejorar coincidencias."""
    if not texto or NLP_SPACY is None:
        return str(texto or "").strip() # Asegurar que siempre devuelva string
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
    # No usamos limpiar_texto_base aquí para mantener el formato original para el DataFrame,
    # spaCy se encargará de la limpieza en _tabla_docai_a_dataframe
    return response.strip()

def _tabla_docai_a_dataframe(table: documentai.Document.Page.Table, full_doc_text: str) -> pd.DataFrame:
    """Convierte un objeto de tabla de Document AI a un DataFrame de pandas.
       Las celdas son limpiadas con spaCy para mejorar la consistencia.
    """
    filas_datos = []
    # Itera por todas las filas (cabeceras y cuerpo) para construir el DataFrame en bruto
    # DocumentAI a veces no distingue bien header_rows de body_rows, así que procesamos todo junto
    # y dejamos que crear_mapa_de_columnas_inteligente encuentre los headers.
    todas_las_filas = list(table.header_rows) + list(table.body_rows)
    for row_idx, row in enumerate(todas_las_filas):
        celdas_fila = [
            # Usar limpiar_texto_spacy para una normalización más robusta de las celdas
            limpiar_texto_spacy(_get_text_from_layout_segments(cell.layout.text_anchor, full_doc_text))
            for cell in row.cells
        ]
        # Asegurarse de que todas las filas tengan la misma cantidad de columnas que la primera fila procesada
        # Esto es importante si algunas filas tienen celdas vacías al final que DocAI omite.
        if filas_datos and len(celdas_fila) < len(filas_datos[0]):
            celdas_fila.extend([""] * (len(filas_datos[0]) - len(celdas_fila)))
        elif filas_datos and len(celdas_fila) > len(filas_datos[0]): # Truncar si es más larga
             celdas_fila = celdas_fila[:len(filas_datos[0])]

        filas_datos.append(celdas_fila)

    # Crear DataFrame sin cabeceras (header=None) para que crear_mapa_de_columnas_inteligente las busque
    return pd.DataFrame(filas_datos) if filas_datos else pd.DataFrame()


def _consolidar_filas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Combina filas parciales que DocAI pudo haber separado por error.
    Se asume que una fila es una continuación si tiene significativamente menos celdas llenas
    que la fila anterior o que el promedio de columnas.
    """
    if df.empty:
        return df

    n_cols = df.shape[1]
    if n_cols == 0: return df

    filas_consolidadas: List[List[str]] = []
    fila_actual: List[str] = [""] * n_cols # Inicializar con celdas vacías

    # Umbral para considerar una fila como continuación (ej: menos del 50% de celdas llenas)
    umbral_celdas_llenas_continuacion = max(1, n_cols // 2)

    for _, row_data in df.iterrows():
        celdas = [str(c).strip() if pd.notna(c) else "" for c in row_data.tolist()]

        # Asegurar que la fila tenga el número correcto de columnas
        if len(celdas) < n_cols:
            celdas.extend([""] * (n_cols - len(celdas)))
        elif len(celdas) > n_cols:
            celdas = celdas[:n_cols]

        celdas_llenas_count = sum(1 for c in celdas if c)

        # Si la fila actual está vacía (inicio o después de consolidar una completa)
        # o si la nueva fila tiene suficientes celdas llenas para ser considerada nueva.
        if sum(1 for c_act in fila_actual if c_act) == 0 or celdas_llenas_count >= umbral_celdas_llenas_continuacion :
            if sum(1 for c_act in fila_actual if c_act) > 0: # Si había algo en fila_actual, guardarla
                filas_consolidadas.append(list(fila_actual)) # Guardar una copia
            fila_actual = list(celdas) # Iniciar nueva fila_actual
        else: # La fila es una continuación, concatenar
            for i in range(n_cols):
                if celdas[i]: # Si la celda de continuación tiene contenido
                    fila_actual[i] = (fila_actual[i] + " " + celdas[i]).strip() if fila_actual[i] else celdas[i]

    # Añadir la última fila_actual si tiene contenido
    if sum(1 for c_act in fila_actual if c_act) > 0:
        filas_consolidadas.append(fila_actual)

    return pd.DataFrame(filas_consolidadas, columns=df.columns if not filas_consolidadas else None)


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

        # Configurar para que el procesador de tablas (si es un Custom Extractor con esa capacidad) funcione mejor
        process_options_val = None # Default to None

        # The following LayoutConfig caused a TypeError.
        # For now, we will rely on default processor options or specialized processor behavior.
        # If specific layout hints are needed for certain general processors,
        # this needs to be revisited with correct ProcessOptions structure.
        #
        # layout_config_for_general_processor = documentai.ProcessOptions.LayoutConfig(
        #     chunking_config=documentai.ProcessOptions.LayoutConfig.ChunkingConfig(
        #         chunk_size=1000,
        #         include_ancestor_headings=True
        #     )
        # )
        #
        # if not (processor_id and processor_id.startswith("product-catalog-")):
        #    # Potentially, this should be nested under an OcrConfig or similar
        #    # process_options_val = documentai.ProcessOptions(ocr_config=documentai.OcrConfig(layout_config=layout_config_for_general_processor))
        #    # For now, defaulting to None if not a known catalog processor.
        #    pass

        request_doc_ai = documentai.ProcessRequest(
            name=resource_name,
            raw_document=raw_document_proto,
            skip_human_review=True,
            process_options=process_options_val # Use the defaulted None
        )

        logger.info(f"[DOCAI-GET] Enviando '{os.path.basename(path)}' a Document AI (ProcessOptions: {process_options_val is not None})...")
        result = client.process_document(request=request_doc_ai)

        if result and result.document:
            logger.info(f"[DOCAI-GET] Documento procesado con éxito.")
            return result.document
        else:
            logger.error(f"[DOCAI-GET] Document AI no devolvió un resultado de documento válido.")
            return None
    except Exception as e:
        logger.error(f"❌ [DOCAI-GET] Error en llamada a API Google Document AI: {e}", exc_info=True)
        raise


def _procesar_documento_tablas(document: documentai.Document, base_filename: str, pyme_rubro_nombre: str) -> List[Dict[str, Any]]:
    """Extrae productos de las tablas de un documento procesado usando mapeo inteligente."""
    todas_las_tablas_docai = []
    if document.pages:
        for page_idx, page in enumerate(document.pages):
            if hasattr(page, "tables") and page.tables:
                logger.info(f"[DOCAI_TABLES] Página {page_idx + 1}: Encontradas {len(page.tables)} tablas.")
                todas_las_tablas_docai.extend(page.tables)
            else:
                logger.info(f"[DOCAI_TABLES] Página {page_idx + 1}: No se encontraron tablas.")


    if not todas_las_tablas_docai:
        logger.warning(f"[DOCAI_TABLES] No se encontraron tablas estructuradas en el documento '{base_filename}'.")
        return []

    logger.info(f"[DOCAI_TABLES] Total {len(todas_las_tablas_docai)} tablas encontradas en el documento. Analizando cada una...")
    productos_extraidos_final: List[Dict[str, Any]] = []

    for i, tabla_docai in enumerate(todas_las_tablas_docai):
        logger.info(f"--- [DOCAI_TABLES] Procesando Tabla Bruta #{i+1} ---")
        df_tabla_bruta = _tabla_docai_a_dataframe(tabla_docai, document.text or "")

        if df_tabla_bruta.empty:
            logger.warning(f"[DOCAI_TABLES] Tabla Bruta #{i+1} estaba vacía. Saltando.")
            continue

        logger.debug(f"[DOCAI_TABLES] Tabla Bruta #{i+1} (antes de consolidar):\n{df_tabla_bruta.head().to_string()}")
        df_consolidada = _consolidar_filas(df_tabla_bruta.copy()) # Usar .copy() para evitar modificar df_tabla_bruta

        if df_consolidada.empty:
            logger.warning(f"[DOCAI_TABLES] Tabla #{i+1} vacía después de consolidación. Saltando.")
            continue
        
        logger.info(f"[DOCAI_TABLES] Tabla #{i+1} consolidada (antes de mapeo inteligente), {df_consolidada.shape[0]} filas.")
        logger.debug(f"[DOCAI_TABLES] Tabla #{i+1} consolidada (head):\n{df_consolidada.head().to_string()}")

        mapa_info = crear_mapa_de_columnas_inteligente(df_consolidada.copy()) # Usar .copy()

        if not mapa_info:
            logger.error(
                f"❌ [DOCAI_TABLES] Tabla #{i+1}: No se pudo determinar el mapa de columnas (nombre, precio) para '{base_filename}'. "
                "Asegúrate que la tabla tenga encabezados claros."
            )
            continue # Saltar esta tabla, probar con la siguiente

        mapa_columnas, fila_inicio_datos = mapa_info
        logger.info(f"[DOCAI_TABLES] Tabla #{i+1}: Mapa de columnas detectado: {mapa_columnas}. Datos inician en fila de tabla consolidada: {fila_inicio_datos}")

        df_datos_tabla_actual: pd.DataFrame
        if fila_inicio_datos >= len(df_consolidada):
            logger.warning(f"[DOCAI_TABLES] Tabla #{i+1}: fila_inicio_datos ({fila_inicio_datos}) está fuera de los límites de la tabla consolidada ({len(df_consolidada)} filas). Saltando tabla.")
            continue

        if fila_inicio_datos > 0:
            # Las cabeceras están en df_consolidada.iloc[fila_inicio_datos - 1]
            # Los datos comienzan en df_consolidada.iloc[fila_inicio_datos:]
            df_datos_tabla_actual = df_consolidada.iloc[fila_inicio_datos:].reset_index(drop=True)
            # Usar los nombres de columna originales que el mapeador identificó
            # Asegurarse de que la fila de cabecera tenga la misma longitud que las columnas del df_datos_tabla_actual
            cabeceras_detectadas = df_consolidada.iloc[fila_inicio_datos - 1].tolist()
            if len(cabeceras_detectadas) == df_datos_tabla_actual.shape[1]:
                 df_datos_tabla_actual.columns = cabeceras_detectadas
            else:
                 logger.warning(f"[DOCAI_TABLES] Tabla #{i+1}: Discrepancia en número de cabeceras ({len(cabeceras_detectadas)}) y columnas de datos ({df_datos_tabla_actual.shape[1]}). Usando columnas por defecto.")
                 # df_datos_tabla_actual.columns ya son índices numéricos si esto ocurre
        else: # fila_inicio_datos es 0
            # El mapeador usó la primera fila como datos o heurísticas con índices.
            # df_consolidada ya está lista, pero sus columnas son índices numéricos (0, 1, 2...).
            # El mapa_columnas referenciará estos índices.
            df_datos_tabla_actual = df_consolidada
            # No es necesario reasignar df_datos_tabla_actual.columns aquí porque mapa_columnas usará los índices numéricos.

        if df_datos_tabla_actual.empty:
            logger.warning(f"[DOCAI_TABLES] Tabla #{i+1}: No hay datos después de aplicar fila_inicio_datos. Saltando.")
            continue
            
        logger.info(f"[DOCAI_TABLES] Tabla #{i+1}: Procesando {len(df_datos_tabla_actual)} filas de datos.")

        for row_idx, row in df_datos_tabla_actual.iterrows():
            registro_actual = {}

            nombre_producto = ""
            col_nombre_orig = mapa_columnas.get('nombre')
            if col_nombre_orig is not None: # col_nombre_orig puede ser int si no hay header
                nombre_producto = str(row.get(col_nombre_orig, "")).strip()

            if not nombre_producto: # Si el nombre principal está vacío, intentar con fallbacks del KEYWORD_MAP
                for fallback_key in KEYWORD_MAP.get('nombre', []): # ej: "producto", "descripcion"
                    if fallback_key == 'nombre': continue # ya intentado
                    col_fallback_orig = mapa_columnas.get(fallback_key)
                    if col_fallback_orig:
                        nombre_producto_fallback = str(row.get(col_fallback_orig, "")).strip()
                        if nombre_producto_fallback:
                            nombre_producto = nombre_producto_fallback
                            logger.debug(f"[DOCAI_TABLES] Tabla #{i+1}, Fila {row_idx}: 'nombre' obtenido de fallback '{fallback_key}': '{nombre_producto}'")
                            break

            if not nombre_producto:
                logger.warning(f"[DOCAI_TABLES] Tabla #{i+1}, Fila {row_idx}: Omitida por 'nombre' vacío o no mapeado. Valor original intentado: '{str(row.get(mapa_columnas.get('nombre', 'N/A'), ''))}'")
                continue

            registro_actual['nombre'] = nombre_producto
            
            for campo_estandar in KEYWORD_MAP.keys():
                if campo_estandar == 'nombre': continue # Ya procesado

                nombre_columna_original = mapa_columnas.get(campo_estandar)
                if nombre_columna_original is not None:
                    valor_celda = str(row.get(nombre_columna_original, "")).strip()
                    registro_actual[campo_estandar] = valor_celda
                else: # Asegurar que todos los campos estándar existan
                    registro_actual[campo_estandar] = ""

            # Aplicar parse_precio_flexible si hay un campo de precio
            precio_col_orig = mapa_columnas.get('precio')
            if precio_col_orig:
                precio_input_val = str(row.get(precio_col_orig, "")).strip()
                if precio_input_val: # Solo parsear si hay algo que parsear
                    precio_str, precio_float, moneda = parse_precio_flexible(precio_input_val)
                    registro_actual["precio_str"] = precio_str
                    registro_actual["precio_float"] = precio_float
                    registro_actual["moneda"] = moneda
                else: # Si la celda de precio mapeada está vacía
                    registro_actual["precio_str"] = ""
                    registro_actual["precio_float"] = None
                    registro_actual["moneda"] = None
            else: # Si no se mapeó ninguna columna a 'precio'
                registro_actual["precio_str"] = ""
                registro_actual["precio_float"] = None
                registro_actual["moneda"] = None

            # Asegurar campos clave para Qdrant con defaults si no se extrajeron
            registro_actual.setdefault("sku", "")
            registro_actual.setdefault("stock", "")
            registro_actual.setdefault("categoria_producto", pyme_rubro_nombre)
            registro_actual.setdefault("marca", "")
            registro_actual.setdefault("descripcion", registro_actual.get("descripcion","") or "") # Asegurar que 'descripcion' exista
            registro_actual.setdefault("descripcion_corta", "")
            registro_actual.setdefault("promocion_texto", "")
            registro_actual.setdefault("unidad", "")

            productos_extraidos_final.append(registro_actual)

    logger.info(
        f"✅ [DOCAI_TABLES] Proceso de todas las tablas completado. Total productos finales: {len(productos_extraidos_final)}. Ejemplo: {productos_extraidos_final[0] if productos_extraidos_final else 'N/A'}"
    )
    return productos_extraidos_final

# --- 3. FUNCIÓN PRINCIPAL REFACTORIZADA PARA PDF ---

def procesar_catalogo_pdf_google(pdf_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Procesa un PDF para extraer productos."""
    base_filename = os.path.basename(pdf_path)
    logger.info(f"[DOCAI_PROC] Iniciando procesamiento PDF (con mapeo inteligente) para: {base_filename}, user_id: {user_id}")

    document = _obtener_documento_ai(pdf_path, mime_type="application/pdf")
    if not document:
        # _obtener_documento_ai ya loggea el error y puede lanzar una excepción si es crítico
        logger.error(f"[DOCAI_PROC] Fallo al obtener el documento procesado por DocAI para {base_filename}.")
        # Devolver lista vacía para que el flujo principal muestre el error de "no productos"
        return []
        # Considerar: raise ValueError(f"Google DocAI no pudo procesar el documento: {base_filename}")

    try:
        return _procesar_documento_tablas(document, base_filename, pyme_rubro_nombre)
    except Exception as e_proc_tablas:
        logger.error(f"❌ [DOCAI_PROC] Error durante _procesar_documento_tablas para {base_filename}: {e_proc_tablas}", exc_info=True)
        return []


def procesar_catalogo_imagen_google(image_path: str, user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Procesa una imagen (PNG o JPG) para extraer productos usando DocAI."""
    base_filename = os.path.basename(image_path)
    logger.info(f"[DOCAI_PROC] Iniciando procesamiento de imagen (con mapeo inteligente) para: {base_filename}, user_id: {user_id}")
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"

    document = _obtener_documento_ai(image_path, mime_type=mime)
    if not document:
        logger.error(f"[DOCAI_PROC] Fallo al obtener el documento procesado por DocAI para imagen {base_filename}.")
        return []
        # Considerar: raise ValueError(f"Google DocAI no pudo procesar la imagen: {base_filename}")

    try:
        return _procesar_documento_tablas(document, base_filename, pyme_rubro_nombre)
    except Exception as e_proc_tablas_img:
        logger.error(f"❌ [DOCAI_PROC] Error durante _procesar_documento_tablas para imagen {base_filename}: {e_proc_tablas_img}", exc_info=True)
        return []

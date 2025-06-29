# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
from typing import List, Dict, Any

from .utils import limpiar_texto_base, crear_mapa_de_columnas_inteligente, KEYWORD_MAP

logger = logging.getLogger(__name__)


def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """Lee un archivo Excel y devuelve las filas normalizadas utilizando mapeo inteligente de columnas."""
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Leyendo archivo: {base_filename} para user_id: {pyme_user_id}")
    try:
        # Cargar sin asumir que la primera fila es el encabezado para el mapeador inteligente
        df_raw = pd.read_excel(path, sheet_name=0, keep_default_na=False, dtype=str, header=None)
    except Exception as e_read:
        logger.error(
            f"❌ Error al leer el archivo Excel '{base_filename}' para user_id {pyme_user_id}: {e_read}",
            exc_info=True,
        )
        raise ValueError(f"No se pudo leer el archivo Excel: {base_filename}")

    if df_raw.empty:
        logger.warning(f"⚠️ El archivo '{base_filename}' (user_id: {pyme_user_id}) está vacío o no se pudo leer contenido.")
        return []

    mapa_info = crear_mapa_de_columnas_inteligente(df_raw)

    if not mapa_info:
        logger.error(
            f"❌ [EXCEL_PROC] No se pudo determinar el mapa de columnas (nombre, precio) para '{base_filename}' (user_id: {pyme_user_id}). "
            "Asegúrate que el archivo tenga encabezados claros como 'Nombre', 'Precio', 'Descripción', etc."
        )
        # Devuelve vacío para que el mensaje de error original se muestre al usuario
        return []

    mapa_columnas, fila_inicio_datos = mapa_info
    # mapa_columnas AHORA contiene INTEGER INDICES como valores, que se refieren a las columnas de df_raw.
    logger.info(f"[EXCEL_PROC] Mapa de columnas (índices en df_raw) detectado para '{base_filename}' (user_id: {pyme_user_id}): {mapa_columnas}. Datos inician en fila de df_raw: {fila_inicio_datos}")

    if fila_inicio_datos >= len(df_raw) and not df_raw.empty:
        logger.warning(f"[EXCEL_PROC] fila_inicio_datos ({fila_inicio_datos}) está fuera de los límites de df_raw ({len(df_raw)} filas) para '{base_filename}'. No hay datos para procesar.")
        return []
    # df_iterar tomará las filas de datos de df_raw.
    # Como df_raw fue leído con header=None, sus columnas ya son 0, 1, 2...
    # Y mapa_columnas.values() también son estos índices 0, 1, 2...
    df_iterar = df_raw.iloc[fila_inicio_datos:].reset_index(drop=True)

    if df_iterar.empty:
        logger.warning(f"[EXCEL_PROC] No se encontraron filas de datos en '{base_filename}' (user_id: {pyme_user_id}) después de aplicar fila_inicio_datos={fila_inicio_datos}.")
        return []

    registros: List[Dict[str, Any]] = []

    # Los nombres de las columnas en df_iterar son RangeIndex (0, 1, 2...)
    # Esto coincide con los valores (índices) que ahora están en mapa_columnas.

    for i, row in df_iterar.iterrows():
        registro_actual = {}

        nombre_producto = ""
        col_idx_nombre = mapa_columnas.get('nombre') # Esto es un Integer index

        if col_idx_nombre is not None and col_idx_nombre < len(row):
            nombre_producto = str(row.iloc[col_idx_nombre]).strip() # Usar iloc para acceder por posición

        if not nombre_producto:
            valor_original_log = ""
            if col_idx_nombre is not None and col_idx_nombre < len(row):
                 valor_original_log = str(row.iloc[col_idx_nombre])
            elif col_idx_nombre is not None:
                 valor_original_log = f"(Índice {col_idx_nombre} fuera de rango para fila con {len(row)} celdas)"
            else:
                 valor_original_log = "(Columna 'nombre' no mapeada)"

            logger.warning(f"[EXCEL_PROC] Fila {i + fila_inicio_datos} de df_raw (fila {i} de datos) (user_id: {pyme_user_id}) omitida: 'nombre' está vacío. Valor original intentado: '{valor_original_log}'")
            continue

        registro_actual['nombre'] = nombre_producto

        for campo_estandar in KEYWORD_MAP.keys(): # Usar KEYWORD_MAP para asegurar todos los campos
            if campo_estandar == 'nombre': # Ya procesado
                continue

            col_idx = mapa_columnas.get(campo_estandar)
            valor_celda = ""
            if col_idx is not None and col_idx < len(row):
                valor_celda = str(row.iloc[col_idx]).strip() # Usar iloc

            registro_actual[campo_estandar] = valor_celda

            if campo_estandar == 'unidad':
                from .utils import parse_unidad_y_cantidad_empaque # Import locally for clarity
                unidad_desc_parsed, cantidad_emp_parsed = parse_unidad_y_cantidad_empaque(valor_celda)
                registro_actual['unidad_parsed'] = unidad_desc_parsed
                registro_actual['cantidad_empaque'] = cantidad_emp_parsed
                logger.debug(f"[EXCEL_PROC] Fila {i + fila_inicio_datos}: 'unidad' original='{valor_celda}', parsed_desc='{unidad_desc_parsed}', parsed_cant_empaque='{cantidad_emp_parsed}'")


        # Asegurar que todos los campos de KEYWORD_MAP existan en el registro, incluso si están vacíos
        for k_std in KEYWORD_MAP.keys():
            if k_std not in registro_actual:
                registro_actual[k_std] = ""
            # Adicionalmente, asegurar que los campos parseados de unidad también existan
            if 'unidad_parsed' not in registro_actual:
                 registro_actual['unidad_parsed'] = registro_actual.get('unidad', "") # fallback al original si no se parseó
            if 'cantidad_empaque' not in registro_actual:
                 registro_actual['cantidad_empaque'] = None


        registros.append(registro_actual)

    if not registros:
        logger.warning(f"⚠️ No se extrajeron registros válidos (con 'nombre') de '{base_filename}' (user_id: {pyme_user_id}) después del mapeo.")
    else:
        logger.info(f"✅ {len(registros)} filas obtenidas de '{base_filename}' (user_id: {pyme_user_id}) usando mapeo inteligente.")
    return registros

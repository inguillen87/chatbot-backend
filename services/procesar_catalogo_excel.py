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
    logger.info(f"[EXCEL_PROC] Mapa de columnas detectado para '{base_filename}' (user_id: {pyme_user_id}): {mapa_columnas}. Datos inician en fila: {fila_inicio_datos}")

    # Si la fila_inicio_datos es 0, significa que la primera fila ya son datos (o no se encontró header explícito y se usa heurística)
    # En este caso, pd.read_excel con header=None ya asignó nombres numéricos a las columnas.
    # El mapa_columnas contendrá esos nombres numéricos como valores si la heurística los usó.
    # Si la primera fila son los headers (fila_inicio_datos > 0), entonces usamos esos headers para el df de datos.
    if fila_inicio_datos > 0:
        # Los encabezados están en df_raw.iloc[fila_inicio_datos - 1]
        # Los datos comienzan en df_raw.iloc[fila_inicio_datos:]
        df_datos = df_raw.iloc[fila_inicio_datos:].reset_index(drop=True)
        # Usar los nombres de columna originales que el mapeador identificó
        df_datos.columns = df_raw.iloc[fila_inicio_datos -1].tolist()

    else: # fila_inicio_datos es 0
        # Esto significa que o bien no hay encabezados, o los encabezados son la primera fila (index 0)
        # y el mapeador inteligente los usó para crear el mapa_columnas.
        # Si la primera fila eran encabezados, df_raw.columns ya son esos.
        # Si no había encabezados y se usó heurística, mapa_columnas referenciará las columnas por su índice (0, 1, 2...).
        # En este caso, el `mapa_columnas` ya tiene las claves correctas (0, 1, 2..) si no había header
        # o los nombres de la primera fila si eran headers.
        # Necesitamos que df_datos tenga las columnas nombradas como espera el mapa_columnas.
        # Si mapa_columnas.values() son strings (nombres de headers), usamos la primera fila como headers.
        # Si mapa_columnas.values() son ints (índices de columnas), usamos header=None.

        # Re-leer el dataframe, esta vez dejando que pandas infiera los encabezados si están en la primera fila,
        # o use índices numéricos si no hay encabezados.
        # Esto es más simple que tratar de reasignar columnas a df_raw directamente.
        # Si la primera fila eran los headers que el mapper usó (fila_inicio_datos == 0 y mapa_columnas.values() son str)
        # entonces df_datos debería tener esos headers.
        # Si no había headers y el mapper usó índices (fila_inicio_datos == 0 y mapa_columnas.values() son int/str(int))
        # entonces df_datos debería tener esos índices como headers.

        # Simplificación: Si fila_inicio_datos es 0, asumimos que la primera fila de df_raw SON los headers
        # o que no hay headers y el mapeador usó los índices posicionales.
        # El mapa_columnas ya contiene los nombres correctos (sean strings o índices numéricos casteados a string)
        # que pandas usaría.
        df_datos = pd.read_excel(path, sheet_name=0, keep_default_na=False, dtype=str, header=0 if any(isinstance(v, str) and not v.isdigit() for v in mapa_columnas.values()) else None)
        if df_datos.empty and not df_raw.empty : # Si leer con header=0 falla pero raw tenía datos, reintentar con header=None
             df_datos = pd.read_excel(path, sheet_name=0, keep_default_na=False, dtype=str, header=None)


    registros: List[Dict[str, Any]] = []
    campos_estandar = list(KEYWORD_MAP.keys()) # nombre, precio, descripcion, sku, etc.

    for i, row in df_datos.iterrows():
        registro_actual = {}
        # Usar el mapa_columnas para extraer datos. Las claves del mapa son los nombres estándar.
        # Los valores del mapa son los nombres de columna originales (o índices si no hay header).

        nombre_producto = ""
        if mapa_columnas.get('nombre'):
            nombre_producto = str(row.get(mapa_columnas['nombre'], "")).strip()

        if not nombre_producto:
            logger.warning(f"[EXCEL_PROC] Fila {i+fila_inicio_datos} (user_id: {pyme_user_id}) omitida: 'nombre' está vacío o no se pudo mapear. Valor original: '{row.get(mapa_columnas.get('nombre', 'N/A'), '')}'")
            continue

        registro_actual['nombre'] = nombre_producto

        for campo_estandar in campos_estandar:
            if campo_estandar == 'nombre': # Ya procesado
                continue
            nombre_columna_original = mapa_columnas.get(campo_estandar)
            if nombre_columna_original is not None: # Puede ser int si no hay header
                valor_celda = str(row.get(nombre_columna_original, "")).strip()
                registro_actual[campo_estandar] = valor_celda
            else:
                registro_actual[campo_estandar] = "" # Asegurar que todos los campos estándar existan

        # Llenar campos faltantes con defaults (vacío) si no fueron mapeados
        for k_std in KEYWORD_MAP.keys():
            if k_std not in registro_actual:
                registro_actual[k_std] = ""

        registros.append(registro_actual)

    if not registros:
        logger.warning(f"⚠️ No se extrajeron registros válidos (con 'nombre') de '{base_filename}' (user_id: {pyme_user_id}) después del mapeo.")
    else:
        logger.info(f"✅ {len(registros)} filas obtenidas de '{base_filename}' (user_id: {pyme_user_id}) usando mapeo inteligente.")
    return registros

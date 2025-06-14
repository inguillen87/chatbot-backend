# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re
from typing import List, Dict, Any
from .utils import (
    limpiar_texto_base,
    parse_precio_flexible,
    parse_cantidad_flexible,
    crear_mapa_de_columnas_inteligente,
    safe_row_get,
)

logger = logging.getLogger(__name__)

def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """
    Procesa un archivo Excel o CSV usando el motor de mapeo inteligente de utils.
    """
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando procesamiento con motor inteligente para: {base_filename}")

    try:
        # Lee el archivo en un DataFrame sin tratar de adivinar encabezados
        df_bruto = pd.read_excel(path, header=None, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
    except Exception as e_read:
        logger.error(f"❌ Error fatal al leer el archivo Excel '{base_filename}': {e_read}", exc_info=True)
        raise ValueError(f"No se pudo leer el archivo Excel. Puede estar corrupto o en un formato no soportado.")

    if df_bruto.empty:
        logger.warning(f"⚠️ El archivo '{base_filename}' está vacío o no se pudo leer.")
        return []

    # 1. Usamos el cerebro para entender el archivo
    resultado_mapeo = crear_mapa_de_columnas_inteligente(df_bruto)
    if not resultado_mapeo:
        raise ValueError(
            "No se encontraron columnas de nombre o precio. Asegurate de que el archivo tenga encabezados claros."
        )

    mapa_columnas, fila_inicio_datos = resultado_mapeo
    
    # 2. Preparamos el DataFrame para la extracción
    df_datos = df_bruto.copy()
    df_datos.columns = df_datos.iloc[fila_inicio_datos - 1].tolist()
    df_datos = df_datos.iloc[fila_inicio_datos:].reset_index(drop=True)
    df_datos.dropna(how='all', inplace=True)

    logger.info(f"DataFrame listo para procesar con {len(df_datos)} filas. Columnas originales usadas: {list(mapa_columnas.values())}")
    
    productos_extraidos: List[Dict[str, Any]] = []
    # 3. Iteramos y extraemos los productos
    for index, row in df_datos.iterrows():
        try:
            nombre_prod = str(safe_row_get(row, mapa_columnas.get('nombre', ''))).strip()
            precio_crudo = str(safe_row_get(row, mapa_columnas.get('precio'))).strip()

            if not nombre_prod or not precio_crudo or len(nombre_prod) < 2: continue

            precio_str, precio_float, moneda = parse_precio_flexible(precio_crudo)

            if precio_float is None and not re.search(r'consultar|s/p', precio_str or "", re.IGNORECASE): continue
                
            producto = {
                "nombre": nombre_prod[:250],
                "precio_str": precio_str if precio_str else "Consultar",
                "precio_float": precio_float,
                "moneda": moneda if moneda else "ARS",
                "sku": str(safe_row_get(row, mapa_columnas.get('sku'))).strip()[:100],
                "descripcion": str(safe_row_get(row, mapa_columnas.get('descripcion'))).strip()[:1000],
                "marca": str(safe_row_get(row, mapa_columnas.get('marca'))).strip()[:100],
                "categoria_qdrant": str(safe_row_get(row, mapa_columnas.get('categoria')) or pyme_rubro_nombre).strip()[:100],
                "unidad": str(safe_row_get(row, mapa_columnas.get('unidad')) or 'unidad').strip()[:50],
                "cantidad_disponible": (
                    str(
                        parse_cantidad_flexible(
                            safe_row_get(row, mapa_columnas.get('stock')) or '1'
                        )
                        or '0'
                    )[:50]
                ),
            }
            productos_extraidos.append(producto)

        except Exception as e_row:
            logger.warning(f"⚠️ Error procesando la fila {index + fila_inicio_datos} del archivo. Saltando. Error: {e_row}")
            continue

    logger.info(f"✅ Proceso completado. Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
    return productos_extraidos

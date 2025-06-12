# services/procesar_catalogo_excel.py
import pandas as pd
import logging
import os
import re
from typing import List, Dict, Any, Optional, Tuple

# Asumimos que utils.py existe en el mismo nivel de carpeta (services/)
from .utils import limpiar_texto_base, parse_precio_flexible

logger = logging.getLogger(__name__)

# --- 1. DICCIONARIO CENTRAL DE PALABRAS CLAVE ---
# Unificamos todas las palabras clave en un solo lugar. Es nuestra fuente de verdad.
KEYWORD_MAP = {
    'sku': ["codigo", "código", "cod.", "sku", "art.", "articulo", "ref", "id", "item code", "ean"],
    'nombre': ["producto", "nombre", "descripción", "descripcion", "detalle", "variedad", "designacion", "item", "title"],
    'precio': ["precio", "lista", "pvp", "valor", "importe", "$", "contado", "oferta", "sugerido", "minorista", "publico", "tarifa"],
    'marca': ["marca", "linea", "línea", "brand", "fabricante", "bodega"],
    'categoria': ["categoria", "categoría", "rubro", "tipo", "familia", "clase"],
    'unidad': ["unidad", "unidades", "unid", "u/m", "presentacion", "envase", "caja x", "pack x", "contenido"],
    'stock': ["stock", "cantidad", "disponible", "existencias", "cant.", "quantity", "qty"],
}

# --- 2. EL NUEVO CEREBRO: MAPEADOR DE COLUMNAS INTELIGENTE ---
def crear_mapa_de_columnas_inteligente(
    df: pd.DataFrame, 
    max_filas_a_revisar: int = 15
) -> Optional[Tuple[Dict[str, str], int]]:
    """
    Analiza las primeras N filas de un DataFrame para encontrar la fila de encabezado
    y crear un mapa de columnas {'campo_estandar': 'nombre_columna_original'}.

    Devuelve: Una tupla (mapa_de_columnas, indice_fila_datos_inicio) o None si no encuentra un mapa válido.
    """
    logger.info(f"[MAPPER] Iniciando búsqueda inteligente de mapa de columnas en {max_filas_a_revisar} filas.")
    
    mejor_mapa = {}
    mejor_fila_idx = -1
    max_campos_encontrados = 0

    # Itera por las primeras filas del DataFrame para encontrar el mejor encabezado
    for i, row in df.head(max_filas_a_revisar).iterrows():
        mapa_actual = {}
        # Convierte la fila a una lista de strings limpios
        celdas_fila = [limpiar_texto_base(str(cell)) for cell in row.tolist() if pd.notna(cell) and str(cell).strip()]
        
        # Para cada campo estándar que queremos encontrar (nombre, precio, etc.)
        for campo_estandar, keywords in KEYWORD_MAP.items():
            # Si ya encontramos un mapeo para este campo, no lo buscamos de nuevo
            if campo_estandar in mapa_actual:
                continue
            
            # Busca en las celdas de la fila actual una coincidencia con las palabras clave
            for celda in celdas_fila:
                for keyword in keywords:
                    if re.search(r'\b' + re.escape(keyword) + r'\b', celda, re.IGNORECASE):
                        # Encontramos una! Mapeamos el campo estándar al nombre de la celda original
                        mapa_actual[campo_estandar] = celda
                        break # Pasamos a la siguiente celda
                if campo_estandar in mapa_actual:
                    break # Pasamos al siguiente campo estándar

        # Si el mapa actual es mejor que el que teníamos, lo guardamos
        if len(mapa_actual) > max_campos_encontrados:
            max_campos_encontrados = len(mapa_actual)
            mejor_mapa = mapa_actual
            mejor_fila_idx = i
            logger.info(f"[MAPPER] Nuevo mejor candidato a encabezado en fila {i} con {len(mapa_actual)} campos encontrados. Mapa: {mapa_actual}")

    # Después de revisar todas las filas, validamos si el mejor mapa encontrado es suficientemente bueno
    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        # El índice de la fila donde empiezan los datos es la siguiente al encabezado
        fila_inicio_datos = mejor_fila_idx + 1
        logger.info(f"✅ [MAPPER] Mapa de columnas final aceptado. Encabezados en fila {mejor_fila_idx}. Datos comienzan en {fila_inicio_datos}.")
        return mejor_mapa, fila_inicio_datos
    else:
        logger.error("[MAPPER] No se pudo crear un mapa de columnas válido. Faltan los campos esenciales 'nombre' y/o 'precio'.")
        return None

# --- 3. FUNCIÓN PRINCIPAL REFACTORIZADA ---
def procesar_catalogo_excel(path: str, pyme_user_id: int, pyme_rubro_nombre: str = "generico") -> List[Dict[str, Any]]:
    """
    Procesa un archivo Excel o CSV, detecta inteligentemente las columnas y extrae los productos.
    """
    base_filename = os.path.basename(path)
    logger.info(f"[EXCEL_PROC] Iniciando NUEVO procesamiento de: {base_filename} para user_id: {pyme_user_id}")

    try:
        # Lee el archivo sin encabezados para analizarlo en bruto
        df_bruto = pd.read_excel(path, header=None, sheet_name=0, keep_default_na=False, na_filter=False, dtype=str)
    except Exception as e_read:
        logger.error(f"❌ Error fatal al leer el archivo Excel/CSV '{base_filename}': {e_read}", exc_info=True)
        return []

    if df_bruto.empty:
        logger.warning(f"⚠️ El archivo '{base_filename}' está vacío o no se pudo leer.")
        return []

    # Llama al nuevo cerebro para obtener el mapa y la fila de inicio
    resultado_mapeo = crear_mapa_de_columnas_inteligente(df_bruto)
    
    if not resultado_mapeo:
        # Si el cerebro no pudo entender el archivo, lo notificamos y salimos.
        raise ValueError("No se pudieron identificar las columnas de 'Nombre' y 'Precio' en el archivo. Por favor, asegúrate de que el archivo tenga encabezados claros.")

    mapa_columnas, fila_inicio_datos = resultado_mapeo
    
    # Renombramos las columnas del DataFrame según nuestro mapa
    # Primero, invertimos el mapa para tener {'nombre_col_original': 'campo_estandar'}
    mapa_inverso = {v: k for k, v in mapa_columnas.items()}
    
    # Usamos la fila de encabezados para renombrar las columnas del DataFrame
    df_datos = df_bruto.copy()
    df_datos.columns = df_datos.iloc[fila_inicio_datos - 1].apply(limpiar_texto_base)
    # Ahora renombramos usando el mapa inverso
    df_datos = df_datos.rename(columns=mapa_inverso)
    # Quitamos las filas que estaban antes de los datos
    df_datos = df_datos.iloc[fila_inicio_datos:].reset_index(drop=True)
    df_datos.dropna(how='all', inplace=True)

    logger.info(f"DataFrame procesado con {len(df_datos)} filas. Columnas mapeadas: {df_datos.columns.tolist()}")

    productos_extraidos: List[Dict[str, Any]] = []
    # Itera por las filas del DataFrame ya limpio y mapeado
    for index, row in df_datos.iterrows():
        try:
            # Extraer datos es ahora mucho más simple y directo
            nombre_prod = str(row.get('nombre', '')).strip()
            precio_crudo = str(row.get('precio', '')).strip()

            # Si no hay nombre o precio en una fila, se salta.
            if not nombre_prod or not precio_crudo:
                continue

            precio_str, precio_float, moneda = parse_precio_flexible(precio_crudo)

            # Si el precio no se puede parsear y no es un texto como "consultar", se salta.
            if precio_float is None and not re.search(r'consultar|s/p', precio_str or "", re.IGNORECASE):
                continue
                
            producto = {
                "nombre": nombre_prod[:250],
                "precio_str": precio_str if precio_str else "Consultar",
                "precio_float": precio_float,
                "moneda": moneda if moneda else "ARS",
                "sku": str(row.get('sku', ''))[:100],
                "descripcion": str(row.get('descripcion', ''))[:1000],
                "marca": str(row.get('marca', ''))[:100],
                "categoria_qdrant": str(row.get('categoria', pyme_rubro_nombre))[:100],
                "unidad": str(row.get('unidad', 'unidad'))[:50],
                "cantidad_disponible": str(row.get('stock', '1'))[:50],
            }
            productos_extraidos.append(producto)

        except Exception as e_row:
            logger.warning(f"⚠️ Error procesando la fila {index + fila_inicio_datos} del archivo. Saltando. Error: {e_row}")
            continue

    logger.info(f"✅ Proceso completado. Total productos extraídos de '{base_filename}': {len(productos_extraidos)}")
    return productos_extraidos
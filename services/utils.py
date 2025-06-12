# services/utils.py
import re
import pandas as pd
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# --- CAJA DE HERRAMIENTAS (Conservamos tus excelentes funciones de parseo) ---

def limpiar_texto_base(texto: Optional[Any]) -> str:
    if texto is None: return ""
    if not isinstance(texto, str):
        try: texto = str(texto)
        except Exception: return ""
    return re.sub(r'\s+', ' ', texto).strip().lower()

def parse_precio_flexible(texto_precio_input: Optional[Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    # Tu potente función para parsear precios
    if texto_precio_input is None: return None, None, None
    texto_precio_str = str(texto_precio_input).strip()
    if not texto_precio_str: return None, None, None
    moneda_detectada = "ARS"
    numero_para_procesar = texto_precio_str
    if re.search(r"(?i)\bUSD\b|U\$S", numero_para_procesar):
        moneda_detectada = "USD"
        numero_para_procesar = re.sub(r"(?i)\bUSD\b|U\$S", "", numero_para_procesar, flags=re.IGNORECASE).strip()
    if "$" in numero_para_procesar:
        numero_para_procesar = numero_para_procesar.replace("$", "", 1).strip()
    if not numero_para_procesar: return texto_precio_str, None, moneda_detectada
    numero_normalizado = numero_para_procesar
    if ',' in numero_normalizado and '.' in numero_normalizado:
        if numero_normalizado.rfind(',') > numero_normalizado.rfind('.'):
            numero_normalizado = numero_normalizado.replace('.', '').replace(',', '.')
        else:
            numero_normalizado = numero_normalizado.replace(',', '')
    elif ',' in numero_normalizado:
        partes_coma = numero_normalizado.split(',')
        if len(partes_coma) > 1 and len(partes_coma[-1]) in [1, 2] and partes_coma[-1].isdigit():
            numero_normalizado = "".join(partes_coma[:-1]) + "." + partes_coma[-1]
        else:
            numero_normalizado = numero_normalizado.replace(',', '')
    numero_final_para_float_str = re.sub(r"[^0-9.]", "", numero_normalizado)
    try:
        if not numero_final_para_float_str or not re.search(r"\d", numero_final_para_float_str): raise ValueError("String numérico no contiene dígitos válidos")
        precio_flt = round(float(numero_final_para_float_str), 2)
        return texto_precio_str, precio_flt, moneda_detectada
    except (ValueError, TypeError):
        if re.search(r"\d", numero_para_procesar): return texto_precio_str, None, moneda_detectada
        return texto_precio_str, None, moneda_detectada

# --- EL CEREBRO INTELIGENTE v2.0 ---

# Diccionario expandido con las palabras clave de tus archivos de ejemplo
KEYWORD_MAP = {
    'sku': [
        "codigo", "código", "cod.", "cod", "sku", "art.", "articulo", 
        "ref", "referencia", "id", "item code", "ean"
    ],
    'nombre': [
        "producto", "nombre", "descripción", "descripcion", "detalle", 
        "variedad", "designacion", "item", "title", "denominacion", "vino"
    ],
    'precio': [
        "precio", "precio lista", "lista", "pvp", "valor", "importe", "$", "contado", 
        "oferta", "sugerido", "minorista", "publico", "tarifa", "precio sugerido contado",
        "$ botella", "$ caja", "precio unitario", "distribuidor"
    ],
    'marca': ["marca", "brand", "fabricante", "bodega", "productor"],
    'categoria': [
        "categoria", "categoría", "rubro", "tipo", "familia", "clase", 
        "linea", "línea"
    ],
    'unidad': [
        "unidad", "unidades", "unid", "un.", "u/m", "presentacion", "envase", 
        "caja x", "un/caja", "pack x", "contenido", "botella"
    ],
    'stock': [
        "stock", "cantidad", "disponible", "existencias", "cant.", "quantity", 
        "qty"
    ],
    'descripcion_larga': [ # Nuevo campo para descripciones más extensas
        "notas de cata", "composicion", "caracteristicas", "observaciones"
    ]
}

def crear_mapa_de_columnas_inteligente(
    df: pd.DataFrame, 
    max_filas_a_revisar: int = 15
) -> Optional[Tuple[Dict[str, str], int]]:
    """
    Analiza las primeras N filas de un DataFrame para encontrar la fila de encabezado
    y crear un mapa de columnas {'campo_estandar': 'nombre_columna_original'}.
    Devuelve: Una tupla (mapa_de_columnas, indice_fila_datos_inicio) o None si no encuentra un mapa válido.
    """
    logger.info(f"[CEREBRO] Iniciando búsqueda inteligente de mapa de columnas...")
    
    mejor_mapa: Dict[str, str] = {}
    mejor_fila_idx: int = -1
    max_campos_encontrados: int = 0

    for i, row in df.head(max_filas_a_revisar).iterrows():
        mapa_actual = {}
        celdas_originales = [str(cell).strip() for cell in row.tolist()]
        # Filtramos filas que son claramente no-encabezados (ej. solo tienen un item largo)
        if len(celdas_originales) < 2 and len(celdas_originales[0]) > 50: continue

        celdas_limpias = [limpiar_texto_base(cell) for cell in celdas_originales]
        
        for campo_estandar, keywords in KEYWORD_MAP.items():
            if campo_estandar in mapa_actual: continue
            for idx, celda_limpia in enumerate(celdas_limpias):
                if not celda_limpia: continue
                for keyword in keywords:
                    # Usamos regex para buscar la palabra completa y evitar sub-matches (ej: 'id' en 'identificacion')
                    if re.search(r'\b' + re.escape(keyword) + r'\b', celda_limpia, re.IGNORECASE):
                        mapa_actual[campo_estandar] = celdas_originales[idx]
                        break
                if campo_estandar in mapa_actual: break

        # Damos más peso a los mapas que encuentran los campos más importantes
        score = 0
        if 'nombre' in mapa_actual: score += 5
        if 'precio' in mapa_actual: score += 5
        if 'sku' in mapa_actual: score += 2
        score += len(mapa_actual) # Y un punto por cada otro campo encontrado

        if score > max_campos_encontrados:
            max_campos_encontrados = score
            mejor_mapa = mapa_actual
            mejor_fila_idx = i

    # Condición final: para ser un mapa válido, DEBE tener nombre y precio.
    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        fila_inicio_datos = mejor_fila_idx + 1
        logger.info(f"✅ [CEREBRO] Mapa de columnas válido encontrado. Encabezados en fila {mejor_fila_idx}. Datos comienzan en {fila_inicio_datos}. Mapa: {mejor_mapa}")
        return mejor_mapa, fila_inicio_datos
    else:
        logger.error("[CEREBRO] No se pudo crear un mapa válido. Faltan campos esenciales 'nombre' y/o 'precio'.")
        return None
# services/utils.py
import re
import pandas as pd
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# --- CAJA DE HERRAMIENTAS DE LIMPIEZA Y PARSEO ---

def limpiar_texto_base(texto: Optional[Any]) -> str:
    """Limpia y normaliza texto de forma robusta, asegurando que la entrada sea un string."""
    if texto is None: return ""
    if not isinstance(texto, str):
        try: texto = str(texto)
        except Exception: return ""
    # Normaliza espacios, convierte a minúsculas
    return re.sub(r'\s+', ' ', texto).strip().lower()

def parse_precio_flexible(texto_precio_input: Optional[Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """
    Tu potente función para parsear precios. La conservamos y usamos porque es excelente.
    Toma cualquier tipo de entrada, la convierte a string y extrae el precio y la moneda.
    """
    if texto_precio_input is None: return None, None, None
    texto_precio_str = str(texto_precio_input).strip()
    if not texto_precio_str: return None, None, None

    moneda_detectada = "ARS"
    numero_para_procesar = texto_precio_str

    if re.search(r"(?i)\bUSD\b|U\$S", numero_para_procesar):
        moneda_detectada = "USD"
        numero_para_procesar = re.sub(r"(?i)\bUSD\b|U\$S", "", numero_para_procesar, flags=re.IGNORECASE).strip()
    elif re.search(r"(?i)\bARS\b", numero_para_procesar):
        moneda_detectada = "ARS"
        # No es necesario quitar ARS si no hay símbolo de peso
    
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
        if not numero_final_para_float_str or not re.search(r"\d", numero_final_para_float_str):
            raise ValueError("String numérico no contiene dígitos válidos")
        precio_flt = round(float(numero_final_para_float_str), 2)
        # Devolvemos el string original por si era "Consultar", el float y la moneda
        return texto_precio_str, precio_flt, moneda_detectada
    except (ValueError, TypeError):
        # Si falla la conversión pero había algún número, devolvemos el texto original
        if re.search(r"\d", numero_para_procesar):
            return texto_precio_str, None, moneda_detectada
        return texto_precio_str, None, moneda_detectada

# --- EL CEREBRO INTELIGENTE ---

KEYWORD_MAP = {
    'sku': ["codigo", "código", "cod.", "sku", "art.", "articulo", "ref", "id", "item code", "ean"],
    'nombre': ["producto", "nombre", "descripción", "descripcion", "detalle", "variedad", "designacion", "item", "title"],
    'precio': ["precio", "lista", "pvp", "valor", "importe", "$", "contado", "oferta", "sugerido", "minorista", "publico", "tarifa"],
    'marca': ["marca", "linea", "línea", "brand", "fabricante", "bodega"],
    'categoria': ["categoria", "categoría", "rubro", "tipo", "familia", "clase"],
    'unidad': ["unidad", "unidades", "unid", "u/m", "presentacion", "envase", "caja x", "pack x", "contenido"],
    'stock': ["stock", "cantidad", "disponible", "existencias", "cant.", "quantity", "qty"],
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
        celdas_limpias = [limpiar_texto_base(cell) for cell in celdas_originales]
        
        for campo_estandar, keywords in KEYWORD_MAP.items():
            if campo_estandar in mapa_actual: continue
            for idx, celda_limpia in enumerate(celdas_limpias):
                if not celda_limpia: continue
                for keyword in keywords:
                    if re.search(r'\b' + re.escape(keyword) + r'\b', celda_limpia, re.IGNORECASE):
                        mapa_actual[campo_estandar] = celdas_originales[idx]
                        break
                if campo_estandar in mapa_actual: break

        if len(mapa_actual) > max_campos_encontrados:
            max_campos_encontrados = len(mapa_actual)
            mejor_mapa = mapa_actual
            mejor_fila_idx = i

    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        fila_inicio_datos = mejor_fila_idx + 1
        logger.info(f"✅ [CEREBRO] Mapa de columnas válido encontrado. Encabezados en fila {mejor_fila_idx}. Datos comienzan en {fila_inicio_datos}. Mapa: {mejor_mapa}")
        return mejor_mapa, fila_inicio_datos
    else:
        logger.error("[CEREBRO] No se pudo crear un mapa válido. Faltan campos esenciales 'nombre' y/o 'precio'.")
        return None
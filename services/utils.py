# services/utils.py
import re
import os
import json
import pandas as pd
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# --- 1. CAJA DE HERRAMIENTAS (Tus funciones originales y probadas) ---

def limpiar_texto_base(texto: Optional[Any]) -> str:
    """Limpia y normaliza texto de forma robusta, asegurando que la entrada sea un string."""
    if texto is None: return ""
    if not isinstance(texto, str):
        try: texto = str(texto)
        except Exception: return ""
    return re.sub(r'\s+', ' ', texto).strip().lower()

def parse_precio_flexible(texto_precio_input: Optional[Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """Tu potente función para parsear precios. Se conserva intacta."""
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
        return texto_precio_str, precio_flt, moneda_detectada
    except (ValueError, TypeError):
        if re.search(r"\d", numero_para_procesar):
            return texto_precio_str, None, moneda_detectada
        return texto_precio_str, None, moneda_detectada

def parse_cantidad_flexible(texto_cantidad_input: Optional[Any]) -> Optional[float]:
    """Intenta extraer un número de una cantidad con formato libre."""
    if texto_cantidad_input is None:
        return None
    texto = str(texto_cantidad_input).strip()
    if not texto:
        return None
    texto_norm = texto.replace(".", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", texto_norm)
    if not match:
        return None
    try:
        num = float(match.group())
        return int(num) if num.is_integer() else num
    except ValueError:
        return None

def extraer_unidades_y_tipos_precio(texto_linea: str, pyme_rubro_nombre: str = "generico") -> tuple[Optional[str], Optional[str]]:
    """Tu excelente función para extraer unidades y tipos de precio. Se conserva intacta."""
    if not texto_linea: return None, None
    texto_linea_lower = limpiar_texto_base(texto_linea) 
    unidad = None; tipo_precio = None
    unidades_patrones = [
        (r"\b(caja(?:s)?\s*x\s*\d{1,3})\b", "caja_especifica"), (r"\b(pack\s*x\s*\d{1,3})\b", "pack_especifico"),
        (r"\b(\d{1,4}\s*ml)\b", "volumen_ml"), (r"\b(\d{1,2}(?:[.,]\d{1,2})?\s*lts?)\b", "volumen_lts"),
        (r"\b(\d{1,3}(?:[.,]\d{1,3})?\s*kilos?g?)\b", "peso_kg"), (r"\b(\d{1,4}\s*gr(?:s)?|gramo(?:s)?)\b", "peso_gr"),
        (r"\b(docena(?:s)?)\b", "docena"), (r"\b(par(?:es)?)\b", "par"),
        (r"\b(blister(?:es)?\s*x\s*\d{1,2})\b", "blister_especifico"), (r"\b(horma)\b", "horma"),
        (r"\b(caja|box)\b", "caja_generica"), (r"\b(botella|bottle)\b", "botella_generica"),
        (r"\b(pack)\b", "pack_generico"), (r"\b(blister)\b", "blister_generico"),
        (r"\b(unidad|unidades|unid\.?|un\.?|u\.?)\b", "unidad_generica")]
    tipos_precio_keywords = { "mayorista": ["mayorista", "por mayor", "distribuidor", "distr?", "mayor"], "minorista": ["minorista", "al detalle", "público", "publico", "consumidor final", "cf", "minor"], "promocion": ["promo", "oferta", "descuento", "dcto", "dto", "especial", "sale", "liquidación", "outlet", "rebaja"]}
    mejor_match_unidad = None; texto_linea_temp = texto_linea_lower
    for patron, _ in unidades_patrones:
        match = re.search(patron, texto_linea_temp) 
        if match:
            unidad_encontrada_raw = match.group(1)
            if mejor_match_unidad is None or len(unidad_encontrada_raw) > len(mejor_match_unidad): mejor_match_unidad = unidad_encontrada_raw
    if mejor_match_unidad: unidad = re.sub(r'\s+', ' ', mejor_match_unidad).strip()
    for tipo, keywords in tipos_precio_keywords.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw.replace("?", "\\w?")) + r'\b', texto_linea_lower): tipo_precio = tipo; break
        if tipo_precio: break
    return unidad, tipo_precio

# --- 2. EL CEREBRO INTELIGENTE v3.0 (Nuestra Lógica de Mapeo Mejorada) ---

KEYWORD_MAP = {
    'sku': [
        "codigo", "código", "cod.", "cod", "sku", "art.", "articulo", "art",
        "ref", "referencia", "id", "item code", "ean"
    ],
    'nombre': [
        "producto", "nombre", "variedad", "designacion", "item",
        "title", "denominacion", "vino", "articulo"
    ],
    'descripcion': [
        "descripcion", "descripción", "detalle", "detalles",
        "descripcion producto", "description", "comentarios"
    ],
    'precio': [
        "precio", "precio lista", "lista", "pvp", "p.v.p", "valor", "importe", "$", "contado", 
        "oferta", "sugerido", "minorista", "publico", "tarifa", "precio sugerido contado",
        "$ botella", "$ caja", "precio unitario", "distribuidor", "gremio", "monto"
    ],
    'marca': ["marca", "brand", "fabricante", "bodega", "productor", "linea", "línea"],
    'categoria': ["categoria", "categoría", "rubro", "tipo", "familia", "clase"],
    'unidad': [
        "unidad", "unidades", "unid", "un.", "u/m", "presentacion", "envase", 
        "caja x", "un/caja", "pack x", "contenido", "botella"
    ],
    'stock': [
        "stock", "cantidad", "disponible", "existencias", "cant.", "quantity", "qty"
    ],
    'descripcion_larga': [
        "notas de cata", "composicion", "caracteristicas", "observaciones", "añada", "cosecha"
    ]
}

def crear_mapa_de_columnas_inteligente(
    df: pd.DataFrame,
    max_filas_a_revisar: int = 50
) -> Optional[Tuple[Dict[str, str], int]]:
    """
    Analiza las primeras N filas de un DataFrame para encontrar la fila de encabezado
    y crear un mapa de columnas {'campo_estandar': 'nombre_columna_original'}.
    Devuelve: Una tupla (mapa_de_columnas, indice_fila_datos_inicio) o None si no encuentra un mapa válido.
    """
    logger.info(f"[CEREBRO] Iniciando búsqueda inteligente de mapa de columnas...")
    
    mejor_mapa: Dict[str, str] = {}
    mejor_fila_idx: int = -1
    mejor_score: int = 0

    for i, row in df.head(max_filas_a_revisar).iterrows():
        mapa_actual = {}
        celdas_originales = [str(cell).strip() for cell in row.tolist()]
        if len([c for c in celdas_originales if c]) < 2: continue

        celdas_limpias = [limpiar_texto_base(cell) for cell in celdas_originales]
        
        for campo_estandar, keywords in KEYWORD_MAP.items():
            if campo_estandar in mapa_actual: continue
            for idx, celda_limpia in enumerate(celdas_limpias):
                if not celda_limpia: continue
                for keyword in keywords:
                    if re.search(r'\b' + re.escape(keyword) + r'\b', celda_limpia, re.IGNORECASE):
                        if celdas_originales[idx] not in mapa_actual.values():
                            mapa_actual[campo_estandar] = celdas_originales[idx]
                            break
                if campo_estandar in mapa_actual: break

        score_actual = 0
        if 'nombre' in mapa_actual: score_actual += 10
        if 'precio' in mapa_actual: score_actual += 10
        if 'sku' in mapa_actual: score_actual += 3
        score_actual += len(mapa_actual)

        if score_actual > mejor_score:
            mejor_score = score_actual
            mejor_mapa = mapa_actual
            mejor_fila_idx = i

    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        fila_inicio_datos = mejor_fila_idx + 1
        logger.info(
            f"✅ [CEREBRO] Mapa de columnas válido encontrado. Encabezados en fila {mejor_fila_idx}. Score: {mejor_score}. Mapa: {mejor_mapa}"
        )
        return mejor_mapa, fila_inicio_datos

    # Intento heurístico adicional cuando falta 'nombre' o 'precio'
    logger.warning(
        f"[CEREBRO] Buscando mapa por heurística. Mapa parcial: {mejor_mapa}"
    )

    if 'precio' not in mejor_mapa:
        for col in df.columns:
            valores = df[col].head(max_filas_a_revisar)
            parseables = sum(1 for v in valores if parse_precio_flexible(v)[1] is not None)
            if parseables >= max(2, len(valores) // 2):
                mejor_mapa['precio'] = col
                break

    if 'nombre' not in mejor_mapa:
        for col in df.columns:
            if col == mejor_mapa.get('precio'):
                continue
            textos = [str(v).strip() for v in df[col].head(max_filas_a_revisar)]
            largas = [t for t in textos if len(t) > 2]
            if len(largas) >= len(textos) // 2:
                mejor_mapa['nombre'] = col
                break

    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        logger.info(f"✅ [CEREBRO] Mapa heurístico obtenido: {mejor_mapa}")
        return mejor_mapa, 0

    logger.error(
        f"[CEREBRO] No se pudo crear un mapa válido. Faltan campos esenciales 'nombre' y/o 'precio'. Mejor mapa encontrado: {mejor_mapa}"
    )
    return None

# --- 3. OTRAS UTILIDADES (Función que ya tenías) ---
def sugerencias_por_rubro(rubro):
    """
    Devuelve una lista de sugerencias de preguntas para el rubro desde /data/sugerencias.json.
    """
    SUGERENCIAS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'sugerencias.json')
    try:
        with open(SUGERENCIAS_PATH, encoding='utf-8') as f:
            sugerencias_data = json.load(f)
    except Exception as e:
        logging.warning(f"[UTILS] No se pudo leer sugerencias.json: {e}")
        sugerencias_data = {}

    rubro_nombre = None
    if isinstance(rubro, str):
        rubro_nombre = rubro.lower().replace(" ", "_")
    elif hasattr(rubro, 'nombre'):
        rubro_nombre = str(rubro.nombre).lower().replace(" ", "_")
    elif isinstance(rubro, int):
        id_map = {1: "bodega", 2: "almacen", 3: "medico", 4: "local_comercial", 5: "municipios"}
        rubro_nombre = id_map.get(rubro)
    
    if not rubro_nombre:
        rubro_nombre = "bodega"  # Fallback seguro

    sugerencias = sugerencias_data.get(rubro_nombre, [])
    if not sugerencias:
        sugerencias = ["Consultá nuestro catálogo", "Contactá a un asesor", "Visitá nuestra web para más info"]
    return sugerencias
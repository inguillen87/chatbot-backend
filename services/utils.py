# services/utils.py
import re
import os
import json
try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas es opcional para tests
    class _DummyPD:
        class Series: ...
        class DataFrame: ...
    pd = _DummyPD()
import logging
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# --- 1. CAJA DE HERRAMIENTAS (Tus funciones originales y probadas) ---

def limpiar_texto_base(texto: Optional[Any]) -> str:
    """Limpia y normaliza texto de forma robusta, asegurando que la entrada sea un string."""
    if texto is None: return ""
    if not isinstance(texto, str):
        try: texto = str(texto)
        except Exception: return ""
    return re.sub(r'\s+', ' ', texto).strip().lower()

def unir_codigos_alfa_numericos(texto: str) -> str:
    """Une secuencias alfanuméricas separadas por espacios (por ejemplo 'de 108 c' -> 'de108c')."""
    if not isinstance(texto, str):
        return ""
    # Junta letras seguidas de números o viceversa cuando están separados solo por espacios
    texto = re.sub(r'([A-Za-z])\s+(?=\d)', r"\1", texto)
    texto = re.sub(r'(\d)\s+(?=[A-Za-z])', r"\1", texto)
    return texto

def parse_precio_flexible(texto_precio_input: Optional[Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """Tu potente función para parsear precios. Se conserva intacta."""
    if texto_precio_input is None: return None, None, None
    texto_precio_str = str(texto_precio_input).strip()
    if not texto_precio_str: return None, None, None
    
    moneda_detectada = "ARS"
    numero_para_procesar = texto_precio_str

    # Extrae el último patrón numérico significativo (para casos como
    # "1/2 DOC POR $ 5.400,00")
    posibles_numeros = re.findall(r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?", numero_para_procesar)
    if posibles_numeros:
        numero_para_procesar = posibles_numeros[-1]

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


def generar_link_google_maps(
    direccion: str | None = None,
    latitud: float | None = None,
    longitud: float | None = None,
) -> str | None:
    """Genera un enlace a Google Maps a partir de una dirección o coordenadas."""
    if latitud is not None and longitud is not None:
        return f"https://www.google.com/maps/search/?api=1&query={latitud},{longitud}"
    if direccion:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(direccion)}"
    return None

def safe_row_get(row: pd.Series, column: Any) -> Any:
    """Safely obtain a value from a DataFrame row by label or position."""
    if isinstance(column, int):
        if column < len(row):
            return row.iloc[column]
        return ""
    return row.get(column, "")

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

def calcular_precio_por_unidad(precio_float: Optional[float], unidad_texto: str) -> Optional[float]:
    """Calcula el precio por unidad cuando la presentación indica varias unidades."""
    if precio_float is None or not unidad_texto:
        return None
    texto = limpiar_texto_base(unidad_texto)
    match = re.search(r"(?:x|por|de)\s*(\d+(?:[.,]\d+)?)", texto)
    if not match:
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:unidades|unidad|u|uds?|pack|caja)", texto)
    if match:
        try:
            cantidad = float(match.group(1).replace(',', '.'))
            if cantidad > 0:
                return round(precio_float / cantidad, 2)
        except ValueError:
            return None
    return None

def calcular_monto_total_items(items: List[Dict[str, Any]]) -> float:
    """Calcula el monto total de una lista de items {cantidad, precio}."""
    total = 0.0
    for item in items:
        try:
            cantidad = float(item.get("cantidad", 1))
            precio = float(item.get("precio", 0))
            total += cantidad * precio
        except (TypeError, ValueError):
            continue
    return round(total, 2)

# --- 2. EL CEREBRO INTELIGENTE v3.0 (Nuestra Lógica de Mapeo Mejorada) ---

KEYWORD_MAP = {
    'sku': [
        "codigo", "código", "cod.", "cod", "sku", "art.", "articulo", "art",
        "ref", "referencia", "id", "item code", "ean"
    ],
    'nombre': [
        "producto", "nombre", "variedad", "designacion", "item",
        "title", "denominacion", "vino", "articulo", "descripcion",
        "descripción", "detalle"
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
                # idx es el índice de la columna en la fila actual (y por ende en el df_raw si header=None)
                for keyword in keywords:
                    if re.search(r'\b' + re.escape(keyword) + r'\b', celda_limpia, re.IGNORECASE):
                        # Guardar el índice de la columna, no el nombre del header.
                        # Asegurarse de no mapear el mismo índice a múltiples campos estándar.
                        if idx not in mapa_actual.values():
                            mapa_actual[campo_estandar] = idx # Guardar el índice de la columna
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

    # Fallback: usar columna de SKU o descripción como nombre cuando no se
    # identificó explícitamente una columna de nombre
    if 'nombre' not in mejor_mapa:
        if 'descripcion' in mejor_mapa:
            mejor_mapa['nombre'] = mejor_mapa['descripcion']
        elif 'sku' in mejor_mapa:
            mejor_mapa['nombre'] = mejor_mapa['sku']
    if 'nombre' in mejor_mapa and 'precio' in mejor_mapa:
        logger.info(f"✅ [CEREBRO] Mapa de columnas asignado por fallback: {mejor_mapa}")
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

import re

def validar_email(email: str) -> bool:
    """Valida formato básico de email."""
    if not isinstance(email, str):
        return False
    return re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email) is not None

def validar_telefono(telefono: str) -> bool:
    """Valida que el teléfono tenga solo números y al menos 8 dígitos."""
    if not isinstance(telefono, str):
        return False
    solo_numeros = re.sub(r"\D", "", telefono)
    return len(solo_numeros) >= 8

def parse_unidad_y_cantidad_empaque(texto_unidad_str: Optional[str]) -> tuple[str, Optional[int]]:
    """
    Intenta extraer una cantidad numérica de empaque y una descripción de unidad.
    Ej: "Caja x 6 botellas" -> ("Caja botellas", 6)
        "Pack de 12"      -> ("Pack", 12)
        "Botella 750ml"   -> ("Botella 750ml", 1) (o None si no se asume 1)
        "6 unidades"      -> ("unidades", 6)
    """
    if not texto_unidad_str or not isinstance(texto_unidad_str, str):
        # Devuelve el texto original (o vacío) y None para la cantidad si la entrada no es válida
        return str(texto_unidad_str or "").strip(), None

    original_descripcion_stripped = texto_unidad_str.strip()
    texto_lower = original_descripcion_stripped.lower()

    cantidad_empaque: Optional[int] = None
    unidad_descripcion: str = original_descripcion_stripped # Default

    # Patrones comunes: "6 pack", "pack x 6", "caja de 12", "12 unidades", "x12"
    # Priorizar patrones que claramente separan número y texto o usan palabras clave.
    patterns = [
        # Caso: "6 pack", "12 unidades", "24 botellas" (número seguido de palabra)
        re.compile(r"(\d+)\s*([a-zA-Záéíóúñü]+(?:(?:\s+de)?\s*[a-zA-Záéíóúñü]+)*)"),
        # Caso: "pack x 6", "caja de 12", "unidades: 24" (palabra clave seguida de número)
        re.compile(r"([a-zA-Záéíóúñü]+(?:(?:\s+de)?\s*[a_zA-Záéíóúñü]+)*)\s*(?:x|de|por|:|pack|-|)\s*(\d+)"),
        # Caso: "x6", "x 6", "/12" (solo un prefijo y número) - más genérico, menos prioritario
        re.compile(r"(?:x|/)\s*(\d+)"),
    ]

    for i, pattern in enumerate(patterns):
        match = pattern.search(texto_lower)
        if match:
            try:
                if i == 0: # "6 pack"
                    cantidad_empaque = int(match.group(1))
                    unidad_descripcion = match.group(2).strip()
                     # Capitalizar la primera letra de la descripción de la unidad si es solo una palabra
                    if ' ' not in unidad_descripcion:
                        unidad_descripcion = unidad_descripcion.capitalize()
                    else: # Capitalizar cada palabra
                        unidad_descripcion = ' '.join(word.capitalize() for word in unidad_descripcion.split())
                elif i == 1: # "pack x 6"
                    cantidad_empaque = int(match.group(2))
                    unidad_descripcion = match.group(1).strip()
                    if ' ' not in unidad_descripcion:
                        unidad_descripcion = unidad_descripcion.capitalize()
                    else:
                        unidad_descripcion = ' '.join(word.capitalize() for word in unidad_descripcion.split())
                elif i == 2: # "x6"
                    cantidad_empaque = int(match.group(1))
                    # Aquí la descripción de la unidad es más difícil, mantenemos la original o una genérica
                    # Podríamos intentar quitar el "xN" de la original.
                    temp_desc = re.sub(r"(?:x|/)\s*" + str(cantidad_empaque), "", original_descripcion_stripped, flags=re.IGNORECASE).strip()
                    unidad_descripcion = temp_desc if temp_desc else "Pack" # Fallback a "Pack" si quitarlo deja vacío

                # Si encontramos una cantidad, rompemos para no sobrescribir con patrones menos específicos
                if cantidad_empaque is not None:
                    break
            except (ValueError, IndexError):
                continue # Error en parsing del match, intentar siguiente patrón

    # Si después de los patrones no se encontró cantidad pero el texto sugiere singularidad
    if cantidad_empaque is None:
        singular_keywords = ["botella", "unidad", "lata", "pieza", "blister", "rollo", "sachet", "frasco", "pote"]
        # No usar "caja" o "pack" aquí porque usualmente implican múltiples, a menos que se especifique "1 caja"
        is_singular = any(keyword in texto_lower for keyword in singular_keywords)
        if is_singular:
            # Verificamos si la palabra "docena" está presente
            if "docena" in texto_lower:
                cantidad_empaque = 12
                unidad_descripcion = "Docena" # O mantener la descripción original
            else:
                cantidad_empaque = 1
            # unidad_descripcion ya es original_descripcion_stripped por defecto
            # Podríamos intentar limpiarla un poco más si es necesario aquí.

    # Si la descripción de la unidad quedó vacía pero la original no, usar la original.
    if not unidad_descripcion.strip() and original_descripcion_stripped:
        unidad_descripcion = original_descripcion_stripped
    elif not unidad_descripcion.strip() and not original_descripcion_stripped: # Ambas vacias
        unidad_descripcion = "" # Asegurar que sea string vacio y no None

    return unidad_descripcion, cantidad_empaque

def formatear_telefono_e164(telefono: str, codigo_pais: str = "54") -> str:
    """Devuelve el teléfono en formato E164 o cadena vacía si no es válido."""
    if not telefono:
        return ""
    telefono = telefono.strip()
    if telefono.startswith("+"):
        return telefono
    solo_numeros = re.sub(r"\D", "", telefono)
    if len(solo_numeros) < 8:
        return ""
    solo_numeros = solo_numeros.lstrip("0")
    return f"+{codigo_pais}{solo_numeros}"

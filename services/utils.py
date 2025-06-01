# services/utils.py
import re
import logging
from typing import Optional, Tuple # Añadido Tuple y Optional

logger = logging.getLogger(__name__)

def limpiar_texto_base(texto: Optional[str]) -> str:
    if texto is None:
        return ""
    if not isinstance(texto, str):
        try:
            texto = str(texto)
        except Exception:
            logger.warning(f"No se pudo convertir a string el valor: {texto}. Se devuelve cadena vacía.")
            return ""
            
    texto_limpio = texto.lower()
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio)
    return texto_limpio.strip()

def parse_precio_flexible(texto_precio_input: Optional[str | float | int]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    if texto_precio_input is None:
        return None, None, None
    
    texto_precio_str = str(texto_precio_input).strip()
    if not texto_precio_str:
        return None, None, None

    moneda_detectada = "ARS" 
    numero_para_procesar = texto_precio_str

    # Mejorado para quitar el símbolo $ solo si no es parte de U$S
    # Primero buscar monedas específicas
    if re.search(r"(?i)\bUSD\b|U\$S", numero_para_procesar): # U$S o USD
        moneda_detectada = "USD"
        numero_para_procesar = re.sub(r"(?i)\bUSD\b|U\$S", "", numero_para_procesar).strip()
    elif re.search(r"(?i)\bARS\b", numero_para_procesar):
        moneda_detectada = "ARS"
        numero_para_procesar = re.sub(r"(?i)\bARS\b", "", numero_para_procesar).strip()
    elif re.search(r"(?i)\bEUR\b|€", numero_para_procesar):
        moneda_detectada = "EUR"
        numero_para_procesar = re.sub(r"(?i)\bEUR\b|€", "", numero_para_procesar).strip()
    # Otros símbolos de moneda
    elif "€" in numero_para_procesar: # Euro símbolo
        moneda_detectada = "EUR"
        numero_para_procesar = numero_para_procesar.replace("€", "").strip()
    elif "$" in numero_para_procesar: # Símbolo $ (podría ser ARS u otra, ARS por defecto si no se especificó USD antes)
        if moneda_detectada == "ARS": # Solo quitar $ si no es parte de U$S ya procesado
            numero_para_procesar = numero_para_procesar.replace("$", "").strip()

    if not numero_para_procesar:
        logger.debug(f"parse_precio_flexible: No quedó parte numérica de '{texto_precio_input}' tras quitar moneda/símbolos.")
        return None, None, None

    numero_normalizado = numero_para_procesar
    if ',' in numero_normalizado and '.' in numero_normalizado:
        if numero_normalizado.rfind(',') > numero_normalizado.rfind('.'):
            numero_normalizado = numero_normalizado.replace('.', '').replace(',', '.')
        else: 
            numero_normalizado = numero_normalizado.replace(',', '')
    elif ',' in numero_normalizado:
        # Si hay múltiples comas, asumir que todas menos la última (si parece decimal) son separadores de miles
        partes_coma = numero_normalizado.split(',')
        if len(partes_coma) > 1 and len(partes_coma[-1]) in [1, 2] and partes_coma[-1].isdigit():
            numero_normalizado = "".join(partes_coma[:-1]) + "." + partes_coma[-1]
        else:
            numero_normalizado = numero_normalizado.replace(',', '') # Tratar todas las comas como ruido
    
    numero_final_para_float_str = re.sub(r"[^0-9.]", "", numero_normalizado)

    try:
        if not numero_final_para_float_str:
            raise ValueError("String numérico vacío después de la normalización")
        precio_flt = float(numero_final_para_float_str)
        
        # Para precio_str, devolver el número limpio que se usó para el float,
        # o el 'numero_para_procesar' si queremos mantener un formato más original sin la moneda.
        # Usaremos numero_final_para_float_str que es el más limpio numéricamente.
        return numero_final_para_float_str, precio_flt, moneda_detectada
    except ValueError:
        # logger.warning(f"parse_precio_flexible: No se pudo convertir a float: '{numero_final_para_float_str}' (procesado de '{texto_precio_input}')")
        if re.search(r"\d", numero_para_procesar):
             return limpiar_texto_base(numero_para_procesar), None, moneda_detectada
        return None, None, moneda_detectada

def extraer_unidades_y_tipos_precio(texto_linea: str, pyme_rubro_nombre: str = "generico") -> tuple[Optional[str], Optional[str]]:
    # ... (Tu función como la tenías, o la versión mejorada de mi respuesta anterior @‶gANVneHZLGZg...)
    # Asegúrate de que esté completa y funcional aquí.
    # Por ejemplo, la versión de la respuesta anterior:
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
    tipos_precio_keywords = {
        "mayorista": ["mayorista", "por mayor", "distribuidor", "distr?", "mayor"],
        "minorista": ["minorista", "al detalle", "público", "publico", "consumidor final", "cf", "minor"],
        "promocion": ["promo", "oferta", "descuento", "dcto", "dto", "especial", "sale", "liquidación", "outlet", "rebaja"]}
    mejor_match_unidad = None
    for patron, _ in unidades_patrones:
        match = re.search(patron, texto_linea_lower)
        if match:
            unidad_encontrada_raw = match.group(1)
            if mejor_match_unidad is None or len(unidad_encontrada_raw) > len(mejor_match_unidad): mejor_match_unidad = unidad_encontrada_raw
    if mejor_match_unidad:
        unidad = re.sub(r'\s+', ' ', mejor_match_unidad).strip()
        #texto_linea_lower = texto_linea_lower.replace(mejor_match_unidad, "", 1).strip() # Opcional quitarlo
    for tipo, keywords in tipos_precio_keywords.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw.replace("?", "\\w?")) + r'\b', texto_linea_lower): tipo_precio = tipo; break
        if tipo_precio: break
    return unidad, tipo_precio
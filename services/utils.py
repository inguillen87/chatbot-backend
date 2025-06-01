# services/utils.py
import re
import logging

logger = logging.getLogger(__name__)

def limpiar_texto_base(texto: str | None) -> str:
    """Limpia espacios extra y convierte a minúsculas. Devuelve string vacío si el input es None."""
    if texto is None:
        return ""
    if not isinstance(texto, str):
        texto = str(texto) # Intentar convertir a string si no lo es
        
    texto_limpio = texto.lower() # Convertir a minúsculas
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio) # Reemplazar múltiples espacios/saltos de línea con uno solo
    return texto_limpio.strip()

def parse_precio_flexible(texto_precio_input: str | float | int | None) -> tuple[str | None, float | None, str | None]:
    """
    Intenta extraer y normalizar un precio y su moneda de un string o valor numérico.
    Devuelve (precio_str_numerico_limpio, precio_float, moneda_detectada).
    Ej: "$ 1.250,50 ARS" -> ("1250.50", 1250.50, "ARS")
        "1250.50 USD" -> ("1250.50", 1250.50, "USD")
        1250.50 (float) -> ("1250.50", 1250.50, "ARS") (Asumiendo ARS por defecto si es número)
    """
    if texto_precio_input is None:
        return None, None, None
    
    texto_precio_str = str(texto_precio_input).strip()
    if not texto_precio_str:
        return None, None, None

    moneda_detectada = "ARS" # Default
    numero_para_procesar = texto_precio_str

    # Detectar y extraer moneda (palabras y símbolos comunes)
    patron_moneda = re.compile(r"(ARS|USD|\$|U\$S)\s*", re.IGNORECASE)
    match_moneda_simbolo = patron_moneda.search(numero_para_procesar)
    
    if match_moneda_simbolo:
        simbolo_encontrado = match_moneda_simbolo.group(1).upper()
        if "USD" in simbolo_encontrado or "U$S" in simbolo_encontrado:
            moneda_detectada = "USD"
        elif "ARS" in simbolo_encontrado:
            moneda_detectada = "ARS"
        # Quitar todos los símbolos de moneda y espacios extra alrededor
        numero_para_procesar = patron_moneda.sub("", numero_para_procesar).strip()

    # Si después de quitar moneda no queda nada, no es un precio válido
    if not numero_para_procesar:
        logger.debug(f"parse_precio_flexible: No quedó parte numérica de '{texto_precio_input}' tras quitar moneda/símbolos.")
        return None, None, None

    # Normalizar separadores numéricos
    # Esta lógica asume que si ambos '.' y ',' existen, el último es el decimal.
    # Si solo hay ',', se asume como decimal. Si solo hay '.', se asume como decimal.
    numero_normalizado = numero_para_procesar
    if ',' in numero_normalizado and '.' in numero_normalizado:
        if numero_normalizado.rfind(',') > numero_normalizado.rfind('.'): # Coma es decimal (ej. 1.234,56)
            numero_normalizado = numero_normalizado.replace('.', '')  # Quitar separador de miles
            numero_normalizado = numero_normalizado.replace(',', '.')  # Convertir coma decimal a punto
        else:  # Punto es decimal (ej. 1,234.56)
            numero_normalizado = numero_normalizado.replace(',', '')  # Quitar separador de miles
    elif ',' in numero_normalizado:  # Solo comas, asumir que la última es decimal
        numero_normalizado = numero_normalizado.replace(',', '.') 
        # Si hay múltiples comas, esto podría ser un problema. Ej. "1,234,56" -> "1.234.56" (inválido)
        # Una lógica más robusta podría quitar todas las comas excepto la última si es seguida por 1 o 2 dígitos.
        # Ejemplo simplificado: si la última coma es decimal:
        if numero_normalizado.count('.') > 1: # Si ahora hay múltiples puntos por el replace de comas
            partes = numero_normalizado.split('.')
            numero_normalizado = "".join(partes[:-1]) + "." + partes[-1]


    # Quitar cualquier caracter no numérico excepto el punto decimal
    numero_final_para_float_str = re.sub(r"[^0-9.]", "", numero_normalizado)

    try:
        precio_flt = float(numero_final_para_float_str)
        # Devolver el string numérico original (sin símbolos de moneda pero con formato) 
        # y el float. Si numero_para_procesar es el original sin moneda, usarlo.
        precio_str_display = numero_para_procesar # El número tal como quedó después de quitar moneda
        return precio_str_display, precio_flt, moneda_detectada
    except ValueError:
        logger.warning(f"parse_precio_flexible: No se pudo convertir a float: '{numero_final_para_float_str}' (procesado de '{texto_precio_input}')")
        # Si no se puede convertir, pero el original (sin moneda) parece un número, devolver el string
        if re.fullmatch(r"[\d.,]+", numero_para_procesar.strip()):
             return numero_para_procesar.strip(), None, moneda_detectada
        return None, None, moneda_detectada # Si no, no era un precio


def extraer_unidades_y_tipos_precio(texto_linea: str, pyme_rubro_nombre: str = "generico") -> tuple[str | None, str | None]:
    """
    Intenta extraer unidades (ej. "caja x 6", "750ml") o tipos de precio (ej. "por mayor") de una línea.
    Devuelve: (unidad_extraida, tipo_precio_extraido)
    """
    # Tu lógica se mantiene, pero asegúrate de que las regex sean robustas
    # y que limpiar_texto_base se aplique consistentemente si es necesario.
    # (El código que pasaste para esta función ya estaba bastante bien)
    if not texto_linea: return None, None
    texto_linea_lower = limpiar_texto_base(texto_linea) # Usar la versión mejorada de limpiar_texto_base
    unidad = None
    tipo_precio = None

    # Patrones ordenados de más específico a más genérico
    unidades_patrones = [
        (r"\b(caja(?:s)?\s*x\s*\d{1,3})\b", "caja_especifica"), # ej. caja x 6, caja x 12
        (r"\b(pack\s*x\s*\d{1,3})\b", "pack_especifico"),   # ej. pack x 24
        (r"\b(\d{1,4}\s*ml)\b", "volumen_ml"),           # ej. 750ml, 1000 ml
        (r"\b(\d{1,2}(?:[.,]\d{1,2})?\s*lts?)\b", "volumen_lts"), # ej. 1 lts, 1.5 lt, 2,5lts
        (r"\b(\d{1,3}(?:[.,]\d{1,3})?\s*kilos?g?)\b", "peso_kg"),# ej. 1kg, 2.5kilos
        (r"\b(\d{1,4}\s*gr(?:s)?|gramo(?:s)?)\b", "peso_gr"),# ej. 250grs, 100 gramos
        (r"\b(docena(?:s)?)\b", "docena"),
        (r"\b(par(?:es)?)\b", "par"),
        (r"\b(blister(?:es)?\s*x\s*\d{1,2})\b", "blister_especifico"),
        (r"\b(caja|box)\b", "caja_generica"), # Caja o Box genérico
        (r"\b(botella|bottle)\b", "botella_generica"),
        (r"\b(pack)\b", "pack_generico"),
        (r"\b(blister)\b", "blister_generico"),
        (r"\b(unidad|unid\.?|un\.?|u\.?)\b", "unidad_generica")
    ]
    
    tipos_precio_keywords = {
        "mayorista": ["mayorista", "por mayor", "distribuidor", "distr?"], # dist? para dist. o distr
        "minorista": ["minorista", "al detalle", "público", "publico", "consumidor final", "cf"],
        "promocion": ["promo", "oferta", "descuento", "dcto", "dto", "especial", "sale", "liquidación", "outlet"]
    }

    # Extraer unidades: buscar la coincidencia más larga y específica primero
    # Esta lógica simple toma la primera que encuentra según el orden de la lista.
    for patron, _ in unidades_patrones:
        match = re.search(patron, texto_linea_lower)
        if match:
            unidad_encontrada_raw = match.group(1)
            # Limpiar un poco la unidad encontrada
            unidad = re.sub(r'\s+', ' ', unidad_encontrada_raw).strip()
            # Remover la unidad del texto para evitar que se confunda con el nombre
            texto_linea_lower = texto_linea_lower.replace(unidad_encontrada_raw, "", 1).strip() 
            break # Tomar la primera (y más específica por orden) unidad encontrada

    # Extraer tipos de precio
    for tipo, keywords in tipos_precio_keywords.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', texto_linea_lower): # Usar word boundaries
                tipo_precio = tipo 
                # Opcional: remover keyword del texto
                # texto_linea_lower = re.sub(r'\b' + re.escape(kw) + r'\b', '', texto_linea_lower).strip()
                break
        if tipo_precio:
            break
            
    return unidad, tipo_precio
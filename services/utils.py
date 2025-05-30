# services/utils.py

import re
import logging
# import pandas as pd # Solo si 'pd.isna' se usa aquí; si no, no es necesario.
                      # La función parse_precio_flexible_excel_input lo maneja sin pd.

def limpiar_texto_base(texto: str) -> str:
    """Limpia espacios extra y caracteres problemáticos comunes."""
    if not texto: 
        return ""
    # Eliminar múltiples espacios, tabulaciones, y saltos de línea residuales
    texto_limpio = re.sub(r'\s+', ' ', texto).strip()
    return texto_limpio

def parse_precio_flexible(texto_precio_input: str | float | None) -> tuple[str | None, float | None, str | None]:
    """
    Intenta extraer y normalizar un precio y su moneda de un string o valor numérico.
    Devuelve (precio_str_original_del_numero, precio_float, moneda_detectada).
    Ej: "$ 1.250,50 ARS" -> ("1.250,50", 1250.50, "ARS")
        "1250.50 USD" -> ("1250.50", 1250.50, "USD")
        1250.50 (float) -> ("1250.50", 1250.50, "ARS")
    """
    if texto_precio_input is None or str(texto_precio_input).strip() == "":
        return None, None, None

    texto_precio_str = str(texto_precio_input) # Convertir a string si es float/int
    texto_limpio_original = limpiar_texto_base(texto_precio_str)
    moneda = "ARS"  # Default
    numero_final_para_float = texto_limpio_original # El string que intentaremos convertir a float

    # Detectar y quitar símbolos/palabras de moneda, actualizando 'moneda'
    # Esta regex busca símbolos al principio o al final, y también palabras como USD/ARS
    # Es importante quitar los símbolos ANTES de intentar normalizar los separadores numéricos.
    
    # Guardar el texto original para ver si se modifica por la extracción de moneda
    texto_antes_extraccion_moneda = numero_final_para_float
    
    if re.search(r'(?i)\bUSD\b', numero_final_para_float):
        moneda = "USD"
        numero_final_para_float = re.sub(r'(?i)\bUSD\b', '', numero_final_para_float).strip()
    elif re.search(r'(?i)\bARS\b', numero_final_para_float):
        moneda = "ARS" # Ya es default, pero por claridad
        numero_final_para_float = re.sub(r'(?i)\bARS\b', '', numero_final_para_float).strip()
    
    # Quitar símbolo $ si está presente, después de las palabras de moneda
    numero_final_para_float = numero_final_para_float.replace("$", "").strip()
    
    # Si después de quitar moneda y $ no queda nada o solo espacios, no era un precio válido
    if not numero_final_para_float:
        logging.debug(f"parse_precio_flexible: No quedó parte numérica de '{texto_precio_input}' tras quitar moneda/símbolos.")
        return None, None, None # O devolver el texto original limpio si se prefiere no perderlo

    # Normalizar separadores numéricos del 'numero_final_para_float'
    # Esta lógica intenta ser robusta para formatos como "1.234,56" (ARS) y "1,234.56" (USD style)
    # y también "1234,56" o "1234.56"
    
    numero_a_convertir = numero_final_para_float
    if ',' in numero_a_convertir and '.' in numero_a_convertir:
        if numero_a_convertir.rfind(',') > numero_a_convertir.rfind('.'): # Coma es decimal (ej. 1.234,56)
            numero_a_convertir = numero_a_convertir.replace('.', '')  # Quitar separador de miles
            numero_a_convertir = numero_a_convertir.replace(',', '.')  # Convertir coma decimal a punto
        else:  # Punto es decimal (ej. 1,234.56)
            numero_a_convertir = numero_a_convertir.replace(',', '')  # Quitar separador de miles
    elif ',' in numero_a_convertir:  # Solo comas, asumir que la última es decimal (ej. 1234,56 o 1,234,56)
        partes_coma = numero_a_convertir.split(',')
        if len(partes_coma[-1]) == 2 and all(c.isdigit() for c in partes_coma[-1]): # Si lo después de la última coma son 2 dígitos
            numero_a_convertir = "".join(partes_coma[:-1]) + "." + partes_coma[-1] # Unir y poner punto decimal
        else: # Si no parece tener decimales con coma, tratar comas como miles o ruido
            numero_a_convertir = numero_a_convertir.replace(',', '')

    # A este punto, numero_a_convertir debería usar '.' como separador decimal (si tiene)
    # y no tener separadores de miles.
    
    try:
        precio_flt = float(numero_a_convertir)
        # Devolver el 'numero_final_para_float' (que es el número sin símbolos de moneda pero con su formato original de puntos/comas)
        # y el 'precio_flt' (el número puro para cálculos)
        return limpiar_texto_base(numero_final_para_float), precio_flt, moneda
    except ValueError:
        logging.warning(f"[UTILS] parse_precio_flexible: No se pudo convertir a float: '{numero_a_convertir}' (procesado de '{texto_precio_input}')")
        # Si no se puede convertir a float, pero parece un número, devolvemos el string y None para float
        if re.fullmatch(r"[\d.,]+", numero_final_para_float): # Rechequear si el string original (sin símbolos) es numérico
             return limpiar_texto_base(numero_final_para_float), None, moneda
        return None, None, moneda # Si no, no era un precio

def extraer_unidades_y_tipos_precio(texto_linea: str, pyme_rubro_nombre: str = "generico") -> tuple[str | None, str | None]:
    """
    Intenta extraer unidades (ej. "caja x 6", "750ml") o tipos de precio (ej. "por mayor") de una línea.
    Devuelve: (unidad_extraida, tipo_precio_extraido)
    """
    if not texto_linea: return None, None
    texto_linea_lower = texto_linea.lower()
    unidad = None
    tipo_precio = None

    unidades_patrones = [
        (r"\b(caja(?:s)?\s*x\s*\d{1,2})\b", "caja_especifica"),
        (r"\b(pack\s*x\s*\d{1,2})\b", "pack_especifico"),
        (r"\b(\d{3,4}\s*ml)\b", "volumen_ml"),
        (r"\b(\d{1,2}\s*lts?)\b", "volumen_lts"),
        (r"\b(docena(?:s)?)\b", "docena"),
        (r"\b(par(?:es)?)\b", "par"),
        (r"\b(kilo(?:s)?|kg)\b", "kilo"),
        (r"\b(gr(?:s)?|gramo(?:s)?)\b", "gramo"),
        (r"\b(caja)\b", "caja_generica"),
        (r"\b(botella)\b", "botella_generica"),
        (r"\b(pack)\b", "pack_generico"),
        (r"\b(unidad|unid\.?|un\.?|u\.?)\b", "unidad") # 'un.' y 'u.' añadidos
    ]
    
    tipos_precio_keywords = {
        "mayorista": ["mayorista", "por mayor", "distribuidor", "dist."],
        "minorista": ["minorista", "al detalle", "público", "consumidor final", "c.f."],
        "promocion": ["promo", "oferta", "descuento", "especial", "sale", "liquidación"]
    }

    # Extraer unidades
    for patron, _ in unidades_patrones:
        match = re.search(patron, texto_linea_lower)
        if match:
            unidad_encontrada = limpiar_texto_base(match.group(1))
            # Heurística: preferir la unidad más larga o específica si hay múltiples matches simples.
            # Esta lógica es simple, podría mejorarse.
            if unidad is None or len(unidad_encontrada) > len(unidad):
                unidad = unidad_encontrada
            # No hacer break aquí para permitir que un patrón más específico posterior reemplace uno más genérico
            # Ejemplo: "caja x6" es mejor que solo "caja". (Se logra ordenando unidades_patrones)

    # Extraer tipos de precio
    for tipo, keywords in tipos_precio_keywords.items():
        if any(kw in texto_linea_lower for kw in keywords):
            tipo_precio = tipo # Tomar el primero que coincida
            break 
            
    return unidad, tipo_precio
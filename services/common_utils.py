# services/common_utils.py
import re
import pandas as pd
from typing import Dict, Any, Tuple, Optional, List

# --- PLACEHOLDER DEFINITIONS ---
# The original definitions for these functions were not found in the codebase.
# These are basic placeholders to allow the application to load.
# The user MUST review and provide the original or correct implementations.

logger = None # Needs proper logger setup if used within these utils

def get_logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger(__name__)
    return logger

def limpiar_texto_base(texto: str) -> str:
    """
    PLACEHOLDER: Basic text cleaning.
    Original implementation needs to be restored.
    """
    if not isinstance(texto, str):
        return ""
    texto_limpio = texto.lower().strip()
    # Add more basic cleaning if necessary, e.g., remove multiple spaces
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio)
    return texto_limpio

def parse_precio_flexible(precio_str: str) -> Tuple[str, Optional[float], Optional[str]]:
    """
    PLACEHOLDER: Basic price parsing.
    Original implementation needs to be restored.
    """
    get_logger().warning(f"Using PLACEHOLDER parse_precio_flexible for: {precio_str}")
    if not isinstance(precio_str, str):
        return "", None, None

    cleaned_price_str = re.sub(r'[^\d,.]', '', precio_str) # Keep digits, comma, dot

    # Try to convert to float
    # Handle cases like "1.234,56" (German) and "1,234.56" (US)
    price_float = None
    moneda = "ARS" # Default

    if not cleaned_price_str:
        return "", None, None

    try:
        # Attempt 1: "1.234,56" -> "1234.56"
        temp_str = cleaned_price_str.replace('.', '').replace(',', '.')
        price_float = float(temp_str)
    except ValueError:
        try:
            # Attempt 2: "1,234.56" -> "1234.56"
            temp_str = cleaned_price_str.replace(',', '')
            price_float = float(temp_str)
        except ValueError:
            get_logger().error(f"Could not parse price string: {precio_str} (cleaned: {cleaned_price_str})")
            return precio_str, None, None # Return original string if parsing fails

    # Basic currency symbol detection (example)
    if '$' in precio_str:
        moneda = "USD" # Or ARS if $ is used for pesos
    elif '€' in precio_str:
        moneda = "EUR"

    return cleaned_price_str, price_float, moneda

def crear_mapa_de_columnas_inteligente(df: pd.DataFrame) -> Optional[Tuple[Dict[str, Any], int]]:
    """
    PLACEHOLDER: Intelligent column mapping.
    Original implementation needs to be restored.
    This is a complex function and likely requires domain-specific logic.
    """
    get_logger().warning("Using PLACEHOLDER crear_mapa_de_columnas_inteligente. This will likely not work correctly.")
    if df.empty:
        return None

    # Extremely naive placeholder: assumes first row is header, maps known keywords
    # This WILL NOT be robust.
    headers = [str(h).lower().strip() for h in df.iloc[0].tolist()]
    mapa = {}
    possible_nombre = ['nombre', 'producto', 'descripción', 'item']
    possible_precio = ['precio', 'valor', 'costo']

    for i, header in enumerate(headers):
        if any(pn in header for pn in possible_nombre) and 'nombre' not in mapa:
            mapa['nombre'] = df.columns[i] # Use original column name/index from df
        elif any(pp in header for pp in possible_precio) and 'precio' not in mapa:
            mapa['precio'] = df.columns[i]

    if 'nombre' not in mapa: # Essential column
        get_logger().error("Placeholder crear_mapa_de_columnas_inteligente: Could not find a 'nombre' column.")
        return None

    return mapa, 1 # Assume data starts from row 1 (after header row 0)

KEYWORD_MAP: Dict[str, List[str]] = {
    # PLACEHOLDER: Basic keyword map. Original needs to be restored.
    "nombre": ["nombre", "producto", "item", "descripción", "designacion"],
    "precio": ["precio", "valor", "costo", "importe"],
    "descripcion": ["descripcion", "detalle", "observaciones"],
    "sku": ["sku", "código", "cod", "referencia", "ref"],
    "marca": ["marca", "fabricante"],
    "unidad": ["unidad", "presentacion", "empaque"],
    "stock": ["stock", "cantidad", "disponible", "existencias"],
    "categoria_producto": ["categoria", "rubro", "tipo", "familia"],
    "talles": ["talle", "talles", "tamaño", "medida"],
    "colores": ["color", "colores"],
    "promocion_texto": ["promocion", "oferta", "descuento"]
}

def parse_unidad_y_cantidad_empaque(unidad_str: str) -> Tuple[str, Optional[int]]:
    """
    PLACEHOLDER: Parses unit string to extract description and pack quantity.
    Original implementation needs to be restored.
    Example: "Caja x 6 botellas" -> ("Caja botellas", 6)
    """
    get_logger().warning(f"Using PLACEHOLDER parse_unidad_y_cantidad_empaque for: {unidad_str}")
    if not isinstance(unidad_str, str):
        return "", None

    unidad_desc = unidad_str
    cantidad_empaque = None

    # Naive attempt to find "x NUMERO" or "NUMERO unidades"
    match_x_num = re.search(r'[xX]\s*(\d+)', unidad_str)
    if match_x_num:
        try:
            cantidad_empaque = int(match_x_num.group(1))
            # Try to remove the quantity part from description for a cleaner desc
            unidad_desc = re.sub(r'[xX]\s*\d+\s*', '', unidad_str, flags=re.IGNORECASE).strip()
            unidad_desc = re.sub(r'\s+', ' ', unidad_desc) # Clean up multiple spaces
        except ValueError:
            pass # Should not happen with \d+
    else:
        match_num_unidad = re.search(r'(\d+)\s*\w+', unidad_str) # e.g. "6 unidades"
        if match_num_unidad:
            try:
                # This is more ambiguous, could be "Pack 6" or "6 items"
                # For now, let's assume if a number is at the start of a word, it might be quantity
                # This needs much better logic from original.
                # cantidad_empaque = int(match_num_unidad.group(1))
                # unidad_desc = re.sub(r'\d+\s*', '', unidad_str, count=1).strip() # Remove first number
                pass # Decided this is too ambiguous for a placeholder
            except ValueError:
                pass

    if not unidad_desc: # If stripping made it empty, revert to original
        unidad_desc = unidad_str

    return limpiar_texto_base(unidad_desc), cantidad_empaque

def unir_codigos_alfa_numericos(texto: str) -> str:
    """
    PLACEHOLDER: Unites alphanumeric codes by removing spaces between them.
    Example: "de 108 c" -> "de108c"
    Original implementation needs to be restored.
    """
    get_logger().warning(f"Using PLACEHOLDER unir_codigos_alfa_numericos for: {texto}")
    if not isinstance(texto, str):
        return ""
    # This is a guess based on the function name and test case.
    # It looks for a pattern of (letter/digit) + (space) + (letter/digit)
    # and removes the space. This might need to be more sophisticated.
    # A simpler approach for "de 108 c" -> "de108c" might be specific to space between alphanumerics.

    # Simpler regex based on example: remove spaces between sequences of alphanumeric characters
    # This regex finds parts like "word1 word2" or "word 123" or "123 word"
    # and replaces the space. It will do it iteratively.
    # For "de 108 c", it would be:
    # 1. "de108 c"
    # 2. "de108c"

    # More robustly, remove all spaces if the string seems like a code.
    # For now, a simple specific case for the test:
    # Find sequences of (alphanum) (space) (alphanum) and remove the space.
    # This needs to be done carefully to not merge "word1 word2" into "word1word2" everywhere.

    # Based on the test 'de 108 c' -> 'de108c'.
    # This suggests removing spaces when they are between alphanumeric characters.
    # A simple way: find all alphanumeric parts, then join them.
    # Or, more carefully, identify segments that look like codes.

    # Iteratively remove spaces between an alphanumeric char and another alphanumeric char.
    # Example: "abc 123 def" -> "abc123def"
    # Example: "ab cde fg 12" -> "abcdefg12"
    # This specific regex looks for an alphanumeric, a space, and an alphanumeric,
    # and replaces it with the two alphanumerics. It might need multiple passes or a loop.

    # A common pattern for this is to join parts that are alphanumeric.
    # Let's try a regex that finds alphanumeric parts and then joins them if they were separated by single spaces.
    # This is still a guess. The original logic is needed.

    # Simpler approach for placeholder: join all alphanumeric segments.
    # This might be too aggressive for general text.
    # parts = re.findall(r'[a-zA-Z0-9]+', texto)
    # return "".join(parts)

    # Let's try to be a bit more conservative and only remove spaces between what looks like code parts.
    # The example "de 108 c" -> "de108c" is key.
    # Replace a space if it's surrounded by alphanumeric characters (or is at an edge next to one).
    # This is tricky. For a placeholder, let's stick to something simple related to the test.

    # This regex finds an alphanumeric character, followed by a space, followed by an alphanumeric character.
    # It replaces this with the two alphanumeric characters, removing the space.
    # It will take multiple passes for something like "a b c".
    # A loop could do this:
    new_texto = texto
    while True:
        # Remove space between a letter/digit and another letter/digit
        intermediate_texto = re.sub(r'([a-zA-Z0-9])\s([a-zA-Z0-9])', r'\1\2', new_texto)
        if intermediate_texto == new_texto: # No more changes made
            break
        new_texto = intermediate_texto
    return new_texto

def validar_email(email: str) -> bool:
    """
    PLACEHOLDER: Validates an email address.
    Original implementation needs to be restored.
    """
    get_logger().warning(f"Using PLACEHOLDER validar_email for: {email}")
    if not isinstance(email, str):
        return False
    # Basic regex for email validation
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))

def validar_telefono(telefono: str) -> bool:
    """
    PLACEHOLDER: Validates a phone number.
    Original implementation needs to be restored.
    Assumes phone number is a string of digits, possibly with spaces or hyphens.
    """
    get_logger().warning(f"Using PLACEHOLDER validar_telefono for: {telefono}")
    if not isinstance(telefono, str):
        return False
    # Remove common formatting and check if it's all digits and reasonable length
    cleaned_telefono = re.sub(r"[^0-9]", "", telefono)
    return 7 <= len(cleaned_telefono) <= 15 # Common international min/max

def formatear_telefono_e164(telefono: str, codigo_pais_defecto: str = "54") -> Optional[str]:
    """
    PLACEHOLDER: Formats a phone number to E.164 format (e.g., +5492611234567).
    Original implementation needs to be restored.
    """
    get_logger().warning(f"Using PLACEHOLDER formatear_telefono_e164 for: {telefono}")
    if not isinstance(telefono, str):
        return None

    cleaned_telefono = re.sub(r"[^0-9]", "", telefono)

    if not cleaned_telefono:
        return None

    # Simplistic logic:
    # If it already starts with '+', assume it's E.164 or close enough for placeholder.
    if telefono.startswith("+"):
        return telefono

    # Remove leading '0' if it's like "0261..." or "011..." for Argentina common local dialing
    if cleaned_telefono.startswith("0") and len(cleaned_telefono) > 5: # Avoid stripping "0" from short codes
        cleaned_telefono = cleaned_telefono[1:]

    # If it's a mobile number in Argentina (commonly 10 digits after area code, e.g., 261xxxxxxx),
    # add '9' after country code. This is very specific to AR and a guess.
    # A real implementation would use a library like phonenumbers.
    if codigo_pais_defecto == "54" and len(cleaned_telefono) == 10: # e.g. 2615551234
        # Check if it's a known mobile prefix structure (highly simplified)
        # For AR, mobile often implies adding a '9' after country code for international format.
        # This is a common pattern but not universally true for all AR numbers.
        # Example: Mendoza mobile 261 + 15 + XXXXXX -> local 261 15XXXXXX
        # For E.164: +54 9 261 XXXXXX (if the '15' was dropped)
        # Or if input is 261XXXXXX (assuming '15' was never there or already removed)
        # This placeholder is too naive for robust AR phone formatting.
        # A proper library (like phonenumbers) is essential.
        # For now, if it's 10 digits and AR, we'll prefix with +549.
        # This is a common case for WhatsApp E.164.
        return f"+{codigo_pais_defecto}9{cleaned_telefono}"
    elif codigo_pais_defecto == "54" and len(cleaned_telefono) > 10 and cleaned_telefono.startswith("9"): # e.g. 9261...
         return f"+{codigo_pais_defecto}{cleaned_telefono}"


    # General case: just prepend country code
    return f"+{codigo_pais_defecto}{cleaned_telefono}"

# Helper function to check if a string can be converted to a number
def is_number(s: Any) -> bool:
    if s is None: return False
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False

# Example usage (can be removed)
if __name__ == '__main__':
    print(f"KEYWORD_MAP: {KEYWORD_MAP}")
    print(limpiar_texto_base("  Esto ES un Ejemplo  "))
    print(parse_precio_flexible(" $ 1.250,50.- "))
    print(parse_precio_flexible(" €2,345.99 "))
    print(parse_precio_flexible("1200.75"))
    print(parse_precio_flexible("No es un precio"))

    df_test_data = {
        'PRODUCTO': ['Manzanas', 'Bananas'],
        'PRECIO': ['100', '50'],
        'DETALLE EXTRA': ['Rojas', 'De Ecuador']
    }
    df_test = pd.DataFrame(df_test_data)
    print(f"Mapa Columnas Test: {crear_mapa_de_columnas_inteligente(df_test)}")

    print(parse_unidad_y_cantidad_empaque("Caja x 12 unidades"))
    print(parse_unidad_y_cantidad_empaque("Pack de 6 latas"))
    print(parse_unidad_y_cantidad_empaque("Botella 750ml"))
    print(parse_unidad_y_cantidad_empaque("Bolsa"))

    # Add logger setup for __main__
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.info("Common utils placeholder script executed.")

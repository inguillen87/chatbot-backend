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

# services/common_utils.py
import re
import unicodedata
import pandas as pd
from typing import Dict, Any, Tuple, Optional, List
from config.feature_flags import FEATURE_ENCUESTAS
from .constants import ConversationState, CONTEXTO_MUNICIPIO

# --- PLACEHOLDER DEFINITIONS ---
# The original definitions for these functions were not found in the codebase.
# These are basic placeholders to allow the application to load.
# The user MUST review and provide the original or correct implementations.

logger = None # Needs proper logger setup if used within these utils

# Tokens that correspond to labels for contact fields. Used to avoid treating
# leftover words such as "nombre" or "dni" as an address when parsing a
# free-form contact message.
CONTACT_LABEL_TOKENS = {
    "nombre",
    "nombrecompleto",
    "completo",
    "apellido",
    "dni",
    "documento",
    "doc",
    "documentonacionaldeidentidad",
    "email",
    "correo",
    "correoelectronico",
    "mail",
    "telefono",
    "tel",
    "celular",
    "cel",
    "whatsapp",
    "contacto",
}


def _normalize_contact_token(value: str) -> str:
    """Return a simplified token for matching contact-field labels."""

    if not value:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())

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

    # Convertir a minúsculas
    texto_limpio = texto.lower()

    # Quitar acentos (opcional, pero bueno para la coincidencia)
    # Necesita unidecode: from unidecode import unidecode
    # texto_limpio = unidecode(texto_limpio)

    # Reemplazar caracteres no alfanuméricos (excepto espacios) con nada, o con un espacio
    # Esto ayuda a normalizar cosas como "Precio-Venta" o "Precio_Venta" a "precio venta"
    # Mantendremos algunos caracteres si son parte de palabras comunes o unidades.
    # Por ahora, un enfoque más simple: reemplazar guiones y underscores con espacios.
    texto_limpio = texto_limpio.replace('-', ' ').replace('_', ' ')

    # Eliminar caracteres especiales que no suelen ser parte de encabezados útiles,
    # excepto puntos si son parte de abreviaturas (ej. desc.) o números.
    # Esta regex mantiene letras, números, espacios y puntos.
    # texto_limpio = re.sub(r'[^a-z0-9\s\.]', '', texto_limpio)


    # Eliminar múltiples espacios y espacios al inicio/final
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio).strip()

    return texto_limpio

def parse_precio_flexible(precio_str: str) -> Tuple[str, Optional[float], Optional[str]]:
    """
    Interpret price strings that may use different thousand/decimal separators
    and currency hints.

    Returns a tuple of (precio_normalizado, precio_float, moneda_detectada).
    * ``precio_normalizado`` is a canonical numeric string if it can be parsed,
      otherwise the original cleaned input.
    * ``precio_float`` is ``None`` when parsing fails.
    * ``moneda_detectada`` is a best-effort guess (``None`` if no hint exists).
    """

    if precio_str is None:
        return "", None, None

    if not isinstance(precio_str, str):
        precio_str = str(precio_str)

    texto_original = precio_str.strip()
    if not texto_original:
        return "", None, None

    texto_lower = texto_original.lower()

    moneda_detectada = None
    if any(token in texto_lower for token in ["usd", "u$s", "us$", "dolar", "dólar"]):
        moneda_detectada = "USD"
    elif "€" in texto_original or "eur" in texto_lower:
        moneda_detectada = "EUR"
    elif any(token in texto_lower for token in ["ars", "peso", "pesos", "$ar"]):
        moneda_detectada = "ARS"
    elif "$" in texto_original:
        # Asumimos pesos argentinos cuando solo hay símbolo "$" sin otras pistas.
        moneda_detectada = "ARS"

    # Conservar dígitos, comas, puntos y signo menos para extraer el número.
    texto_numerico = re.sub(r"[^0-9,\.-]", "", texto_original.replace(" ", ""))
    if "-" in texto_numerico:
        texto_numerico = ("-" if texto_numerico.startswith("-") else "") + texto_numerico.replace("-", "")
    texto_numerico = texto_numerico.strip(".,")
    if not texto_numerico:
        return texto_original, None, moneda_detectada

    logger = get_logger()

    # Determinar separador decimal usando heurísticas basadas en la última
    # aparición de punto/coma y la longitud del tramo final.
    decimal_sep = None
    last_dot = texto_numerico.rfind(".")
    last_comma = texto_numerico.rfind(",")

    if last_dot != -1 and last_comma != -1:
        decimal_sep = "." if last_dot > last_comma else ","
    elif texto_numerico.count(",") == 1 and len(texto_numerico.split(",")[-1]) <= 2:
        decimal_sep = ","
    elif texto_numerico.count(".") == 1 and len(texto_numerico.split(".")[-1]) <= 2:
        decimal_sep = "."

    if decimal_sep == ",":
        numero_normalizado = texto_numerico.replace(".", "").replace(",", ".")
    elif decimal_sep == ".":
        numero_normalizado = texto_numerico.replace(",", "")
    else:
        numero_normalizado = texto_numerico.replace(",", "").replace(".", "")

    try:
        precio_float = float(numero_normalizado)
    except ValueError:
        logger.warning(
            "No se pudo parsear el precio de forma flexible: '%s' (procesado como '%s')",
            texto_original,
            texto_numerico,
        )
        return texto_original, None, moneda_detectada

    if precio_float.is_integer():
        precio_normalizado = str(int(precio_float))
    else:
        precio_normalizado = f"{precio_float:.2f}".rstrip("0").rstrip(".")

    return precio_normalizado, precio_float, moneda_detectada

def crear_mapa_de_columnas_inteligente(df: pd.DataFrame, umbral_similitud: float = 0.8) -> Optional[Tuple[Dict[str, Any], int]]:
    """
    PLACEHOLDER: Intelligent column mapping.
    Original implementation needs to be restored.
    This is a complex function and likely requires domain-specific logic.
    """
    # Import Levenshtein aquí para mantenerlo contenido si esta función evoluciona mucho
    # o para facilitar el manejo de su ausencia si no se puede instalar.
    try:
        import Levenshtein
    except ImportError:
        get_logger().error("La biblioteca 'python-Levenshtein' no está instalada. El mapeo inteligente de columnas no funcionará. Por favor, instálala (pip install python-Levenshtein).")
        # Fallback a una función no inteligente o error. Por ahora, error.
        raise ImportError("python-Levenshtein no está instalado, es necesario para crear_mapa_de_columnas_inteligente.")

    logger = get_logger() # Asegurar que el logger esté inicializado

    def calcular_similitud_levenshtein(s1: str, s2: str) -> float:
        """Calcula la similitud normalizada basada en la distancia de Levenshtein."""
        if not s1 and not s2:
            return 1.0
        if not s1 or not s2:
            return 0.0
        distancia = Levenshtein.distance(s1, s2)
        longitud_max = max(len(s1), len(s2))
        if longitud_max == 0:
            return 1.0
        similitud = 1 - (distancia / longitud_max)
        return similitud

    logger.info("Iniciando mapeo inteligente de columnas...")
    if df.empty:
        logger.warning("DataFrame vacío, no se puede mapear.")
        return None

    # Para esta implementación, asumimos que la primera fila contiene los encabezados.
    # La lógica de detección de encabezados o el uso de parámetros del usuario (ej. fila_encabezado)
    # se puede añadir en el futuro.
    nombres_columnas_usuario_original = [str(col) for col in df.columns]
    fila_inicio_datos = 0 # Si df.columns son los encabezados, los datos empiezan en la fila 0 del df de datos.
                         # Sin embargo, si la primera fila del *archivo* era el encabezado, y el df se leyó
                         # con header=0 (default de pandas), entonces los datos empiezan en la fila 1 del archivo original.
                         # Esto depende de cómo se haya leído el df ANTES de llamar a esta función.
                         # Por ahora, asumimos que el df que llega aquí ya tiene los encabezados como df.columns
                         # y los datos comienzan desde la primera fila del df (índice 0).
                         # Los procesadores de Excel/DocAI deben asegurar esto.
                         # Si el df fue leído con header=None y la primera fila es el encabezado,
                         # entonces nombres_columnas_usuario_original debería ser df.iloc[0] y fila_inicio_datos = 1.
                         # ---
                         # Revisión: Los procesadores (excel, docai) leen el df con header=None
                         # y luego esta función es llamada. El `crear_mapa_de_columnas_inteligente`
                         # original (placeholder) usaba df.iloc[0].tolist() y devolvía fila_inicio_datos = 1.
                         # Vamos a seguir ese patrón para consistencia con el flujo actual.

    # --- Inicio de la Detección Mejorada de Fila de Encabezado ---
    mejor_fila_encabezado_idx = -1
    max_puntaje_encabezado = -1
    MAX_FILAS_A_CHEQUEAR_PARA_ENCABEZADO = min(5, len(df)) # No chequear más de 5 filas o el total de filas

    if MAX_FILAS_A_CHEQUEAR_PARA_ENCABEZADO == 0:
        logger.warning("DataFrame con 0 filas pasado a crear_mapa_de_columnas_inteligente después del chequeo de df.empty.")
        return None

    for i in range(MAX_FILAS_A_CHEQUEAR_PARA_ENCABEZADO):
        fila_actual_valores = [str(x) for x in df.iloc[i].tolist()]
        if all(not valor.strip() for valor in fila_actual_valores): # Si toda la fila está vacía (o solo espacios)
            logger.debug(f"Fila {i} está vacía, saltando para detección de encabezado.")
            continue

        fila_normalizada = [limpiar_texto_base(valor) for valor in fila_actual_valores]

        puntaje_fila_actual = 0
        celdas_mapeadas_en_fila = 0
        celdas_texto_en_fila = 0

        for celda_norm in fila_normalizada:
            if not celda_norm: continue # Saltar celdas vacías en la fila normalizada
            celdas_texto_en_fila +=1 # Contar celdas con texto
            for campo_std, sinonimos_std in KEYWORD_MAP.items():
                for sinonimo in sinonimos_std:
                    sim = calcular_similitud_levenshtein(celda_norm, limpiar_texto_base(sinonimo))
                    if sim >= umbral_similitud: # Usar el mismo umbral que para el mapeo final
                        puntaje_fila_actual += sim
                        if campo_std in ["nombre", "precio", "sku", "descripcion"]: # Dar más peso a campos clave
                            puntaje_fila_actual += 0.5
                        celdas_mapeadas_en_fila +=1
                        break # Celda mapeada a un campo estándar, no necesita chequear más sinónimos para esta celda
        
        # Ajustar puntaje por proporción de celdas de texto y celdas mapeadas
        if celdas_texto_en_fila > 0:
            puntaje_fila_actual = (puntaje_fila_actual / celdas_texto_en_fila) * (celdas_mapeadas_en_fila / len(fila_normalizada))
        else: # Fila sin texto
            puntaje_fila_actual = 0

        logger.debug(f"Fila {i} para encabezado: Valores: {fila_actual_valores}, Puntaje: {puntaje_fila_actual:.2f}, Celdas Mapeadas: {celdas_mapeadas_en_fila}, Celdas Texto: {celdas_texto_en_fila}")

        if puntaje_fila_actual > max_puntaje_encabezado:
            max_puntaje_encabezado = puntaje_fila_actual
            mejor_fila_encabezado_idx = i

    # Decidir si el mejor puntaje es suficientemente bueno
    UMBRAL_MINIMO_PUNTAJE_ENCABEZADO = 0.2 # Ajustable. Si es muy bajo, puede tomar filas de datos.
    if mejor_fila_encabezado_idx != -1 and max_puntaje_encabezado >= UMBRAL_MINIMO_PUNTAJE_ENCABEZADO:
        nombres_columnas_usuario_original = [str(x) for x in df.iloc[mejor_fila_encabezado_idx].tolist()]
        fila_inicio_datos = mejor_fila_encabezado_idx + 1
        logger.info(f"Fila de encabezado detectada en índice {mejor_fila_encabezado_idx} con puntaje {max_puntaje_encabezado:.2f}.")
    elif not df.empty and df.iloc[0].isnull().all() and len(df) > 1: # Si la primera está vacía y hay más filas
        logger.warning("Primera fila vacía, usando segunda fila como encabezado (fallback).")
        nombres_columnas_usuario_original = [str(x) for x in df.iloc[1].tolist()]
        fila_inicio_datos = 2
    elif not df.empty: # Fallback a la primera fila si la detección no fue clara pero hay datos
        logger.warning(f"Detección de encabezado no fue clara (puntaje max: {max_puntaje_encabezado:.2f}). Usando primera fila como encabezado (fallback).")
        nombres_columnas_usuario_original = [str(x) for x in df.iloc[0].tolist()]
        fila_inicio_datos = 1
    else: # DataFrame probablemente vacío o sin encabezados útiles
        logger.error("No se pudo determinar una fila de encabezado válida.")
        return None
    # --- Fin de la Detección Mejorada de Fila de Encabezado ---


    logger.info(f"Encabezados originales (de la fila detectada {mejor_fila_encabezado_idx if mejor_fila_encabezado_idx !=-1 else '0/1 por fallback'}): {nombres_columnas_usuario_original}")
    
    nombres_columnas_usuario_normalizados = [limpiar_texto_base(col_name) for col_name in nombres_columnas_usuario_original]
    logger.info(f"Encabezados normalizados para matching: {nombres_columnas_usuario_normalizados}")

    mapa_columnas: Dict[str, Any] = {}
    columnas_usuario_mapeadas_flags = [False] * len(nombres_columnas_usuario_normalizados)
    # umbral_similitud ya está definido como parámetro de la función

    for campo_estandar_backend, sinonimos_backend in KEYWORD_MAP.items():
        mejor_similitud_para_campo_actual = -1.0
        mejor_indice_col_usuario_para_campo_actual = -1
        sinonimos_backend_normalizados = [limpiar_texto_base(s) for s in sinonimos_backend]

        for idx_col_usuario, encabezado_usuario_norm in enumerate(nombres_columnas_usuario_normalizados):
            if columnas_usuario_mapeadas_flags[idx_col_usuario] or not encabezado_usuario_norm: # Si ya mapeada o vacía
                continue

            similitud_max_con_sinonimos = 0.0
            for sinonimo_norm in sinonimos_backend_normalizados:
                if not sinonimo_norm: continue
                sim = calcular_similitud_levenshtein(encabezado_usuario_norm, sinonimo_norm)
                if sim > similitud_max_con_sinonimos:
                    similitud_max_con_sinonimos = sim
            
            # Considerar solo si esta columna es la mejor para este campo_estandar_backend HASTA AHORA
            if similitud_max_con_sinonimos > mejor_similitud_para_campo_actual:
                mejor_similitud_para_campo_actual = similitud_max_con_sinonimos
                mejor_indice_col_usuario_para_campo_actual = idx_col_usuario
            # Si hay empate en similitud, podríamos tener una lógica para preferir la primera columna de usuario
            # o la que tenga un nombre de encabezado más corto/largo, etc. Por ahora, la primera que alcance la mejor similitud.

        # Una vez evaluadas todas las columnas de usuario para el campo_estandar_backend actual:
        if mejor_similitud_para_campo_actual >= umbral_similitud and mejor_indice_col_usuario_para_campo_actual != -1:
            # Verificar si esta columna de usuario (mejor_indice_col_usuario_para_campo_actual)
            # ya fue mapeada a OTRO campo_estandar_backend con MAYOR similitud.
            # Esto requiere una estrategia más global o multi-pasada.
            # Simplificación: si la columna no está mapeada AÚN, la tomamos.
            if not columnas_usuario_mapeadas_flags[mejor_indice_col_usuario_para_campo_actual]:
                columna_df_original_a_mapear = df.columns[mejor_indice_col_usuario_para_campo_actual]
                mapa_columnas[campo_estandar_backend] = columna_df_original_a_mapear
                columnas_usuario_mapeadas_flags[mejor_indice_col_usuario_para_campo_actual] = True
                logger.info(f"MAPEADO: Campo Backend '{campo_estandar_backend}' -> Columna Usuario Original '{nombres_columnas_usuario_original[mejor_indice_col_usuario_para_campo_actual]}' (DF Col: {columna_df_original_a_mapear}) con similitud {mejor_similitud_para_campo_actual:.2f}")
            else:
                # Esta columna ya fue asignada a otro campo estándar, probablemente con mejor score para ESE campo.
                logger.debug(f"Columna '{nombres_columnas_usuario_original[mejor_indice_col_usuario_para_campo_actual]}' ya mapeada. Campo '{campo_estandar_backend}' no pudo usarla aunque tuvo similitud {mejor_similitud_para_campo_actual:.2f}.")


    if 'nombre' not in mapa_columnas:
        logger.error("Error Crítico: El campo esencial 'nombre' no pudo ser mapeado.")
        return None

    logger.info(f"Mapeo final: {mapa_columnas}")
    logger.info(f"Los datos del archivo comenzarán en la fila del archivo original: {fila_inicio_datos} (considerando la primera fila como índice 0). El DataFrame de datos se tomará desde df.iloc[{fila_inicio_datos}:]")

    return mapa_columnas, fila_inicio_datos

KEYWORD_MAP: Dict[str, List[str]] = {
    "nombre": [
        "nombre", "producto", "item", "articulo", "descripción", "descripcion",
        "designacion", "titulo", "name", "product", "title", "nombre del producto"
    ],
    "precio": [
        "precio", "valor", "costo", "importe", "precio venta", "precio unitario",
        "price", "unit price", "cost"
    ],
    "descripcion": [
        "descripcion", "descripción", "detalle", "observaciones", "info adicional",
        "description", "details", "additional info", "long description", "full description"
    ],
    "descripcion_corta": [
        "descripcion corta", "desc. corta", "desc corta", "resumen", "breve descripcion",
        "short description", "summary", "short_description", "shortdescription"
    ],
    "sku": [
        "sku", "código", "codigo", "cod", "referencia", "ref", "item code", "product code",
        "codigo de barras", "barcode"
    ],
    "marca": [
        "marca", "fabricante", "brand", "manufacturer"
    ],
    "unidad": [ # Formato de venta, ej: "Caja x 6", "Pack de 3", "Botella 750ml", "Kg", "Unidad"
        "unidad", "presentacion", "empaque", "formato", "unit", "package", "presentation", "pack_size"
    ],
    "stock": [ # Cantidad disponible
        "stock", "cantidad", "disponible", "existencias", "disponibilidad",
        "quantity", "qty", "available", "in stock", "stock disponible"
    ],
    "categoria_producto": [
        "categoria", "rubro", "tipo", "familia", "linea", "category", "type", "group", "line"
    ],
    "talles": [ # Para indumentaria, calzado, etc.
        "talle", "talles", "tamaño", "medida", "size", "sizes"
    ],
    "colores": [ # Para productos con variantes de color
        "color", "colores", "colour", "colours"
    ],
    "promocion_texto": [ # Texto descriptivo de una promoción
        "promocion", "promo", "oferta", "descuento", "rebaja", "promotion", "offer", "discount", "sale"
    ],
    "imagen_url": [ # Si el catálogo incluye URLs de imágenes
        "imagen", "foto", "url imagen", "link imagen", "image", "picture", "image url", "img_url"
    ],
    # Campos adicionales que podrían ser útiles
    "peso": ["peso", "weight"],
    "dimensiones": ["dimensiones", "medidas", "largo", "ancho", "alto", "dimensions", "length", "width", "height"],
    "ean_upc": ["ean", "upc", "codigo ean", "codigo upc"],
    "material": ["material", "composicion"],
    "origen": ["origen", "pais de origen", "fabricado en", "origin", "made in"],
    "precio_lista": ["precio lista", "precio regular", "list price", "regular price"], # Precio antes de descuento
    "moneda": ["moneda", "divisa", "currency"]
}

def parse_unidad_y_cantidad_empaque(unidad_str: str) -> Tuple[str, Optional[int]]:
    """
    PLACEHOLDER: Parses unit string to extract description and pack quantity.
    Original implementation needs to be restored.
    Example: "Caja x 6 botellas" -> ("Caja botellas", 6)
    """
    logger = get_logger()
    if not isinstance(unidad_str, str) or not unidad_str.strip():
        return "", None

    texto_original = unidad_str.strip()
    unidad_desc_limpia = texto_original # Default
    cantidad_empaque = None

    # Regex para encontrar "X CANTIDAD", "POR CANTIDAD", "DE CANTIDAD" o simplemente "CANTIDAD"
    patrones_cantidad = [
        # Formato: (Desc Antes) [separador] (Cantidad) [separador] (Desc Después opcional)
        # Ej: "Caja x 6 botellas", "Pack de 12 Unidades", "Bolsa por 5 KG"
        r'^(?P<desc_antes>.*?)[\s\-_]*(?:[xX]|POR|DE)[\s\-_]+(?P<cantidad>\d+)[\s\-_]*(?P<desc_despues>.*)$',
        # Formato: (Cantidad) [separador] (Desc Después)
        # Ej: "6 Botellas", "12 Unidades Pack"
        r'^(?P<cantidad>\d+)[\s\-_]+(?P<desc_despues>.*?)$',
        # Formato: (Desc Antes) [separador] (Cantidad) (SIN Desc Después explícita, pero puede haber unidad pegada al número)
        # Ej: "Caja Pack x6", "Botella 750ml" (este último es mejor manejado por el bloque de abajo)
        r'^(?P<desc_antes>.*?)[\s\-_]*(?:[xX]|POR|DE)?[\s\-_]*(?P<cantidad>\d+)$',
    ]

    for patron_regex in patrones_cantidad:
        match = re.search(patron_regex, texto_original, re.IGNORECASE)
        if match:
            dict_match = match.groupdict()
            try:
                cand_cantidad = dict_match.get('cantidad')
                if cand_cantidad:
                    cantidad_empaque = int(cand_cantidad)
                    if cantidad_empaque > 0:
                        desc_antes = limpiar_texto_base(dict_match.get('desc_antes', '')).strip()
                        desc_despues = limpiar_texto_base(dict_match.get('desc_despues', '')).strip()

                        if desc_antes and desc_despues:
                            unidad_desc_limpia = f"{desc_antes} {desc_despues}".strip()
                        elif desc_despues: # Cantidad al inicio
                            unidad_desc_limpia = desc_despues
                        elif desc_antes: # Cantidad al final
                            unidad_desc_limpia = desc_antes
                        else: # Solo cantidad? Raro.
                             unidad_desc_limpia = "" # Será reemplazado por texto_original si queda vacío

                        # Si la descripción es solo la unidad (ej. "ml", "kg") y la cantidad es grande, puede ser correcto.
                        # Si la descripción quedó vacía, usar el texto original sin el número y separadores.
                        if not unidad_desc_limpia.strip():
                            temp_desc = texto_original
                            # Quitar el número y los separadores que lo rodean.
                            temp_desc = re.sub(r'[\s\-_]*(?:[xX]|POR|DE)?[\s\-_]*' + re.escape(cand_cantidad) + r'[\s\-_]*', ' ', temp_desc, flags=re.IGNORECASE)
                            unidad_desc_limpia = limpiar_texto_base(temp_desc).strip()
                            if not unidad_desc_limpia.strip(): # Si aún así queda vacío
                                unidad_desc_limpia = texto_original # Fallback final a todo el texto original

                        logger.debug(f"Parse unidad (patrón '{patron_regex}'): '{texto_original}' -> desc='{unidad_desc_limpia}', cant={cantidad_empaque}")
                        return unidad_desc_limpia, cantidad_empaque
                cantidad_empaque = None # Resetear si la cantidad no fue válida
            except ValueError:
                cantidad_empaque = None
    
    # Caso especial para unidades pegadas al número: "750ml", "1kg", "5L"
    # Este regex busca un número seguido inmediatamente por letras (la unidad)
    match_num_unidad_pegada = re.match(r'^(?P<cantidad>\d+)(?P<unidad_pegada>[a-zA-Z]+)$', texto_original.replace(" ","")) # Quitar espacios para ej "750 ml" -> "750ml"
    if not cantidad_empaque and match_num_unidad_pegada:
        try:
            cand_cantidad = match_num_unidad_pegada.group('cantidad')
            unidad_pegada = match_num_unidad_pegada.group('unidad_pegada')
            if cand_cantidad and unidad_pegada:
                cantidad_empaque = int(cand_cantidad)
                # Aquí la descripción es la unidad pegada. Podríamos tener un map para expandirlas (ml -> mililitros)
                unidad_desc_limpia = limpiar_texto_base(unidad_pegada)
                logger.debug(f"Parse unidad (num+unidad pegada): '{texto_original}' -> desc='{unidad_desc_limpia}', cant={cantidad_empaque}")
                return unidad_desc_limpia, cantidad_empaque
            cantidad_empaque = None
        except ValueError:
            cantidad_empaque = None

    # Fallback final: si no se pudo extraer cantidad, la descripción es el texto original limpio.
    if not cantidad_empaque:
        unidad_desc_limpia = limpiar_texto_base(texto_original)
        logger.debug(f"Parse unidad (sin cantidad extraída): '{texto_original}' -> desc='{unidad_desc_limpia}', cant=None")
        return unidad_desc_limpia, None

    # Este return es por si algún flujo anterior asignó cantidad_empaque pero no retornó.
    # Debería ser cubierto por los returns dentro del bucle/condiciones.
    return limpiar_texto_base(unidad_desc_limpia), cantidad_empaque


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

def _get_main_menu_payload(
    context: dict,
    welcome_message_override: Optional[str] = None,
    reduced: bool = False,
) -> Dict[str, Any]:
    """
    Generates the main menu payload with the new, structured layout.
    """
    viewer_user = context.get("viewer_user_obj")
    profile_name = context.get("profile_name")
    owner_user = context.get("user_obj")

    def _normalize_str(value: Optional[object]) -> str:
        if value is None:
            return ""
        return str(value).strip().lower()

    def _should_ignore_viewer_identity(viewer, owner) -> bool:
        if not viewer:
            return False

        owner_id = getattr(owner, "id", None) if owner else None
        viewer_id = getattr(viewer, "id", None)
        if owner_id and viewer_id and viewer_id == owner_id:
            return True

        viewer_empresa_id = getattr(viewer, "empresa_id", None)
        if owner_id and viewer_empresa_id and viewer_empresa_id == owner_id:
            return True

        owner_empresa_id = getattr(owner, "empresa_id", None) if owner else None
        if owner_empresa_id and viewer_empresa_id and viewer_empresa_id == owner_empresa_id:
            return True

        viewer_role = _normalize_str(getattr(viewer, "rol", None) or getattr(viewer, "role", None))
        internal_roles = {"admin", "super_admin", "empleado", "staff", "municipio", "empresa"}

        owner_municipio_id = getattr(owner, "municipio_id", None) if owner else None
        viewer_municipio_id = getattr(viewer, "municipio_id", None)
        if (
            owner_municipio_id
            and viewer_municipio_id
            and viewer_municipio_id == owner_municipio_id
            and viewer_role in internal_roles
        ):
            return True

        session_kind = context.get("session_kind") or context.get("viewer_session_kind")
        if session_kind and _normalize_str(session_kind) == "widget" and viewer_role in internal_roles:
            return True

        return False

    user_name = None
    if isinstance(profile_name, str) and profile_name.strip():
        owner_name = None
        if owner_user:
            owner_name = getattr(owner_user, "nombre", None) or getattr(owner_user, "name", None)
        # Avoid greeting with the admin/owner name when the session is anonymous
        if not owner_name or profile_name.strip().lower() != str(owner_name).strip().lower():
            user_name = profile_name.strip()
    if not user_name and viewer_user and not _should_ignore_viewer_identity(viewer_user, owner_user):
        user_name = getattr(viewer_user, "nombre", None) or getattr(viewer_user, "name", None)

    if not user_name:
        municipal_context = (
            context.get("chat_db_context_data", {}).get(CONTEXTO_MUNICIPIO, {})
            if isinstance(context.get("chat_db_context_data"), dict)
            else {}
        )
        contacto_usuario = municipal_context.get("contacto_usuario", {})
        if isinstance(contacto_usuario, dict):
            nombre_contacto = contacto_usuario.get("nombre")
            if isinstance(nombre_contacto, str) and nombre_contacto.strip():
                user_name = nombre_contacto.strip()

    municipio_config = context.get("municipio_config_actual") or {}

    def _resolve_tenant_name() -> str:
        tenant_name = "tu municipio"
        if isinstance(municipio_config, dict):
            tenant_name = (
                municipio_config.get("nombre")
                or municipio_config.get("nombre_municipio")
                or municipio_config.get("municipio_nombre")
                or tenant_name
            )
        if owner_user and tenant_name == "tu municipio":
            tenant_name = getattr(owner_user, "nombre_empresa", None) or getattr(owner_user, "name", "tu municipio")
        return tenant_name

    def _safe_format(template: str, values: dict) -> str:
        class _SafeDict(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        return template.format_map(_SafeDict(values))

    tenant_name_text = _resolve_tenant_name()

    if welcome_message_override:
        welcome_message = welcome_message_override
    elif user_name:
        welcome_message = f"👋 *¡Hola, {user_name}!*"
    else:
        # User's name is not known, ask for it.
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault(CONTEXTO_MUNICIPIO, {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_NOMBRE_INICIAL.name
        assistant_name = None
        if isinstance(municipio_config, dict):
            assistant_name = municipio_config.get("assistant_name") or municipio_config.get("bot_name")
        if not assistant_name:
            assistant_name = tenant_name_text if tenant_name_text != "tu municipio" else "JUNI"
        return {
            "message_body": f"¡Hola! Soy {assistant_name}, el asistente virtual de {tenant_name_text}. Para una atención más personalizada, ¿podrías decirme tu nombre?",
            "message_type": "text",
            "fuente": "pedir_nombre_inicial"
        }

    # Determine tenant name for text body
    tenant_name_text = "tu municipio"
    municipio_config = context.get("municipio_config_actual") or {}
    if isinstance(municipio_config, dict):
        tenant_name_text = (
            municipio_config.get("nombre")
            or municipio_config.get("nombre_municipio")
            or municipio_config.get("municipio_nombre")
            or tenant_name_text
        )
    if owner_user and tenant_name_text == "tu municipio":
        tenant_name_text = getattr(owner_user, "nombre_empresa", None) or getattr(owner_user, "name", "tu municipio")

    if welcome_message == f"👋 *¡Hola, {user_name}!*":
        welcome_message = f"{welcome_message} Bienvenido a *{tenant_name_text}*."

    assistant_intro = ""

    if reduced:
        main_text_body = "\n\n".join(
            part
            for part in [
                assistant_intro,
                "Estas son las opciones principales del municipio.",
                "Podés compartir tu ubicación, enviarnos fotos o mandarnos una nota de voz con lo que necesitás.",
                "Elegí una o contame qué necesitás y te ayudo al instante.",
            ]
            if part
        )
    else:
        main_text_body = "\n\n".join(
            part
            for part in [
                assistant_intro,
                "*Podés compartir tu ubicación, enviarnos fotos o mandarnos una nota de voz* con lo que necesitás y te ofreceremos opciones para trámites, reclamos y más. Este servicio es accesible y está listo para ayudarte.",
                "También podés usar emojis para realizar acciones rápidas.",
                "¿Cómo te puedo ayudar hoy?",
            ]
            if part
        )

    channel = context.get("channel", "web")
    if channel == "whatsapp":
        # Simplified menu for WhatsApp: only top-level categories
        whatsapp_buttons = [
            {"texto": "🗣️ Reclamos y Consultas", "action_id": "mostrar_menu_reclamos"},
            {"texto": "🚗 Trámites y Turnos", "action_id": "mostrar_menu_tramites"},
            {"texto": "📰 Información del Municipio", "action_id": "mostrar_menu_informacion"},
        ]
        whatsapp_buttons.append({"texto": "🛍️ Catálogo y Beneficios", "action_id": "mostrar_menu_catalogo"})
        if FEATURE_ENCUESTAS:
            whatsapp_buttons.append({"texto": "🗳️ Participación Ciudadana", "action_id": "mostrar_menu_encuestas"})
        whatsapp_buttons.extend([
            {"texto": "🅿️ Estacionamiento", "action_id": "mostrar_menu_estacionamiento"},
            {"texto": "❓ Ayuda", "action_id": "mostrar_menu_ayuda"},
        ])

        categorias = [{
            "titulo": "*Categorías*",
            "botones": whatsapp_buttons,
        }]

        flat_buttons = []
        for boton in categorias[0].get('botones', []):
            new_boton = boton.copy()
            new_boton['id'] = new_boton.get('action_id', new_boton['texto'])
            flat_buttons.append(new_boton)
    else:
        # Full accordion-style menu for web/widget channels
        categorias = [
            {"titulo": "🗣️ Reclamos y Consultas", "botones": [
                {"texto": "📝 Iniciar un Reclamo", "action_id": "iniciar_reclamo"},
                {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia"},
                {"texto": "🤔 Consultar Estado de Reclamo", "action_id": "consultar_estado_reclamo"},
                {"texto": "📞 Contactos Útiles", "action_id": "contactos_utiles"},
            ]},
            {"titulo": "🚗 Trámites y Turnos", "botones": [
                {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
                {"texto": "🗓️ Solicitar Otros Turnos", "action_id": "solicitar_turnos"},
                {"texto": "💵 Pagar Tasas Municipales", "action_id": "pago_de_tasas_vigentes"},
            ]},
            {"titulo": "📰 Información del Municipio", "botones": [
                {"texto": "🎭 Agenda Cultural y Noticias", "action_id": "agenda_y_noticias"},
                {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
                {"texto": "🏗️ Obras", "action_id": "obras"},
            ]},
        ]

        # Only add Punto Limpio for valid municipal tenants (e.g. Junín) or generic "municipio"
        is_junin_or_generic = False
        if owner_user:
            owner_type = getattr(owner_user, "tipo_chat", "")
            owner_slug = getattr(owner_user, "municipio_id", "") # Assuming municipio_id might act as slug or id check
            if owner_type == "municipio" or (owner_user.id == 4): # 4 is often default municipality
                 is_junin_or_generic = True
        elif context.get("chat_db_context_data", {}).get(CONTEXTO_MUNICIPIO):
             is_junin_or_generic = True # Context exists, implies municipality

        # Explicit check for pyme context to disable it
        if context.get("tipo_entidad") == "pyme":
             is_junin_or_generic = False

        if is_junin_or_generic:
            # Append Punto Limpio only if appropriate
            categorias[-1]["botones"].append({"texto": "♻️ Punto Limpio", "action_id": "punto_limpio"})

        categorias.append({"titulo": "🛍️ Catálogo y Beneficios", "botones": [
                {"texto": "📂 Ver Catálogo", "action_id": "catalogo_ver"},
                {"texto": "🎁 Canje de Puntos", "action_id": "catalogo_canje_puntos"},
                {"texto": "🛒 Compra de Productos", "action_id": "catalogo_compras"},
                {"texto": "❤️ Donaciones", "action_id": "catalogo_donaciones"},
            ]},
        )

        if FEATURE_ENCUESTAS:
            categorias.append({
                "titulo": "🗳️ Participación Ciudadana",
                "botones": [
                    {"texto": "🗳️ Encuestas Activas", "action_id": "mostrar_menu_encuestas"},
                ],
            })

        categorias.extend([
            {"titulo": "🅿️ Estacionamiento", "botones": [
                {"texto": "🅿️ Buscar Estacionamiento Libre", "action_id": "buscar_estacionamiento"},
            ]},
            {"titulo": "❓ Ayuda", "botones": [
                {"texto": "ℹ️ Cómo usar el bot", "action_id": "mostrar_menu_ayuda"},
            ]},
        ])

        flat_buttons = []
        for categoria in categorias:
            for boton in categoria.get('botones', []):
                new_boton = boton.copy()
                new_boton['id'] = new_boton.get('action_id', new_boton['texto'])
                flat_buttons.append(new_boton)

    def _normalize_for_audio(value: str | None) -> str:
        """Strip emojis/markdown so the spoken greeting sounds natural."""
        normalized = clean_text_for_tts(value) if isinstance(value, str) else clean_text_for_tts(str(value) if value else "")
        return normalized.strip()

    safe_user_name = _normalize_for_audio(user_name)

    # Determine tenant/bot name dynamically
    tenant_name = tenant_name_text
    bot_name = "el asistente virtual"
    if isinstance(municipio_config, dict):
        bot_name = municipio_config.get("assistant_name") or municipio_config.get("bot_name") or bot_name

    if safe_user_name:
        audio_greeting = f"Hola {safe_user_name}, soy {bot_name} de {tenant_name}."
    else:
        audio_greeting = f"Hola, soy {bot_name} de {tenant_name}."

    if reduced:
        audio_intro = (
            "Volvimos al menú principal para seguir con tu gestión. "
            "Podés enviar ubicación, fotos o notas de voz para que te ayudemos mejor."
        )
        audio_prompt = "Elegí una categoría para continuar."
    else:
        audio_intro = (
            "Puedo ayudarte con trámites, reclamos e información del municipio. "
            "Podés compartir tu ubicación, fotos o notas de voz y usar emojis para acciones rápidas."
        )
        audio_prompt = "Elegí una categoría para comenzar."

    if channel == "whatsapp" and categorias:
        primary_texts = [
            text for text in (
                _normalize_for_audio(btn.get("texto"))
                for btn in categorias[0].get("botones", [])
            ) if text
        ]
    else:
        primary_texts = [
            text for text in (
                _normalize_for_audio(cat.get("titulo"))
                for cat in categorias
            ) if text
        ]
        if not primary_texts:
            primary_texts = [
                text for text in (
                    _normalize_for_audio(btn.get("texto"))
                    for cat in categorias
                    for btn in cat.get("botones", [])
                ) if text
            ]

    audio_options = [
        f"Opción {idx}, {text}."
        for idx, text in enumerate(primary_texts, start=1)
    ]
    audio_closing = "Respondé con el número de la opción o pedime que repita el menú."
    audio_parts = [audio_greeting, audio_intro, audio_prompt, *audio_options, audio_closing]
    audio_text = " ".join(part.strip() for part in audio_parts if part)

    response = {
        "message_body": f"{welcome_message}\n\n{main_text_body}",
        "options_list": flat_buttons,
        "message_type": "interactive_list",
        "accion_backend": "responder_directamente",
        "fuente": "greeting_handler_structured_menu_v2",
        "categorias": categorias,
        "audio_text": audio_text,
        "generar_audio": True
    }
    # Do not include a header image in the initial greeting menu to keep the
    # conversation lightweight and similar to other professional bots like
    # Boti. Removing the image avoids large headers in WhatsApp.
    return response

def clean_text_for_tts(text: str) -> str:
    """
    Cleans text for Text-to-Speech by removing markdown, emojis, and other symbols.
    """
    if not isinstance(text, str):
        return ""

    # Remove markdown characters (bold, italics)
    text = re.sub(r'(\*\*|__|\*|_|~)', '', text)

    # Remove emojis
    # This regex is a common pattern for emojis.
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\U00002702-\U000027B0"  # Dingbats
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub(r'', text)

    # Remove other special characters that might be read aloud, like the hand wave emoji not caught by the range
    text = text.replace('👋', '').replace('🛠️', '').replace('📄', '').replace('📅', '').replace('📰', '').replace('🗣️', '').replace('📸', '').replace('📍', '')

    # Replace newlines with periods to encourage pauses
    text = re.sub(r'\s*\n+\s*', '. ', text)
    # Replace multiple spaces with a single space
    text = re.sub(r'\s+', ' ', text).strip()

    return text

def parse_cantidad_flexible(cantidad_str: Any) -> Optional[int]:
    """
    PLACEHOLDER: Parses a flexible quantity string (e.g., "6 units", "12", "1 dozen") into an integer.
    Attempts to extract the first number found.
    Original implementation needs to be restored for more robust parsing.
    """
    get_logger().warning(f"Using PLACEHOLDER parse_cantidad_flexible for: {cantidad_str}")
    if cantidad_str is None:
        return None

    s = str(cantidad_str)

    # Try to extract first number found
    match = re.search(r'\d+', s)
    if match:
        try:
            return int(match.group(0))
        except ValueError:
            return None

    # Add more sophisticated parsing here if needed (e.g., "dozen" -> 12)
    # For placeholder, this is basic.
    return None

def validar_email(email: str) -> bool:
    """Valida si un email tiene formato correcto."""
    patron = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    return bool(re.match(patron, email))

def validar_telefono(telefono: str) -> bool:
    """Valida si un teléfono tiene formato numérico y longitud razonable (6-20 dígitos)."""
    if not isinstance(telefono, str):
        return False
    telefono_limpio = telefono.strip()
    if not telefono_limpio:
        return False

    telefono_limpio = re.sub(
        r'^(tel\.?|teléfono|telefono|cel\.?|celular|whatsapp|wsapp|wa)[:\s-]*',
        '',
        telefono_limpio,
        flags=re.IGNORECASE,
    )
    telefono_limpio = re.sub(r'\b(int|interno|intern)\b\.?:?', '', telefono_limpio, flags=re.IGNORECASE)
    telefono_limpio = telefono_limpio.strip()

    if re.search(r'[A-Za-z]', telefono_limpio):
        return False

    solo_numeros = re.sub(r"\D", "", telefono_limpio)

    return 6 <= len(solo_numeros) <= 20

def formatear_telefono_e164(telefono: str, cod_pais: str = "54") -> str:
    """
    Normaliza un número de teléfono a formato E.164 (por defecto para Argentina).
    Ejemplo: "11 2345-6789" -> "+541123456789"
    """
    if not isinstance(telefono, str):
        return ""
    solo_numeros = re.sub(r"\D", "", telefono)
    if solo_numeros.startswith(cod_pais):
        # El número ya incluye el código de país. No se modifica.
        return f"+{solo_numeros}"

    # Lógica específica para Argentina para añadir el '9' a móviles
    if cod_pais == "54":
        # Quitar '0' y '15' si están al principio
        if solo_numeros.startswith('0'):
            solo_numeros = solo_numeros[1:]
        if solo_numeros.startswith('15'):
            solo_numeros = solo_numeros[2:]

        # Si después de limpiar, el número tiene 10 dígitos, es un móvil.
        if len(solo_numeros) == 10:
            return f"+{cod_pais}9{solo_numeros}"

    # Para otros países o números fijos de Argentina
    return f"+{cod_pais}{solo_numeros}"

def calcular_precio_por_unidad(precio_total: float, cantidad: int) -> float:
    """Devuelve el precio por unidad dado un total y la cantidad."""
    try:
        return float(precio_total) / int(cantidad) if cantidad else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def generar_link_google_maps(
    direccion: str | None = None,
    latitud: float | None = None,
    longitud: float | None = None,
) -> str:
    """Genera un enlace de Google Maps basado en una dirección o coordenadas."""
    base = "https://www.google.com/maps/search/?api=1&query="
    if latitud is not None and longitud is not None:
        return f"{base}{latitud},{longitud}"
    if direccion:
        from urllib.parse import quote_plus
        return f"{base}{quote_plus(direccion)}"
    return ""

def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calcula la similaridad coseno entre dos vectores."""
    import numpy as np

    # Idealmente, registrar warnings/errors aquí si se usa un logger configurado
    if not isinstance(vec1, list) or not isinstance(vec2, list):
        # get_logger().warning("Cosine similarity: input is not a list.")
        return 0.0
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        # get_logger().warning(f"Cosine similarity: Invalid or mismatched length vectors. vec1_len={len(vec1)}, vec2_len={len(vec2)}")
        return 0.0

    vec1_np = np.array(vec1, dtype=np.float32)
    vec2_np = np.array(vec2, dtype=np.float32)

    dot_product = np.dot(vec1_np, vec2_np)
    norm_vec1 = np.linalg.norm(vec1_np)
    norm_vec2 = np.linalg.norm(vec2_np)

    if norm_vec1 == 0 or norm_vec2 == 0:
        # get_logger().warning(f"Cosine similarity: Zero norm vector. norm_vec1={norm_vec1}, norm_vec2={norm_vec2}")
        return 0.0

    similarity = dot_product / (norm_vec1 * norm_vec2)
    return float(similarity) # Asegurar que devuelve float nativo

def construir_respuesta_sugerir_registro(mensaje_personalizado: Optional[str] = None, tipo_entidad: str = "pyme", channel: str = "web"):
    """
    Construye un diccionario de respuesta estándar para sugerir el registro o inicio de sesión.
    Se usa cuando el bot proactivamente quiere que el usuario anónimo se identifique.
    """
    mensaje_base_web = "Para una experiencia más completa, guardar tu historial y acceder a todas las funciones, te recomiendo crear una cuenta o iniciar sesión."
    mensaje_base_whatsapp = "Para ayudarte mejor y guardar tu historial, te recomiendo crear una cuenta o iniciar sesión." # Shorter

    mensaje_final = ""
    # Use personalized message if provided, otherwise use channel-specific base.
    if mensaje_personalizado:
        # If personalized message is very short, it might be an intro. Append channel-specific base.
        # Otherwise, assume personalized message is complete enough.
        if len(mensaje_personalizado) < 30 and channel == "whatsapp": # Arbitrary threshold
             mensaje_final = f"{mensaje_personalizado} {mensaje_base_whatsapp}"
        elif len(mensaje_personalizado) < 40 and channel != "whatsapp":
             mensaje_final = f"{mensaje_personalizado} {mensaje_base_web}"
        else:
            mensaje_final = mensaje_personalizado # Personalized message is likely complete
    else: # No personalized message, use channel-specific base
        mensaje_final = mensaje_base_whatsapp if channel == "whatsapp" else mensaje_base_web

    # Las acciones 'register_widget' y 'login_widget' deben ser manejadas por el frontend del widget embebido
    # para mostrar los formularios correspondientes que luego llaman a /widget/register y /widget/login.
    return {
        "respuesta": mensaje_final,
        "fuente": "sistema_sugerencia_registro", # Fuente clara para identificar esta acción
        "botones": [
            {"texto": "Registrarme Gratis", "action": "register_widget"},
            {"texto": "Iniciar Sesión", "action": "login_widget"}
        ],
        "tipo_respuesta": "sugerencia_registro", # Tipo especial para que el frontend lo maneje
        # Devolver un contexto vacío o el último conocido, para que el frontend no lo pierda.
        # Los handlers (pyme/municipio) deben asegurarse de pasar el contexto actual si es necesario.
        f"contexto_{tipo_entidad}": {} # O el contexto que se le pase a esta función
    }

def extract_multiple_contact_details_regex(text: str, potential_fields: list | None = None) -> dict:
    """
    Extract contact details using improved regex and structural heuristics.
    """
    if not text:
        return {}

    if potential_fields is None:
        potential_fields = ["nombre", "dni", "email", "telefono", "direccion"]

    from utils.validators import (
        extract_address,
        extract_email,
        extract_phone,
        extract_name,
        extract_dni,
    )

    extracted_data: dict[str, Optional[str]] = {}

    nombre_prefijo = None
    if "nombre" in potential_fields:
        dni_pattern = re.search(r"\b\d{7,8}\b", text)
        if dni_pattern:
            posible_prefijo = text[:dni_pattern.start()].strip(" ,")
            if posible_prefijo:
                posible_nombre = extract_name(posible_prefijo)
                if posible_nombre and len(posible_nombre.split()) >= 2:
                    nombre_prefijo = posible_nombre

    remaining_text = f" {text} "

    def _remove_from_remaining(value: Optional[str]) -> None:
        nonlocal remaining_text
        if value:
            safe_value = re.escape(value)
            remaining_text = re.sub(safe_value, " ", remaining_text, flags=re.IGNORECASE)

    # --- Step 1: Extract easily identifiable patterns first ---
    # Order: email, DNI, then phone, as DNI is more specific and less ambiguous.
    if "email" in potential_fields:
        email = extract_email(remaining_text)
        if email:
            extracted_data["email"] = email
            _remove_from_remaining(email)

    if "dni" in potential_fields:
        dni_match_tuple = extract_dni(remaining_text)
        if dni_match_tuple:
            normalized_dni, raw_dni = dni_match_tuple
            extracted_data["dni"] = normalized_dni
            _remove_from_remaining(raw_dni)
            # Also remove variants with "DNI" prefix for cleaner remaining text
            _remove_from_remaining(f"DNI {raw_dni}")

    if "telefono" in potential_fields:
        phone_match = extract_phone(remaining_text)
        if phone_match:
            normalized_phone, raw_phone = phone_match
            extracted_data["telefono"] = normalized_phone
            _remove_from_remaining(raw_phone)

    # --- Step 2: Attempt to find name and address from the remaining text ---
    remaining_text = re.sub(r'\s+', ' ', remaining_text).strip(" ,")

    if nombre_prefijo and "nombre" in potential_fields:
        extracted_data.setdefault("nombre", nombre_prefijo)
        _remove_from_remaining(nombre_prefijo)
        remaining_text = re.sub(r'\s+', ' ', remaining_text).strip(" ,")

    dir_match = re.search(r'(?:dirección|direccion|domicilio)\s*[:\s-]\s*(.*)', remaining_text, re.IGNORECASE)
    if dir_match:
        address_candidate = dir_match.group(1).strip()
        if "direccion" in potential_fields and address_candidate:
            extracted_data["direccion"] = address_candidate
        _remove_from_remaining(dir_match.group(0))
        remaining_text = re.sub(r'\s+', ' ', remaining_text).strip(" ,")

    if "direccion" not in extracted_data and "direccion" in potential_fields and remaining_text:
        address_candidate = extract_address(remaining_text)
        if not address_candidate:
            if re.search(r'\d', remaining_text) and len(remaining_text.split()) >= 2:
                address_candidate = remaining_text.strip(" ,")
        if address_candidate:
            extracted_data["direccion"] = address_candidate.strip()
            _remove_from_remaining(address_candidate)
            remaining_text = re.sub(r'\s+', ' ', remaining_text).strip(" ,")

    if "nombre" in potential_fields:
        name = extract_name(remaining_text)
        if name:
            extracted_data["nombre"] = name
            _remove_from_remaining(name)
            remaining_text = re.sub(r'\s+', ' ', remaining_text).strip(" ,")

    if "direccion" not in extracted_data and "direccion" in potential_fields and remaining_text:
        cleaned_remaining = remaining_text.strip(" ,")
        if cleaned_remaining:
            normalized_joined = _normalize_contact_token(cleaned_remaining)
            normalized_tokens = [
                token
                for token in (
                    _normalize_contact_token(part)
                    for part in cleaned_remaining.split()
                )
                if token
            ]

            tokens_are_labels = (
                bool(normalized_tokens)
                and all(token in CONTACT_LABEL_TOKENS for token in normalized_tokens)
            )

            if normalized_joined in CONTACT_LABEL_TOKENS or tokens_are_labels:
                cleaned_remaining = ""

        if cleaned_remaining:
            extracted_data["direccion"] = cleaned_remaining

    return {k: v for k, v in extracted_data.items() if v}

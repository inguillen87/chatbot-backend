from services.cohere_ai import get_cohere_response # Asegúrate de poder importarlo aquí
import json
import logging
import requests
import os
import unicodedata # <--- ¡Importante agregar esta línea!
import re
from services.config_loader import cargar_configuracion_municipio

# ... (el resto de tus herramientas y diccionarios)

# --- NUEVA HERRAMIENTA DE SUGERENCIA DE CATEGORÍAS ---

def crear_prompt_sugerir_categorias(texto_usuario: str, categorias_disponibles: list[str]) -> str:
    """Crea un prompt específico para que el LLM sugiera categorías relevantes."""
    
    lista_categorias_str = "\n".join(f"- {cat}" for cat in categorias_disponibles)
    
    prompt = f"""
Tu tarea es actuar como un experto clasificador de reclamos municipales.
Dado un RECLAMO DE USUARIO y una LISTA DE CATEGORÍAS, tu única función es seleccionar las 3 categorías más relevantes de la lista que mejor correspondan al reclamo del usuario.

LISTA DE CATEGORÍAS DISPONIBLES:
{lista_categorias_str}

RECLAMO DE USUARIO: "{texto_usuario}"

INSTRUCCIONES:
- Analiza el reclamo del usuario y compáralo con la lista de categorías.
- Devuelve SÓLO un objeto JSON que contenga una única clave "sugerencias" con una lista de hasta 3 nombres de categorías extraídos EXACTAMENTE de la lista proporcionada.
- Si ninguna categoría parece relevante, devuelve una lista vacía.

Ejemplo 1:
- Reclamo: "la esquina de mi casa está a oscuras y el asfalto es un desastre"
- Respuesta: {{"sugerencias": ["Luminaria", "Arreglo de calle"]}}

Ejemplo 2:
- Reclamo: "quiero saber el teléfono del intendente"
- Respuesta: {{"sugerencias": []}}

Tu respuesta:
"""
    return prompt

def sugerir_categorias_relevantes(texto_usuario: str) -> list[str]:
    """
    Usa el LLM para obtener una lista de categorías sugeridas basadas en el texto del usuario.
    """
    todas_las_categorias = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values()))) # Still useful for keyword matching
    # LLM call removed. Category suggestion is now expected from the main Gemini call.
    # This function now performs basic keyword matching as a fallback or primary if called directly.
    logger.info(f"Sugiriendo categorías (NO-LLM) para: '{texto_usuario[:50]}...'")
    sugeridas = []
    if not texto_usuario: return sugeridas

    texto_norm = normalizar_texto(texto_usuario)
    from services.categorias_municipio import CATEGORIAS_RECLAMO
    # Contar ocurrencias de keywords para cada categoría
    conteo_categorias = {cat: 0 for cat in CATEGORIAS_RECLAMO} # Use the defined list
    palabras_usuario = set(texto_norm.split())

    for keyword, category_target in KEYWORD_TO_CATEGORY_MAP.items():
        # Usar una keyword normalizada para la comparación si es necesario,
        # aunque KEYWORD_TO_CATEGORY_MAP ya tiene claves en minúscula y sin acentos (asumido).
        if keyword in palabras_usuario:
            conteo_categorias[category_target] = conteo_categorias.get(category_target, 0) + 1
            if keyword in texto_norm: # Dar más peso si es una frase
                 conteo_categorias[category_target] = conteo_categorias.get(category_target, 0) + 2


    # Ordenar por conteo descendente
    categorias_ordenadas = sorted(conteo_categorias.items(), key=lambda item: item[1], reverse=True)

    for cat, count in categorias_ordenadas:
        if count > 0 and len(sugeridas) < 3:
            if cat not in sugeridas: # Evitar duplicados si diferentes keywords apuntan a la misma categoría
                 sugeridas.append(cat)
        if len(sugeridas) >= 3:
            break

    if not sugeridas and texto_usuario:
        # Si después del keyword matching no hay nada, pero había texto, sugerir "Otro Motivo"
        # Asegurarse que "Otro Motivo" sea una de las CATEGORIAS_RECLAMO válidas.
        if "Otro Motivo" in CATEGORIAS_RECLAMO: # Check against the defined list
            sugeridas.append("Otro Motivo")

    logger.info(f"Categorías sugeridas (NO-LLM) para '{texto_usuario[:50]}...': {sugeridas}")
    return sugeridas # Devuelve hasta 3, o menos si no hay suficientes matches.
   
logger = logging.getLogger(__name__)
Maps_API_KEY = os.environ.get("Maps_API_KEY")
MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")
CONFIG_MUNICIPIO = cargar_configuracion_municipio(MUNICIPIO_ID, "config.json")


# --- NUEVA FUNCIÓN DE NORMALIZACIÓN ---
def normalizar_texto(texto: str) -> str:
    """Normaliza un texto eliminando acentos y puntuación sin modificar palabras."""

    if not texto:
        return ""

    texto = texto.lower().strip()

    # Quitar diacríticos (acentos)
    texto = ''.join(
        c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)
    )

    # Mantener solo caracteres alfanuméricos y espacios
    texto = re.sub(r"[^a-z0-9\s]", "", texto)

    # Normalizar espacios múltiples
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto

# --- VALIDACIÓN DE DIRECCIONES ---
def direccion_es_valida(texto: str) -> bool:
    """Heurística simple para verificar si una dirección parece válida a grandes rasgos."""
    if not texto:
        return False
    texto_norm = normalizar_texto(texto)
    # Verifica que haya al menos una palabra (nombre de calle) y al menos un número.
    # Esta es una validación muy básica. La función `parse_direccion_completa` hará el trabajo pesado.
    tiene_numero = bool(re.search(r"\d+", texto_norm)) # Un número cualquiera
    tiene_palabras_calle = bool(re.search(r"[a-zA-Z]{2,}", texto_norm)) # Al menos una palabra de 2+ letras para la calle

    # Podríamos añadir más heurísticas si es necesario, por ejemplo,
    # si la parte numérica está muy separada de la parte de texto, etc.
    # Pero es mejor dejar que el LLM lo maneje.
    return tiene_numero and tiene_palabras_calle


def parse_direccion_completa(texto_direccion: str, municipio_config: dict = None) -> dict | None:
    """
    Usa un LLM para extraer componentes estructurados de una dirección.
    Args:
        texto_direccion: La dirección proporcionada por el usuario.
        municipio_config: Configuración del municipio actual (puede contener ciudad/provincia por defecto).
    Returns:
        Un diccionario con los campos de la dirección o None si falla la extracción.
    """
    if not texto_direccion:
        return None

    if municipio_config is None:
        # This case should be less frequent if context always provides one (even the global one)
        logger.warning("[ParseDireccion] municipio_config no fue proporcionado, usando un diccionario vacío como fallback para defaults.")
        municipio_config = {}

    # Prioritize '_default' suffixed keys, then direct keys, then hardcoded N/A
    default_localidad = municipio_config.get('ciudad_default', municipio_config.get('ciudad', 'Localidad Desconocida'))
    default_provincia = municipio_config.get('provincia_default', municipio_config.get('provincia', 'Provincia Desconocida'))

    # LLM call removed. This function now performs basic regex/keyword parsing.
    # The main Gemini call (JULES_SYSTEM_PROMPT) is expected to provide structured address if possible.

    logger.info(f"[ParseDireccion NON-LLM] Parseando: '{texto_direccion}' con defaults: Loc='{default_localidad}', Prov='{default_provincia}'")
    parsed_data = {}
    if not texto_direccion or not isinstance(texto_direccion, str):
        return parsed_data # Devuelve dict vacío si no hay texto

    # Limpiar y normalizar texto de entrada
    direccion_limpia = texto_direccion.strip()

    # Patrones para extraer número, piso, depto, etc.
    # Este es un intento muy básico y puede necesitar mejoras significativas o una librería dedicada.

    # Extraer CP al final (ej: ..., 5500 o (5500))
    try:
        cp_match = re.search(r"(\b\d{4}\b|\(\d{4}\))$", direccion_limpia, re.TIMEOUT)
        if cp_match:
            parsed_data["codigo_postal"] = cp_match.group(1).replace("(", "").replace(")", "")
            direccion_limpia = direccion_limpia[:cp_match.start()].strip().rstrip(',')
            logger.debug(f"CP extraído: {parsed_data['codigo_postal']}, resto: '{direccion_limpia}'")

        match_calle_numero_final = re.match(r"^(.*?)\s+(\d+[a-zA-Z]?(?:\s*(?:bis|altos|piso\s*\w+|dpto\s*\w+))?)\s*(?:,\s*(.*))?$", direccion_limpia, re.IGNORECASE | re.TIMEOUT)

        calle_original = direccion_limpia

        if match_calle_numero_final:
            parsed_data["calle"] = match_calle_numero_final.group(1).strip().rstrip(',')
            parsed_data["numero"] = match_calle_numero_final.group(2).strip()
            resto_direccion_post_numero = (match_calle_numero_final.group(3) or "").strip()
            calle_original = parsed_data["calle"]
            logger.debug(f"Calle: {parsed_data['calle']}, Numero: {parsed_data['numero']}, Resto post-numero: '{resto_direccion_post_numero}'")

            num_lower = parsed_data["numero"].lower()
            piso_depto_match_en_num = re.search(r"(?:piso|p)\s*(\w+)(?:\s*(?:dpto|d)\s*(\w+))?", num_lower, re.TIMEOUT)
            if piso_depto_match_en_num:
                parsed_data["piso"] = piso_depto_match_en_num.group(1)
                if piso_depto_match_en_num.group(2): parsed_data["departamento"] = piso_depto_match_en_num.group(2)
                parsed_data["numero"] = num_lower[:piso_depto_match_en_num.start()].strip()

            if not parsed_data.get("piso") and resto_direccion_post_numero:
                piso_depto_match_resto = re.search(r"(?:Piso|P)\s*(\w+)(?:\s*(?:Dpto|D|Depto\.?)\s*(\w+))?", resto_direccion_post_numero, re.IGNORECASE | re.TIMEOUT)
                if piso_depto_match_resto:
                    parsed_data["piso"] = piso_depto_match_resto.group(1)
                    if piso_depto_match_resto.group(2): parsed_data["departamento"] = piso_depto_match_resto.group(2)
                    resto_direccion_post_numero = resto_direccion_post_numero.replace(piso_depto_match_resto.group(0), "").strip().rstrip(',').strip()

            direccion_limpia = resto_direccion_post_numero
        else:
            parts_sin_numero = direccion_limpia.split(',', 1)
            parsed_data["calle"] = parts_sin_numero[0].strip()
            calle_original = parsed_data["calle"]
            direccion_limpia = parts_sin_numero[1].strip() if len(parts_sin_numero) > 1 else ""
            logger.debug(f"Calle (sin num claro en regex): {parsed_data['calle']}, Resto: '{direccion_limpia}'")
    except re.error as e:
        logger.error(f"Error de regex en parse_direccion_completa: {e}")
        return None

    partes_restantes = [p.strip() for p in direccion_limpia.split(',') if p.strip()]

    if default_localidad != "Localidad Desconocida": parsed_data["localidad"] = default_localidad
    if default_provincia != "Provincia Desconocida": parsed_data["provincia"] = default_provincia

    if len(partes_restantes) == 1:
        if parsed_data.get("localidad") == "Localidad Desconocida" and not any(char.isdigit() for char in partes_restantes[0]):
            parsed_data["localidad"] = partes_restantes[0]
        else:
            parsed_data["barrio"] = partes_restantes[0]

    elif len(partes_restantes) >= 2:
        if (parsed_data.get("localidad") == "Localidad Desconocida" or partes_restantes[0].lower() != default_localidad.lower()) and \
           not any(char.isdigit() for char in partes_restantes[0]):
            parsed_data["localidad"] = partes_restantes[0]
            if not any(char.isdigit() for char in partes_restantes[1]):
                 parsed_data["provincia"] = partes_restantes[1]
                 if len(partes_restantes) > 2 and not any(char.isdigit() for char in partes_restantes[2]):
                     parsed_data["barrio"] = partes_restantes[0]
                     parsed_data["localidad"] = partes_restantes[1]
                     parsed_data["provincia"] = partes_restantes[2]
        elif not any(char.isdigit() for char in partes_restantes[0]):
            parsed_data["barrio"] = partes_restantes[0] if len(partes_restantes[0]) > 2 else None
            if len(partes_restantes) > 1 and not any(char.isdigit() for char in partes_restantes[1]):
                 pass

    if not parsed_data.get("calle"):
        logger.warning(f"No se pudo parsear la calle de la dirección (NON-LLM): '{texto_direccion}'")
        return {}

    logger.info(f"Dirección parseada (NON-LLM) para '{texto_direccion}': {parsed_data}")
    return parsed_data

# --- HERRAMIENTA 1: CONSULTA DE RECOLECCIÓN ---
def consultar_recoleccion_por_direccion(direccion: str) -> str:
    """
    Herramienta profesional que usa la API de Google Maps para geocodificar una dirección
    y luego determina el horario de recolección.
    """
    # ... (El código de esta función está perfecto, no necesita cambios)
    logger.info(f"[HERRAMIENTA GEO] Buscando horario para: '{direccion}'")

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        # Return a message that allows the flow to continue if this function is called unexpectedly during a reclamo.
        return "Error de configuración: El servicio de mapas no está disponible en este momento. No se pudo validar la dirección geográficamente, pero puedes continuar con el reclamo si la dirección es correcta."

    ciudad = CONFIG_MUNICIPIO.get("ciudad", "")
    if ciudad and ciudad.lower() not in direccion.lower():
        direccion_completa = f"{direccion}, {ciudad}"
    else:
        direccion_completa = direccion

    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(direccion_completa)}&key={Maps_API_KEY}"

    try:
        response = requests.get(geocode_url)
        response.raise_for_status()
        data = response.json()

        if not data or data['status'] != 'OK' or not data.get('results'):
            logger.warning(f"[HERRAMIENTA GEO] La API de Google no pudo geocodificar la dirección: {direccion}")
            return "No pude verificar esa dirección. ¿Puedes ser un poco más específico, incluyendo la ciudad?"

        location = data['results'][0]['geometry']['location']
        lat, lng = location['lat'], location['lng']
        logger.info(f"[HERRAMIENTA GEO] Coordenadas para '{direccion}': Lat={lat}, Lng={lng}")

        if -34.595 <= lat <= -34.580 and -60.955 <= lng <= -60.935:
            return f"Detecté que la dirección '{direccion}' está en la **zona céntrica**. Allí, la recolección es de **Lunes a Sábado por la noche (a partir de las 22:00 hs)**."
        elif -34.580 <= lat <= -34.570 and -60.935 <= lng <= -60.920:
            return f"Para la zona de **Villa Belgrano**, la recolección es los días **Martes, Jueves y Sábado por la mañana (a partir de las 08:00 hs)**."
        else:
            return "Según la ubicación, te corresponde el servicio de recolección zonal. Los días son **Lunes, Miércoles y Viernes por la noche (a partir de las 21:00 hs)**. Te recomiendo confirmarlo en la web del municipio."
    except requests.exceptions.RequestException as e:
        logger.error(f"[HERRAMIENTA GEO] Error de conexión con la API de Google: {e}")
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."


# --- HERRAMIENTA 2: CATEGORIZACIÓN DE RECLAMOS ---

# En herramientas_municipio.py, reemplaza tu diccionario

KEYWORD_TO_CATEGORY_MAP = {
    # Luminaria
    "luminaria": "Luminaria", "luz": "Luminaria", "poste": "Luminaria", "farol": "Luminaria", "iluminacion": "Luminaria", "foco": "Luminaria", "lampara": "Luminaria",
    # Arbol Caido
    "arbol": "Arbol Caido", "arbol caido": "Arbol Caido", "rama": "Arbol Caido", "ramas": "Arbol Caido", "gajo": "Arbol Caido", "tronco": "Arbol Caido",
    # Limpieza
    "limpieza": "Limpieza", "basura": "Limpieza", "mugre": "Limpieza", "escombros": "Limpieza", "pasto": "Limpieza", "yuyos": "Limpieza", "maleza": "Limpieza", "desmalezado": "Limpieza", "baldio": "Limpieza",
    # Arreglo de calle
    "bache": "Arreglo de calle", "calle": "Arreglo de calle", "asfalto": "Arreglo de calle", "vereda": "Arreglo de calle", "pozo": "Arreglo de calle", "pavimento": "Arreglo de calle", "calzada": "Arreglo de calle", "hueco": "Arreglo de calle",
    # Falta de agua, rotura de caño
    "agua": "Falta de agua, rotura de caño", "caño": "Falta de agua, rotura de caño", "cano": "Falta de agua, rotura de caño", "perdida": "Falta de agua, rotura de caño", "fuga": "Falta de agua, rotura de caño", "rotura": "Falta de agua, rotura de caño", "tuberia": "Falta de agua, rotura de caño",
    # Rotura de semaforo
    "semaforo": "Rotura de semaforo", "semáforo": "Rotura de semaforo", "luz roja": "Rotura de semaforo", "luz verde": "Rotura de semaforo",
    # Fumigacion
    "fumigacion": "Fumigacion", "fumigar": "Fumigacion", "bichos": "Fumigacion", "plaga": "Fumigacion", "mosquitos": "Fumigacion", "insectos": "Fumigacion", "ratas": "Fumigacion", "cucarachas": "Fumigacion",
    # Riego de Calle
    "riego": "Riego de Calle", "regar": "Riego de Calle",
    # Castracion de mascota
    "castracion": "Castracion de mascota", "castración": "Castracion de mascota", "castrar": "Castracion de mascota", "mascota": "Castracion de mascota", "perro": "Castracion de mascota", "gato": "Castracion de mascota", "esterilizacion": "Castracion de mascota", "esterilización": "Castracion de mascota",
    # Inspeccion de comercio
    "inspeccion": "Inspeccion de comercio", "inspección": "Inspeccion de comercio", "comercio": "Inspeccion de comercio", "negocio": "Inspeccion de comercio", "habilitacion": "Inspeccion de comercio",
    # Tramites de Obras Privadas
    "obra": "Tramites de Obras Privadas", "construccion": "Tramites de Obras Privadas", "construcción": "Tramites de Obras Privadas", "plano": "Tramites de Obras Privadas",
    # Incendio
    "incendio": "Incendio", "fuego": "Incendio", "humo": "Incendio", "quema": "Incendio", "llamas": "Incendio",
}


def categorizar_reclamo_por_palabra_clave(texto_usuario: str) -> str:
    texto_normalizado = normalizar_texto(texto_usuario)

    # 1. Intentá match EXACTO con la lista de categorías ya normalizadas
    categorias_normalizadas = {normalizar_texto(cat): cat for cat in set(KEYWORD_TO_CATEGORY_MAP.values())}
    if texto_normalizado in categorias_normalizadas:
        return categorias_normalizadas[texto_normalizado]

    # 2. Si no, buscá por keywords
    for keyword, category in KEYWORD_TO_CATEGORY_MAP.items():
        if keyword in texto_normalizado:
            return category

    return "Otros"


# --- NUEVA FUNCIÓN-HERRAMIENTA: AGENDA DE EVENTOS ---

def consultar_eventos_culturales(fecha: str) -> str:
    """
    Consulta una agenda de eventos FAKE para una fecha dada.
    En un futuro, esto consultaría una base de datos real.
    """
    # Normalizamos la fecha que nos llega del LLM para poder buscarla.
    fecha_normalizada = normalizar_texto(fecha)
    
    # Cargamos la agenda desde la configuración del municipio
    agenda = cargar_configuracion_municipio(MUNICIPIO_ID, "agenda.json")
    if not isinstance(agenda, dict):
        agenda = {}
    
    eventos = agenda.get(fecha_normalizada)
    
    if eventos:
        lista_eventos = "\n".join(f"- {evento}" for evento in eventos)
        return f"Para la fecha '{fecha}', encontré los siguientes eventos:\n{lista_eventos}"
    else:
        # El LLM es bueno interpretando fechas, si nos pasa '15 de junio' y no lo tenemos, damos esta respuesta.
        return f"No encontré eventos programados específicamente para '{fecha}'. Puedes consultar la agenda completa en la web del municipio."


# --- ACTUALIZA TU TOOL_REGISTRY ASÍ ---

TOOL_REGISTRY = {
    "consultar_recoleccion_por_direccion": {
        "funcion": consultar_recoleccion_por_direccion,
        "descripcion": "Se usa para obtener los horarios y días de recolección de basura para una dirección específica.",
        "parametros": {
            "direccion": {
                "type": "string",
                "description": f"La dirección completa del lugar. Ejemplo: '{CONFIG_MUNICIPIO.get('ejemplo_direccion', 'Av. Siempreviva 123')}'."
            }
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    
    # --- NUEVA HERRAMIENTA REGISTRADA ---
    "consultar_eventos_culturales": {
        "funcion": consultar_eventos_culturales,
        "descripcion": "Consulta la agenda de eventos culturales, recitales o actividades municipales para una fecha específica, como 'hoy', 'mañana' o 'el sábado'.",
        "parametros": {
            "fecha": {"type": "string", "description": "La fecha de la consulta. Puede ser una palabra como 'hoy', 'mañana', 'este fin de semana', o una fecha específica como '15 de junio'."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    }
}

def log_uso_herramienta(nombre, usuario, parametros, resultado):
    logger.info(f"[USO_HERRAMIENTA] {nombre} | Usuario: {usuario} | Parámetros: {parametros} | Resultado: {resultado[:100]}")

def validar_y_formatear_direccion(direccion: str) -> dict | None:
    """
    Valida y formatea una dirección utilizando la API de Google Maps.
    """
    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return None

    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(direccion)}&key={Maps_API_KEY}&language=es"

    try:
        response = requests.get(geocode_url)
        response.raise_for_status()
        data = response.json()

        if data and data.get('status') == 'OK' and data.get('results'):
            best_result = data['results'][0]
            formatted_address = best_result.get('formatted_address')
            location = best_result['geometry']['location']
            lat, lng = location['lat'], location['lng']

            return {
                "formatted_address": formatted_address,
                "lat": lat,
                "lng": lng
            }
        else:
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error de conexión con Google API para geocoding ({direccion}): {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado en geocoding para {direccion}: {e}", exc_info=True)
        return None

def obtener_direccion_de_coordenadas(lat: float, lon: float) -> dict | None:
    """
    Obtiene una dirección formateada y componentes estructurados a partir de coordenadas lat/lon
    usando la API de Google Geocoding.
    """
    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return None

    reverse_geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&key={Maps_API_KEY}&language=es"

    try:
        response = requests.get(reverse_geocode_url)
        response.raise_for_status() # Lanza HTTPError para respuestas 4xx/5xx
        data = response.json()

        if data and data.get('status') == 'OK' and data.get('results'):
            # La primera resultado suele ser la más específica.
            best_result = data['results'][0]
            formatted_address = best_result.get('formatted_address')

            # Inicializar campos
            calle, numero, localidad, provincia, cp, barrio = "", "", "", "", "", ""

            for component in best_result.get('address_components', []):
                types = component.get('types', [])
                if 'street_number' in types:
                    numero = component['long_name']
                if 'route' in types: # 'route' suele ser el nombre de la calle
                    calle = component['long_name']
                # 'locality' es la ciudad/localidad principal. 'postal_town' puede ser un fallback.
                if 'locality' in types or 'postal_town' in types:
                    localidad = component['long_name']
                # 'administrative_area_level_1' suele ser la provincia/estado.
                if 'administrative_area_level_1' in types:
                    provincia = component['long_name']
                if 'postal_code' in types:
                    cp = component['long_name']
                if 'neighborhood' in types: # Barrio
                    barrio = component['long_name']

            # Si no se pudo extraer calle pero sí localidad, y la dirección formateada existe,
            # es posible que la dirección formateada contenga más detalles.
            # No intentaremos un parseo complejo de formatted_address aquí,
            # priorizamos los componentes estructurados.

            if formatted_address: # Devolver siempre si hay una dirección formateada
                return {
                    "formatted_address": formatted_address,
                    "calle": calle or None,
                    "numero": numero or None,
                    "localidad": localidad or None,
                    "provincia": provincia or None,
                    "codigo_postal": cp or None,
                    "barrio": barrio or None
                }
            # Si no hay formatted_address pero sí componentes mínimos (calle y localidad)
            elif calle and localidad:
                 # Construir una dirección formateada básica si es posible
                constructed_address_parts = []
                if calle: constructed_address_parts.append(calle)
                if numero: constructed_address_parts.append(numero)
                if localidad: constructed_address_parts.append(localidad)
                if provincia and localidad != provincia : constructed_address_parts.append(provincia) # Avoid "Junin, Junin"

                return {
                    "formatted_address": ", ".join(filter(None,constructed_address_parts)),
                    "calle": calle, "numero": numero, "localidad": localidad, "provincia": provincia,
                    "codigo_postal": cp, "barrio": barrio
                }
            else: # No hay suficiente información para una dirección útil
                logger.warning(f"Google API no devolvió dirección formateada ni componentes suficientes para {lat},{lon}.")
                return None

        else:
            logger.warning(f"Google API no pudo obtener dirección para {lat},{lon}. Status: {data.get('status')}, Error: {data.get('error_message', 'N/A')}")
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Error de conexión con Google API para reverse geocoding ({lat},{lon}): {e}")
        return None
    except Exception as e: # Captura errores de JSONDecodeError u otros inesperados
        logger.error(f"Error inesperado en reverse geocoding para {lat},{lon}: {e}", exc_info=True)
        return None
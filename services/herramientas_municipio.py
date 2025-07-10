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
    todas_las_categorias = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
    prompt = crear_prompt_sugerir_categorias(texto_usuario, todas_las_categorias)
    try:
        respuesta_llm = get_cohere_response(message=prompt, preamble="Eres un experto clasificador. Responde solo con el JSON solicitado.")
        resultado = json.loads(respuesta_llm)
        sugerencias = resultado.get("sugerencias", [])
        if isinstance(sugerencias, list) and sugerencias:
            return sugerencias[:3]
    except Exception as e:
        logger.error(f"[sugerir_categorias] Error al procesar sugerencias del LLM: {e}")
    # Fallback: usa matcher clásico si el LLM no responde bien
    fallback = categorizar_reclamo_por_palabra_clave(texto_usuario)
    return [fallback] if fallback and fallback != "Otros" else []
   
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
    default_localidad = municipio_config.get('ciudad_default', municipio_config.get('ciudad', 'N/A'))
    default_provincia = municipio_config.get('provincia_default', municipio_config.get('provincia', 'N/A'))

    logger.info(f"[ParseDireccion] Usando defaults para LLM - Localidad: '{default_localidad}', Provincia: '{default_provincia}' desde config: {municipio_config}")


    # If provincia is N/A (or not set) but ciudad field contains a comma, try to split them
    # This handles cases where config might have "ciudad": "Junín, Mendoza" or "ciudad_default": "Junín, Mendoza"
    if (default_provincia == 'N/A' or not default_provincia) and isinstance(default_localidad, str) and ',' in default_localidad:
        parts = default_localidad.split(',', 1)
        potential_localidad = parts[0].strip()
        potential_provincia = parts[1].strip()
        # Basic check if the split parts look plausible as localidad and provincia
        if len(potential_localidad) > 2 and len(potential_provincia) > 2:
            default_localidad = potential_localidad # Update local variables for the prompt
            default_provincia = potential_provincia
            logger.info(f"[ParseDireccion] Split 'default_localidad' from config into Localidad: {default_localidad}, Provincia: {default_provincia}")

    prompt = f"""
Eres un experto en interpretar direcciones en Argentina. Dada la siguiente DIRECCIÓN PROPORCIONADA, extráela en un formato JSON con los campos: "calle", "numero", "localidad", "provincia", "codigo_postal", "barrio", "otros_detalles".

Considera la siguiente información del municipio para el cual trabajas (si está disponible):
- Localidad principal: {default_localidad}
- Provincia principal: {default_provincia}

INSTRUCCIONES DETALLADAS:
1.  **Calle y Número**: Identificá claramente el nombre de la calle y el número de puerta.
2.  **Localidad y Provincia**:
    *   Si la DIRECCIÓN PROPORCIONADA incluye explícitamente una localidad y/o provincia, utilizá esas.
    *   Si la DIRECCIÓN PROPORCIONADA NO incluye localidad pero sí calle y número, y la calle y número parecen válidos para el contexto del municipio, podés ASUMIR la "Localidad principal" y "Provincia principal" del municipio si están definidas.
    *   Si la DIRECCIÓN PROPORCIONADA NO incluye provincia pero sí localidad, y la localidad es conocida en el contexto de la "Provincia principal", podés ASUMIR la "Provincia principal".
3.  **Código Postal, Barrio, Otros Detalles**: Extraelos si están presentes. Si no, dejalos como null o string vacío.
4.  **Formato de Salida**: Respondé ÚNICAMENTE con el objeto JSON. No incluyas explicaciones adicionales.
    *   Si un campo no se puede determinar, su valor debe ser `null` o un string vacío.
    *   Asegurate que el JSON esté bien formado.

EJEMPLOS:
- DIRECCIÓN PROPORCIONADA: "San Martín 123, Junín, Mendoza"
  (Asumiendo que el bot no tiene info de municipio_config o es genérico)
  RESPUESTA JSON: {{"calle": "San Martín", "numero": "123", "localidad": "Junín", "provincia": "Mendoza", "codigo_postal": null, "barrio": null, "otros_detalles": null}}

- DIRECCIÓN PROPORCIONADA: "Belgrano 456"
  (Asumiendo municipio_config: {{"ciudad": "Godoy Cruz", "provincia": "Mendoza"}})
  RESPUESTA JSON: {{"calle": "Belgrano", "numero": "456", "localidad": "Godoy Cruz", "provincia": "Mendoza", "codigo_postal": null, "barrio": null, "otros_detalles": null}}

- DIRECCIÓN PROPORCIONADA: "Rivadavia al 789, Ciudad"
  (Asumiendo municipio_config: {{"ciudad": "San Rafael", "provincia": "Mendoza"}})
  RESPUESTA JSON: {{"calle": "Rivadavia", "numero": "789", "localidad": "Ciudad", "provincia": "Mendoza", "codigo_postal": null, "barrio": null, "otros_detalles": null}}
  (Nota: "Ciudad" como localidad es común, el LLM debería tomarla si la provincia es Mendoza)

- DIRECCIÓN PROPORCIONADA: "esquina de Soler y Paraguay, Palermo"
  (Asumiendo municipio_config: {{"ciudad": "CABA", "provincia": "Buenos Aires"}})
  RESPUESTA JSON: {{"calle": "esquina de Soler y Paraguay", "numero": null, "localidad": "Palermo", "provincia": "Buenos Aires", "codigo_postal": null, "barrio": "Palermo", "otros_detalles": "esquina"}}

DIRECCIÓN PROPORCIONADA: "{texto_direccion}"

RESPUESTA JSON:
"""
    try:
        respuesta_llm = get_cohere_response(
            message=prompt,
            preamble="Sos un experto en extraer direcciones a formato JSON."
        )
        logger.info(f"[ParseDireccion] LLM response for address '{texto_direccion}': {respuesta_llm}")
        parsed_data = json.loads(respuesta_llm)

        # Validaciones básicas de la estructura devuelta
        if not isinstance(parsed_data, dict):
            logger.info(f"[ParseDireccion] LLM no devolvió un diccionario para: {texto_direccion}")
            return None

        # Asegurar que al menos calle y número O calle y otros_detalles (para esquinas) estén presentes
        calle = parsed_data.get("calle")
        numero = parsed_data.get("numero")
        otros_detalles = parsed_data.get("otros_detalles")

        if not calle:  # La calle es fundamental
            logger.info(f"[ParseDireccion] LLM no extrajo 'calle' para: {texto_direccion}")
            return None

        # Si no hay número, y 'otros_detalles' no indica una esquina o referencia válida, podría ser inválido.
        # Esta lógica puede ser más compleja. Por ahora, si hay calle, se considera un intento válido de parseo.
        # if not numero and not (otros_detalles and ("esquina" in otros_detalles.lower() or "entre" in otros_detalles.lower())):
        #     logger.warning(f"[ParseDireccion] LLM no extrajo 'numero' ni detalles de esquina válidos para: {texto_direccion}")
        #     return None

        # Normalizar campos opcionales a None si son strings vacíos
        for key in ["codigo_postal", "barrio", "otros_detalles", "numero", "localidad", "provincia"]:
            if key in parsed_data and parsed_data[key] == "":
                parsed_data[key] = None

        return parsed_data
    except json.JSONDecodeError:
        logger.error(f"[ParseDireccion] Error al decodificar JSON del LLM para dirección: {texto_direccion}. Respuesta LLM: {respuesta_llm}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"[ParseDireccion] Error inesperado al parsear dirección con LLM: {e}", exc_info=True)
        return None

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
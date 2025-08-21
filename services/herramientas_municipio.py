import json
import logging
import requests
import os
import unicodedata # <--- ¡Importante agregar esta línea!
import re
from numpy import mean
from services.config_loader import cargar_configuracion_municipio
from services.location_service import geocode_address
from services.google_text_to_speech import TextToSpeechService

# Instanciar el servicio de TTS
tts_service = TextToSpeechService()

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
    """Verifica si una dirección es válida utilizando el servicio de geocodificación."""
    if not texto:
        return False

    geocode_result = geocode_address(texto)
    return geocode_result is not None


def parse_direccion_completa(texto_direccion: str, municipio_config: dict = None) -> dict | None:
    """
    Placeholder function. LLM-based address parsing is deprecated from this helper.
    Geocoding functions are now the primary source for structured address data.
    """
    logger.info(f"Skipping deprecated LLM-based address parsing for: '{texto_direccion}'")
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


from services.google_search import google_search
from services.scraper_avanzado import extraer_noticias
from services.google_search import google_search
from geopy.distance import great_circle
import random

def _get_feature_center(feature: dict):
    """Calculates the center of a GeoJSON feature's geometry."""
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates")
    geom_type = geom.get("type")

    if not coords:
        return None

    if geom_type == 'Point':
        # Coords are [lon, lat]
        return (coords[1], coords[0]) # Return (lat, lon)
    elif geom_type == 'LineString':
        # Coords are [[lon1, lat1], [lon2, lat2], ...]
        # Return the mean of lats and lons
        lats = [p[1] for p in coords]
        lons = [p[0] for p in coords]
        return (mean(lats), mean(lons))
    elif geom_type == 'Polygon':
        # Coords are [[ [lon1, lat1], [lon2, lat2], ... ]]
        # Return the mean of lats and lons of the outer ring
        points = coords[0]
        lats = [p[1] for p in points]
        lons = [p[0] for p in points]
        return (mean(lats), mean(lons))
    return None


def consultar_estacionamiento(ubicacion: str) -> str:
    """
    Consulta la disponibilidad de estacionamiento simulada cerca de una ubicación.
    """
    logger.info(f"[HERRAMIENTA ESTACIONAMIENTO] Consultando para: '{ubicacion}'")

    # 1. Geocode user location
    user_coords = geocode_address(ubicacion)
    if not user_coords:
        return "No pude verificar la ubicación que me indicaste. ¿Podrías intentarlo de nuevo con más detalles?"

    user_lat_lon = (user_coords['lat'], user_coords['lng'])

    # 2. Load parking data
    # Assuming the file is per-municipality, but for now, we load a default.
    # A proper implementation would get municipio_id from context.
    municipio_id = "default"
    parking_data = cargar_configuracion_municipio(municipio_id, "estacionamiento.geojson")

    if not parking_data or not parking_data.get("features"):
        return "No tengo información sobre estacionamiento disponible en este momento."

    # 3. Find the closest parking feature
    closest_feature = None
    min_distance_km = float('inf')

    for feature in parking_data["features"]:
        center_coords = _get_feature_center(feature)
        if center_coords:
            distance = great_circle(user_lat_lon, center_coords).km
            if distance < min_distance_km:
                min_distance_km = distance
                closest_feature = feature

    # 4. Simulate and return result
    if closest_feature and min_distance_km < 2: # Only report if within 2km
        props = closest_feature["properties"]
        total_spots = props.get("total_spots", 0)
        occupancy_rate = props.get("base_occupancy_rate", 1.0)

        # Simulate some randomness
        occupied_spots = int(total_spots * occupancy_rate)
        random_factor = random.randint(-3, 3)
        occupied_spots += random_factor

        # Clamp values
        occupied_spots = max(0, min(total_spots, occupied_spots))

        available_spots = total_spots - occupied_spots

        if available_spots > 0:
            return f"En la zona de '{props.get('name', 'tu ubicación')}', hay aproximadamente {available_spots} lugares de estacionamiento disponibles."
        else:
            return f"La zona de '{props.get('name', 'tu ubicación')}' parece estar completa en este momento. Te sugiero buscar en calles aledañas."
    else:
        return "No encontré información de estacionamiento cerca de la ubicación que me indicaste."


# --- HERRAMIENTA DINÁMICA: AGENDA DE EVENTOS CON GOOGLE SEARCH ---

def consultar_publicaciones(context: dict = None, tipo_publicacion: str = 'general') -> dict:
    """
    Consulta las últimas publicaciones (noticias o eventos) desde el archivo JSON del municipio.
    """
    if not context or not context.get('user_obj'):
        return {"message_body": "No se pudo determinar el municipio para consultar las publicaciones."}

    municipio_id = context.get('user_obj').municipio_id
    if not municipio_id:
        return {"message_body": "Error: El usuario no está asociado a ningún municipio."}

    try:
        posts_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'municipios', str(municipio_id), 'posts.json')

        if not os.path.exists(posts_path):
            return {"message_body": "No hay publicaciones para mostrar en este momento.", "options_list": [], "message_type": "text"}

        with open(posts_path, 'r', encoding='utf-8') as f:
            content = f.read()
            all_posts = json.loads(content) if content else []

        if not all_posts:
            return {"message_body": "No hay publicaciones para mostrar en este momento.", "options_list": [], "message_type": "text"}

        # Filter by type if not 'general'
        if tipo_publicacion != 'general':
            filtered_posts = [p for p in all_posts if p.get('tipo') == tipo_publicacion]
        else:
            filtered_posts = all_posts

        if not filtered_posts:
            return {"message_body": f"No hay publicaciones del tipo '{tipo_publicacion}' para mostrar en este momento.", "options_list": [], "message_type": "text"}

        latest_posts = filtered_posts[:5]

        type_title = "publicaciones"
        if tipo_publicacion == 'news':
            type_title = "noticias"
        elif tipo_publicacion == 'event':
            type_title = "eventos"

        message = f"Aquí están las últimas {type_title} de la municipalidad:\n"
        buttons = []
        for post in latest_posts:
            message += f"\n- *{post.get('titulo')}*: {post.get('descripcion')}"
            if post.get('link'):
                buttons.append({
                    "texto": f"Ver '{post.get('titulo')}'",
                    "url": post.get('link'),
                    "type": "url"
                })

        return {
            "message_body": message,
            "options_list": buttons,
            "message_type": "interactive_buttons" if buttons else "text"
        }

    except Exception as e:
        logger.error(f"Error al leer el archivo de publicaciones para el municipio {municipio_id}: {e}", exc_info=True)
        return {"message_body": "Lo siento, hubo un problema al intentar obtener las publicaciones.", "options_list": [], "message_type": "text"}

def consultar_noticias_municipio() -> str:
    """
    Consulta las últimas noticias del municipio y las formatea para el usuario.
    """
    # URL hardcodeada temporalmente. Debería venir de la config del municipio.
    url_noticias = "https://www.juninmendoza.gov.ar/category/noticias/"

    resultado_scrape = extraer_noticias(url_noticias, limit=3)

    if "error" in resultado_scrape or not resultado_scrape.get("noticias"):
        error_msg = resultado_scrape.get("error", "No se encontraron noticias.")
        logger.warning(f"[HERRAMIENTA NOTICIAS] Falló el scrapeo: {error_msg}")
        # Fallback a un link genérico
        return (
            "No pude obtener las últimas noticias en este momento. "
            "Puedes consultarlas directamente en el sitio web: https://www.juninmendoza.gov.ar/noticias/"
        )

    mensaje = "Aquí están las últimas noticias de Junín Mendoza:\n\n"
    for i, noticia in enumerate(resultado_scrape.get("noticias", []), 1):
        mensaje += f"📰 *{noticia['titulo']}*\n"
        mensaje += f"   {noticia['link']}\n\n"

    return mensaje.strip()


def buscar_puntos_de_interes(
    rubro: str = None,
    tipo_lugar: str = None,
    localidad: str = None,
    opennow: bool = False,
    context: dict = None,
    ubicacion: str = None,
    tipo_negocio: str = None,
) -> str:
    """
    Busca puntos de interés cercanos a la ubicación del usuario.
    """
    # Aceptar sinónimos de parámetros usados por el LLM
    if not rubro:
        if tipo_lugar:
            rubro = tipo_lugar
        elif tipo_negocio:
            rubro = tipo_negocio

    if not localidad and ubicacion:
        localidad = ubicacion

    if context and context.get('last_search'):
        if not rubro:
            rubro = context['last_search'].get('rubro')
        if not localidad:
            localidad = context['last_search'].get('localidad')

    if not localidad:
        return "No tengo la localidad para buscar. Por favor, decime dónde querés buscar."

    if not rubro:
        return "Por favor, decime qué tipo de lugar o comercio estás buscando (por ejemplo, 'farmacia', 'ferretería', etc.)."

    logger.info(f"[HERRAMIENTA POI] Buscando puntos de interés para: rubro='{rubro}', localidad='{localidad}', opennow={opennow}")

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA POI] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return "Error de configuración: El servicio de mapas no está disponible en este momento."

    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(localidad)}&key={Maps_API_KEY}&language=es"

    try:
        response = requests.get(geocode_url)
        response.raise_for_status()
        data = response.json()

        if not data or data.get('status') != 'OK' or not data.get('results'):
            logger.warning(f"[HERRAMIENTA POI] La API de Google no pudo geocodificar la localidad: {localidad}")
            return f"No pude encontrar la localidad '{localidad}'. ¿Puedes ser más específico?"

        location = data['results'][0]['geometry']['location']
        lat, lng = location['lat'], location['lng']

        keyword_param = str(rubro) if rubro else ""
        places_url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius=5000&keyword={requests.utils.quote(keyword_param)}&key={Maps_API_KEY}&language=es"
        if opennow:
            places_url += "&opennow=true"

        response = requests.get(places_url)
        response.raise_for_status()
        data = response.json()

        if data and data.get('status') == 'OK' and data.get('results'):
            if context:
                context['last_search'] = {
                    'rubro': rubro,
                    'localidad': localidad,
                    'results': data['results']
                }

            page = context.get('last_search_page', 0) if context else 0
            start = page * 3
            end = start + 3
            results_to_show = data['results'][start:end]

            if not results_to_show:
                return "No hay más resultados para mostrar."

            mensaje = f"Encontré estos lugares para '{rubro}' cerca de tu ubicación:\n"
            for i, item in enumerate(results_to_show, 1):
                nombre = item.get('name')
                direccion = item.get('vicinity')
                place_id = item.get('place_id')
                maps_link = f"https://www.google.com/maps/place/?q=place_id:{place_id}"

                details_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,formatted_phone_number&key={Maps_API_KEY}&language=es"
                details_response = requests.get(details_url)
                details_data = details_response.json()
                telefono = details_data.get('result', {}).get('formatted_phone_number', 'No disponible')

                whatsapp_link = ""
                if telefono != 'No disponible':
                    telefono_numerico = ''.join(filter(str.isdigit, telefono))
                    if telefono_numerico:
                        whatsapp_link = f"https://wa.me/{telefono_numerico}"

                mensaje += f"{i}. {nombre}\n   Dirección: {direccion}\n   Tel: {telefono}\n"
                if whatsapp_link:
                    mensaje += f"   WhatsApp: {whatsapp_link}\n"
                mensaje += f"   Ver en mapa: {maps_link}\n"

            if len(data['results']) > end:
                mensaje += "\nSi querés ver más resultados, respondé 'más'."
                if context:
                    context['last_search_page'] = page + 1
            elif context:
                context.pop('last_search_page', None)


            return mensaje
        else:
            if opennow:
                return f"No encontré resultados para '{rubro}' abiertos en este momento en '{localidad}'. ¿Querés que te muestre todos igualmente?"
            else:
                return f"No encontré resultados para '{rubro}' en '{localidad}'."

    except requests.exceptions.RequestException as e:
        logger.error(f"Error de conexión con Google API para POI ({rubro}, {localidad}): {e}")
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."
    except Exception as e:
        logger.error(f"Error inesperado en búsqueda de POI para {rubro}, {localidad}: {e}", exc_info=True)
        return "Ocurrió un error inesperado al buscar los puntos de interés."

def log_uso_herramienta(nombre, usuario, parametros, resultado):
    logger.info(f"[USO_HERRAMIENTA] {nombre} | Usuario: {usuario} | Parámetros: {parametros} | Resultado: {resultado[:100]}")

def validar_y_formatear_direccion(direccion: str) -> dict | None:
    """
    Valida y formatea una dirección utilizando la API de Google Maps,
    con bias hacia Argentina.
    """
    if not Maps_API_KEY:
        logger.error("[GEO] Maps_API_KEY no configurada.")
        return None

    # Componentes para sesgar la búsqueda a Argentina
    params = {
        'address': direccion,
        'key': Maps_API_KEY,
        'language': 'es',
        'components': 'country:AR'
    }

    geocode_url = "https://maps.googleapis.com/maps/api/geocode/json"

    try:
        response = requests.get(geocode_url, params=params)
        response.raise_for_status()
        data = response.json()

        if data and data.get('status') == 'OK' and data.get('results'):
            best_result = data['results'][0]

            # Additional check: Does the result actually fall within a reasonable area?
            # This can prevent overly broad matches. For now, we trust Google's first result if status is OK.

            formatted_address = best_result.get('formatted_address')
            location = best_result['geometry']['location']
            lat, lng = location['lat'], location['lng']

            logger.info(f"[GEO] Dirección '{direccion}' geocodificada exitosamente a '{formatted_address}' ({lat}, {lng}).")

            return {
                "formatted_address": formatted_address,
                "lat": lat,
                "lng": lng
            }
        else:
            # Log the failure reason from Google
            status = data.get('status', 'N/A')
            error_message = data.get('error_message', 'No error message provided.')
            logger.warning(f"[GEO] Falla al geocodificar '{direccion}'. Status: {status}. Error: {error_message}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"[GEO] Error de conexión con Google API para geocoding ({direccion}): {e}")
        return None
    except Exception as e:
        logger.error(f"[GEO] Error inesperado en geocoding para {direccion}: {e}", exc_info=True)
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
    "consultar_publicaciones": {
        "funcion": consultar_publicaciones,
        "descripcion": "Consulta las últimas publicaciones, como noticias o eventos, del municipio.",
        "parametros": {
            "tipo_publicacion": {"type": "string", "description": "El tipo de publicación a buscar. Puede ser 'news' para noticias o 'event' para eventos. Si se omite, busca todo."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "consultar_noticias": {
        "funcion": consultar_noticias_municipio,
        "descripcion": "Consulta las 3 noticias más recientes del sitio web del municipio. No necesita parámetros.",
        "parametros": {},
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_puntos_de_interes": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca puntos de interés cercanos a la ubicación del usuario. Los puntos de interés pueden ser: veterinarias, farmacias, hospitales, etc.",
        "parametros": {
            "rubro": {"type": "string", "description": "El tipo de punto de interés a buscar. Por ejemplo: 'veterinaria', 'farmacia', 'hospital', etc."},
            "localidad": {"type": "string", "description": "La localidad donde se encuentra el usuario."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_poi": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca puntos de interés cercanos a la ubicación del usuario. Los puntos de interés pueden ser: veterinarias, farmacias, hospitales, etc.",
        "parametros": {
            "rubro": {"type": "string", "description": "El tipo de punto de interés a buscar. Por ejemplo: 'veterinaria', 'farmacia', 'hospital', etc."},
            "localidad": {"type": "string", "description": "La localidad donde se encuentra el usuario."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_comercios_por_rubro_y_localidad": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca comercios o servicios por rubro y localidad. Es una alias de 'buscar_puntos_de_interes'.",
        "parametros": {
            "rubro": {"type": "string", "description": "El rubro del comercio a buscar. Por ejemplo: 'veterinaria', 'ferretería', 'restaurante'."},
            "localidad": {"type": "string", "description": "La localidad donde buscar el comercio."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_comercios": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca comercios o servicios por rubro y localidad. Es una alias de 'buscar_puntos_de_interes'.",
        "parametros": {
            "rubro": {"type": "string", "description": "El rubro del comercio a buscar. Por ejemplo: 'veterinaria', 'ferretería', 'restaurante'."},
            "localidad": {"type": "string", "description": "La localidad donde buscar el comercio."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_lugares_cercanos": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Alias de 'buscar_puntos_de_interes'. Busca lugares de interés o comercios cercanos a una ubicación dada.",
        "parametros": {
            "rubro": {"type": "string", "description": "El tipo de lugar o comercio a buscar."},
            "localidad": {"type": "string", "description": "La ubicación o localidad para centrar la búsqueda."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_negocios_cercanos": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Alias de 'buscar_puntos_de_interes'. Busca negocios cercanos por rubro y ubicación.",
        "parametros": {
            "rubro": {"type": "string", "description": "El rubro o tipo de negocio a buscar."},
            "localidad": {"type": "string", "description": "La ubicación o localidad de referencia."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "consultar_estacionamiento": {
        "funcion": consultar_estacionamiento,
        "descripcion": "Consulta la disponibilidad de estacionamiento simulada cerca de una ubicación específica.",
        "parametros": {
            "ubicacion": {"type": "string", "description": "La dirección o punto de referencia donde el usuario quiere buscar estacionamiento. Ejemplo: 'Plaza de Junin' o 'San Martín y Lavalle'."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "generar_respuesta_audio": {
        "funcion": tts_service.synthesize_speech,
        "descripcion": "Convierte un texto a voz y devuelve la URL de un archivo de audio.",
        "parametros": {
            "text": {
                "type": "string",
                "description": "El texto que se convertirá a voz."
            }
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "google_search": {
        "funcion": google_search,
        "descripcion": "Busca en Google cuando ninguna otra herramienta es apropiada. Utilízala para consultas generales, buscar información específica o encontrar puntos de interés no cubiertos por otras herramientas.",
        "parametros": {
            "query": {"type": "string", "description": "La consulta de búsqueda precisa para Google."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    }
}
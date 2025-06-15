from services.cohere_ai import get_cohere_response # Asegúrate de poder importarlo aquí
import json
import logging
import requests
import os
import unicodedata # <--- ¡Importante agregar esta línea!
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
    # Obtenemos la lista única de todas las categorías posibles desde nuestro diccionario
    todas_las_categorias = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
    
    # Creamos el prompt especializado
    prompt = crear_prompt_sugerir_categorias(texto_usuario, todas_las_categorias)
    
    try:
        # Llamamos al LLM
        respuesta_llm = get_cohere_response(message=prompt, preamble="Eres un experto clasificador. Responde solo con el JSON solicitado.")
        
        # Parseamos la respuesta JSON
        resultado = json.loads(respuesta_llm)
        sugerencias = resultado.get("sugerencias", [])
        
        # Devolvemos solo las primeras 3 sugerencias, si las hay
        if isinstance(sugerencias, list):
            return sugerencias[:3]
            
    except (json.JSONDecodeError, TypeError, Exception) as e:
        logger.error(f"[sugerir_categorias] Error al procesar sugerencias del LLM: {e}")
        return [] # En caso de error, devolvemos una lista vacía

    return []
   
logger = logging.getLogger(__name__)
Maps_API_KEY = os.environ.get("Maps_API_KEY")
MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")
CONFIG_MUNICIPIO = cargar_configuracion_municipio(MUNICIPIO_ID, "config.json")


# --- NUEVA FUNCIÓN DE NORMALIZACIÓN ---
def normalizar_texto(texto: str) -> str:
    """
    Convierte un texto a minúsculas y le quita todos los acentos y diacríticos.
    Ej: "Árbol Caído" -> "arbol caido"
    """
    # NFD descompone los caracteres en su forma base y sus diacríticos (ej: 'á' -> 'a' + '´')
    forma_normalizada = unicodedata.normalize('NFD', texto)
    # Luego, nos quedamos solo con los caracteres que no son diacríticos (los ASCII)
    texto_sin_acentos = "".join(c for c in forma_normalizada if not unicodedata.combining(c))
    
    return texto_sin_acentos.lower()

# --- HERRAMIENTA 1: CONSULTA DE RECOLECCIÓN ---
def consultar_recoleccion_por_direccion(direccion: str) -> str:
    """
    Herramienta profesional que usa la API de Google Maps para geocodificar una dirección
    y luego determina el horario de recolección.
    """
    # ... (El código de esta función está perfecto, no necesita cambios)
    logger.info(f"[HERRAMIENTA GEO] Buscando horario para: '{direccion}'")

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps no configurada.")
        return "Error de configuración interna. No puedo acceder al servicio de mapas."

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
    "agua": "Falta de agua, rotura de caño", "caño": "Falta de agua, rotura de caño", "cano": "Falta de agua, rotura de caño", "perdida": "Falta de agua, rotura de caño", "fuga": "Falta de agua, rotura de caño", "rotura": "Falta de agua, rotura de caño",
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
        }
    },
    
    # --- NUEVA HERRAMIENTA REGISTRADA ---
    "consultar_eventos_culturales": {
        "funcion": consultar_eventos_culturales,
        "descripcion": "Consulta la agenda de eventos culturales, recitales o actividades municipales para una fecha específica, como 'hoy', 'mañana' o 'el sábado'.",
        "parametros": {
            "fecha": {"type": "string", "description": "La fecha de la consulta. Puede ser una palabra como 'hoy', 'mañana', 'este fin de semana', o una fecha específica como '15 de junio'."}
        }
    }
}
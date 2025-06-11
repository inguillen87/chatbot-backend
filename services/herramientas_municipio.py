# /app/bots/municipio/herramientas_municipio.py

import logging
import requests
import os

logger = logging.getLogger(__name__)
Maps_API_KEY = os.environ.get("Maps_API_KEY")

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

    if "junin" not in direccion.lower():
        direccion_completa = f"{direccion}, Junín, Mendoza"
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

# AJUSTE 1: Diccionario definido ANTES de la función que lo usa.
KEYWORD_TO_CATEGORY_MAP = {
    # Categoría: Luminaria
    "luminaria": "Luminaria", "luz": "Luminaria", "poste": "Luminaria",
    "foco": "Luminaria", "lampara": "Luminaria", "iluminacion": "Luminaria",
    "farol": "Luminaria",
    
    # Categoría: Arbol Caido
    "arbol": "Arbol Caido", "árbol": "Arbol Caido", "rama": "Arbol Caido",
    "gajo": "Arbol Caido",
    
    # Categoría: Limpieza
    "limpieza": "Limpieza", "basura": "Limpieza", "mugre": "Limpieza",
    "escombros": "Limpieza", "pasto": "Limpieza", "yuyos": "Limpieza",
    "desmalezamiento": "Limpieza", "baldío": "Limpieza",
    
    # Categoría: Arreglo de calle
    "bache": "Arreglo de calle", "calle": "Arreglo de calle", "asfalto": "Arreglo de calle",
    "vereda": "Arreglo de calle", "pozo": "Arreglo de calle", "rotura": "Arreglo de calle",
    "pavimento": "Arreglo de calle",
    
    # Categoría: Falta de agua, rotura de caño
    "agua": "Falta de agua, rotura de caño", "caño": "Falta de agua, rotura de caño",
    "perdida": "Falta de agua, rotura de caño", "fuga": "Falta de agua, rotura de caño",
    
    # Categoría: Rotura de semaforo
    "semaforo": "Rotura de semaforo", "semáforo": "Rotura de semaforo",
    
    # Categoría: Fumigacion
    "fumigacion": "Fumigacion", "fumigar": "Fumigacion", "bichos": "Fumigacion",
    "plaga": "Fumigacion", "mosquitos": "Fumigacion", "ratas": "Fumigacion",
    
    # Categoría: Riego de Calle
    "riego": "Riego de Calle", "regar": "Riego de Calle",
    
    # Categoría: Castracion de mascota
    "castracion": "Castracion de mascota", "castrar": "Castracion de mascota",
    "mascota": "Castracion de mascota", "perro": "Castracion de mascota",
    "gato": "Castracion de mascota",
    
    # Categoría: Inspeccion de comercio
    "inspeccion": "Inspeccion de comercio", "inspección": "Inspeccion de comercio",
    "comercio": "Inspeccion de comercio", "negocio": "Inspeccion de comercio",
    "habilitacion": "Inspeccion de comercio",
    
    # Categoría: Tramites de Obras Privadas
    "obra": "Tramites de Obras Privadas", "construccion": "Tramites de Obras Privadas",
    "plano": "Tramites de Obras Privadas",
}

def categorizar_reclamo_por_palabra_clave(texto_usuario: str) -> str:
    """
    Analiza el texto del usuario en busca de palabras clave para asignar una categoría.
    """
    texto_lower = texto_usuario.lower()
    for keyword, category in KEYWORD_TO_CATEGORY_MAP.items():
        if keyword in texto_lower:
            return category
            
    return "Otros"
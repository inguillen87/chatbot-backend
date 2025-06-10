import logging
import requests
import os

logger = logging.getLogger(__name__)

# La clave se lee de forma segura desde las variables de entorno de tu servidor
Maps_API_KEY = os.environ.get("Maps_API_KEY")

def consultar_recoleccion_por_direccion(direccion: str) -> str:
    """
    Herramienta profesional que usa la API de Google Maps para geocodificar una dirección
    y luego determina el horario de recolección.
    """
    logger.info(f"[HERRAMIENTA GEO] Buscando horario para: '{direccion}'")

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps no configurada.")
        return "Error de configuración interna. No puedo acceder al servicio de mapas."

    # Aseguramos que la dirección incluya la ciudad para mayor precisión
    if "junin" not in direccion.lower():
        direccion_completa = f"{direccion}, Junín, Mendoza"
    else:
        direccion_completa = direccion

    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(direccion_completa)}&key={Maps_API_KEY}"

    try:
        response = requests.get(geocode_url)
        response.raise_for_status()  # Lanza un error si la petición falla (ej: 4xx, 5xx)
        data = response.json()

        if not data or data['status'] != 'OK' or not data.get('results'):
            logger.warning(f"[HERRAMIENTA GEO] La API de Google no pudo geocodificar la dirección: {direccion}")
            return "No pude verificar esa dirección. ¿Puedes ser un poco más específico, incluyendo la ciudad?"

        # Obtenemos la latitud y longitud
        location = data['results'][0]['geometry']['location']
        lat, lng = location['lat'], location['lng']
        logger.info(f"[HERRAMIENTA GEO] Coordenadas para '{direccion}': Lat={lat}, Lng={lng}")

        # --- LÓGICA DE DECISIÓN BASADA EN COORDENADAS ---
        # Aquí es donde conectarías con el sistema real del municipio.
        # Por ahora, simulamos la lógica basada en zonas geográficas de Junín.

        # Ejemplo de polígono para la zona céntrica de Junín
        if -34.595 <= lat <= -34.580 and -60.955 <= lng <= -60.935:
            return f"Detecté que la dirección '{direccion}' está en la **zona céntrica**. Allí, la recolección es de **Lunes a Sábado por la noche (a partir de las 22:00 hs)**."
        
        # Ejemplo de polígono para el barrio "Villa Belgrano"
        elif -34.580 <= lat <= -34.570 and -60.935 <= lng <= -60.920:
             return f"Para la zona de **Villa Belgrano**, la recolección es los días **Martes, Jueves y Sábado por la mañana (a partir de las 08:00 hs)**."

        else:
            return "Según la ubicación, te corresponde el servicio de recolección zonal. Los días son **Lunes, Miércoles y Viernes por la noche (a partir de las 21:00 hs)**. Te recomiendo confirmarlo en la web del municipio."

    except requests.exceptions.RequestException as e:
        logger.error(f"[HERRAMIENTA GEO] Error de conexión con la API de Google: {e}")
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."
import os
from geopy.geocoders import GoogleV3
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import logging

logger = logging.getLogger(__name__)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

def reverse(lat, lon):
    """
    Obtiene la dirección a partir de latitud y longitud usando Google Maps.
    Returns a dictionary with structured address components.
    """
    if not GOOGLE_API_KEY:
        logger.error("La API key de Google Maps no está configurada.")
        return None

    try:
        geolocator = GoogleV3(api_key=GOOGLE_API_KEY)
        location = geolocator.reverse((lat, lon), exactly_one=True, language="es")

        if not location or not location.raw or 'address_components' not in location.raw:
            logger.warning(f"Respuesta incompleta de geolocalización para {lat},{lon}")
            return None

        address_components = location.raw['address_components']

        def get_component(component_type):
            for component in address_components:
                if component_type in component['types']:
                    return component['long_name']
            return None

        calle = get_component('route')
        altura = get_component('street_number')
        distrito = get_component('locality') or get_component('administrative_area_level_2')

        return {
            "direccion": location.address,
            "distrito": distrito,
            "calle": calle,
            "altura": altura
        }

    except GeocoderTimedOut:
        logger.error("Timeout al llamar a la API de geolocalización.")
        return None
    except GeocoderServiceError as e:
        logger.error(f"Error en el servicio de geolocalización: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado en geolocalización: {e}")
        return None

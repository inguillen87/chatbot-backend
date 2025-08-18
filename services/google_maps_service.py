import os
from geopy.geocoders import GoogleV3
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import logging

logger = logging.getLogger(__name__)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

def obtener_direccion_de_coordenadas(lat, lon):
    """
    Obtiene la dirección a partir de latitud y longitud usando Google Maps.
    """
    if not GOOGLE_API_KEY:
        logger.error("La API key de Google Maps no está configurada.")
        return None

    try:
        geolocator = GoogleV3(api_key=GOOGLE_API_KEY)
        location = geolocator.reverse((lat, lon), exactly_one=True)
        return location.address
    except GeocoderTimedOut:
        logger.error("Timeout al llamar a la API de geolocalización.")
        return None
    except GeocoderServiceError as e:
        logger.error(f"Error en el servicio de geolocalización: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado en geolocalización: {e}")
        return None

def get_coordinates(address: str):
    """
    Obtiene las coordenadas (latitud, longitud) de una dirección.
    """
    if not GOOGLE_API_KEY:
        logger.error("La API key de Google Maps no está configurada.")
        return None

    try:
        geolocator = GoogleV3(api_key=GOOGLE_API_KEY)
        location = geolocator.geocode(address)
        if location:
            return {"lat": location.latitude, "lon": location.longitude}
        else:
            logger.warning(f"No se pudieron encontrar coordenadas para la dirección: {address}")
            return None
    except GeocoderTimedOut:
        logger.error("Timeout al llamar a la API de geolocalización.")
        return None
    except GeocoderServiceError as e:
        logger.error(f"Error en el servicio de geolocalización: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado en geolocalización: {e}")
        return None

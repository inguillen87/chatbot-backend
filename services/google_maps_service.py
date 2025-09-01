import os
import logging

from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from geopy.geocoders import GoogleV3, Nominatim, MapTiler

logger = logging.getLogger(__name__)


def _get_geolocators():
    """Return geolocators ordered by priority for geocoding calls."""
    locators = []
    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        locators.append(GoogleV3(api_key=google_key))
    else:
        logger.warning("La API key de Google Maps no está configurada.")

    maptiler_key = os.getenv("MAPTILER_API_KEY")
    if maptiler_key:
        locators.append(MapTiler(api_key=maptiler_key))
    else:
        logger.warning("La API key de MapTiler no está configurada.")

    locators.append(Nominatim(user_agent="chatboc"))
    return locators


def obtener_direccion_de_coordenadas(lat, lon):
    """Obtiene la dirección a partir de latitud y longitud."""
    for geolocator in _get_geolocators():
        try:
            location = geolocator.reverse((lat, lon), exactly_one=True)
            if location:
                return location.address
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            logger.warning(
                "Error en geolocalizador %s: %s", geolocator.__class__.__name__, e
            )
        except Exception as e:
            logger.error(
                "Error inesperado en geolocalizador %s: %s",
                geolocator.__class__.__name__,
                e,
            )
    logger.error("No se pudo obtener la dirección para las coordenadas")
    return None


def get_coordinates(address: str):
    """Obtiene las coordenadas (latitud, longitud) de una dirección."""
    for geolocator in _get_geolocators():
        try:
            location = geolocator.geocode(address)
            if location:
                return {"lat": location.latitude, "lon": location.longitude}
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            logger.warning(
                "Error en geolocalizador %s: %s", geolocator.__class__.__name__, e
            )
        except Exception as e:
            logger.error(
                "Error inesperado en geolocalizador %s: %s",
                geolocator.__class__.__name__,
                e,
            )
    logger.warning(
        "No se pudieron encontrar coordenadas para la dirección: %s", address
    )
    return None

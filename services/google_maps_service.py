import logging
import os

from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import GoogleV3, MapTiler, Nominatim

from services.llm_provider_network_policy import llm_provider_network_allowed


logger = logging.getLogger(__name__)


class GoogleMapsService:
    """Backward-compatible holder for the configured Google API key."""

    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")


servicio_google_maps = GoogleMapsService()


def _get_geolocators():
    """Return geolocators ordered by priority for geocoding calls."""

    if not llm_provider_network_allowed("geocoding"):
        logger.info("Geocoding providers skipped reason=test_network_disabled")
        return []

    locators = []
    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        locators.append(GoogleV3(api_key=google_key))
    else:
        logger.warning("Google geocoder unavailable reason=missing_api_key")

    maptiler_key = os.getenv("MAPTILER_API_KEY")
    if maptiler_key:
        locators.append(MapTiler(api_key=maptiler_key))
    else:
        logger.warning("MapTiler geocoder unavailable reason=missing_api_key")

    locators.append(Nominatim(user_agent="chatboc"))
    return locators


def _log_geocoder_failure(geolocator, exc: Exception) -> None:
    logger.warning(
        "Geocoder request failed provider=%s error_type=%s",
        geolocator.__class__.__name__,
        type(exc).__name__,
    )


def obtener_direccion_de_coordenadas(lat, lon):
    """Resolve an address for coordinates without logging coordinate values."""

    for geolocator in _get_geolocators():
        try:
            location = geolocator.reverse((lat, lon), exactly_one=True)
            if location:
                return location.address
        except (GeocoderTimedOut, GeocoderServiceError) as exc:
            _log_geocoder_failure(geolocator, exc)
        except Exception as exc:
            logger.error(
                "Unexpected geocoder failure provider=%s error_type=%s",
                geolocator.__class__.__name__,
                type(exc).__name__,
            )

    logger.warning(
        "Coordinates could not be reverse geocoded has_coordinates=%s",
        lat is not None and lon is not None,
    )
    return None


def get_coordinates(address: str):
    """Resolve coordinates for an address without logging the address value."""

    for geolocator in _get_geolocators():
        try:
            location = geolocator.geocode(address)
            if location:
                return {"lat": location.latitude, "lon": location.longitude}
        except (GeocoderTimedOut, GeocoderServiceError) as exc:
            _log_geocoder_failure(geolocator, exc)
        except Exception as exc:
            logger.error(
                "Unexpected geocoder failure provider=%s error_type=%s",
                geolocator.__class__.__name__,
                type(exc).__name__,
            )

    logger.warning(
        "Address could not be geocoded input_chars=%s",
        len(str(address or "")),
    )
    return None

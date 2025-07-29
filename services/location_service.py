import logging
import os
import googlemaps

logger = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

def get_gmaps_client():
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY not set.")
        return None
    return googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

def geocode_address(address):
    """
    Geocodes an address using the Google Geocoding API.

    Args:
        address (str): The address to geocode.

    Returns:
        dict: A dictionary containing the geocoding results, or None if an error occurs.
    """
    gmaps = get_gmaps_client()
    if not gmaps:
        return None

    try:
        geocode_result = gmaps.geocode(address)
        if geocode_result:
            return geocode_result[0]
        return None
    except Exception as e:
        logger.error(f"Error geocoding address: {e}", exc_info=True)
        return None

def find_nearby_places(location, keyword, radius=1500):
    """
    Finds nearby places using the Google Places API.

    Args:
        location (dict): A dictionary containing the latitude and longitude.
        keyword (str): The keyword to search for.
        radius (int): The search radius in meters.

    Returns:
        list: A list of nearby places, or None if an error occurs.
    """
    gmaps = get_gmaps_client()
    if not gmaps:
        return None

    try:
        places_result = gmaps.places_nearby(
            location=location,
            keyword=keyword,
            radius=radius
        )
        return places_result.get("results", [])
    except Exception as e:
        logger.error(f"Error finding nearby places: {e}", exc_info=True)
        return None

import re
from typing import Optional, Tuple

def extract_coordinates_from_google_maps_url(url: str) -> Optional[Tuple[float, float]]:
    """Extract latitude and longitude from a Google Maps URL.

    Supports URLs of the form:
    - https://maps.google.com/maps?q=lat,lon
    - https://maps.google.com/maps/search/.../@lat,lon,zoom
    Returns a tuple (lat, lon) if found, otherwise None.
    """
    if not url:
        return None
    patterns = [
        r"maps\.google\.com.*[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
        r"maps\.google\.com.*@(-?\d+\.\d+),(-?\d+\.\d+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            try:
                lat = float(match.group(1))
                lon = float(match.group(2))
                return lat, lon
            except ValueError:
                continue
    return None

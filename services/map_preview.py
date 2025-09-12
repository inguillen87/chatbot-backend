from __future__ import annotations

"""Utility for generating static map previews.

This module currently uses the public OpenStreetMap static map service.
It returns a URL that can be used to display a static map centered on the
provided latitude and longitude, along with a short alt text for
accessibility.
"""

from typing import Tuple


BASE_URL = "https://staticmap.openstreetmap.de/staticmap.php"


def generate_static_map(lat: float, lon: float) -> Tuple[str, str]:
    """Return a static map image URL and alt text.

    Parameters
    ----------
    lat: float
        Latitude of the map center.
    lon: float
        Longitude of the map center.

    Returns
    -------
    Tuple[str, str]
        A tuple ``(url, alt_text)`` where ``url`` points to a static map image
        and ``alt_text`` describes the image for screen readers.
    """
    url = (
        f"{BASE_URL}?center={lat},{lon}&zoom=15&size=600x400"
        f"&markers={lat},{lon},red-pushpin"
    )
    alt_text = f"Mapa de la ubicación ({lat}, {lon})"
    return url, alt_text

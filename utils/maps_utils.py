import re
from typing import Optional, Tuple


def extraer_coordenadas_de_url_google_maps(url: str) -> Optional[Tuple[float, float]]:
    """Extrae latitud y longitud desde una URL de Google Maps.

    Soporta URLs del tipo:
    - https://maps.google.com/maps?q=lat,lon
    - https://maps.google.com/maps/search/.../@lat,lon,zoom
    Devuelve una tupla ``(latitud, longitud)`` si se encuentran los
    valores, en caso contrario ``None``.
    """
    if not url:
        return None
    patrones = [
        r"maps\.google\.com.*[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
        r"maps\.google\.com.*@(-?\d+\.\d+),(-?\d+\.\d+)",
    ]
    for patron in patrones:
        match = re.search(patron, url)
        if match:
            try:
                latitud = float(match.group(1))
                longitud = float(match.group(2))
                return latitud, longitud
            except ValueError:
                continue
    return None

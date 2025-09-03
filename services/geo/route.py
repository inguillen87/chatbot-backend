import logging
from typing import Tuple, Dict, Any
import requests

logger = logging.getLogger(__name__)
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"

def obtener_ruta(origen: Tuple[float, float], destino: Tuple[float, float]) -> Dict[str, Any] | None:
    """Obtiene una ruta entre dos puntos usando la API pública de OSRM.

    Args:
        origen: Tupla ``(lat, lon)`` con el punto de partida.
        destino: Tupla ``(lat, lon)`` con el punto de llegada.

    Returns:
        Diccionario con la distancia en metros, la duración en segundos y una lista
        de pares ``[lat, lon]`` que representan la ruta, o ``None`` si falla.
    """
    try:
        url = OSRM_URL.format(lon1=origen[1], lat1=origen[0], lon2=destino[1], lat2=destino[0])
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        routes = data.get("routes")
        if not routes:
            return None
        route = routes[0]
        coords = route.get("geometry", {}).get("coordinates", [])
        path = [[lat, lon] for lon, lat in coords]
        return {
            "distancia_m": route.get("distance"),
            "duracion_s": route.get("duration"),
            "ruta": path,
        }
    except Exception as e:
        logger.error("Error obteniendo ruta: %s", e)
        return None

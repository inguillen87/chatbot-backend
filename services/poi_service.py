import logging
from typing import Any, Dict, List, Optional

from services.location_service import find_nearby_places

logger = logging.getLogger(__name__)


def nearby(
    lat: float,
    lon: float,
    *,
    keyword: Optional[str] = None,
    place_type: Optional[str] = None,
    radius: int = 2000,
    open_now: bool = False,
) -> Optional[List[Dict[str, Any]]]:
    if lat is None or lon is None:
        return []

    term = keyword or place_type
    if not term:
        return []

    results = find_nearby_places({"lat": lat, "lng": lon}, term, radius=radius)
    if results is None:
        return None
    results = results or []
    if open_now:
        results = [
            item
            for item in results
            if item.get("opening_hours", {}).get("open_now") is True
        ]
    return results

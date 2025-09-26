import math
from typing import List, Dict, Any
import random

def _dist_m(lat1, lon1, lat2, lon2):
    R=6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

def seleccionar_camara_para_ubicacion(lat, lon, cams: List[Dict[str, Any]]):
    """Return the closest camera within a 5 km radius."""

    mejor = None
    dmin = 5000
    for c in cams:
        d = _dist_m(lat, lon, c["lat"], c["lon"])
        if d < dmin:
            dmin = d
            mejor = c
    return mejor if dmin < 5000 else None

def evaluar_ocupacion_rois(detecciones, rois_geojson):
    """detecciones = [{x1,y1,x2,y2}] en pixeles; rois con image_size y polygons en px"""
    import shapely.geometry as geom

    res=[]
    for feat in rois_geojson["features"]:
        poly = geom.Polygon(feat["geometry"]["coordinates"][0])
        ocupado=False
        for bb in detecciones:
            rect = geom.Polygon([[bb["x1"],bb["y1"]],[bb["x2"],bb["y1"]],[bb["x2"],bb["y2"]],[bb["x1"],bb["y2"]]])
            if poly.intersects(rect):
                ocupado=True; break
        res.append({
            "id": feat["properties"]["id"],
            "label": feat["properties"]["label"],
            "libre": (not ocupado),
        })
    return res

def simular_detecciones(
    rois_geojson: Dict[str, Any],
    ocupacion_estimacion: float,
    seed: str | None = None,
) -> List[Dict[str, float]]:
    """Simulate car detections for a set of ROIs.

    Args:
        rois_geojson: GeoJSON definition of the ROIs.
        ocupacion_estimacion: Expected occupancy probability (0-1).
        seed: Optional deterministic seed so consecutive calls within a
            window yield coherent results.

    Returns:
        A list of bounding boxes representing detected vehicles.
    """

    rng = random.Random(seed) if seed is not None else random.Random()
    ocupacion = max(0.0, min(1.0, ocupacion_estimacion or 0.0))
    detecciones: List[Dict[str, float]] = []

    for feat in rois_geojson.get("features", []):
        if rng.random() < ocupacion:
            poly_coords = feat["geometry"]["coordinates"][0]
            min_x = min(p[0] for p in poly_coords)
            max_x = max(p[0] for p in poly_coords)
            min_y = min(p[1] for p in poly_coords)
            max_y = max(p[1] for p in poly_coords)

            center_x = rng.uniform(min_x, max_x)
            center_y = rng.uniform(min_y, max_y)

            deteccion = {
                "x1": center_x - 6,
                "y1": center_y - 6,
                "x2": center_x + 6,
                "y2": center_y + 6,
            }
            detecciones.append(deteccion)

    return detecciones

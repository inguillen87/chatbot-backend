import math
from typing import List, Dict
import random

def _dist_m(lat1, lon1, lat2, lon2):
    R=6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

def seleccionar_camara_para_ubicacion(lat, lon, cams: List[Dict]):
    # simple: elegir la más cercana dentro de 5 km
    mejor = None; dmin = 5000
    for c in cams:
        d = _dist_m(lat, lon, c["lat"], c["lon"])
        if d < dmin:
            dmin = d; mejor = c
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

def simular_detecciones(rois_geojson: Dict) -> List[Dict]:
    """
    Simula detecciones de autos para un conjunto de ROIs.
    Genera una detección para aproximadamente el 50% de los ROIs.
    """
    detecciones = []
    for feat in rois_geojson["features"]:
        # Simular con un 50% de probabilidad que el lugar está ocupado
        if random.random() < 0.5:
            poly_coords = feat["geometry"]["coordinates"][0]

            # Encontrar el bounding box del polígono para generar un punto dentro
            min_x = min(p[0] for p in poly_coords)
            max_x = max(p[0] for p in poly_coords)
            min_y = min(p[1] for p in poly_coords)
            max_y = max(p[1] for p in poly_coords)

            # Generar un punto aleatorio dentro del bounding box del ROI
            # y crear una pequeña detección (bounding box) alrededor de ese punto.
            center_x = random.uniform(min_x, max_x)
            center_y = random.uniform(min_y, max_y)

            # Crear un bounding box de 10x10 alrededor del centro
            deteccion = {
                "x1": center_x - 5, "y1": center_y - 5,
                "x2": center_x + 5, "y2": center_y + 5
            }
            detecciones.append(deteccion)

    return detecciones

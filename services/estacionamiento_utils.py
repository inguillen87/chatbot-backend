import math
from typing import List, Dict

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

import hashlib
import math
import random
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

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


def _normalizar_texto(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    return re.sub(r"[^a-z0-9\s]", " ", texto)


def _tokenizar(texto: str) -> List[str]:
    return [token for token in _normalizar_texto(texto).split() if len(token) > 2]


def _build_camera_metadata(cam: Dict[str, Any]) -> Dict[str, Any]:
    keywords: List[str] = []
    for field in ("nombre", "ubicacion_referencia"):
        keywords.extend(_tokenizar(cam.get(field, "")))
    for alias in cam.get("alias", []) or []:
        keywords.extend(_tokenizar(alias))
    for calle in cam.get("calles", []) or []:
        keywords.extend(_tokenizar(calle))

    weights: Dict[str, float] = {}
    for kw in keywords:
        weights[kw] = weights.get(kw, 0.0) + 1.0

    joined = " ".join(sorted(set(keywords)))

    return {
        "keywords": set(keywords),
        "weights": weights,
        "joined": joined,
        "city": _normalizar_texto(cam.get("ciudad", "")),
        "municipio": _normalizar_texto(cam.get("municipio", "")),
    }


def _score_match(texto_norm: str, metadata: Dict[str, Any]) -> float:
    score = 0.0
    matched = 0
    for kw, weight in metadata["weights"].items():
        if kw and kw in texto_norm:
            score += weight
            matched += 1
    if metadata.get("city") and metadata["city"] in texto_norm:
        score += 1.8
    if metadata.get("municipio") and metadata["municipio"] in texto_norm:
        score += 1.2
    if matched:
        ratio = SequenceMatcher(None, texto_norm, metadata["joined"]).ratio()
        score += ratio * 0.8
    return score


def _desplazar_por_metros(lat: float, lon: float, distancia: float, angulo_grados: float) -> tuple[float, float]:
    if not distancia:
        return lat, lon
    rad = math.radians(angulo_grados)
    delta_lat = (distancia * math.cos(rad)) / 111_320.0
    denom = max(math.cos(math.radians(lat)), 0.00001)
    delta_lon = (distancia * math.sin(rad)) / (111_320.0 * denom)
    return lat + delta_lat, lon + delta_lon


def generar_punto_cercano(cam: Dict[str, Any], texto: str) -> tuple[float, float]:
    """Genera una ubicación simulada dentro del radio de cobertura de la cámara."""

    cobertura = cam.get("cobertura_m") or 450
    try:
        cobertura = float(cobertura)
    except (TypeError, ValueError):
        cobertura = 450.0

    coverage = max(120.0, min(cobertura, 1200.0))
    hash_input = f"{cam.get('id')}-{texto}".encode("utf-8", "ignore")
    seed_value = int(hashlib.sha256(hash_input).hexdigest()[:12], 16)
    rng = random.Random(seed_value)
    distancia = rng.uniform(coverage * 0.15, coverage * 0.6)
    angulo = rng.uniform(0, 360)
    return _desplazar_por_metros(cam["lat"], cam["lon"], distancia, angulo)


def aproximar_coordenadas_por_texto(
    texto: str,
    cams: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Intenta estimar coordenadas para una consulta textual sin usar Google."""

    texto_norm = _normalizar_texto(texto)
    if not texto_norm.strip():
        return None

    mejor_cam: Optional[Dict[str, Any]] = None
    mejor_meta: Optional[Dict[str, Any]] = None
    mejor_score = 0.0

    for cam in cams:
        meta = _build_camera_metadata(cam)
        score = _score_match(texto_norm, meta)
        if score > mejor_score:
            mejor_score = score
            mejor_cam = cam
            mejor_meta = meta

    if not mejor_cam or mejor_score < 1.2:
        return None

    lat, lon = generar_punto_cercano(mejor_cam, texto_norm)
    matched_keywords = sorted(
        kw for kw in (mejor_meta["keywords"] if mejor_meta else []) if kw in texto_norm
    )
    confidence = max(0.4, min(0.95, 0.35 + mejor_score / 4.5))

    return {
        "lat": lat,
        "lon": lon,
        "camera": mejor_cam,
        "confidence": confidence,
        "matched_keywords": matched_keywords,
        "score": mejor_score,
    }

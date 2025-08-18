import os, json, time
from typing import Dict, Any
from pathlib import Path
from services.google_maps_service import get_coordinates
from services.logging_config import get_logger
from services.vision_estacionamiento import analizar_frame_y_contar_autos
from services.estacionamiento_utils import seleccionar_camara_para_ubicacion, evaluar_ocupacion_rois

logger = get_logger(__name__)
BASE = Path(__file__).resolve().parents[1]
CAM_FILE = BASE / "data/estacionamiento/camaras.json"

with open(CAM_FILE, "r", encoding="utf-8") as f:
    CAMARAS = json.load(f)

def consultar_ocupacion(ubicacion_texto_o_coord: Any) -> Dict[str, Any]:
    """
    Input: texto ("San Martín 1200, Junín") o dict {"lat":..., "lon":...}
    Output: dict con texto + lista de segmentos libres/ocupados.
    """
    if isinstance(ubicacion_texto_o_coord, dict) and "lat" in ubicacion_texto_o_coord:
        user_lat, user_lon = ubicacion_texto_o_coord["lat"], ubicacion_texto_o_coord["lon"]
    else:
        coords = get_coordinates(str(ubicacion_texto_o_coord))
        if not coords:
            return {"texto": "No pude ubicar esa dirección. Probá con calle y altura (ej.: San Martín 1200)."}
        user_lat, user_lon = coords["lat"], coords["lon"]

    cam = seleccionar_camara_para_ubicacion(user_lat, user_lon, CAMARAS)
    if not cam:
        return {"texto": "Por ahora no tengo cámaras cerca de esa zona. Probá con otra dirección."}

    # Extraer frame (cada ~10s) -> analizar con visión
    from services.video_frame import obtener_frame_png
    frame_png = obtener_frame_png(cam["url"], fps_interval=10)  # devuelve bytes PNG
    if not frame_png:
        return {"texto": "No pude obtener video en este momento. Probá más tarde."}

    # Cargar ROIs
    with open(cam["rois_file"], "r", encoding="utf-8") as fr:
        rois = json.load(fr)

    detecciones = analizar_frame_y_contar_autos(frame_png)  # devuelve lista de bboxes [{"x1":..,"y1":..,"x2":..,"y2":..}]
    resumen = evaluar_ocupacion_rois(detecciones, rois)

    libres = sum(1 for s in resumen if s["libre"])
    ocupados = sum(1 for s in resumen if not s["libre"])
    ts = time.strftime("%H:%M:%S")

    texto = (
        f"🅿️ *Ocupación estimada* ({cam['nombre']})\n"
        f"• Libres: {libres}\n• Ocupados: {ocupados}\n• {ts}\n\n"
        "Segmentos:\n" + "\n".join([f"- {s['label']}: {'LIBRE ✅' if s['libre'] else 'OCUPADO ❌'}" for s in resumen])
    )
    return {"texto": texto}

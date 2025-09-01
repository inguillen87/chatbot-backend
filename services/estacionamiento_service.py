import os, json, time
from typing import Dict, Any
from pathlib import Path
from services.google_maps_service import get_coordinates
from services.logging_config import get_logger
from services.estacionamiento_utils import seleccionar_camara_para_ubicacion, evaluar_ocupacion_rois, simular_detecciones

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

    # Cargar ROIs usando rutas relativas al directorio base del proyecto.
    # En algunos entornos la aplicación puede ejecutarse con un *cwd*
    # diferente al repositorio, lo que provocaba `FileNotFoundError` al
    # intentar abrir `cam["rois_file"]` directamente.  Construimos la ruta
    # absoluta respecto a ``BASE`` para que siempre se encuentre el archivo
    # de regiones de interés.
    rois_path = BASE / cam["rois_file"]
    with open(rois_path, "r", encoding="utf-8") as fr:
        rois = json.load(fr)

    # Simular detecciones en lugar de analizar un frame real
    detecciones = simular_detecciones(rois)
    resumen = evaluar_ocupacion_rois(detecciones, rois)

    libres = sum(1 for s in resumen if s["libre"])
    ocupados = sum(1 for s in resumen if not s["libre"])
    ts = time.strftime("%H:%M:%S")

    texto = (
        f"🅿️ *Ocupación estimada (Simulación)* ({cam['nombre']})\n"
        f"• Libres: {libres}\n• Ocupados: {ocupados}\n• {ts}\n\n"
        "Segmentos:\n" + "\n".join([f"- {s['label']}: {'LIBRE ✅' if s['libre'] else 'OCUPADO ❌'}" for s in resumen])
    )
    return {
        "texto": texto,
        "camera": cam.get("nombre"),
        "libres": libres,
        "ocupados": ocupados,
        "timestamp": ts,
        "segmentos": resumen,
    }

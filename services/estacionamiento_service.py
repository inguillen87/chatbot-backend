import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from services.logging_config import get_logger
from services.estacionamiento_utils import (
    seleccionar_camara_para_ubicacion,
    evaluar_ocupacion_rois,
    simular_detecciones,
    _dist_m,
    aproximar_coordenadas_por_texto,
)
from services.openai_maps_service import geocodificar_texto_llm

logger = get_logger(__name__)
BASE = Path(__file__).resolve().parents[1]
CAM_FILE = BASE / "data/estacionamiento/camaras.json"

with open(CAM_FILE, "r", encoding="utf-8") as f:
    CAMARAS = json.load(f)


def _parse_time_to_minutes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # interpret as hour in 24h format (e.g., 13.5 -> 13:30)
        hours = int(value)
        minutes = int((value - hours) * 60)
        return hours * 60 + minutes
    if isinstance(value, str):
        try:
            pieces = value.strip().split(":")
            if len(pieces) == 1:
                hours = int(pieces[0])
                minutes = 0
            else:
                hours = int(pieces[0])
                minutes = int(pieces[1])
            return hours * 60 + minutes
        except ValueError:
            return None
    return None


def _esta_en_rango(now: datetime, inicio: Any, fin: Any) -> bool:
    start_minutes = _parse_time_to_minutes(inicio)
    end_minutes = _parse_time_to_minutes(fin)
    if start_minutes is None or end_minutes is None:
        return False
    current = now.hour * 60 + now.minute
    if start_minutes <= end_minutes:
        return start_minutes <= current <= end_minutes
    # Horario pasa por medianoche
    return current >= start_minutes or current <= end_minutes


def _factor_por_horario(cam: Dict[str, Any], now: datetime) -> float:
    factor = 1.0
    slots = cam.get("horarios_pico") or []
    if not isinstance(slots, list):
        return factor

    weekday = now.weekday()
    is_weekend = weekday >= 5
    nombre_dia = now.strftime("%A").lower()

    for slot in slots:
        if not isinstance(slot, dict):
            continue

        dias = slot.get("dias")
        if dias:
            dias_normalizados = [str(d).lower() for d in dias]
            if "weekday" in dias_normalizados and weekday >= 5:
                continue
            if "weekend" in dias_normalizados and weekday < 5:
                continue
            if (
                "weekday" not in dias_normalizados
                and "weekend" not in dias_normalizados
                and nombre_dia not in dias_normalizados
            ):
                continue

        if not _esta_en_rango(now, slot.get("start") or slot.get("hora_inicio"), slot.get("end") or slot.get("hora_fin")):
            continue

        slot_factor = slot.get("factor") or 1.0
        try:
            factor *= float(slot_factor)
        except (TypeError, ValueError):
            continue

    if is_weekend:
        try:
            weekend_factor = float(cam.get("factor_fin_de_semana"))
        except (TypeError, ValueError):
            weekend_factor = 1.0
        factor *= weekend_factor or 1.0

    return factor


def _estimar_ocupacion_y_confianza(
    cam: Dict[str, Any], now: datetime, distance_m: float
) -> Tuple[float, float]:
    base = cam.get("ocupacion_base", 0.55)
    try:
        base = float(base)
    except (TypeError, ValueError):
        base = 0.55

    factor = _factor_por_horario(cam, now)
    ocupacion_estimada = base * factor

    cobertura = cam.get("cobertura_m")
    try:
        cobertura = float(cobertura) if cobertura is not None else None
    except (TypeError, ValueError):
        cobertura = None

    penalizacion = 0.0
    if cobertura:
        exceso = max(distance_m - cobertura, 0)
        if exceso > 0:
            penalizacion = min(0.6, exceso / (cobertura * 1.5))
            ocupacion_estimada *= max(0.3, 1 - penalizacion)

    rng_seed = f"{cam.get('id') or cam.get('nombre')}-{now.strftime('%Y%m%d%H')}"
    # Variación leve para que la simulación parezca viva
    jitter = (hash(rng_seed) % 1000) / 1000.0
    jitter = (jitter - 0.5) * 0.12  # +-6%
    ocupacion_estimada += ocupacion_estimada * jitter

    ocupacion_estimada = max(0.05, min(0.95, ocupacion_estimada))
    confianza = max(0.35, 1 - penalizacion * 0.85)

    return ocupacion_estimada, confianza

def _extraer_coordenadas(ubicacion: Dict[str, Any]) -> Tuple[float | None, float | None]:
    """Acepta distintos formatos de coordenadas y las normaliza a floats."""

    if not isinstance(ubicacion, dict):
        return None, None

    lat_candidates = (
        ubicacion.get("lat"),
        ubicacion.get("latitude"),
        ubicacion.get("latitud"),
    )
    lon_candidates = (
        ubicacion.get("lon"),
        ubicacion.get("lng"),
        ubicacion.get("longitude"),
        ubicacion.get("longitud"),
    )

    lat = next((value for value in lat_candidates if value not in (None, "")), None)
    lon = next((value for value in lon_candidates if value not in (None, "")), None)

    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        return None, None

    return lat_f, lon_f


def _normalizar_consulta_textual(texto: str) -> str:
    if not texto:
        return ""
    normalized = texto.replace("_", " ")
    normalized = " ".join(normalized.split())
    return normalized.strip()


def _es_comando_sin_direccion(texto: str) -> bool:
    if not texto:
        return True
    lowered = texto.lower()
    comandos = {
        "buscar_estacionamiento",
        "buscar estacionamiento",
        "compartir ubicacion",
        "compartir ubicación",
        "compartir tu ubicacion",
    }
    if lowered in comandos:
        return True
    if "_" in texto and lowered.replace("_", " ") in comandos:
        return True
    # Evitar tratar como dirección cadenas de una sola palabra genérica
    tokens = lowered.split()
    if len(tokens) == 1 and tokens[0] in {"buscar", "estacionamiento", "ubicacion"}:
        return True
    return False


def _confianza_desde_precision(precision_m: Any) -> float | None:
    try:
        precision = float(precision_m)
    except (TypeError, ValueError):
        return None
    if precision <= 0:
        return 0.95
    if precision >= 500:
        return 0.45
    ratio = max(0.0, min(1.0, 1 - (precision / 500)))
    return 0.45 + (0.5 * ratio)


def consultar_ocupacion(ubicacion_texto_o_coord: Any) -> Dict[str, Any]:
    """
    Input: texto ("San Martín 1200, Junín") o dict {"lat":..., "lon":...}
    Output: dict con texto + lista de segmentos libres/ocupados.
    """
    user_lat = None
    user_lon = None
    geocode_source = "direct"
    geocode_confidence = None
    matched_cam = None
    matched_reference = None

    texto = None
    ubicacion_dict = ubicacion_texto_o_coord if isinstance(ubicacion_texto_o_coord, dict) else None

    if ubicacion_dict:
        user_lat, user_lon = _extraer_coordenadas(ubicacion_dict)
        if user_lat is not None and user_lon is not None:
            geocode_source = "provided"
            matched_reference = (
                ubicacion_dict.get("address")
                or ubicacion_dict.get("formatted_address")
                or ubicacion_dict.get("description")
            )
            if not matched_reference:
                matched_reference = ubicacion_dict.get("label")
            geocode_confidence = _confianza_desde_precision(
                ubicacion_dict.get("accuracy")
                or ubicacion_dict.get("precision")
            )
        else:
            texto = (
                ubicacion_dict.get("address")
                or ubicacion_dict.get("formatted_address")
                or ubicacion_dict.get("description")
                or ubicacion_dict.get("label")
            )
    else:
        texto = str(ubicacion_texto_o_coord or "").strip()

    if user_lat is None or user_lon is None:
        texto_str = texto or ""
        texto_normalizado = _normalizar_consulta_textual(texto_str)
        if _es_comando_sin_direccion(texto_normalizado):
            return {
                "texto": "Necesito una calle, altura o que compartas tu ubicación para recomendarte dónde estacionar.",
                "geocode_source": "invalid_query",
            }
        if texto_str.strip():
            heuristica = aproximar_coordenadas_por_texto(texto_str, CAMARAS)
            if heuristica:
                user_lat = heuristica["lat"]
                user_lon = heuristica["lon"]
                matched_cam = heuristica.get("camera")
                geocode_confidence = heuristica.get("confidence")
                geocode_source = "camaras_heuristica"
                matched_reference = ", ".join(heuristica.get("matched_keywords", [])) or None
        if user_lat is None and texto_str.strip():
            llm_result = geocodificar_texto_llm(
                texto_str,
                puntos_referencia=CAMARAS,
            )
            if llm_result:
                try:
                    user_lat = float(llm_result.get("lat"))
                    user_lon = float(llm_result.get("lon"))
                    geocode_confidence = llm_result.get("confidence")
                    matched_reference = llm_result.get("matched_reference")
                    geocode_source = "openai_llm"
                except (TypeError, ValueError):
                    user_lat = user_lon = None

    if user_lat is None or user_lon is None:
        logger.info("No se pudo resolver la ubicación para estacionamiento a partir de %s", ubicacion_texto_o_coord)
        return {"texto": "No pude ubicar esa dirección. Probá con calle y altura (ej.: San Martín 1200)."}

    cam = matched_cam or seleccionar_camara_para_ubicacion(user_lat, user_lon, CAMARAS)
    if not cam:
        return {"texto": "Por ahora no tengo cámaras cerca de esa zona. Probá con otra dirección."}

    distance_m = _dist_m(user_lat, user_lon, cam["lat"], cam["lon"])

    # Cargar ROIs usando rutas relativas al directorio base del proyecto.
    rois_path = BASE / cam["rois_file"]
    with open(rois_path, "r", encoding="utf-8") as fr:
        rois = json.load(fr)

    ahora = datetime.now()
    ocupacion_prob, confianza = _estimar_ocupacion_y_confianza(cam, ahora, distance_m)

    # Simular detecciones en lugar de analizar un frame real
    slice_seed = int(time.time() // 300)  # ventanas de 5 minutos
    seed = f"{cam.get('id') or cam.get('nombre')}-{slice_seed}"
    detecciones = simular_detecciones(rois, ocupacion_prob, seed=seed)
    resumen = evaluar_ocupacion_rois(detecciones, rois)

    libres = sum(1 for s in resumen if s["libre"])
    ocupados = sum(1 for s in resumen if not s["libre"])
    ts = ahora.strftime("%H:%M:%S")

    ubicacion_referencia = cam.get("ubicacion_referencia") or ""
    distancia_txt = "{:.1f} km".format(distance_m / 1000) if distance_m >= 1000 else f"{int(distance_m)} m"
    confianza_txt = f"{int(confianza * 100)}%"

    segmentos_txt = "\n".join(
        [f"- {s['label']}: {'LIBRE ✅' if s['libre'] else 'OCUPADO ❌'}" for s in resumen]
    )

    texto = (
        "🅿️ *Ocupación estimada con cámaras municipales (demo)*\n"
        f"{cam['nombre']}" + (f" · {ubicacion_referencia}" if ubicacion_referencia else "") + "\n"
        f"Distancia a tu ubicación: {distancia_txt}\n"
        f"Confianza de la estimación: {confianza_txt}\n"
        f"Libres: {libres} · Ocupados: {ocupados} (actualizado {ts})\n\n"
        "Segmentos observados:\n" + segmentos_txt
    )
    if cam.get("url"):
        texto += f"\nFuente demo: {cam['url']}"
    if cam.get("demo_frame"):
        texto += f"\nImagen de referencia: {cam['demo_frame']}"

    return {
        "texto": texto,
        "camera": cam.get("nombre"),
        "libres": libres,
        "ocupados": ocupados,
        "timestamp": ts,
        "segmentos": resumen,
        "confidence": confianza,
        "distance_m": distance_m,
        "demo_frame": cam.get("demo_frame"),
        "reference_location": ubicacion_referencia,
        "occupancy_probability": ocupacion_prob,
        "geocode_source": geocode_source,
        "geocode_confidence": geocode_confidence,
        "matched_reference": matched_reference,
    }

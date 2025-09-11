import io
import requests
from typing import Dict, Any

from services.vision_fallback_service import analyze_image_smart
from services.llm_utils import extract_complaint_details_llm

try:  # optional dependency for video frame extraction
    import imageio.v2 as imageio
except Exception:  # pragma: no cover - fallback when imageio not installed
    imageio = None


def _twilio_fetch_media(url: str, sid: str, token: str) -> bytes:
    resp = requests.get(url, auth=(sid, token), timeout=15)
    resp.raise_for_status()
    return resp.content


def _derive_details(text: str, default_desc: str) -> Dict[str, str]:
    details = extract_complaint_details_llm(text or "") or {}
    categoria = details.get("tipo_problema") or "Otros"
    descripcion = details.get("descripcion_problema") or text.strip()[:300] or default_desc
    return {"category": categoria, "description": descripcion}


def analyze_image_from_url(url: str, sid: str, token: str) -> Dict[str, Any]:
    """Download an image and extract category/description via vision + LLM."""
    try:
        raw = _twilio_fetch_media(url, sid, token)
        vision = analyze_image_smart(raw)
        text = vision.get("text", "")
        details = _derive_details(text, "Foto adjunta de la situación.")
        return {
            "es_reclamo": True,
            "categoria_sugerida": details["category"],
            "descripcion_sugerida": details["description"],
            "texto_ocr": text,
            "evidence": "image",
            "text": text,
        }
    except Exception as e:  # pragma: no cover - network or vision errors
        return {
            "es_reclamo": True,
            "categoria_sugerida": "Otros",
            "descripcion_sugerida": "Foto adjunta de la situación.",
            "texto_ocr": "",
            "evidence": "image",
            "error": str(e),
        }


def analyze_video_from_url(url: str, sid: str, token: str) -> Dict[str, Any]:
    """Extract the first frame from a video and run image analysis on it."""
    try:
        raw = _twilio_fetch_media(url, sid, token)
        if not imageio:
            raise RuntimeError("imageio not available")
        reader = imageio.get_reader(io.BytesIO(raw), format="ffmpeg")
        frame = reader.get_data(0)
        buf = io.BytesIO()
        imageio.imwrite(buf, frame, format="jpeg")
        img_bytes = buf.getvalue()
        vision = analyze_image_smart(img_bytes)
        text = vision.get("text", "")
        details = _derive_details(text, "Video adjunto de la situación.")
        return {
            "es_reclamo": True,
            "categoria_sugerida": details["category"],
            "descripcion_sugerida": details["description"],
            "texto_ocr": text,
            "evidence": "video",
            "text": text,
        }
    except Exception as e:  # pragma: no cover - fallback for missing deps
        return {
            "es_reclamo": True,
            "categoria_sugerida": "Otros",
            "descripcion_sugerida": "Video adjunto de la situación.",
            "texto_ocr": "",
            "evidence": "video",
            "error": str(e),
        }

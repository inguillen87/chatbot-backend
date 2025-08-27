import base64
import json
import logging
import os
from typing import Any, Dict, Optional

from openai import OpenAI
import cohere

from .google_vision_service import analyze_image_from_content as analyze_google

logger = logging.getLogger(__name__)

def _call_openai(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Analyze an image using OpenAI's vision models."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        client = OpenAI(api_key=api_key)
        prompt = (
            "Describe the image for a municipal complaint system. "
            "Return a JSON with keys: labels (list of keywords), "
            "objects (list of main objects) and text (string with any text found)."
        )
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image": {"data": b64, "mime_type": "image/jpeg"}}
                ]
            }],
            max_output_tokens=300
        )
        text = response.output[0].content[0].text
        return json.loads(text)
    except Exception as e:
        logger.error(f"OpenAI Vision failed: {e}", exc_info=True)
        return None

def _call_cohere(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Analyze an image using Cohere's multimodal API."""
    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        co = cohere.Client(api_key)
        prompt = (
            "Describe the image for a municipal complaint system. "
            "Return JSON with keys: labels, objects, text."
        )
        resp = co.chat(
            model="command-r-plus",
            message=prompt,
            images=[{"base64": b64}]
        )
        return json.loads(resp.text)
    except Exception as e:
        logger.error(f"Cohere vision failed: {e}", exc_info=True)
        return None

def _normalize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert provider-agnostic result into the format used in the app."""
    labels = ["" if l is None else l for l in result.get("labels", [])]
    objects = ["" if o is None else o for o in result.get("objects", [])]
    normalized: Dict[str, Any] = {
        "labels": [{"description": lbl} for lbl in labels if lbl],
        "objects": [{"name": obj} for obj in objects if obj],
    }
    text = result.get("text") or ""
    if text:
        normalized["full_text_annotation"] = {"description": text}
    return normalized


def analyze_image_smart(image_bytes: bytes) -> Dict[str, Any]:
    """Analyze image bytes using OpenAI, then Cohere, then Google Vision."""
    result = _call_openai(image_bytes)
    if result:
        return _normalize_result(result)
    logger.warning("Falling back to Cohere vision...")
    result = _call_cohere(image_bytes)
    if result:
        return _normalize_result(result)
    logger.warning("Falling back to Google Vision service...")
    return analyze_google(image_bytes)

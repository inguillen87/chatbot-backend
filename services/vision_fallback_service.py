import base64
import json
import logging
import os
import re
from ast import literal_eval
from typing import Any, Dict, Optional
import httpx
from openai import OpenAI
import cohere

logger = logging.getLogger(__name__)

def _ensure_json_prompt(prompt: str) -> str:
    suffix = "\nResponde solo JSON válido sin texto adicional."
    if suffix.strip().lower() in prompt.lower():
        return prompt
    return f"{prompt}{suffix}"

def _safe_json_loads(text: str) -> Dict[str, Any]:
    def _strip_code_fences(payload: str) -> str:
        if not payload:
            return payload
        fenced = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)
        match = fenced.search(payload)
        return match.group(1) if match else payload

    candidates = []
    if text:
        text_stripped = _strip_code_fences(text.strip())
        candidates.append(text_stripped)
        candidates.append(re.sub(r"[\x00-\x1f]", " ", text_stripped))

        obj_start = text_stripped.find("{")
        obj_end = text_stripped.rfind("}")
        if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
            candidates.append(text_stripped[obj_start : obj_end + 1])

        arr_start = text_stripped.find("[")
        arr_end = text_stripped.rfind("]")
        if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
            candidates.append(text_stripped[arr_start : arr_end + 1])

    def _attempt(payload: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(payload, strict=False)
        except json.JSONDecodeError:
            cleaned = re.sub(r"[\x00-\x1f]", " ", payload)
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            try:
                return json.loads(cleaned, strict=False)
            except json.JSONDecodeError:
                try:
                    return literal_eval(payload)
                except Exception:
                    return None

    for cand in candidates:
        res = _attempt(cand)
        if isinstance(res, (dict, list)):
            return res if isinstance(res, dict) else {"data": res}

    logger.warning("No se pudo parsear JSON válido desde la respuesta del modelo.")
    return None

def _call_openai(image_bytes: bytes, custom_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Analyze an image using OpenAI and return structured JSON."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = OpenAI(api_key=api_key, http_client=http_client)
        prompt = custom_prompt or (
            "Describe the image for a municipal complaint system. "
            "Return JSON with keys: labels, objects, text."
        )
        prompt = _ensure_json_prompt(prompt)

        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        message = completion.choices[0].message
        content = message.content
        logger.info(f"DEBUG RAW OPENAI CONTENT: {content}")
        if not content:
            raise ValueError("No content returned from OpenAI")
        return _safe_json_loads(content)
    except Exception as e:
        logger.error(f"OpenAI Vision failed: {e}", exc_info=True)
        return None


def _call_openai_image_text(image_bytes: bytes, custom_prompt: Optional[str] = None) -> Optional[str]:
    """Extract raw text from an image using OpenAI (no JSON enforcement)."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = OpenAI(api_key=api_key, http_client=http_client)
        prompt = custom_prompt or (
            "Extrae TODO el texto visible de la imagen respetando saltos de línea. "
            "No agregues explicaciones."
        )

        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            max_tokens=4096,
        )
        message = completion.choices[0].message
        content = message.content
        return content.strip() if content else None
    except Exception as exc:
        logger.error("OpenAI OCR failed: %s", exc, exc_info=True)
        return None


def _call_openai_text(text: str, custom_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Analyze text using OpenAI and return structured JSON."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None
    try:
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = OpenAI(api_key=api_key, http_client=http_client)
        prompt = custom_prompt or (
            "Extrae la tabla del catálogo en JSON con claves "
            "'columns' (lista de strings) y 'rows' (lista de listas ordenadas según columns). "
            "No inventes datos, deja vacío si no se ve."
        )
        prompt = _ensure_json_prompt(prompt)

        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": f"{prompt}\n\n{str(text)}",
            }],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        message = completion.choices[0].message
        content = message.content
        logger.info(f"DEBUG RAW OPENAI CONTENT: {content}")
        if not content:
            raise ValueError("No content returned from OpenAI")
        return _safe_json_loads(content)
    except Exception as e:
        logger.error(f"OpenAI text analysis failed: {e}", exc_info=True)
        return None

def _call_cohere(image_bytes: bytes, custom_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Analyze an image using Cohere's multimodal API."""
    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        co = cohere.Client(api_key)
        prompt = custom_prompt or (
            "Describe the image for a municipal complaint system. "
            "Return JSON with keys: labels, objects, text."
        )
        try:
            resp = co.chat(
                model="command-r-plus",
                message=prompt,
                images=[{"data": b64, "mime_type": "image/jpeg"}],
            )
            text = resp.text
        except TypeError:
            # Older SDKs may not support the images parameter; fall back to generate()
            resp = co.generate(
                model="command-r-plus",
                prompt=prompt,
                image_url=f"data:image/jpeg;base64,{b64}",
            )
            text = resp.generations[0].text
        return json.loads(text)
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


def analyze_image_smart(image_bytes: bytes, prompt: Optional[str] = None) -> Dict[str, Any]:
    """Analyze image bytes using OpenAI, then Cohere."""
    result = _call_openai(image_bytes, custom_prompt=prompt)
    if result:
        return _normalize_result(result)
    logger.warning("Falling back to Cohere vision...")
    result = _call_cohere(image_bytes, custom_prompt=prompt)
    if result:
        return _normalize_result(result)
    logger.error("All vision providers failed")
    return {"labels": [], "objects": []}


def analyze_image_structured(image_bytes: bytes, prompt: str) -> Optional[Dict[str, Any]]:
    """Analyze image bytes and return provider JSON without normalization."""
    result = _call_openai(image_bytes, custom_prompt=prompt)
    if result:
        return result
    logger.warning("Structured vision failed for OpenAI; skipping Cohere structured fallback.")
    logger.error("All structured vision providers failed")
    return None


def analyze_image_text(image_bytes: bytes, prompt: Optional[str] = None) -> Optional[str]:
    """Extract raw text from image bytes."""
    return _call_openai_image_text(image_bytes, custom_prompt=prompt)


def analyze_text_structured(text: str, prompt: str) -> Optional[Dict[str, Any]]:
    """Analyze text and return provider JSON without normalization."""
    result = _call_openai_text(text, custom_prompt=prompt)
    if result:
        return result
    logger.error("Structured text analysis failed")
    return None

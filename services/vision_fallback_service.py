import os
import base64
import json
import logging
from typing import Optional, Dict, Any, List
import httpx
from openai import OpenAI
import cohere

# Configure logger
logger = logging.getLogger(__name__)

# --- Schemas ---

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Lista de etiquetas descriptivas de la imagen (ej. 'bache', 'árbol caído')."
        },
        "objects": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Lista de objetos detectados en la imagen."
        },
        "text": {
            "type": "string",
            "description": "Texto legible extraído de la imagen, si lo hay."
        }
    },
    "required": ["labels", "objects", "text"],
    "additionalProperties": False
}

TABLE_SCHEMA_ONLY = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Nombres de las columnas detectadas."
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {"type": "null"}
                    ]
                }
            },
            "description": "Filas de datos. Cada fila debe tener el mismo número de elementos que 'columns'."
        }
    },
    "required": ["columns", "rows"],
    "additionalProperties": False
}

# --- Helper Functions ---

def _safe_json_loads(text: str) -> Optional[Dict[str, Any]]:
    """Clean markdown code blocks and parse JSON."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove first line (```json) and last line (```)
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1])
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse JSON from LLM: {text[:100]}...")
        return None

def _ensure_json_prompt(prompt: str) -> str:
    """Append instruction to force JSON if not present."""
    if "json" not in prompt.lower():
        return f"{prompt} Respond ONLY with valid JSON."
    return prompt

def _openai_model():
    """Return the preferred OpenAI model."""
    # Use gpt-4o for best vision/JSON performance
    return "gpt-4o"

def _call_openai(
    image_bytes: bytes,
    custom_prompt: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Analyze image using OpenAI Vision with Structured Outputs.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None

    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = OpenAI(api_key=api_key, http_client=http_client)

        prompt = custom_prompt or "Analyze this image."
        model = _openai_model()

        # Determine max_tokens. gpt-4o supports up to 16k output,
        # but we set a safe high limit to avoid truncation of large tables.
        max_tokens = 16384

        # Construct the response_format for Structured Outputs
        # Note: 'json_schema' requires strict=True for 100% adherence.
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "vision_payload",
                "schema": schema or VISION_SCHEMA,
                "strict": True,
            },
        }

        completion = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            max_tokens=max_tokens,
            temperature=0,
            response_format=response_format,
        )

        message = completion.choices[0].message
        content = message.content

        if not content:
            # Should not happen with successful 200 OK and strict JSON
            logger.warning("OpenAI returned empty content.")
            return None

        return json.loads(content)

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

        model = _openai_model()

        completion = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            max_tokens=4096,
        )
        return completion.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("OpenAI OCR failed: %s", exc, exc_info=True)
        return None


def _call_openai_text(
    text: str,
    custom_prompt: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
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

        model = _openai_model()
        schema = schema or TABLE_SCHEMA_ONLY
        max_tokens = 16384

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "catalog_table",
                "schema": schema,
                "strict": True,
            },
        }

        completion = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": f"{prompt}\n\n{str(text)}",
            }],
            max_tokens=max_tokens,
            temperature=0,
            response_format=response_format,
        )

        content = completion.choices[0].message.content
        if not content:
            raise ValueError("No content returned from OpenAI")

        return json.loads(content)

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
            resp = co.generate(
                model="command-r-plus",
                prompt=prompt,
                image_url=f"data:image/jpeg;base64,{b64}",
            )
            text = resp.generations[0].text

        # Cohere doesn't guarantee JSON, so we use safe loads
        return _safe_json_loads(text)
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
    result = _call_openai(image_bytes, custom_prompt=prompt, schema=VISION_SCHEMA)
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
    result = _call_openai(image_bytes, custom_prompt=prompt, schema=TABLE_SCHEMA_ONLY)
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
    result = _call_openai_text(text, custom_prompt=prompt, schema=TABLE_SCHEMA_ONLY)
    if result:
        return result
    logger.error("Structured text analysis failed")
    return None

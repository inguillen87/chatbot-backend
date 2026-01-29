import base64
import json
import logging
import os
import re
from typing import Any, Dict, Optional
import httpx
from openai import OpenAI
import cohere

logger = logging.getLogger(__name__)

def _call_openai(image_bytes: bytes, custom_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Analyze an image using OpenAI's vision models."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        # Use a client that ignores proxy environment variables to avoid
        # `Client.__init__()` receiving unsupported arguments.
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = OpenAI(api_key=api_key, http_client=http_client)
        prompt = custom_prompt or (
            "Describe la imagen en español para un sistema de reclamos municipales. "
            "Devuelve un JSON con las claves: labels (lista de palabras clave en español), "
            "objects (lista de objetos principales en español) y text (cadena con cualquier texto encontrado en español)."
        )

        # Use the modern Responses API when available; otherwise fall back
        # to chat completions for older OpenAI client versions. If the
        # Responses API call fails for any reason, attempt the chat
        # completions path.
        text = ""
        if hasattr(client, "responses"):
            try:
                response = client.responses.create(
                    model="gpt-4.1-mini",
                    input=[{
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image": {"data": b64, "mime_type": "image/jpeg"}},
                        ],
                    }],
                    max_output_tokens=300,
                )
                text = response.output[0].content[0].text
            except Exception as exc:
                logger.warning("Responses API unavailable (%s); falling back to chat completions", exc)

        if not text:
            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
                max_tokens=300,
            )
            message = completion.choices[0].message
            # ``message`` may be a dict (old SDK) or a pydantic object (new SDK)
            content = (
                message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            )
            if isinstance(content, list):
                parts = []
                for part in content:
                    parts.append(
                        part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                    )
                text = "".join(parts)
            else:
                text = content

        if not text:
            raise ValueError("No content returned from OpenAI")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise
    except Exception as e:
        logger.error(f"OpenAI Vision failed: {e}", exc_info=True)
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
            # Older SDKs may not support the ``images`` parameter; fall back to generate()
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

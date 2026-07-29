from __future__ import annotations

import base64
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from services import vision_fallback_service

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
REMOTE_IMAGE_TIMEOUT = (3.05, 10.0)
MAX_REMOTE_REDIRECTS = 3
ALLOWED_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

_MULTIMODAL_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "nombre": {"type": ["string", "null"]},
        "cantidad": {"type": ["number", "string", "null"]},
        "descripcion": {"type": ["string", "null"]},
        "marca": {"type": ["string", "null"]},
        "concepto": {"type": ["string", "null"]},
    },
    "required": ["nombre", "cantidad", "descripcion", "marca", "concepto"],
}

MULTIMODAL_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {
            "anyOf": [
                {"type": "string", "enum": ["crear_reclamo", "invalido"]},
                {"type": "null"},
            ]
        },
        "data": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "categoria": {"type": ["string", "null"]},
                        "descripcion": {"type": ["string", "null"]},
                    },
                    "required": ["categoria", "descripcion"],
                },
                {"type": "null"},
            ]
        },
        "items": {"type": "array", "items": _MULTIMODAL_ITEM_SCHEMA},
        "productos": {"type": "array", "items": _MULTIMODAL_ITEM_SCHEMA},
    },
    "required": ["intent", "data", "items", "productos"],
}


class UnsafeImageSource(ValueError):
    """The image source is unsafe, unsupported, or exceeds local limits."""


def _normalise_content_type(value: object) -> str:
    content_type = str(value or "").split(";", 1)[0].strip().lower()
    return "image/jpeg" if content_type == "image/jpg" else content_type


def _sniff_image_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _assert_public_remote_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeImageSource("remote image URL must use HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeImageSource("remote image URL credentials are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise UnsafeImageSource("local image hosts are not allowed")

    try:
        addresses = socket.getaddrinfo(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, ValueError) as exc:
        raise UnsafeImageSource("remote image host could not be resolved") from exc

    if not addresses:
        raise UnsafeImageSource("remote image host has no addresses")
    for address in addresses:
        raw_ip = str(address[4][0]).split("%", 1)[0]
        try:
            resolved_ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise UnsafeImageSource("remote image host resolved unexpectedly") from exc
        if not resolved_ip.is_global:
            raise UnsafeImageSource("remote image host resolved to a non-public address")


def _bounded_response_body(response: requests.Response) -> tuple[bytes, str]:
    declared_type = _normalise_content_type(response.headers.get("Content-Type"))
    if declared_type not in ALLOWED_IMAGE_TYPES:
        raise UnsafeImageSource("remote content type is not a supported image")

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError) as exc:
            raise UnsafeImageSource("remote content length is invalid") from exc
        if declared_size < 1 or declared_size > MAX_IMAGE_BYTES:
            raise UnsafeImageSource("remote image exceeds the size limit")

    chunks: list[bytes] = []
    downloaded = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        downloaded += len(chunk)
        if downloaded > MAX_IMAGE_BYTES:
            raise UnsafeImageSource("remote image exceeds the size limit")
        chunks.append(chunk)

    content = b"".join(chunks)
    detected_type = _sniff_image_type(content)
    if not content or detected_type is None:
        raise UnsafeImageSource("remote response is not a supported image")
    if detected_type != declared_type:
        raise UnsafeImageSource("remote image type does not match its content")
    return content, detected_type


def _download_remote_image(url: str) -> tuple[bytes, str]:
    session = requests.Session()
    session.trust_env = False
    current_url = url
    try:
        for redirect_count in range(MAX_REMOTE_REDIRECTS + 1):
            _assert_public_remote_url(current_url)
            response = session.get(
                current_url,
                allow_redirects=False,
                headers={"Accept": ", ".join(sorted(ALLOWED_IMAGE_TYPES))},
                stream=True,
                timeout=REMOTE_IMAGE_TIMEOUT,
            )
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REMOTE_REDIRECTS:
                        raise UnsafeImageSource("remote image redirected too many times")
                    location = response.headers.get("Location")
                    if not location:
                        raise UnsafeImageSource("remote image redirect has no location")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                return _bounded_response_body(response)
            finally:
                response.close()
    finally:
        session.close()
    raise UnsafeImageSource("remote image could not be downloaded")


def _load_image_source(image_path_or_url: str) -> tuple[bytes, str]:
    source = str(image_path_or_url or "").strip()
    if source.startswith(("http://", "https://")):
        return _download_remote_image(source)

    path = Path(source)
    size = path.stat().st_size
    if size < 1 or size > MAX_IMAGE_BYTES:
        raise UnsafeImageSource("local image exceeds the size limit")
    with path.open("rb") as image_file:
        content = image_file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise UnsafeImageSource("local image exceeds the size limit")
    detected_type = _sniff_image_type(content)
    if detected_type is None:
        raise UnsafeImageSource("local file is not a supported image")
    return content, detected_type


def encode_image_to_base64(image_path_or_url: str) -> str | None:
    """Load a bounded, validated image and return its Base64 payload."""

    try:
        content, _content_type = _load_image_source(image_path_or_url)
        return base64.b64encode(content).decode("ascii")
    except Exception as exc:
        logger.warning(
            "Image encoding failed source_type=%s error_type=%s",
            "remote"
            if str(image_path_or_url or "").startswith(("http://", "https://"))
            else "local",
            type(exc).__name__,
        )
        return None


def _normalise_analysis_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    intent = payload.get("intent")
    if intent in {"crear_reclamo", "invalido"}:
        result: dict[str, Any] = {"intent": intent}
        if intent == "crear_reclamo":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            result["data"] = {
                key: value
                for key, value in data.items()
                if key in {"categoria", "descripcion"} and value not in (None, "")
            }
        return result

    items = payload.get("items") or payload.get("productos")
    if isinstance(items, list):
        cleaned_items = []
        for item in items:
            if isinstance(item, dict):
                cleaned_items.append(
                    {key: value for key, value in item.items() if value is not None}
                )
        return {"items": cleaned_items}
    return None


def analizar_imagen_openai(image_path_or_url: str, prompt: str) -> dict | None:
    """Analyze one validated image through Responses structured output."""

    try:
        image_bytes, _content_type = _load_image_source(image_path_or_url)
    except Exception as exc:
        logger.warning(
            "Image source rejected source_type=%s error_type=%s",
            "remote"
            if str(image_path_or_url or "").startswith(("http://", "https://"))
            else "local",
            type(exc).__name__,
        )
        return None

    try:
        payload = vision_fallback_service._call_openai(
            image_bytes,
            custom_prompt=prompt,
            schema=MULTIMODAL_OUTPUT_SCHEMA,
            schema_name="multimodal_analysis",
            safety_subject=image_path_or_url,
        )
    except vision_fallback_service.OpenAIAmbiguousVisionFailure:
        raise
    if not isinstance(payload, dict):
        return None
    return _normalise_analysis_result(payload)


def analizar_imagen_con_fallback(image_path_or_url: str, prompt: str) -> dict | None:
    """Analyze an image once; ambiguous OpenAI outcomes are never resubmitted."""

    logger.info(
        "Analyzing image source_type=%s",
        "remote"
        if str(image_path_or_url or "").startswith(("http://", "https://"))
        else "local",
    )
    try:
        return analizar_imagen_openai(image_path_or_url, prompt)
    except vision_fallback_service.OpenAIAmbiguousVisionFailure:
        logger.warning("Image fallback suppressed reason=ambiguous_openai_outcome")
        return None
    except Exception as exc:
        logger.warning(
            "OpenAI image analysis wrapper failed error_type=%s",
            type(exc).__name__,
        )
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Multimodal analyzer structure updated.")

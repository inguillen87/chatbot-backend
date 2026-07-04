from __future__ import annotations

from typing import Any

from utils.time_utils import datetime_to_iso_utc
from services.r2_service import r2_service


def resolve_attachment_delivery_url(url_or_key: str | None, mime_type: str | None = None) -> dict[str, Any]:
    """Resolve an attachment URL/key into the safest client-facing delivery URL."""

    original_url = str(url_or_key or "").strip()
    if not original_url:
        return {
            "url": None,
            "download_url": None,
            "storage_provider": "unknown",
            "storage_access": "missing",
            "storage_url": None,
            "is_private": False,
        }

    object_key = r2_service.object_key_from_url(original_url)
    if not object_key:
        return {
            "url": original_url,
            "download_url": original_url,
            "storage_provider": "external",
            "storage_access": "external",
            "storage_url": original_url,
            "is_private": False,
        }

    is_public = r2_service.is_public_asset_key(object_key, mime_type)
    resolved_url = r2_service.resolve_download_url(object_key, mime_type) or original_url
    return {
        "url": resolved_url,
        "download_url": resolved_url,
        "storage_provider": "cloudflare_r2",
        "storage_access": "public" if is_public else "signed",
        "storage_url": original_url,
        "is_private": not is_public,
    }


def serialize_attachment_for_delivery(
    attachment: Any,
    *,
    meta: dict[str, Any] | None = None,
    thumb_url: str | None = None,
    analisis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize an attachment-like object or dict for CRM/chat delivery."""

    if isinstance(attachment, dict):
        attachment_id = attachment.get("id") or attachment.get("archivo_id") or attachment.get("attachment_id")
        filename = attachment.get("filename") or attachment.get("name") or attachment.get("original_filename")
        original_name = attachment.get("name") or attachment.get("original_filename") or attachment.get("nombre") or filename
        mime_type = attachment.get("mimeType") or attachment.get("mime_type") or attachment.get("mimetype") or attachment.get("mime")
        size = attachment.get("size") or attachment.get("tamano")
        url = attachment.get("url") or attachment.get("public_url") or attachment.get("archivo_url")
        uploaded_at = attachment.get("uploadedAt") or attachment.get("uploaded_at") or attachment.get("fecha")
        raw_thumb_url = thumb_url or attachment.get("thumbUrl") or attachment.get("thumbnailUrl") or attachment.get("thumb_url")
    else:
        attachment_id = getattr(attachment, "id", None)
        filename = getattr(attachment, "filename", None)
        original_name = getattr(attachment, "nombre_original", None) or filename
        mime_type = getattr(attachment, "mime", None)
        size = getattr(attachment, "tamano", None)
        url = getattr(attachment, "url", None)
        uploaded_at = datetime_to_iso_utc(getattr(attachment, "fecha", None)) if getattr(attachment, "fecha", None) else None
        raw_thumb_url = thumb_url

    delivery = resolve_attachment_delivery_url(url, mime_type)
    payload = {
        "id": attachment_id,
        "url": delivery["url"],
        "downloadUrl": delivery["download_url"],
        "download_url": delivery["download_url"],
        "storage_url": delivery["storage_url"],
        "storage_provider": delivery["storage_provider"],
        "storage_access": delivery["storage_access"],
        "is_private": delivery["is_private"],
        "name": original_name,
        "filename": filename or original_name,
        "original_filename": original_name,
        "mimeType": mime_type,
        "mime_type": mime_type,
        "size": size,
        "uploadedAt": uploaded_at,
        "fecha": uploaded_at,
    }

    if raw_thumb_url:
        thumb_delivery = resolve_attachment_delivery_url(raw_thumb_url, "image/webp")
        payload["thumbUrl"] = thumb_delivery["url"]
        payload["thumbnailUrl"] = thumb_delivery["url"]
        payload["thumb_storage_url"] = thumb_delivery["storage_url"]
        payload["thumb_storage_access"] = thumb_delivery["storage_access"]

    if meta is not None:
        payload["meta"] = meta
    if analisis is not None:
        payload["analisis"] = analisis

    return {key: value for key, value in payload.items() if value is not None}

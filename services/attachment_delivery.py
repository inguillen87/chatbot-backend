from __future__ import annotations

from typing import Any
import hashlib
import json
import os
import re
import time
from flask import current_app, g, has_app_context, has_request_context

from utils.time_utils import datetime_to_iso_utc
from services.r2_service import r2_service
from services.media_cache_policy import cache_control_for_key, PRIVATE_ATTACHMENT_CACHE


def _sensitive_reference(value):
    """Cheap classification only; unrelated responses never touch DB or SDK."""
    if not isinstance(value, str):
        return False
    if value.startswith('r2-private://'):
        return True
    if '://' not in value:
        return (value.startswith('/static/uploads/') or ('/' in value and not value.startswith('/')
                and cache_control_for_key(value) == PRIVATE_ATTACHMENT_CACHE))
    key = r2_service.object_key_from_url(value)
    if key is not None:
        return not r2_service.is_public_asset_key(key)
    return value.startswith('/static/uploads/')


def _private_reference_and_mapping(original_url):
    if original_url.startswith('r2-private://'):
        return original_url, None
    raw_map = current_app.config.get('PRIVATE_ATTACHMENT_LEGACY_MAP_JSON', os.environ.get('PRIVATE_ATTACHMENT_LEGACY_MAP_JSON')) if has_app_context() else None
    manifest = json.loads(raw_map) if isinstance(raw_map, str) else raw_map
    records = manifest.get('records', {}) if isinstance(manifest, dict) and manifest.get('schema') == 'private.attachment.mapping.v1' else {}
    mapping = records.get(hashlib.sha256(original_url.encode('utf-8')).hexdigest())
    if (not isinstance(mapping, dict) or not isinstance(mapping.get('verification_receipt_id'), str)
        or not mapping['verification_receipt_id'].strip()
        or not isinstance(mapping.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', mapping['sha256'])
        or type(mapping.get('byte_size')) is not int or mapping['byte_size'] <= 0
        or not isinstance(mapping.get('mime_type'), str) or not mapping['mime_type'].strip()
        or not isinstance(mapping.get('tenant_slug'), str) or not mapping['tenant_slug'].strip()
        or not isinstance(mapping.get('private_object_etag'), str) or not mapping['private_object_etag'].strip()):
        raise ValueError('private_attachment_mapping_required')
    return mapping.get('private_ref'), mapping


def _authorized_attachment_resource(attachment, reference):
    """Refresh the exact stored resource and apply the existing file/ticket ACL."""
    if not has_request_context() or getattr(g, 'widget_session', None) or getattr(g, 'demo_mode', False):
        return False
    actor_id = getattr(getattr(g, 'current_user', None), 'id', None)
    attachment_id = (attachment.get('id') or attachment.get('archivo_id') or attachment.get('attachment_id')) if isinstance(attachment, dict) else getattr(attachment, 'id', None)
    if not actor_id or not attachment_id:
        return False
    try:
        from models import db, ArchivoAdjunto, AnalisisArchivo, User
        from routes.archivos import _tiene_permiso
        db.session.expire_all()
        actor = db.session.get(User, actor_id, populate_existing=True)
        row = db.session.get(ArchivoAdjunto, attachment_id, populate_existing=True)
        if actor is None or row is None:
            return False
        if row.url != reference:
            # Thumbnail refs may only come from this resource's stored analysis.
            thumbnails = AnalisisArchivo.query.filter_by(archivo_adjunto_id=row.id, tipo_analisis='thumbnail_meta').all()
            if not any(isinstance(item.datos_estructurados, dict) and item.datos_estructurados.get('url') == reference for item in thumbnails):
                return False
        return _tiene_permiso(actor, row)
    except Exception:
        return False


def read_authorized_attachment_bytes(reference, mime_type, *, max_bytes=15 * 1024 * 1024, attachment=None):
    """Bounded bytes for existing processors, with fresh exact auth after I/O."""
    from services.private_attachment_storage import (
        private_attachment_storage, authorized_private_attachment_tenant, MAX_PRIVATE_ATTACHMENT_BYTES,
    )
    from services.r2_service import R2ObjectStorageUnavailableError
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_PRIVATE_ATTACHMENT_BYTES:
        raise R2ObjectStorageUnavailableError('private_attachment_read_unavailable')
    original = str(reference or '').strip()
    key = r2_service.object_key_from_url(original)
    if key and r2_service.is_public_asset_key(key, mime_type):
        # Only an existing configured public asset; redirects cannot introduce
        # an unrelated provider. Sensitive legacy URLs never reach this path.
        import requests
        from services.bounded_media import read_bounded_response_body
        with requests.get(r2_service.public_url_for_key(key), stream=True, timeout=(2, 2), allow_redirects=False) as response:
            if response.status_code != 200:
                raise R2ObjectStorageUnavailableError('public_attachment_read_unavailable')
            return read_bounded_response_body(response, max_bytes=max_bytes)
    storage = None
    try:
        private_ref, expected = _private_reference_and_mapping(original)
        storage = private_attachment_storage()
        key = storage.object_key_from_url(private_ref)
        tenant = storage.tenant_for_key(key)
        if (not tenant or (expected is not None and expected['tenant_slug'] != tenant)
            or not authorized_private_attachment_tenant(tenant) or not _authorized_attachment_resource(attachment, original)):
            raise R2ObjectStorageUnavailableError('private_attachment_access_required')
        data = storage.read_verified_bytes(key, mime_type=mime_type, max_bytes=max_bytes, expected=expected)
        if not authorized_private_attachment_tenant(tenant) or not _authorized_attachment_resource(attachment, original):
            raise R2ObjectStorageUnavailableError('private_attachment_access_required')
        return data
    except R2ObjectStorageUnavailableError:
        raise
    except Exception:
        raise R2ObjectStorageUnavailableError('private_attachment_read_unavailable') from None
    finally:
        if storage is not None:
            storage.close()


def register_attachment_delivery_redaction(app):
    """Cover legacy raw URL fields without changing unrelated JSON contracts."""
    @app.after_request
    def redact_private_attachment_references(response):
        if not response.is_json:
            return response
        payload = response.get_json(silent=True)
        changed = False

        def safe_existing_url(value):
            # The response-wide hook never grants access or mints signatures.
            # Only public assets or a URL already authorized in this request survive.
            key = r2_service.object_key_from_url(value)
            if key and r2_service.is_public_asset_key(key):
                return r2_service.public_url_for_key(key)
            cache = getattr(g, '_private_attachment_delivery_cache', {}) if has_request_context() else {}
            if any(item[1].get('url') == value for item in cache.values()):
                return resolve_attachment_delivery_url(value).get('url')
            return None

        def sanitize(value, attachment_context=False):
            nonlocal changed
            if isinstance(value, dict):
                attachment_context = attachment_context or ('url' in value and
                    (any(key in value for key in ('filename', 'nombre_original', 'mime', 'mime_type', 'mimeType'))
                    or ('nombre' in value and 'tamano' in value)))
                result = {}
                blocked = False
                for key, item in value.items():
                    if key in {'storage_url', 'thumb_storage_url'} and (attachment_context or _sensitive_reference(item)):
                        changed = True
                        continue
                    force_attachment_url = key in {'archivo_url', 'foto_url_directa', 'thumbUrl', 'thumbnailUrl'} or (
                        attachment_context and (key in {'url', 'downloadUrl', 'download_url', 'thumb_url', 'thumbnail_url', 'file_url', 'public_url', 'original_url', 'source_url'}
                        or (isinstance(item, str) and ('://' in item or item.startswith('/static/uploads/')))))
                    if force_attachment_url and isinstance(item, str) and item:
                        filtered_url = safe_existing_url(item)
                        if filtered_url != item:
                            changed = True
                            blocked = blocked or filtered_url is None
                        result[key] = filtered_url
                    else:
                        nested_attachment = attachment_context or key in {
                            'attachmentInfo', 'attachment_info', 'attachments', 'archivos_adjuntos',
                            'source_attachment', 'sourceAttachment', 'uploaded_file_info',
                        }
                        result[key] = sanitize(item, nested_attachment)
                if blocked and attachment_context:
                    result['is_private'] = False
                    result['storage_access'] = 'unavailable'
                    result['reason_code'] = 'attachment_private_access_required'
                    result['availability_message'] = 'El archivo no está disponible. Solicita su recuperación al administrador.'
                return result
            if isinstance(value, list):
                return [sanitize(item, attachment_context) for item in value]
            if _sensitive_reference(value):
                changed = True
                return None
            return value

        filtered = sanitize(payload)
        if changed:
            response.set_data(app.json.dumps(filtered))
            response.headers['Cache-Control'] = 'private, no-store'
        return response


def resolve_attachment_delivery_url(url_or_key: str | None, mime_type: str | None = None, *, attachment=None) -> dict[str, Any]:
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

    if has_request_context():
        for cached_tenant, cached_payload, cached_id, cached_reference in getattr(g, '_private_attachment_delivery_cache', {}).values():
            if cached_payload.get('url') == original_url:
                from services.private_attachment_storage import authorized_private_attachment_tenant
                if authorized_private_attachment_tenant(cached_tenant) and _authorized_attachment_resource({'id': cached_id}, cached_reference):
                    return dict(cached_payload)
                return {'url': None, 'download_url': None, 'storage_provider': 'cloudflare_r2',
                        'storage_access': 'unavailable', 'is_private': False,
                        'reason_code': 'attachment_private_access_required'}

    object_key = r2_service.object_key_from_url(original_url)
    if object_key and r2_service.is_public_asset_key(object_key, mime_type):
        resolved_url = r2_service.public_url_for_key(object_key)
        return {'url': resolved_url, 'download_url': resolved_url,
                'storage_provider': 'cloudflare_r2', 'storage_access': 'public',
                'storage_url': original_url, 'is_private': False}

    def unavailable(reason):
        return {'url': None, 'download_url': None, 'storage_provider': 'cloudflare_r2',
                'storage_access': 'unavailable', 'is_private': False,
                'reason_code': reason,
                'availability_message': 'El archivo no está disponible. Solicita su recuperación al administrador.'}

    from services.private_attachment_storage import (
        private_attachment_storage, authorized_private_attachment_tenant,
    )
    try:
        reference, mapping = _private_reference_and_mapping(original_url)
    except (ValueError, TypeError, AttributeError):
        return unavailable('attachment_private_migration_required')
    try:
        storage = private_attachment_storage()
        key = storage.object_key_from_url(reference)
        tenant_slug = storage.tenant_for_key(key)
        if not tenant_slug or (mapping is not None and mapping['tenant_slug'] != tenant_slug):
            return unavailable('attachment_private_reference_invalid')
        if not authorized_private_attachment_tenant(tenant_slug) or not _authorized_attachment_resource(attachment, original_url):
            return unavailable('attachment_private_access_required')
        cache_key = (reference, mime_type)
        cache = getattr(g, '_private_attachment_delivery_cache', {}) if has_request_context() else {}
        if cache_key in cache:
            return dict(cache[cache_key][1])
        if has_request_context():
            started = getattr(g, '_private_attachment_delivery_started', None)
            attempts = getattr(g, '_private_attachment_delivery_attempts', 0)
            if attempts >= 3 or (started is not None and time.monotonic() - started >= 8):
                return unavailable('attachment_private_delivery_budget_exhausted')
            g._private_attachment_delivery_started = started or time.monotonic()
            g._private_attachment_delivery_attempts = attempts + 1
        resolved_url = storage.generate_presigned_download_url(key, expected=mapping, mime_type=mime_type)
        # HEAD may overlap retirement, disabled actor or tenant/membership change.
        if (not resolved_url or not authorized_private_attachment_tenant(tenant_slug)
            or not _authorized_attachment_resource(attachment, original_url)):
            return unavailable('attachment_private_delivery_unavailable')
        result = {'url': resolved_url, 'download_url': resolved_url,
                  'storage_provider': 'cloudflare_r2', 'storage_access': 'signed', 'is_private': True}
        attachment_id = (attachment.get('id') or attachment.get('archivo_id') or attachment.get('attachment_id')) if isinstance(attachment, dict) else attachment.id
        cache[cache_key] = (tenant_slug, result, attachment_id, original_url)
        if has_request_context():
            g._private_attachment_delivery_cache = cache
        return dict(result)
    except Exception:
        return unavailable('attachment_private_storage_unavailable')
    finally:
        if 'storage' in locals():
            try:
                storage.close()
            except Exception:
                pass


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

    delivery = resolve_attachment_delivery_url(url, mime_type, attachment=attachment)
    payload = {
        "id": attachment_id,
        "url": delivery["url"],
        "downloadUrl": delivery["download_url"],
        "download_url": delivery["download_url"],
        "storage_url": delivery.get("storage_url"),
        "storage_provider": delivery["storage_provider"],
        "storage_access": delivery["storage_access"],
        "is_private": delivery["is_private"],
        "reason_code": delivery.get('reason_code'),
        "availability_message": delivery.get('availability_message'),
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
        thumb_delivery = resolve_attachment_delivery_url(raw_thumb_url, "image/webp", attachment=attachment)
        payload["thumbUrl"] = thumb_delivery["url"]
        payload["thumbnailUrl"] = thumb_delivery["url"]
        payload["thumb_storage_url"] = thumb_delivery.get("storage_url")
        payload["thumb_storage_access"] = thumb_delivery["storage_access"]

    def redact_metadata(value):
        if isinstance(value, dict):
            return {key: redact_metadata(item) for key, item in value.items()
                    if not (key in {'storage_url', 'thumb_storage_url'} and _sensitive_reference(item))}
        if isinstance(value, list):
            return [redact_metadata(item) for item in value]
        if _sensitive_reference(value) or (isinstance(value, str) and value in (url, raw_thumb_url)):
            bound_mime = mime_type if value == url else ('image/webp' if value == raw_thumb_url else None)
            return resolve_attachment_delivery_url(value, bound_mime, attachment=attachment).get('url')
        return value
    if meta is not None:
        payload["meta"] = redact_metadata(meta)
    if analisis is not None:
        payload["analisis"] = redact_metadata(analisis)

    return {key: value for key, value in payload.items() if value is not None or key in {'url', 'downloadUrl', 'download_url'}}

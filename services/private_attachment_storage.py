"""Backend-bound private attachment storage; public asset storage stays separate."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from urllib.parse import urlsplit

from flask import current_app, g, has_app_context, has_request_context

from services.r2_service import (
    R2Service, R2ObjectStorageUnavailableError, R2ObjectContentInvalidError, is_valid_r2_bucket_name, is_valid_private_r2_endpoint,
    normalise_r2_object_key, r2_service, _safe_segment, _safe_file_extension,
)

REFERENCE_SCHEME = 'r2-private'
MAX_PRIVATE_ATTACHMENT_BYTES = 15 * 1024 * 1024
PRIVATE_DOWNLOAD_TTL_SECONDS = 120
_PUBLIC_CONTEXTS = frozenset({
    'audio', 'audio_cache', 'avatar', 'avatars', 'brand', 'branding', 'logo', 'logos',
    'catalog', 'catalogo', 'catalogos', 'market', 'marketplace', 'product', 'productos',
    'catalog_product_images', 'profile_avatars', 'eventos',
})


def _setting(name):
    if has_app_context() and name in current_app.config:
        return current_app.config.get(name)
    return os.environ.get(name)


def public_asset_context(kind):
    return str(kind or '').strip().lower() in _PUBLIC_CONTEXTS


def private_attachment_storage():
    """Build solely from trusted private settings; never use the public pair."""
    prefix = _setting('INSTITUTIONAL_KNOWLEDGE_R2_PREFIX')
    bucket = _setting('INSTITUTIONAL_KNOWLEDGE_R2_BUCKET_NAME')
    access = _setting('INSTITUTIONAL_KNOWLEDGE_R2_ACCESS_KEY_ID')
    secret = _setting('INSTITUTIONAL_KNOWLEDGE_R2_SECRET_ACCESS_KEY')
    private_endpoint = _setting('INSTITUTIONAL_KNOWLEDGE_R2_ENDPOINT_URL')
    if private_endpoint is not None and not is_valid_private_r2_endpoint(private_endpoint):
        raise R2ObjectStorageUnavailableError('private_attachment_storage_unavailable')
    endpoint = private_endpoint if private_endpoint is not None else (_setting('R2_ENDPOINT_URL') or r2_service.endpoint_url)
    if (not isinstance(prefix, str) or not re.fullmatch(r'knowledge-private/[a-f0-9]{32}', prefix)
        or not is_valid_r2_bucket_name(bucket)
        or bucket in (_setting('R2_BUCKET_NAME'), r2_service.bucket_name)
        or not all(isinstance(value, str) and value.strip() for value in (access, secret, endpoint))
        or access in (_setting('R2_ACCESS_KEY_ID'), r2_service.access_key_id)):
        raise R2ObjectStorageUnavailableError('private_attachment_storage_unavailable')
    return PrivateAttachmentStorage(endpoint=endpoint, bucket=bucket, access=access,
                                    secret=secret, namespace='private-attachments/' + prefix.rsplit('/', 1)[1])


class PrivateAttachmentStorage(R2Service):
    """A short-lived private-only client with value-free errors and internal refs."""
    private_storage_contract = 'private.attachment.storage.v1'

    def __init__(self, *, endpoint, bucket, access, secret, namespace):
        super().__init__()
        self.endpoint_url, self.bucket_name = endpoint, bucket
        self.access_key_id, self.secret_access_key = access, secret
        self.public_base_url = None
        self.namespace = namespace

    @property
    def is_configured(self):
        return bool(self.endpoint_url and self.bucket_name and self.access_key_id and self.secret_access_key)

    def _create_client(self, *, timeout_seconds=None, access_key_id=None, secret_access_key=None):
        return super()._create_client(timeout_seconds=min(float(timeout_seconds or 2), 2),
                                      access_key_id=self.access_key_id, secret_access_key=self.secret_access_key)

    def close(self):
        try:
            close = getattr(self.client, 'close', None)
            if callable(close):
                close()
        finally:
            self.client = None
            self._client_initialization_attempted = False

    def _tenant_segment(self, tenant_slug):
        if not isinstance(tenant_slug, str) or not tenant_slug or _safe_segment(tenant_slug) != tenant_slug:
            raise R2ObjectStorageUnavailableError('private_attachment_tenant_invalid')
        return tenant_slug

    def _valid_key(self, key):
        if not isinstance(key, str) or normalise_r2_object_key(key) != key:
            return False
        return re.fullmatch(re.escape(self.namespace) + r'/tenants/[a-z0-9][a-z0-9._-]*/(?:attachments|temporary)/[a-f0-9]{32}(?:\.[a-z0-9]{1,12})?', key) is not None

    def generate_key(self, filename, tenant_slug, context_type='attachments'):
        tenant = self._tenant_segment(tenant_slug)
        return f'{self.namespace}/tenants/{tenant}/attachments/{uuid.uuid4().hex}{_safe_file_extension(filename)}'

    def temporary_key(self, filename, tenant_slug, upload_id):
        tenant = self._tenant_segment(tenant_slug)
        if not isinstance(upload_id, str) or not re.fullmatch(r'[a-f0-9]{32}', upload_id):
            raise R2ObjectStorageUnavailableError('private_attachment_intent_invalid')
        return f'{self.namespace}/tenants/{tenant}/temporary/{upload_id}{_safe_file_extension(filename)}'

    def public_url_for_key(self, key):
        # Compatibility return value for upload callers. This is storage state,
        # never a public URL, and must pass authenticated delivery before use.
        if not self._valid_key(key):
            return None
        return f'{REFERENCE_SCHEME}://{self.bucket_name}/{key}'

    def object_key_from_url(self, reference):
        if not isinstance(reference, str):
            return None
        parsed = urlsplit(reference)
        if (parsed.scheme != REFERENCE_SCHEME or parsed.netloc != self.bucket_name
            or parsed.query or parsed.fragment or '%' in parsed.path):
            return None
        key = parsed.path.lstrip('/')
        return key if self._valid_key(key) else None

    def tenant_for_key(self, key):
        return key[len(self.namespace) + len('/tenants/'):].split('/', 1)[0] if self._valid_key(key) else None

    def is_public_asset_key(self, key, content_type=None):
        return False

    def upload_file_with_key(self, file_obj, key, content_type):
        if not self._valid_key(key):
            raise R2ObjectStorageUnavailableError('private_attachment_key_invalid')
        client = self._get_client()
        if client is None:
            return None
        try:
            data = file_obj.read(MAX_PRIVATE_ATTACHMENT_BYTES + 1)
            if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PRIVATE_ATTACHMENT_BYTES:
                return None
            client.put_object(Bucket=self.bucket_name, Key=key, Body=data,
                              ContentType=content_type, CacheControl='private, no-store',
                              Metadata={'sha256': hashlib.sha256(data).hexdigest()})
            return self.public_url_for_key(key)
        except Exception:
            raise R2ObjectStorageUnavailableError('private_attachment_upload_unavailable') from None
        finally:
            self.close()

    def generate_presigned_upload_url(self, key, content_type, content_length, expires_in=None):
        if not self._valid_key(key) or '/temporary/' not in key:
            return None
        try:
            return super().generate_presigned_upload_url(key, content_type, content_length, expires_in)
        finally:
            self.close()

    def head_object(self, key):
        if not self._valid_key(key):
            raise R2ObjectStorageUnavailableError('private_attachment_key_invalid')
        try:
            return super().head_object(key)
        finally:
            self.close()

    def copy_object(self, source_key, destination_key, content_type, *, source_etag):
        if (not self._valid_key(source_key) or not self._valid_key(destination_key)
            or self.tenant_for_key(source_key) != self.tenant_for_key(destination_key)):
            raise R2ObjectStorageUnavailableError('private_attachment_key_invalid')
        try:
            return super().copy_object(source_key, destination_key, content_type, source_etag=source_etag)
        finally:
            self.close()

    def delete_object(self, key):
        if not self._valid_key(key):
            return False
        try:
            return super().delete_object(key)
        finally:
            self.close()

    def generate_presigned_download_url(self, key, expires_in=None, *, expected=None, mime_type=None):
        if not self._valid_key(key) or '/attachments/' not in key:
            return None
        try:
            metadata = self.head_object(key)
            if (not isinstance(metadata, dict) or type(metadata.get('ContentLength')) is not int
                or not 0 < metadata['ContentLength'] <= MAX_PRIVATE_ATTACHMENT_BYTES):
                return None
            if not isinstance(metadata.get('ContentType'), str) or (mime_type and
                metadata['ContentType'].split(';', 1)[0].strip().lower() != mime_type.split(';', 1)[0].strip().lower()):
                return None
            if expected is not None and (metadata['ContentLength'] != expected.get('byte_size')
                or metadata.get('ContentType') != expected.get('mime_type')
                or metadata.get('ETag') != expected.get('private_object_etag')
                or (metadata.get('Metadata') or {}).get('sha256') != expected.get('sha256')):
                return None
            return self._get_client().generate_presigned_url(
                'get_object', Params={'Bucket': self.bucket_name, 'Key': key,
                                     'ResponseCacheControl': 'private, no-store'},
                ExpiresIn=PRIVATE_DOWNLOAD_TTL_SECONDS)
        except Exception:
            return None
        finally:
            self.close()

    def resolve_download_url(self, key, content_type=None, expires_in=None):
        return self.generate_presigned_download_url(key, expires_in)

    def read_verified_bytes(self, key, *, mime_type, max_bytes, expected=None):
        if not self._valid_key(key) or type(max_bytes) is not int or not 0 < max_bytes <= MAX_PRIVATE_ATTACHMENT_BYTES:
            raise R2ObjectStorageUnavailableError('private_attachment_read_unavailable')
        body = None
        try:
            result = self._get_client().get_object(Bucket=self.bucket_name, Key=key)
            body, size = result.get('Body'), result.get('ContentLength')
            actual_mime = str(result.get('ContentType') or '').split(';', 1)[0].strip().lower()
            if (type(size) is not int or not 0 < size <= max_bytes or actual_mime != str(mime_type).split(';', 1)[0].strip().lower()
                or not callable(getattr(body, 'read', None))):
                raise R2ObjectContentInvalidError('private_attachment_content_invalid')
            data = body.read(max_bytes + 1)
            digest = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None
            stored_hash = (result.get('Metadata') or {}).get('sha256')
            if (not isinstance(data, bytes) or len(data) != size
                or (stored_hash is not None and stored_hash != digest)
                or (expected is not None and (size != expected.get('byte_size')
                    or actual_mime != expected.get('mime_type') or digest != expected.get('sha256')
                    or result.get('ETag') != expected.get('private_object_etag')))):
                raise R2ObjectContentInvalidError('private_attachment_content_invalid')
            return data
        except (R2ObjectContentInvalidError, R2ObjectStorageUnavailableError):
            raise
        except Exception:
            raise R2ObjectStorageUnavailableError('private_attachment_read_unavailable') from None
        finally:
            close = getattr(body, 'close', None)
            if callable(close):
                close()
            self.close()


def authorized_private_attachment_tenant(tenant_slug):
    """Require exact current durable auth and fresh consistent membership."""
    if not has_request_context() or getattr(g, 'widget_session', None) or getattr(g, 'demo_mode', False):
        return False
    actor_id = getattr(getattr(g, 'current_user', None), 'id', None)
    if not actor_id:
        return False
    try:
        from models import db, TenantProfile, User
        from services.auth_session_lifecycle import request_auth_session_active
        from utils.auth_helpers import is_user_auth_disabled
        from utils.roles import is_authorized_superadmin_user
        from utils.tenant_admin_access import resolve_consistent_user_tenant
        db.session.expire_all()
        actor = db.session.get(User, actor_id, populate_existing=True)
        tenant = TenantProfile.query.filter_by(slug=tenant_slug, is_active=True).one_or_none()
        if actor is None or tenant is None or is_user_auth_disabled(actor) or not request_auth_session_active(actor_id):
            return False
        resolved = resolve_consistent_user_tenant(actor)
        return is_authorized_superadmin_user(actor) or (resolved is not None and resolved.id == tenant.id)
    except Exception:
        return False

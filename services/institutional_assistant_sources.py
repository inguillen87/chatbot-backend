"""Verified private object delivery for sources in the current tenant bundle."""
import hashlib
import io
import os
import re
from PIL import Image

from flask import current_app

from models import db, TenantProfile, User
from services.institutional_assistant import read_state
from services.institutional_assistant_content import (
    ContentError, MAX_SOURCE_BYTES, SOURCE_FORMATS, source_delivery,
)
from services.r2_service import (
    r2_service, R2ObjectContentInvalidError, R2ObjectStorageUnavailableError,
)
from utils.auth_helpers import auth_session_version, is_user_auth_disabled
from utils.tenant_admin_access import can_manage_tenant_control_plane

MAX_PDF_BYTES = MAX_SOURCE_BYTES  # Compatibility for existing PDF consumers.
PREFIX_ENV = 'INSTITUTIONAL_KNOWLEDGE_R2_PREFIX'


def _source(tenant, source_id, revision, *, public=False):
    state = read_state(tenant, public=public)
    if state is None or state['revision'] != revision:
        raise ContentError('knowledge_revision_conflict', 412)
    source = state['bundle']['sources'].get(source_id)
    if source is None:
        raise ContentError('knowledge_source_not_available', 404)
    return source


def _valid_document(data, format_name):
    if format_name == 'pdf':
        return data.startswith(b'%PDF-')
    if format_name == 'jpeg':
        if not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9'):
            return False
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'JPEG':
                    return False
                image.verify()
            return True
        except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
            return False
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        return False
    return bool(text.strip()) and not any(ord(char) < 32 and char not in '\n\r\t' for char in text)


def source_document(tenant, source_id, revision, *, public=False, actor=None):
    """Verify bounded original bytes and fresh authorization before delivery."""
    source = _source(tenant, source_id, revision, public=public)
    actor_id = getattr(actor, 'id', None)
    session_version = auth_session_version(actor) if actor_id else None
    if not public and (not actor_id or is_user_auth_disabled(actor)
                       or not can_manage_tenant_control_plane(actor, tenant)):
        raise ContentError('knowledge_forbidden', 403)
    prefix = current_app.config.get(PREFIX_ENV, os.environ.get(PREFIX_ENV))
    if not isinstance(prefix, str) or not re.fullmatch(r'knowledge-private/[a-f0-9]{32}', prefix):
        raise ContentError('knowledge_source_storage_unavailable', 503)
    sha256 = source.get('sha256')
    if not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256):
        raise ContentError('knowledge_state_invalid', 503)
    try:
        delivery = source_delivery(source)
    except ContentError:
        raise ContentError('knowledge_state_invalid', 503) from None
    format_name, mime = delivery['format'], delivery['mime_type']
    extension = SOURCE_FORMATS[format_name][1]
    declared_size = source.get('byte_size')
    if 'byte_size' in source and (type(declared_size) is not int or not 1 <= declared_size <= MAX_SOURCE_BYTES):
        raise ContentError('knowledge_state_invalid', 503)
    # No caller URL, object key or source URL participates in storage lookup.
    key = f'{prefix}/tenants/{tenant.id}/sources/{sha256}.{extension}'
    try:
        data = r2_service.read_object_bytes(key, max_bytes=MAX_SOURCE_BYTES, content_type=mime)
    except R2ObjectContentInvalidError:
        raise ContentError('knowledge_source_integrity_failed', 503) from None
    except R2ObjectStorageUnavailableError:
        raise ContentError('knowledge_source_storage_unavailable', 503) from None
    if data is None:
        raise ContentError('knowledge_source_not_available', 404)
    if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_SOURCE_BYTES
        or not _valid_document(data, format_name) or hashlib.sha256(data).hexdigest() != sha256
        or (declared_size is not None and len(data) != declared_size)):
        raise ContentError('knowledge_source_integrity_failed', 503)
    # Storage I/O may overlap a retirement, tenant deactivation or revocation.
    db.session.expire_all()
    fresh_tenant = db.session.get(TenantProfile, tenant.id, populate_existing=True)
    if fresh_tenant is None or not fresh_tenant.is_active:
        raise ContentError('knowledge_not_available', 404)
    if actor_id:
        fresh_actor = db.session.get(User, actor_id, populate_existing=True)
        if (fresh_actor is None or is_user_auth_disabled(fresh_actor)
            or not can_manage_tenant_control_plane(fresh_actor, fresh_tenant)
            or auth_session_version(fresh_actor) != session_version):
            raise ContentError('knowledge_forbidden', 403)
    latest_source = _source(fresh_tenant, source_id, revision, public=public)
    if (latest_source['sha256'] != sha256 or source_delivery(latest_source) != delivery):
        raise ContentError('knowledge_revision_conflict', 412)
    return data, delivery['filename'], mime


def source_pdf(tenant, source_id, revision, *, public=False, actor=None):
    """Preserve the existing PDF-only helper contract."""
    source = _source(tenant, source_id, revision, public=public)
    if source.get('format', 'pdf') != 'pdf':
        raise ContentError('knowledge_source_format_invalid', 422)
    data, filename, _ = source_document(tenant, source_id, revision, public=public, actor=actor)
    return data, filename

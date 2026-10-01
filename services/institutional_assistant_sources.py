"""Verified private object delivery for sources in the current tenant bundle."""
import hashlib
import os
import re

from flask import current_app
from werkzeug.utils import secure_filename

from models import db, TenantProfile, User
from services.institutional_assistant import read_state
from services.institutional_assistant_content import ContentError
from services.r2_service import (
    r2_service, R2ObjectContentInvalidError, R2ObjectStorageUnavailableError,
)
from utils.auth_helpers import auth_session_version
from utils.tenant_admin_access import can_manage_tenant_control_plane

MAX_PDF_BYTES = 8 * 1024 * 1024
PREFIX_ENV = 'INSTITUTIONAL_KNOWLEDGE_R2_PREFIX'


def _source(tenant, source_id, revision, *, public=False):
    state = read_state(tenant, public=public)
    if state is None or state['revision'] != revision:
        raise ContentError('knowledge_revision_conflict', 412)
    source = state['bundle']['sources'].get(source_id)
    if source is None:
        raise ContentError('knowledge_source_not_available', 404)
    return source


def source_pdf(tenant, source_id, revision, *, public=False, actor=None):
    """Validate the whole PDF and fresh authorization before releasing bytes."""
    source = _source(tenant, source_id, revision, public=public)
    actor_id = getattr(actor, 'id', None)
    session_version = auth_session_version(actor) if actor_id else None
    if not public and (not actor_id or not can_manage_tenant_control_plane(actor, tenant)):
        raise ContentError('knowledge_forbidden', 403)
    prefix = current_app.config.get(PREFIX_ENV, os.environ.get(PREFIX_ENV))
    if not isinstance(prefix, str) or not re.fullmatch(r'knowledge-private/[a-f0-9]{32}', prefix):
        raise ContentError('knowledge_source_storage_unavailable', 503)
    sha256 = source.get('sha256')
    if not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256):
        raise ContentError('knowledge_state_invalid', 503)
    # No caller URL, object key or source URL participates in storage lookup.
    key = f'{prefix}/tenants/{tenant.id}/sources/{sha256}.pdf'
    try:
        data = r2_service.read_object_bytes(key, max_bytes=MAX_PDF_BYTES,
                                           content_type='application/pdf')
    except R2ObjectContentInvalidError:
        raise ContentError('knowledge_source_integrity_failed', 503) from None
    except R2ObjectStorageUnavailableError:
        raise ContentError('knowledge_source_storage_unavailable', 503) from None
    if data is None:
        raise ContentError('knowledge_source_not_available', 404)
    if (not data.startswith(b'%PDF-') or not 0 < len(data) <= MAX_PDF_BYTES
        or hashlib.sha256(data).hexdigest() != sha256):
        raise ContentError('knowledge_source_integrity_failed', 503)
    # Storage I/O may overlap a retirement, tenant deactivation or revocation.
    db.session.expire_all()
    fresh_tenant = db.session.get(TenantProfile, tenant.id, populate_existing=True)
    if fresh_tenant is None or not fresh_tenant.is_active:
        raise ContentError('knowledge_not_available', 404)
    if actor_id:
        fresh_actor = db.session.get(User, actor_id, populate_existing=True)
        if (fresh_actor is None or not can_manage_tenant_control_plane(fresh_actor, fresh_tenant)
            or auth_session_version(fresh_actor) != session_version):
            raise ContentError('knowledge_forbidden', 403)
    latest_source = _source(fresh_tenant, source_id, revision, public=public)
    if latest_source['sha256'] != sha256:
        raise ContentError('knowledge_revision_conflict', 412)
    filename = (secure_filename(latest_source['title'])[:96].strip('._') or 'documento') + '.pdf'
    return data, filename

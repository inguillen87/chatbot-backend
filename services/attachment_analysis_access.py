"""Purpose-bound upload authority for private file analysis outside HTTP.

The broker receives neither a login token nor a retirement proof. Every read
rechecks the original durable session, current membership and exact resource.
"""
from __future__ import annotations

import hashlib
import secrets

from flask import current_app, g, has_request_context
from itsdangerous import URLSafeTimedSerializer

CONTRACT = "chatboc.attachment_analysis.v1"
MAX_GRANT_AGE_SECONDS = 3600


class AttachmentAnalysisAccessError(ValueError):
    def __init__(self):
        super().__init__("attachment_analysis_access_required")


def _serializer():
    secret = current_app.config.get("SECRET_KEY")
    if not secret:
        raise AttachmentAnalysisAccessError()
    return URLSafeTimedSerializer(secret, salt=CONTRACT,
                                  signer_kwargs={"digest_method": hashlib.sha256})


def _binding(row):
    return {
        "id": row.id,
        "user_id": row.user_id,
        "reference_sha256": hashlib.sha256((row.url or "").encode()).hexdigest(),
        "mime": row.mime,
        "size": row.tamano,
        "pyme_ticket_id": row.pyme_ticket_id,
        "municipio_ticket_id": row.municipio_ticket_id,
    }


def _fresh_resource(payload):
    from models import ArchivoAdjunto, User, db
    from routes.archivos import _tiene_permiso
    from services.auth_session_lifecycle import lineage_for_claims
    from utils.tenant_admin_access import resolve_consistent_user_tenant

    db.session.expire_all()
    lineage = lineage_for_claims(payload["session"])
    actor = db.session.get(User, lineage.actor_id, populate_existing=True)
    tenant = resolve_consistent_user_tenant(actor)
    row = db.session.get(ArchivoAdjunto, payload["attachment"]["id"], populate_existing=True)
    if (lineage.revision != payload["session_revision"] or tenant is None
        or tenant.is_active is not True or tenant.id != payload["tenant_id"]
        or tenant.slug != payload["tenant_slug"] or row is None
        or row.user_id != actor.id or _binding(row) != payload["attachment"]
        or not _tiene_permiso(actor, row)):
        raise AttachmentAnalysisAccessError()
    return row


def create_attachment_analysis_grant(attachment_id: int) -> str:
    """Delegate one authenticated upload, never a public/widget request."""
    if (not has_request_context() or getattr(g, "widget_session", None)
        or getattr(g, "demo_mode", False) or type(attachment_id) is not int):
        raise AttachmentAnalysisAccessError()
    try:
        from models import ArchivoAdjunto, AuthSession, User, db
        from services.auth_session_lifecycle import descriptor_for_request
        from utils.tenant_admin_access import resolve_consistent_user_tenant

        actor_id = getattr(getattr(g, "current_user", None), "id", None)
        descriptor = descriptor_for_request()
        if not actor_id or not descriptor or str(actor_id) != descriptor["actor_id"]:
            raise AttachmentAnalysisAccessError()
        db.session.expire_all()
        lineage = db.session.get(AuthSession, descriptor["lineage_id"], populate_existing=True)
        actor = db.session.get(User, actor_id, populate_existing=True)
        tenant = resolve_consistent_user_tenant(actor)
        row = db.session.get(ArchivoAdjunto, attachment_id, populate_existing=True)
        if lineage is None or tenant is None or row is None:
            raise AttachmentAnalysisAccessError()
        payload = {
            "contract": CONTRACT,
            "attachment": _binding(row),
            "tenant_id": tenant.id,
            "tenant_slug": tenant.slug,
            "session_revision": lineage.revision,
            "session": {
                "asid": lineage.id, "user_id": actor.id,
                "auth_provider": lineage.provider, "auth_audience": lineage.audience,
                "clerk_sid": lineage.provider_session_id, "jti": secrets.token_hex(16),
            },
        }
        _fresh_resource(payload)
        return _serializer().dumps(payload)
    except AttachmentAnalysisAccessError:
        raise
    except Exception:
        raise AttachmentAnalysisAccessError() from None


class BackgroundAttachmentAccess:
    """Explicit worker-only byte reader; it cannot authorize an HTTP route."""

    def __init__(self, attachment_id: int, grant: str | None):
        self.attachment_id = attachment_id
        self.grant = grant

    def require(self):
        if (has_request_context() or type(self.attachment_id) is not int
            or not isinstance(self.grant, str) or not 1 <= len(self.grant) <= 8192):
            raise AttachmentAnalysisAccessError()
        try:
            payload = _serializer().loads(self.grant, max_age=MAX_GRANT_AGE_SECONDS)
            if (payload["contract"] != CONTRACT
                or payload["attachment"]["id"] != self.attachment_id):
                raise AttachmentAnalysisAccessError()
            return _fresh_resource(payload)
        except AttachmentAnalysisAccessError:
            raise
        except Exception:
            raise AttachmentAnalysisAccessError() from None

    def read_bytes(self, reference, mime_type, *, attachment=None, max_bytes=15 * 1024 * 1024):
        from services.attachment_delivery import _read_attachment_bytes
        from services.analisis_archivo_service import _analysis_writer_fence_enabled
        from utils.tenant_admin_access import resolve_consistent_user_tenant
        from models import User, db

        row = self.require()
        if (getattr(attachment, "id", None) != row.id or reference != row.url
            or (mime_type or "").lower() != (row.mime or "").lower()
            or _analysis_writer_fence_enabled()):
            raise AttachmentAnalysisAccessError()

        def authorize(tenant_slug):
            fresh = self.require()
            tenant = resolve_consistent_user_tenant(db.session.get(User, fresh.user_id))
            return (not _analysis_writer_fence_enabled() and tenant is not None
                    and tenant.slug == tenant_slug)

        data = _read_attachment_bytes(reference, mime_type, max_bytes=max_bytes,
                                      authorize=authorize)
        self.require()
        if _analysis_writer_fence_enabled():
            raise AttachmentAnalysisAccessError()
        return data

    def before_provider(self):
        from services.analisis_archivo_service import _analysis_writer_fence_enabled
        self.require()
        if _analysis_writer_fence_enabled():
            raise AttachmentAnalysisAccessError()

"""Transaction and receipt primitives for LOCAL WhatsApp drafts only.

Models are supplied by the caller to avoid application-bootstrap imports. These
helpers do not authenticate users, contact providers or create schema objects.
"""
from contextlib import contextmanager
from hashlib import sha256
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

EVENT_TYPE = 'whatsapp_template_pack.local_drafts_materialized'


class TemplatePackTransactionError(RuntimeError):
    def __init__(self, reason_code: str, status_code: int = 409):
        self.reason_code = reason_code
        self.status_code = status_code
        super().__init__(reason_code)


def key_digest(key: str) -> str:
    return sha256(key.encode('utf-8')).hexdigest()


@contextmanager
def local_draft_transaction(session, tenant_model, tenant_id: int):
    """Serialize writers for one tenant and commit draft rows with their audit.

    The caller rechecks access on the refreshed row before writing. The route
    must not commit internally; even flush/serialization failures roll back.
    """
    try:
        if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id < 1:
            raise TemplatePackTransactionError('template_pack_invalid_tenant', 400)
        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
        tenant = session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update().one_or_none()
        if tenant is None or getattr(tenant, 'is_active', False) is not True:
            raise TemplatePackTransactionError('template_pack_tenant_unavailable', 403)
        yield tenant
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise TemplatePackTransactionError('template_pack_write_conflict') from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise TemplatePackTransactionError('template_pack_retry_same_operation', 503) from exc
    except BaseException:
        session.rollback()
        raise


def _matching(receipts, key: str):
    digest = key_digest(key)
    return [dict(r) for r in receipts if isinstance(r, dict) and
        (r.get('idempotency_key_hash') == digest or r.get('idempotency_key') == key)]


def find_draft_receipt(session, audit_model, registry_model, tenant_id: int, key: str):
    """Prefer durable audit; retain compatibility with legacy per-row receipts."""
    rows = session.query(audit_model).filter(
        audit_model.tenant_id == tenant_id,
        audit_model.event_type == EVENT_TYPE,
        audit_model.resource_type == 'whatsapp_template_pack',
        or_(audit_model.details['idempotency_key_hash'].as_string() == key_digest(key),
            audit_model.details['idempotency_key'].as_string() == key),
    ).all()
    candidates = _matching([row.details for row in rows], key)
    if not candidates:
        drafts = session.query(registry_model).filter_by(
            tenant_id=tenant_id, provider='chatboc', channel='whatsapp').all()
        for row in drafts:
            metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            receipts = metadata.get('materialization_receipts')
            if isinstance(receipts, list):
                candidates.extend(_matching(receipts, key))
    if not candidates:
        return None
    fingerprints = {str(item.get('request_fingerprint') or '') for item in candidates}
    if len(fingerprints) != 1 or '' in fingerprints:
        raise TemplatePackTransactionError('template_pack_receipt_conflict')
    return candidates[0]

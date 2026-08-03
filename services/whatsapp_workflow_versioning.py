from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping
import uuid

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import AuditEvent
from models_whatsapp_workflows import (
    WhatsAppWorkflowActivation,
    WhatsAppWorkflowDraftRevision,
    WhatsAppWorkflowReview,
    WhatsAppWorkflowVersion,
)
from services.whatsapp_workflow_studio import validate_workflow_draft


WORKFLOW_CONTROL_PLANE_CONTRACT_VERSION = "whatsapp.workflow_control_plane.v1"
WORKFLOW_REVIEW_CONTRACT_VERSION = "whatsapp.workflow_review.v1"
WORKFLOW_PUBLICATION_CONTRACT_VERSION = "whatsapp.workflow_publication.v1"
WORKFLOW_RUNTIME_BINDING_REASON = "workflow_runtime_binding_missing"

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_REVIEW_DECISIONS = frozenset({"approved", "rejected"})
_REVIEW_OPERATIONS = frozenset({"publish", "rollback"})


class WorkflowStudioError(ValueError):
    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        status_code: int = 400,
        action_hint: str = "review_workflow_request",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self.action_hint = action_hint


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _config_value(config: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    return getter(key, default) if callable(getter) else default


def _parse_tenant_allowlist(raw_value: Any) -> tuple[set[int], bool]:
    if not isinstance(raw_value, str):
        return set(), False
    raw_value = raw_value.strip()
    if not raw_value:
        return set(), True
    tenant_ids: set[int] = set()
    for raw_tenant_id in raw_value.split(","):
        normalized = raw_tenant_id.strip()
        try:
            tenant_id = int(normalized)
        except (TypeError, ValueError):
            return set(), False
        if tenant_id <= 0 or str(tenant_id) != normalized:
            return set(), False
        tenant_ids.add(tenant_id)
    return tenant_ids, True


def workflow_durable_control_plane_gate(
    config: Mapping[str, Any] | None,
    *,
    tenant_id: int,
    plan_allowed: bool = True,
) -> dict[str, Any]:
    """Return an explicit fail-closed gate; config truthiness is never inferred."""

    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        normalized_tenant_id = 0
    enabled = (
        _config_value(config, "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1", False)
        is True
    )
    allowlist, allowlist_valid = _parse_tenant_allowlist(
        _config_value(config, "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS", "")
    )
    reasons: list[str] = []
    if not enabled:
        reasons.append("workflow_durable_control_plane_disabled")
    if plan_allowed is not True:
        reasons.append("workflow_durable_plan_required")
    if not allowlist_valid:
        reasons.append("workflow_durable_tenant_allowlist_invalid")
    elif normalized_tenant_id not in allowlist:
        reasons.append("workflow_durable_tenant_not_allowlisted")
    return {
        "contract_version": "whatsapp.workflow_control_plane_gate.v1",
        "available": not reasons,
        "enabled": enabled,
        "plan_allowed": plan_allowed is True,
        "tenant_allowlisted": allowlist_valid and normalized_tenant_id in allowlist,
        "reason_codes": reasons,
        "runtime_binding": {
            "available": False,
            "reason_code": WORKFLOW_RUNTIME_BINDING_REASON,
        },
    }

def require_workflow_durable_control_plane(
    config: Mapping[str, Any] | None,
    *,
    tenant_id: int,
    plan_allowed: bool = True,
) -> dict[str, Any]:
    gate = workflow_durable_control_plane_gate(
        config,
        tenant_id=tenant_id,
        plan_allowed=plan_allowed,
    )
    if gate["available"]:
        return gate
    reason_code = gate["reason_codes"][0]
    raise WorkflowStudioError(
        reason_code,
        "El plano de control durable de Workflow Studio no esta habilitado para este tenant.",
        status_code=503,
        action_hint="enable_reviewed_workflow_canary",
    )


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        value = None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = 0
    if normalized < 1:
        raise WorkflowStudioError(
            f"workflow_{field}_invalid",
            f"{field} debe ser un entero positivo.",
        )
    return normalized


def _opaque_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise WorkflowStudioError(
            f"workflow_{field}_invalid",
            f"{field} no es una referencia valida.",
        )
    try:
        normalized = str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        raise WorkflowStudioError(
            f"workflow_{field}_invalid",
            f"{field} no es una referencia valida.",
        ) from None
    return normalized


def _idempotency(value: Any) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not _IDEMPOTENCY_KEY.fullmatch(normalized):
        raise WorkflowStudioError(
            "workflow_idempotency_key_invalid",
            "Se requiere una clave de idempotencia opaca de 8 a 128 caracteres.",
        )
    return normalized


def _review_note(value: Any) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > 1000:
        raise WorkflowStudioError(
            "workflow_review_note_invalid",
            "La revision requiere una nota de 1 a 1000 caracteres.",
        )
    return normalized


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _draft_payload(row: WhatsAppWorkflowDraftRevision, *, include_document: bool = True) -> dict[str, Any]:
    payload = {
        "draft_revision_id": row.id,
        "workflow_id": row.workflow_id,
        "revision": row.revision,
        "schema_version": row.schema_version,
        "draft_digest": row.draft_digest,
        "authored_by_user_id": row.authored_by_user_id,
        "created_at": _iso(row.created_at),
        "immutable": True,
    }
    if include_document:
        payload["draft"] = row.draft_json
    return payload


def _review_payload(row: WhatsAppWorkflowReview) -> dict[str, Any]:
    return {
        "contract_version": WORKFLOW_REVIEW_CONTRACT_VERSION,
        "review_id": row.id,
        "workflow_id": row.workflow_id,
        "operation": row.operation,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "subject_digest": row.subject_digest,
        "subject_sequence": row.subject_sequence,
        "decision": row.decision,
        "review_note": row.review_note,
        "reviewed_by_user_id": row.reviewed_by_user_id,
        "reviewed_at": _iso(row.reviewed_at),
        "immutable": True,
    }


def _version_payload(row: WhatsAppWorkflowVersion, *, include_document: bool = True) -> dict[str, Any]:
    payload = {
        "version_id": row.id,
        "workflow_id": row.workflow_id,
        "version": row.version,
        "version_kind": row.version_kind,
        "source_draft_revision_id": row.source_draft_revision_id,
        "restored_from_version_id": row.restored_from_version_id,
        "review_id": row.review_id,
        "schema_version": row.schema_version,
        "content_digest": row.content_digest,
        "published_by_user_id": row.published_by_user_id,
        "published_at": _iso(row.published_at),
        "immutable": True,
    }
    if include_document:
        payload["document"] = row.content_json
    return payload


def _activation_payload(row: WhatsAppWorkflowActivation) -> dict[str, Any]:
    return {
        "activation_id": row.id,
        "workflow_id": row.workflow_id,
        "sequence": row.sequence,
        "workflow_version_id": row.workflow_version_id,
        "previous_activation_id": row.previous_activation_id,
        "activation_kind": row.activation_kind,
        "activated_by_user_id": row.activated_by_user_id,
        "activated_at": _iso(row.activated_at),
        "immutable": True,
        "runtime_consumed": False,
    }


def _audit(
    *,
    tenant_id: int,
    actor_user_id: int,
    event_type: str,
    workflow_id: str,
    details: Mapping[str, Any],
) -> None:
    db.session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            resource_type="whatsapp_workflow",
            resource_id=workflow_id,
            details=dict(details),
        )
    )


def _idempotency_conflict() -> WorkflowStudioError:
    return WorkflowStudioError(
        "workflow_idempotency_conflict",
        "La clave de idempotencia ya fue usada con otro contenido.",
        status_code=409,
        action_hint="reuse_original_request_or_new_idempotency_key",
    )


def _acquire_workflow_write_lock(
    *,
    tenant_id: int,
    workflow_id: str,
) -> WhatsAppWorkflowDraftRevision | None:
    """Serialize every mutation for a workflow on its immutable first draft.

    PostgreSQL's ``FOR UPDATE`` row lock is transaction-scoped and, unlike a
    lock on the current tail row, remains the same as new drafts, reviews and
    versions are appended. All service write paths acquire this anchor before
    re-reading mutable tails. SQLite ignores ``FOR UPDATE``; its single-writer
    transaction plus the migration guards/unique constraints still fail closed.
    """

    return (
        WhatsAppWorkflowDraftRevision.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            revision=1,
        )
        .with_for_update()
        .one_or_none()
    )


def _draft_replay(
    *,
    tenant_id: int,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    existing = WhatsAppWorkflowDraftRevision.query.filter_by(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    ).one_or_none()
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise _idempotency_conflict()
    return {
        "contract_version": WORKFLOW_CONTROL_PLANE_CONTRACT_VERSION,
        "status": "draft_saved",
        "idempotent_replay": True,
        "draft_revision": _draft_payload(existing),
        "runtime_binding": {
            "available": False,
            "reason_code": WORKFLOW_RUNTIME_BINDING_REASON,
        },
    }


def save_workflow_draft(
    *,
    tenant_id: int,
    tenant_slug: str,
    actor_user_id: int,
    draft: Any,
    idempotency_key: Any,
    workflow_id: Any = None,
    expected_revision: Any = None,
) -> dict[str, Any]:
    tenant_id = _positive_int(tenant_id, field="tenant_id")
    actor_user_id = _positive_int(actor_user_id, field="actor_user_id")
    key = _idempotency(idempotency_key)

    creating = workflow_id is None
    normalized_workflow_id = str(uuid.uuid4()) if creating else _opaque_id(
        workflow_id,
        field="workflow_id",
    )
    normalized_expected_revision = None
    if not creating:
        normalized_expected_revision = _positive_int(
            expected_revision,
            field="expected_revision",
        )
    elif expected_revision is not None:
        raise WorkflowStudioError(
            "workflow_expected_revision_not_allowed",
            "expected_revision solo se usa al agregar una revision.",
        )

    validation = validate_workflow_draft(
        draft,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
    )
    if not validation["valid"]:
        raise WorkflowStudioError(
            "workflow_draft_invalid",
            "El borrador no supera la validacion determinista.",
            status_code=422,
            action_hint="resolve_validation_blockers",
        )
    normalized_draft = validation["normalized_draft"]
    request_hash = _digest(
        {
            "operation": "create_draft" if creating else "revise_draft",
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "workflow_id": None if creating else normalized_workflow_id,
            "expected_revision": normalized_expected_revision,
            "draft_digest": validation["draft_digest"],
        }
    )
    replay = _draft_replay(
        tenant_id=tenant_id,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    latest = None
    if not creating:
        _acquire_workflow_write_lock(
            tenant_id=tenant_id,
            workflow_id=normalized_workflow_id,
        )
        # A caller may have waited for the workflow anchor. Re-read both the
        # idempotency ledger and latest draft only after that wait completes.
        replay = _draft_replay(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        latest = (
            WhatsAppWorkflowDraftRevision.query.filter_by(
                tenant_id=tenant_id,
                workflow_id=normalized_workflow_id,
            )
            .order_by(WhatsAppWorkflowDraftRevision.revision.desc())
            .first()
        )
        if latest is None:
            raise WorkflowStudioError(
                "workflow_not_found",
                "El workflow no existe dentro del tenant autenticado.",
                status_code=404,
                action_hint="select_existing_workflow",
            )
        if latest.revision != normalized_expected_revision:
            raise WorkflowStudioError(
                "workflow_draft_revision_conflict",
                "El borrador cambio; recarga la revision mas reciente antes de guardar.",
                status_code=409,
                action_hint="reload_latest_draft",
            )
    else:
        # Creation has no shared workflow id to anchor yet. The tenant-scoped
        # unique idempotency key closes the concurrent-create race at commit.
        replay = _draft_replay(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay

    row = WhatsAppWorkflowDraftRevision(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        workflow_id=normalized_workflow_id,
        revision=1 if latest is None else latest.revision + 1,
        schema_version=validation["normalized_draft"]["schema_version"],
        draft_digest=validation["draft_digest"],
        draft_json=normalized_draft,
        authored_by_user_id=actor_user_id,
        idempotency_key=key,
        request_hash=request_hash,
        created_at=_utc_now(),
    )
    db.session.add(row)
    _audit(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type="whatsapp_workflow.draft_revision_saved",
        workflow_id=normalized_workflow_id,
        details={
            "draft_revision_id": row.id,
            "revision": row.revision,
            "draft_digest": row.draft_digest,
        },
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = WhatsAppWorkflowDraftRevision.query.filter_by(
            tenant_id=tenant_id,
            idempotency_key=key,
        ).one_or_none()
        if replay is not None and replay.request_hash == request_hash:
            row = replay
            replayed = True
        else:
            raise WorkflowStudioError(
                "workflow_concurrent_update",
                "Otro cambio fue guardado al mismo tiempo; recarga antes de continuar.",
                status_code=409,
                action_hint="reload_latest_draft",
            ) from None
    else:
        replayed = False
    return {
        "contract_version": WORKFLOW_CONTROL_PLANE_CONTRACT_VERSION,
        "status": "draft_saved",
        "idempotent_replay": replayed,
        "draft_revision": _draft_payload(row),
        "runtime_binding": {"available": False, "reason_code": WORKFLOW_RUNTIME_BINDING_REASON},
    }


def review_workflow_subject(
    *,
    tenant_id: int,
    workflow_id: Any,
    reviewer_user_id: int,
    operation: Any,
    subject_id: Any,
    decision: Any,
    review_note: Any,
    idempotency_key: Any,
) -> dict[str, Any]:
    tenant_id = _positive_int(tenant_id, field="tenant_id")
    reviewer_user_id = _positive_int(reviewer_user_id, field="reviewer_user_id")
    workflow_id = _opaque_id(workflow_id, field="workflow_id")
    subject_id = _opaque_id(subject_id, field="subject_id")
    operation = operation.strip().lower() if isinstance(operation, str) else ""
    decision = decision.strip().lower() if isinstance(decision, str) else ""
    if operation not in _REVIEW_OPERATIONS:
        raise WorkflowStudioError("workflow_review_operation_invalid", "La operacion de revision no esta soportada.")
    if decision not in _REVIEW_DECISIONS:
        raise WorkflowStudioError("workflow_review_decision_invalid", "La decision debe ser approved o rejected.")
    note = _review_note(review_note)
    key = _idempotency(idempotency_key)

    _acquire_workflow_write_lock(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
    )
    subject_type = "draft_revision" if operation == "publish" else "published_version"
    if operation == "publish":
        subject = WhatsAppWorkflowDraftRevision.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            id=subject_id,
        ).one_or_none()
        subject_digest = subject.draft_digest if subject is not None else None
    else:
        subject = WhatsAppWorkflowVersion.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            id=subject_id,
        ).one_or_none()
        subject_digest = subject.content_digest if subject is not None else None
    if subject is None or subject_digest is None:
        raise WorkflowStudioError(
            "workflow_review_subject_not_found",
            "El objeto a revisar no existe dentro de este workflow y tenant.",
            status_code=404,
            action_hint="select_existing_review_subject",
        )

    request_hash = _digest(
        {
            "operation": operation,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "subject_digest": subject_digest,
            "decision": decision,
            "review_note": note,
            "reviewer_user_id": reviewer_user_id,
        }
    )
    existing = WhatsAppWorkflowReview.query.filter_by(
        tenant_id=tenant_id,
        idempotency_key=key,
    ).one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise _idempotency_conflict()
        return {
            "contract_version": WORKFLOW_REVIEW_CONTRACT_VERSION,
            "status": "review_recorded",
            "idempotent_replay": True,
            "review": _review_payload(existing),
        }

    latest_review = (
        WhatsAppWorkflowReview.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            operation=operation,
            subject_id=subject_id,
            subject_digest=subject_digest,
        )
        .order_by(WhatsAppWorkflowReview.subject_sequence.desc())
        .first()
    )
    row = WhatsAppWorkflowReview(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        operation=operation,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_digest=subject_digest,
        subject_sequence=(
            1 if latest_review is None else latest_review.subject_sequence + 1
        ),
        decision=decision,
        review_note=note,
        reviewed_by_user_id=reviewer_user_id,
        idempotency_key=key,
        request_hash=request_hash,
        reviewed_at=_utc_now(),
    )
    db.session.add(row)
    _audit(
        tenant_id=tenant_id,
        actor_user_id=reviewer_user_id,
        event_type="whatsapp_workflow.review_recorded",
        workflow_id=workflow_id,
        details={
            "review_id": row.id,
            "operation": operation,
            "subject_id": subject_id,
            "subject_digest": subject_digest,
            "subject_sequence": row.subject_sequence,
            "decision": decision,
        },
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = WhatsAppWorkflowReview.query.filter_by(
            tenant_id=tenant_id,
            idempotency_key=key,
        ).one_or_none()
        if replay is None or replay.request_hash != request_hash:
            raise WorkflowStudioError(
                "workflow_review_conflict",
                "La revision no pudo registrarse de forma consistente.",
                status_code=409,
            ) from None
        row = replay
        replayed = True
    else:
        replayed = False
    return {
        "contract_version": WORKFLOW_REVIEW_CONTRACT_VERSION,
        "status": "review_recorded",
        "idempotent_replay": replayed,
        "review": _review_payload(row),
    }


def _approved_review(
    *,
    tenant_id: int,
    workflow_id: str,
    review_id: str,
    operation: str,
    subject_id: str,
    subject_digest: str,
    publisher_user_id: int,
    subject_author_user_id: int | None = None,
) -> WhatsAppWorkflowReview:
    review = WhatsAppWorkflowReview.query.filter_by(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        id=review_id,
    ).one_or_none()
    if review is None:
        raise WorkflowStudioError(
            "workflow_review_not_found",
            "La revision no existe dentro de este tenant y workflow.",
            status_code=404,
            action_hint="request_new_review",
        )
    if (
        review.operation != operation
        or review.subject_id != subject_id
        or review.subject_digest != subject_digest
    ):
        raise WorkflowStudioError(
            "workflow_review_scope_mismatch",
            "La revision no corresponde exactamente al objeto solicitado.",
            status_code=409,
            action_hint="request_review_for_exact_subject",
        )
    latest_review = (
        WhatsAppWorkflowReview.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            operation=operation,
            subject_id=subject_id,
            subject_digest=subject_digest,
        )
        .order_by(WhatsAppWorkflowReview.subject_sequence.desc())
        .first()
    )
    if latest_review is None or latest_review.id != review.id:
        raise WorkflowStudioError(
            "workflow_review_superseded",
            "La revision indicada fue reemplazada por una decision posterior.",
            status_code=409,
            action_hint="use_latest_approved_review",
        )
    if review.decision != "approved":
        raise WorkflowStudioError(
            "workflow_review_not_approved",
            "La publicacion exige una revision aprobada.",
            status_code=409,
            action_hint="resolve_review_rejection",
        )
    if review.reviewed_by_user_id == publisher_user_id:
        raise WorkflowStudioError(
            "workflow_separation_of_duties_required",
            "Quien publica no puede aprobar su propia operacion.",
            status_code=409,
            action_hint="request_independent_review",
        )
    if (
        operation == "publish"
        and subject_author_user_id is not None
        and review.reviewed_by_user_id == subject_author_user_id
    ):
        raise WorkflowStudioError(
            "workflow_independent_content_review_required",
            "Quien creo la revision del borrador no puede aprobar su propio contenido.",
            status_code=409,
            action_hint="request_independent_content_review",
        )
    return review


def _publication_payload(
    *,
    version: WhatsAppWorkflowVersion,
    activation: WhatsAppWorkflowActivation,
    idempotent_replay: bool,
    currently_active: bool,
) -> dict[str, Any]:
    return {
        "contract_version": WORKFLOW_PUBLICATION_CONTRACT_VERSION,
        "status": "control_plane_activation_recorded",
        "idempotent_replay": idempotent_replay,
        "currently_active": currently_active,
        "version": _version_payload(version),
        "activation": _activation_payload(activation),
        "runtime_binding": {
            "available": False,
            "consumes_active_version": False,
            "reason_code": WORKFLOW_RUNTIME_BINDING_REASON,
        },
        "external_effects": {
            "provider_calls": 0,
            "messages_sent": 0,
            "tickets_created": 0,
            "handoffs_created": 0,
        },
    }


def _publication_replay(
    *,
    tenant_id: int,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    version = WhatsAppWorkflowVersion.query.filter_by(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    ).one_or_none()
    if version is None:
        return None
    if version.request_hash != request_hash:
        raise _idempotency_conflict()
    activation = WhatsAppWorkflowActivation.query.filter_by(
        tenant_id=tenant_id,
        workflow_version_id=version.id,
    ).one_or_none()
    if activation is None or activation.request_hash != request_hash:
        raise WorkflowStudioError(
            "workflow_publication_ledger_corrupt",
            "La publicacion existe sin una activacion consistente.",
            status_code=500,
            action_hint="contact_support",
        )
    latest_activation = (
        WhatsAppWorkflowActivation.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=version.workflow_id,
        )
        .order_by(WhatsAppWorkflowActivation.sequence.desc())
        .first()
    )
    return _publication_payload(
        version=version,
        activation=activation,
        idempotent_replay=True,
        currently_active=(
            latest_activation is not None and latest_activation.id == activation.id
        ),
    )


def _latest_version_and_activation(
    *,
    tenant_id: int,
    workflow_id: str,
) -> tuple[WhatsAppWorkflowVersion | None, WhatsAppWorkflowActivation | None]:
    version = (
        WhatsAppWorkflowVersion.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowVersion.version.desc())
        .with_for_update()
        .first()
    )
    activation = (
        WhatsAppWorkflowActivation.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowActivation.sequence.desc())
        .with_for_update()
        .first()
    )
    if (version is None) != (activation is None):
        raise WorkflowStudioError(
            "workflow_publication_ledger_corrupt",
            "El ledger de versiones y activaciones no es consistente.",
            status_code=500,
            action_hint="contact_support",
        )
    if version is not None and activation is not None and (
        activation.workflow_version_id != version.id
        or activation.sequence != version.version
    ):
        raise WorkflowStudioError(
            "workflow_publication_ledger_corrupt",
            "La ultima activacion no coincide con la ultima version publicada.",
            status_code=500,
            action_hint="contact_support",
        )
    return version, activation


def _append_publication_locked(
    *,
    tenant_id: int,
    workflow_id: str,
    actor_user_id: int,
    key: str,
    request_hash: str,
    version_kind: str,
    source_draft: WhatsAppWorkflowDraftRevision,
    review: WhatsAppWorkflowReview,
    restored_from: WhatsAppWorkflowVersion | None,
    latest_version: WhatsAppWorkflowVersion | None,
    latest_activation: WhatsAppWorkflowActivation | None,
) -> dict[str, Any]:
    content_digest = (
        restored_from.content_digest
        if restored_from is not None
        else source_draft.draft_digest
    )
    if latest_version is not None and latest_version.content_digest == content_digest:
        if version_kind == "rollback":
            raise WorkflowStudioError(
                "workflow_rollback_target_already_active",
                "La version objetivo ya representa el contenido activo.",
                status_code=409,
                action_hint="select_different_published_version",
            )
        raise WorkflowStudioError(
            "workflow_content_already_active",
            "El contenido de esta revision ya es el activo del plano de control.",
            status_code=409,
            action_hint="save_changed_draft_before_publish",
        )
    content_source = (
        restored_from.content_json if restored_from is not None else source_draft.draft_json
    )
    # Preserve object order for SQLite's json() equality guard while still
    # taking an independent deep snapshot. PostgreSQL compares JSONB
    # semantically; the digest remains canonical on both dialects.
    content_snapshot = json.loads(json.dumps(content_source, ensure_ascii=False))
    version = WhatsAppWorkflowVersion(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        version=1 if latest_version is None else latest_version.version + 1,
        version_kind=version_kind,
        source_draft_revision_id=source_draft.id,
        restored_from_version_id=restored_from.id if restored_from is not None else None,
        review_id=review.id,
        schema_version=source_draft.schema_version,
        content_digest=content_digest,
        content_json=content_snapshot,
        published_by_user_id=actor_user_id,
        idempotency_key=key,
        request_hash=request_hash,
        published_at=_utc_now(),
    )
    activation = WhatsAppWorkflowActivation(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        sequence=1 if latest_activation is None else latest_activation.sequence + 1,
        workflow_version_id=version.id,
        previous_activation_id=latest_activation.id if latest_activation is not None else None,
        activation_kind=version_kind,
        activated_by_user_id=actor_user_id,
        idempotency_key=key,
        request_hash=request_hash,
        activated_at=_utc_now(),
    )
    db.session.add_all([version, activation])
    _audit(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type=f"whatsapp_workflow.{version_kind}_activated",
        workflow_id=workflow_id,
        details={
            "version_id": version.id,
            "version": version.version,
            "activation_id": activation.id,
            "activation_sequence": activation.sequence,
            "content_digest": version.content_digest,
            "review_id": review.id,
            "restored_from_version_id": version.restored_from_version_id,
            "runtime_consumed": False,
        },
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = _publication_replay(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise WorkflowStudioError(
            "workflow_publication_conflict",
            "El workflow cambio durante la operacion; recarga el ledger.",
            status_code=409,
            action_hint="reload_workflow_ledger",
        ) from None
    return _publication_payload(
        version=version,
        activation=activation,
        idempotent_replay=False,
        currently_active=True,
    )


def publish_workflow(
    *,
    tenant_id: int,
    workflow_id: Any,
    publisher_user_id: int,
    draft_revision_id: Any,
    review_id: Any,
    idempotency_key: Any,
) -> dict[str, Any]:
    tenant_id = _positive_int(tenant_id, field="tenant_id")
    publisher_user_id = _positive_int(publisher_user_id, field="publisher_user_id")
    workflow_id = _opaque_id(workflow_id, field="workflow_id")
    draft_revision_id = _opaque_id(draft_revision_id, field="draft_revision_id")
    review_id = _opaque_id(review_id, field="review_id")
    key = _idempotency(idempotency_key)
    request_hash = _digest(
        {
            "operation": "publish",
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "draft_revision_id": draft_revision_id,
            "review_id": review_id,
            "publisher_user_id": publisher_user_id,
        }
    )
    replay = _publication_replay(
        tenant_id=tenant_id,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    _acquire_workflow_write_lock(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
    )
    # The anchor wait may have admitted a winner using this idempotency key,
    # appended a newer draft, recorded a later review, or changed activation.
    # Every publication precondition is therefore re-read below the lock.
    replay = _publication_replay(
        tenant_id=tenant_id,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    draft = WhatsAppWorkflowDraftRevision.query.filter_by(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        id=draft_revision_id,
    ).one_or_none()
    if draft is None:
        raise WorkflowStudioError(
            "workflow_draft_revision_not_found",
            "La revision no existe dentro de este tenant y workflow.",
            status_code=404,
            action_hint="select_existing_draft_revision",
        )
    latest_draft = (
        WhatsAppWorkflowDraftRevision.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowDraftRevision.revision.desc())
        .first()
    )
    if latest_draft is None or latest_draft.id != draft.id:
        raise WorkflowStudioError(
            "workflow_draft_revision_stale",
            "Solo se puede publicar la ultima revision durable.",
            status_code=409,
            action_hint="review_latest_draft_revision",
        )
    review = _approved_review(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        review_id=review_id,
        operation="publish",
        subject_id=draft.id,
        subject_digest=draft.draft_digest,
        publisher_user_id=publisher_user_id,
        subject_author_user_id=draft.authored_by_user_id,
    )
    latest_version, latest_activation = _latest_version_and_activation(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
    )
    return _append_publication_locked(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        actor_user_id=publisher_user_id,
        key=key,
        request_hash=request_hash,
        version_kind="publish",
        source_draft=draft,
        review=review,
        restored_from=None,
        latest_version=latest_version,
        latest_activation=latest_activation,
    )


def rollback_workflow(
    *,
    tenant_id: int,
    workflow_id: Any,
    publisher_user_id: int,
    target_version_id: Any,
    review_id: Any,
    idempotency_key: Any,
) -> dict[str, Any]:
    tenant_id = _positive_int(tenant_id, field="tenant_id")
    publisher_user_id = _positive_int(publisher_user_id, field="publisher_user_id")
    workflow_id = _opaque_id(workflow_id, field="workflow_id")
    target_version_id = _opaque_id(target_version_id, field="target_version_id")
    review_id = _opaque_id(review_id, field="review_id")
    key = _idempotency(idempotency_key)
    request_hash = _digest(
        {
            "operation": "rollback",
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "target_version_id": target_version_id,
            "review_id": review_id,
            "publisher_user_id": publisher_user_id,
        }
    )
    replay = _publication_replay(
        tenant_id=tenant_id,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    _acquire_workflow_write_lock(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
    )
    replay = _publication_replay(
        tenant_id=tenant_id,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    target = WhatsAppWorkflowVersion.query.filter_by(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        id=target_version_id,
    ).one_or_none()
    if target is None:
        raise WorkflowStudioError(
            "workflow_rollback_target_not_found",
            "La version objetivo no existe dentro de este tenant y workflow.",
            status_code=404,
            action_hint="select_existing_published_version",
        )
    review = _approved_review(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        review_id=review_id,
        operation="rollback",
        subject_id=target.id,
        subject_digest=target.content_digest,
        publisher_user_id=publisher_user_id,
    )
    latest_version, latest_activation = _latest_version_and_activation(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
    )
    if latest_version is None or latest_activation is None:
        raise WorkflowStudioError(
            "workflow_rollback_without_active_version",
            "No existe una version activa que pueda reemplazarse.",
            status_code=409,
            action_hint="publish_first_version",
        )
    source_draft = WhatsAppWorkflowDraftRevision.query.filter_by(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        id=target.source_draft_revision_id,
    ).one_or_none()
    if source_draft is None:
        raise WorkflowStudioError(
            "workflow_publication_ledger_corrupt",
            "La version objetivo no conserva su revision de origen.",
            status_code=500,
            action_hint="contact_support",
        )
    return _append_publication_locked(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        actor_user_id=publisher_user_id,
        key=key,
        request_hash=request_hash,
        version_kind="rollback",
        source_draft=source_draft,
        review=review,
        restored_from=target,
        latest_version=latest_version,
        latest_activation=latest_activation,
    )


def get_workflow_ledger(*, tenant_id: int, workflow_id: Any) -> dict[str, Any]:
    tenant_id = _positive_int(tenant_id, field="tenant_id")
    workflow_id = _opaque_id(workflow_id, field="workflow_id")
    drafts = (
        WhatsAppWorkflowDraftRevision.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowDraftRevision.revision.asc())
        .all()
    )
    if not drafts:
        raise WorkflowStudioError(
            "workflow_not_found",
            "El workflow no existe dentro del tenant autenticado.",
            status_code=404,
            action_hint="select_existing_workflow",
        )
    reviews = (
        WhatsAppWorkflowReview.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowReview.reviewed_at.asc())
        .all()
    )
    versions = (
        WhatsAppWorkflowVersion.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowVersion.version.asc())
        .all()
    )
    activations = (
        WhatsAppWorkflowActivation.query.filter_by(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        .order_by(WhatsAppWorkflowActivation.sequence.asc())
        .all()
    )
    if len(versions) != len(activations):
        raise WorkflowStudioError(
            "workflow_publication_ledger_corrupt",
            "El ledger publicado no tiene una activacion por version.",
            status_code=500,
            action_hint="contact_support",
        )
    previous_activation_id = None
    for expected_sequence, (version, activation) in enumerate(
        zip(versions, activations),
        start=1,
    ):
        if (
            version.version != expected_sequence
            or activation.sequence != expected_sequence
            or activation.workflow_version_id != version.id
            or activation.activation_kind != version.version_kind
            or activation.previous_activation_id != previous_activation_id
        ):
            raise WorkflowStudioError(
                "workflow_publication_ledger_corrupt",
                "La secuencia inmutable de versiones y activaciones no es consistente.",
                status_code=500,
                action_hint="contact_support",
            )
        previous_activation_id = activation.id
    active_version_id = activations[-1].workflow_version_id if activations else None
    return {
        "contract_version": WORKFLOW_CONTROL_PLANE_CONTRACT_VERSION,
        "status": "ready",
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "latest_draft_revision": _draft_payload(drafts[-1]),
        "draft_revisions": [_draft_payload(row, include_document=False) for row in drafts],
        "reviews": [_review_payload(row) for row in reviews],
        "published_versions": [_version_payload(row) for row in versions],
        "activations": [_activation_payload(row) for row in activations],
        "active_version_id": active_version_id,
        "runtime_binding": {
            "available": False,
            "consumes_active_version": False,
            "reason_code": WORKFLOW_RUNTIME_BINDING_REASON,
        },
    }


def list_workflow_ledgers(*, tenant_id: int) -> dict[str, Any]:
    tenant_id = _positive_int(tenant_id, field="tenant_id")
    workflow_ids = [
        row[0]
        for row in (
            db.session.query(WhatsAppWorkflowDraftRevision.workflow_id)
            .filter(WhatsAppWorkflowDraftRevision.tenant_id == tenant_id)
            .distinct()
            .order_by(WhatsAppWorkflowDraftRevision.workflow_id.asc())
            .limit(101)
            .all()
        )
    ]
    truncated = len(workflow_ids) > 100
    workflow_ids = workflow_ids[:100]
    workflows = []
    for workflow_id in workflow_ids:
        ledger = get_workflow_ledger(tenant_id=tenant_id, workflow_id=workflow_id)
        workflows.append(
            {
                "workflow_id": workflow_id,
                "name": ledger["latest_draft_revision"]["draft"].get("name"),
                "latest_draft_revision": ledger["latest_draft_revision"]["revision"],
                "published_version_count": len(ledger["published_versions"]),
                "active_version_id": ledger["active_version_id"],
            }
        )
    return {
        "contract_version": WORKFLOW_CONTROL_PLANE_CONTRACT_VERSION,
        "status": "ready",
        "tenant_id": tenant_id,
        "workflows": workflows,
        "limit": 100,
        "truncated": truncated,
        "runtime_binding": {
            "available": False,
            "consumes_active_version": False,
            "reason_code": WORKFLOW_RUNTIME_BINDING_REASON,
        },
    }

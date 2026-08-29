"""Operational privacy lifecycle for survey responses.

The public intake path owns minimization before persistence.  This module owns
the complementary retention boundary: an expired source-anonymous response is
deleted only after its durable effects reached a terminal state, together with
its receipt, effect metadata and per-response analytics event.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
import argparse
import json
import uuid

from flask import current_app, has_app_context
from sqlalchemy import exists

from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)
from database import db
from models import (
    AnalyticsEventV2,
    AuditEvent,
    EncRespuesta,
    EncRespuestaDetalle,
    SurveyResponseEffect,
    SurveyResponseReceipt,
)


PRIVACY_MODE_SOURCE_ANONYMOUS = "source_anonymous"
RETENTION_PURGE_CONTRACT_VERSION = "surveys.privacy_retention_purge.v1"
_TERMINAL_EFFECT_STATUSES = frozenset({"succeeded", "skipped", "dead"})


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("limit must be an integer between 1 and 1000")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit must be an integer between 1 and 1000") from exc
    if parsed < 1 or parsed > 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    return parsed


def _retention_candidate_query(
    *,
    operation_now: datetime,
    tenant_id: Optional[int],
    limit: int,
    lock_for_purge: bool,
):
    """Build the deterministic retention batch claim.

    PostgreSQL keeps the selected response rows locked through the evidence
    deletes, audit insert and commit below. ``SKIP LOCKED`` lets overlapping
    Vercel cron invocations divide the backlog instead of purging and auditing
    the same response twice. SQLAlchemy intentionally omits the clause for
    SQLite, preserving the sequential single-writer test/runtime path.

    Dry runs do not claim work because they return without a commit and must
    not leave transaction-scoped row locks behind in a long-lived process.
    """

    non_terminal_effect_exists = exists().where(
        SurveyResponseEffect.response_id == EncRespuesta.id,
        SurveyResponseEffect.status.notin_(_TERMINAL_EFFECT_STATUSES),
    )
    query = EncRespuesta.query.filter(
        EncRespuesta.privacy_mode == PRIVACY_MODE_SOURCE_ANONYMOUS,
        EncRespuesta.retention_expires_at.isnot(None),
        EncRespuesta.retention_expires_at <= operation_now,
        ~non_terminal_effect_exists,
    )
    if tenant_id is not None:
        query = query.filter(EncRespuesta.tenant_id == tenant_id)
    query = query.order_by(
        EncRespuesta.retention_expires_at.asc(),
        EncRespuesta.id.asc(),
    ).limit(limit)
    if lock_for_purge:
        query = query.with_for_update(skip_locked=True)
    return query


def purge_expired_source_anonymous_responses(
    *,
    now: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
    limit: int = 200,
    dry_run: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Delete expired minimized responses after every durable effect is terminal.

    Pending/processing/retry effects are a hard stop for that row; deleting the
    source first would make the effect unrecoverable.  Terminal effect rows are
    removed in the same transaction as the response and its exactly-once
    receipt.  The audit record contains only aggregate counts and opaque survey
    ids, never response ids, fingerprints or answer content.
    """

    operation_now = _utc(now)
    if cutover_writer_fence_enabled(
        current_app.config if has_app_context() else None
    ):
        return {
            "contract_version": RETENTION_PURGE_CONTRACT_VERSION,
            "status": "fenced",
            "dry_run": bool(dry_run),
            "eligible": 0,
            "deleted": 0,
            "tenant_count": 0,
            "cutoff_at": operation_now.isoformat(),
        }
    bounded_limit = _bounded_limit(limit)
    parsed_tenant_id: Optional[int] = None
    if tenant_id is not None:
        try:
            parsed_tenant_id = int(tenant_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("tenant_id must be a positive integer") from exc
        if parsed_tenant_id <= 0:
            raise ValueError("tenant_id must be a positive integer")

    rows = _retention_candidate_query(
        operation_now=operation_now,
        tenant_id=parsed_tenant_id,
        limit=bounded_limit,
        lock_for_purge=not dry_run,
    ).all()
    response_ids = [int(row.id) for row in rows]
    tenant_ids = sorted({int(row.tenant_id) for row in rows})
    survey_ids_by_tenant: dict[int, set[int]] = {}
    for row in rows:
        survey_ids_by_tenant.setdefault(int(row.tenant_id), set()).add(
            int(row.encuesta_id)
        )

    result = {
        "contract_version": RETENTION_PURGE_CONTRACT_VERSION,
        "dry_run": bool(dry_run),
        "eligible": len(response_ids),
        "deleted": 0,
        "tenant_count": len(tenant_ids),
        "cutoff_at": operation_now.isoformat(),
    }
    if dry_run or not response_ids:
        return result

    entity_refs = [
        f"survey:{int(row.encuesta_id)}:response:{int(row.id)}" for row in rows
    ]
    try:
        AnalyticsEventV2.query.filter(
            AnalyticsEventV2.entity_ref.in_(entity_refs)
        ).delete(synchronize_session=False)
        SurveyResponseReceipt.query.filter(
            SurveyResponseReceipt.response_id.in_(response_ids)
        ).delete(synchronize_session=False)
        SurveyResponseEffect.query.filter(
            SurveyResponseEffect.response_id.in_(response_ids)
        ).delete(synchronize_session=False)
        EncRespuestaDetalle.query.filter(
            EncRespuestaDetalle.respuesta_id.in_(response_ids)
        ).delete(synchronize_session=False)
        EncRespuesta.query.filter(EncRespuesta.id.in_(response_ids)).delete(
            synchronize_session=False
        )

        batch_ref = f"survey-retention:{uuid.uuid4().hex}"
        for current_tenant_id, survey_ids in survey_ids_by_tenant.items():
            db.session.add(
                AuditEvent(
                    tenant_id=current_tenant_id,
                    actor_user_id=None,
                    event_type="survey_privacy_retention_purge",
                    resource_type="survey_response_batch",
                    resource_id=batch_ref,
                    details={
                        "contract_version": RETENTION_PURGE_CONTRACT_VERSION,
                        "deleted_count": sum(
                            1
                            for row in rows
                            if int(row.tenant_id) == current_tenant_id
                        ),
                        "survey_ids": sorted(survey_ids),
                        "cutoff_at": operation_now.isoformat(),
                        "source_identifiers_in_audit": False,
                    },
                    ip_address=None,
                )
            )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:
        if commit:
            db.session.rollback()
        raise

    result["deleted"] = len(response_ids)
    return result


def run_retention_purge_batches(
    *,
    now: Optional[datetime] = None,
    tenant_id: Optional[int] = None,
    batch_size: int = 200,
    max_batches: int = 20,
    dry_run: bool = False,
) -> dict[str, Any]:
    operation_now = _utc(now)
    if cutover_writer_fence_enabled(
        current_app.config if has_app_context() else None
    ):
        return {
            "contract_version": RETENTION_PURGE_CONTRACT_VERSION,
            "status": "fenced",
            "dry_run": bool(dry_run),
            "batches": 0,
            "eligible": 0,
            "deleted": 0,
            "cutoff_at": operation_now.isoformat(),
            "exhausted_batch_budget": False,
        }
    bounded_batch_size = _bounded_limit(batch_size)
    if isinstance(max_batches, bool):
        raise ValueError("max_batches must be an integer between 1 and 100")
    try:
        bounded_max_batches = int(max_batches)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_batches must be an integer between 1 and 100") from exc
    if bounded_max_batches < 1 or bounded_max_batches > 100:
        raise ValueError("max_batches must be an integer between 1 and 100")

    total_eligible = 0
    total_deleted = 0
    batches = 0
    for _ in range(bounded_max_batches):
        batch = purge_expired_source_anonymous_responses(
            now=operation_now,
            tenant_id=tenant_id,
            limit=bounded_batch_size,
            dry_run=dry_run,
            commit=True,
        )
        batches += 1
        total_eligible += int(batch["eligible"])
        total_deleted += int(batch["deleted"])
        if dry_run or int(batch["eligible"]) < bounded_batch_size:
            break

    return {
        "contract_version": RETENTION_PURGE_CONTRACT_VERSION,
        "dry_run": bool(dry_run),
        "batches": batches,
        "eligible": total_eligible,
        "deleted": total_deleted,
        "cutoff_at": operation_now.isoformat(),
        "exhausted_batch_budget": (
            not dry_run
            and batches == bounded_max_batches
            and total_eligible >= bounded_batch_size * bounded_max_batches
        ),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge expired source-anonymous survey responses.",
    )
    parser.add_argument("--tenant-id", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-batches", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if cutover_writer_fence_enabled():
        print(
            json.dumps(
                {
                    **background_writer_fence_report("survey_privacy_retention"),
                    "batches": 0,
                    "eligible": 0,
                    "deleted": 0,
                },
                sort_keys=True,
            )
        )
        return 0

    from app import create_app

    application = create_app()
    with application.app_context():
        result = run_retention_purge_batches(
            tenant_id=args.tenant_id,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, sort_keys=True))
    return 2 if result["exhausted_batch_budget"] else 0


__all__ = [
    "RETENTION_PURGE_CONTRACT_VERSION",
    "purge_expired_source_anonymous_responses",
    "run_retention_purge_batches",
]


if __name__ == "__main__":  # pragma: no cover - exercised by deployment smoke
    raise SystemExit(main())

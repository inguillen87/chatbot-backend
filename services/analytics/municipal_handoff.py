from datetime import datetime, timezone

from sqlalchemy import case, func, select

from extensions import db
from models import MunicipioTicket, MunicipioTicketHandoffEvent, TenantProfile
from services.municipio_ticket_handoff import (
    HANDOFF_ACCEPTED_AUDIT_EVENT,
    HANDOFF_ACCEPTANCE_COVERAGE,
    HANDOFF_REQUESTED_AUDIT_EVENT,
    handoff_acceptance_audit_exists,
)
from services.tenant_ticket_scope import municipio_ticket_scope_filter
from utils.time_utils import get_local_now


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def municipal_handoff_overview(
    tenant: TenantProfile, start: datetime, end: datetime, *, channel: str | None = None,
) -> dict:
    """Count accepted requests within the instrumented municipal inbox cohort."""
    start, cutoff = _utc(start), min(_utc(end), get_local_now())
    evidence = {
        "contract_version": "analytics.municipal_inbox_handoff.v1",
        "basis": "recorded_acceptances_of_tracked_municipal_inbox_requests",
        "coverage": HANDOFF_ACCEPTANCE_COVERAGE,
        "state": "unavailable",
        "request_count": None,
        "accepted_count": None,
        "period": {"start_at": start.isoformat(), "end_exclusive_at": cutoff.isoformat()},
    }
    if channel:
        return {**evidence, "reason_code": "municipal_handoff_intake_channel_unavailable"}
    if start >= cutoff:
        return {**evidence, "reason_code": "municipal_handoff_period_unavailable"}

    handoff = MunicipioTicketHandoffEvent
    accepted = handoff_acceptance_audit_exists(HANDOFF_ACCEPTED_AUDIT_EVENT, cutoff=cutoff)
    total, accepted_count = db.session.execute(
        select(func.count(), func.coalesce(func.sum(case((accepted, 1), else_=0)), 0))
        .select_from(handoff)
        .join(MunicipioTicket, MunicipioTicket.id == handoff.ticket_id)
        .where(
            handoff.tenant_id == tenant.id,
            municipio_ticket_scope_filter(tenant),
            handoff.contract_version == handoff.CONTRACT_VERSION,
            handoff.action == "handoff",
            handoff.status == "requested",
            handoff.created_at >= start,
            handoff.created_at < cutoff,
            handoff_acceptance_audit_exists(HANDOFF_REQUESTED_AUDIT_EVENT),
        )
    ).one()
    if not total:
        return {**evidence, "reason_code": "municipal_handoff_no_tracked_requests"}
    return {**evidence, "state": "observed", "request_count": total, "accepted_count": accepted_count}

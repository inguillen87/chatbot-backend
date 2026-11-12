from datetime import datetime, timezone

from sqlalchemy import and_, func, select

from extensions import db
from models import TenantProfile, TenantTicket, TenantTicketReplyEvent
from utils.roles import ROLE_ALIASES, ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN
from utils.time_utils import get_local_now


# Same human roles admitted by the omnichannel action writer. The role snapshot
# belongs to the reply event, so later changes to a user's role do not relabel it.
_OPERATOR_ROLES = {ROLE_TENANT_ADMIN, ROLE_EMPLEADO, ROLE_SUPERADMIN, "supervisor", "manager"}
_RECORDED_OPERATOR_ROLES = _OPERATOR_ROLES | {
    alias for alias, role in ROLE_ALIASES.items() if role in _OPERATOR_ROLES
}


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def inbox_first_reply_overview(
    tenant: TenantProfile, start: datetime, end: datetime, *, channel: str | None = None,
) -> dict:
    """Measure first recorded public inbox replies within the case intake window."""
    start, cutoff = _utc(start), min(_utc(end), get_local_now())
    evidence = {
        "contract_version": "analytics.inbox_first_public_reply.v1",
        "basis": "case_created_to_first_recorded_public_inbox_reply",
        "population": "tenant_tickets_created_in_period",
        "coverage": "durable_inbox_replies_only",
        "state": "unavailable",
        "average_minutes": None,
        "cohort_cases": None,
        "sample_cases": None,
        "period": {"start_at": start.isoformat(), "end_exclusive_at": cutoff.isoformat()},
    }
    if channel:
        return {**evidence, "reason_code": "inbox_reply_channel_scope_unavailable"}
    if start >= cutoff:
        return {**evidence, "reason_code": "inbox_reply_period_unavailable"}

    ticket, reply = TenantTicket, TenantTicketReplyEvent
    first_by_ticket = (
        select(ticket.created_at.label("opened_at"), func.min(reply.created_at).label("first_reply_at"))
        .select_from(ticket)
        .outerjoin(reply, and_(
            reply.ticket_id == ticket.id,
            reply.tenant_id == ticket.tenant_id,
            reply.contract_version == reply.CONTRACT_VERSION,
            reply.visibility == "public",
            reply.actor_user_id > 0,
            func.lower(func.trim(reply.actor_role)).in_(_RECORDED_OPERATOR_ROLES),
            reply.created_at >= ticket.created_at,
            reply.created_at < cutoff,
        ))
        .where(ticket.tenant_id == tenant.id, ticket.created_at >= start, ticket.created_at < cutoff)
        .group_by(ticket.id, ticket.created_at)
        .subquery()
    )
    opened_at, first_at = first_by_ticket.c.opened_at, first_by_ticket.c.first_reply_at
    if db.session.get_bind().dialect.name == "sqlite":
        elapsed_minutes = (func.julianday(first_at) - func.julianday(opened_at)) * 1440
    else:
        elapsed_minutes = func.extract("epoch", first_at - opened_at) / 60

    cohort_cases, sample_cases, average = db.session.execute(
        select(func.count(), func.count(elapsed_minutes), func.avg(elapsed_minutes)).select_from(first_by_ticket)
    ).one()
    evidence.update(cohort_cases=cohort_cases, sample_cases=sample_cases)
    if not sample_cases:
        reason = "inbox_reply_no_recorded_samples" if cohort_cases else "inbox_reply_no_cases_in_period"
        return {**evidence, "reason_code": reason}
    return {**evidence, "state": "observed", "average_minutes": round(float(average), 2)}

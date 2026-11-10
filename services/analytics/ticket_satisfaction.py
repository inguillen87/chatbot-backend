from datetime import datetime, timezone

from sqlalchemy import Integer, cast, func, select

from extensions import db
from models import MunicipioTicket, PymeTicket, TenantProfile, TicketSatisfaccion
from services.tenant_ticket_scope import municipio_ticket_scope_filter


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ticket_satisfaction_overview(
    tenant: TenantProfile, start: datetime, end: datetime, *, channel: str | None = None,
) -> dict:
    evidence = {
        "contract_version": "analytics.ticket_satisfaction.v1",
        "state": "unavailable",
        "rated_ticket_count": None,
        "basis": "first_valid_rating_recorded_at",
        "scale_verified": False,
        "label": "Casos calificados",
    }
    if channel:
        return {
            **evidence,
            "reason_code": "rating_channel_unavailable",
            "detail": "Las calificaciones no registran el canal en el que se recibieron.",
        }

    ticket_type = (tenant.tipo or "").strip().lower()
    if ticket_type == "municipio" and tenant.municipio_id is not None:
        model = MunicipioTicket
        tenant_scope = municipio_ticket_scope_filter(tenant)
    elif ticket_type == "pyme" and tenant.pyme_id is not None:
        model = PymeTicket
        tenant_scope = PymeTicket.tenant_id == tenant.id
    else:
        return {
            **evidence,
            "reason_code": "rating_tenant_type_unavailable",
            "detail": "No hay una fuente de calificaciones de casos para esta organización.",
        }

    start, end = _utc(start), _utc(end)
    if start > end:
        return {
            **evidence,
            "reason_code": "rating_period_invalid",
            "detail": "Seleccioná un período con la fecha de inicio anterior o igual a la de fin.",
        }

    rating = TicketSatisfaccion
    # Select the first stored valid score before filtering by date. A later
    # retry must not move a case into a new period, even if the first date is missing.
    first_rating_ids = (
        select(func.min(rating.id))
        .select_from(rating)
        .join(model, model.id == rating.ticket_id)
        .where(
            tenant_scope,
            rating.tipo == ticket_type,
            rating.puntuacion.between(0, 10),
            rating.puntuacion == cast(rating.puntuacion, Integer),
        )
        .group_by(rating.ticket_id)
    )
    count = db.session.scalar(
        select(func.count(rating.id)).where(rating.id.in_(first_rating_ids), rating.fecha.between(start, end))
    )
    return {
        **evidence,
        "state": "observed",
        "rated_ticket_count": count,
        "detail": "Cada caso cuenta una sola vez en el período de su primera calificación válida.",
    }

"""Database access helpers for analytics services."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import DefaultDict, Iterable, List, Sequence

from sqlalchemy import and_, func
from sqlalchemy.orm import Query

from extensions import db
from models import (
    ArchivoAdjunto,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TicketComentario,
    TicketSatisfaccion,
    User,
    TenantProfile,
)
from services.tenant_ticket_scope import (
    municipio_ticket_scope_filter,
    resolve_unique_tenant_for_owner,
    tenant_owner_ids,
)

from .filters import AnalyticsFilters
from .helpers import tenant_as_int
from .models import (
    AnalyticsCohortMetric,
    AnalyticsDailyMetric,
    AnalyticsGeoCell,
    AnalyticsTopMetric,
    AnalyticsWhatsappTemplate,
)


def _apply_date_range(query: Query, column, filters: AnalyticsFilters) -> Query:
    if filters.date_from:
        query = query.filter(column >= filters.date_from)
    if filters.date_to:
        query = query.filter(column <= filters.date_to)
    return query


def _apply_bbox(query: Query, model, filters: AnalyticsFilters) -> Query:
    if not filters.bbox:
        return query
    min_lon, min_lat, max_lon, max_lat = filters.bbox
    return query.filter(
        model.longitud.isnot(None),
        model.latitud.isnot(None),
        model.longitud.between(min_lon, max_lon),
        model.latitud.between(min_lat, max_lat),
    )


def _municipio_tenant_for_filters(filters: AnalyticsFilters) -> TenantProfile | None:
    tenant = None
    owner_id = tenant_as_int(filters.tenant_id)
    exact_tenant_id = getattr(filters, "tenant_profile_id", None)
    if exact_tenant_id is not None:
        tenant = db.session.get(TenantProfile, exact_tenant_id)
        if tenant is None or owner_id not in tenant_owner_ids(tenant):
            tenant = None
    elif owner_id is not None:
        try:
            resolution = resolve_unique_tenant_for_owner(owner_id)
        except ValueError:
            resolution = None
        if resolution is not None and resolution.status == "unique":
            tenant = resolution.tenant
    return tenant


def _analytics_snapshot_tenant_key(filters: AnalyticsFilters) -> str | None:
    """Return a legacy snapshot key only when it cannot mix municipal tenants."""

    if filters.scope != "municipio":
        return filters.tenant_id
    tenant = _municipio_tenant_for_filters(filters)
    if tenant is None:
        return None
    owners = tenant_owner_ids(tenant)
    if len(owners) != 1:
        return None
    try:
        resolution = resolve_unique_tenant_for_owner(owners[0])
    except ValueError:
        return None
    if (
        resolution.status != "unique"
        or resolution.tenant is None
        or int(resolution.tenant.id) != int(tenant.id)
    ):
        return None
    return str(owners[0])


def municipio_ticket_query(filters: AnalyticsFilters) -> Query:
    query = db.session.query(MunicipioTicket)
    tenant = _municipio_tenant_for_filters(filters)

    # ``municipio_ticket_scope_filter(None)`` is deliberately false: invalid,
    # orphaned and ambiguous legacy owner identifiers never become broad reads.
    query = query.filter(municipio_ticket_scope_filter(tenant))

    query = _apply_date_range(query, MunicipioTicket.fecha, filters)
    query = _apply_bbox(query, MunicipioTicket, filters)

    if filters.canales:
        query = query.filter(MunicipioTicket.canal_ingreso.in_(filters.canales))
    if filters.categorias:
        query = query.filter(MunicipioTicket.categoria.in_(filters.categorias))
    if filters.estados:
        query = query.filter(MunicipioTicket.estado.in_(filters.estados))
    if filters.zonas:
        query = query.filter(MunicipioTicket.distrito.in_(filters.zonas))

    return query


def pyme_ticket_query(filters: AnalyticsFilters) -> Query:
    tenant_id = tenant_as_int(filters.tenant_id)
    query = db.session.query(PymeTicket)
    if tenant_id is not None:
        query = query.filter(PymeTicket.user_id == tenant_id)
    else:
        query = query.filter(PymeTicket.user_id == filters.tenant_id)

    query = _apply_date_range(query, PymeTicket.fecha, filters)
    query = _apply_bbox(query, PymeTicket, filters)

    if filters.canales:
        query = query.filter(PymeTicket.canal.in_(filters.canales)) if hasattr(PymeTicket, "canal") else query
    if filters.categorias:
        query = query.filter(PymeTicket.categoria.in_(filters.categorias))
    if filters.estados:
        query = query.filter(PymeTicket.estado.in_(filters.estados))
    if filters.zonas:
        query = query.filter(PymeTicket.direccion.in_(filters.zonas))

    return query


def pyme_pedido_query(filters: AnalyticsFilters) -> Query:
    tenant_id = tenant_as_int(filters.tenant_id)
    query = db.session.query(PymePedido)
    if tenant_id is not None:
        query = query.filter(PymePedido.pyme_id == tenant_id)
    else:
        query = query.filter(PymePedido.pyme_id == filters.tenant_id)
    query = _apply_date_range(query, PymePedido.fecha, filters)
    query = _apply_bbox(query, PymePedido, filters)
    if filters.canales and hasattr(PymePedido, "canal"):
        query = query.filter(PymePedido.canal.in_(filters.canales))
    if filters.pyme_ids:
        query = query.filter(PymePedido.pyme_id.in_(filters.pyme_ids))
    return query


def load_municipio_dataset(filters: AnalyticsFilters):
    tickets = municipio_ticket_query(filters).all()
    ticket_ids = [ticket.id for ticket in tickets]
    comments: DefaultDict[int, List[TicketComentario]] = defaultdict(list)
    attachments: DefaultDict[int, List[ArchivoAdjunto]] = defaultdict(list)
    surveys: DefaultDict[int, List[TicketSatisfaccion]] = defaultdict(list)

    if ticket_ids:
        comment_query = db.session.query(TicketComentario).filter(
            TicketComentario.municipio_ticket_id.in_(ticket_ids)
        )
        if filters.agentes:
            comment_query = comment_query.filter(TicketComentario.user_id.in_(filters.agentes))
        for comment in comment_query.order_by(TicketComentario.fecha.asc()).all():
            comments[comment.municipio_ticket_id].append(comment)

        attachment_query = db.session.query(ArchivoAdjunto).filter(
            ArchivoAdjunto.municipio_ticket_id.in_(ticket_ids)
        )
        for attachment in attachment_query.all():
            attachments[attachment.municipio_ticket_id].append(attachment)

        survey_query = db.session.query(TicketSatisfaccion).filter(
            TicketSatisfaccion.ticket_id.in_(ticket_ids),
            TicketSatisfaccion.tipo == "municipio",
        )
        for survey in survey_query.all():
            surveys[survey.ticket_id].append(survey)

    return tickets, comments, attachments, surveys


def load_pyme_dataset(filters: AnalyticsFilters):
    tickets = pyme_ticket_query(filters).all()
    ticket_ids = [ticket.id for ticket in tickets]
    comments: DefaultDict[int, List[TicketComentario]] = defaultdict(list)
    attachments: DefaultDict[int, List[ArchivoAdjunto]] = defaultdict(list)
    surveys: DefaultDict[int, List[TicketSatisfaccion]] = defaultdict(list)

    if ticket_ids:
        comment_query = db.session.query(TicketComentario).filter(
            TicketComentario.pyme_ticket_id.in_(ticket_ids)
        )
        if filters.agentes:
            comment_query = comment_query.filter(TicketComentario.user_id.in_(filters.agentes))
        for comment in comment_query.order_by(TicketComentario.fecha.asc()).all():
            comments[comment.pyme_ticket_id].append(comment)

        attachment_query = db.session.query(ArchivoAdjunto).filter(
            ArchivoAdjunto.pyme_ticket_id.in_(ticket_ids)
        )
        for attachment in attachment_query.all():
            attachments[attachment.pyme_ticket_id].append(attachment)

        survey_query = db.session.query(TicketSatisfaccion).filter(
            TicketSatisfaccion.ticket_id.in_(ticket_ids),
            TicketSatisfaccion.tipo == "pyme",
        )
        for survey in survey_query.all():
            surveys[survey.ticket_id].append(survey)

    pedidos = pyme_pedido_query(filters).all()

    return tickets, pedidos, comments, attachments, surveys


def load_users(user_ids: Sequence[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    rows = db.session.query(User).filter(User.id.in_(user_ids)).all()
    return {row.id: row for row in rows}


def fetch_daily_metrics(filters: AnalyticsFilters, metric: str) -> List[AnalyticsDailyMetric]:
    tenant_key = _analytics_snapshot_tenant_key(filters)
    if tenant_key is None:
        return []
    query = db.session.query(AnalyticsDailyMetric).filter(
        AnalyticsDailyMetric.tenant_id == tenant_key,
        AnalyticsDailyMetric.scope == filters.scope,
        AnalyticsDailyMetric.metric == metric,
    )
    if filters.date_from:
        query = query.filter(AnalyticsDailyMetric.metric_date >= filters.date_from.date())
    if filters.date_to:
        query = query.filter(AnalyticsDailyMetric.metric_date <= filters.date_to.date())
    return query.order_by(AnalyticsDailyMetric.metric_date.asc()).all()


def fetch_geo_cells(filters: AnalyticsFilters) -> List[AnalyticsGeoCell]:
    tenant_key = _analytics_snapshot_tenant_key(filters)
    if tenant_key is None:
        return []
    query = db.session.query(AnalyticsGeoCell).filter(
        AnalyticsGeoCell.tenant_id == tenant_key,
        AnalyticsGeoCell.scope == filters.scope,
    )
    if filters.date_from:
        query = query.filter(AnalyticsGeoCell.metric_date >= filters.date_from.date())
    if filters.date_to:
        query = query.filter(AnalyticsGeoCell.metric_date <= filters.date_to.date())
    return query.order_by(AnalyticsGeoCell.metric_date.asc()).all()


def fetch_top_metrics(filters: AnalyticsFilters, category: str) -> List[AnalyticsTopMetric]:
    tenant_key = _analytics_snapshot_tenant_key(filters)
    if tenant_key is None:
        return []
    query = db.session.query(AnalyticsTopMetric).filter(
        AnalyticsTopMetric.tenant_id == tenant_key,
        AnalyticsTopMetric.scope == filters.scope,
        AnalyticsTopMetric.category == category,
    )
    if filters.date_from:
        query = query.filter(AnalyticsTopMetric.metric_date >= filters.date_from.date())
    if filters.date_to:
        query = query.filter(AnalyticsTopMetric.metric_date <= filters.date_to.date())
    return query.order_by(AnalyticsTopMetric.value.desc()).all()


def fetch_cohorts(filters: AnalyticsFilters) -> List[AnalyticsCohortMetric]:
    tenant_key = _analytics_snapshot_tenant_key(filters)
    if tenant_key is None:
        return []
    query = db.session.query(AnalyticsCohortMetric).filter(
        AnalyticsCohortMetric.tenant_id == tenant_key
    )
    return query.order_by(AnalyticsCohortMetric.cohort_key.asc()).all()


def fetch_whatsapp_templates(filters: AnalyticsFilters) -> List[AnalyticsWhatsappTemplate]:
    tenant_key = _analytics_snapshot_tenant_key(filters)
    if tenant_key is None:
        return []
    query = db.session.query(AnalyticsWhatsappTemplate).filter(
        AnalyticsWhatsappTemplate.tenant_id == tenant_key
    )
    if filters.date_from:
        query = query.filter(AnalyticsWhatsappTemplate.metric_date >= filters.date_from.date())
    if filters.date_to:
        query = query.filter(AnalyticsWhatsappTemplate.metric_date <= filters.date_to.date())
    return query.order_by(AnalyticsWhatsappTemplate.metric_date.asc(), AnalyticsWhatsappTemplate.template_name.asc()).all()

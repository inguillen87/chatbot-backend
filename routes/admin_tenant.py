from flask import Blueprint, request, jsonify, g, current_app
import requests
import uuid
from sqlalchemy import Numeric, and_, case, cast, func, or_
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime, timezone, timedelta

from cutover_writer_fence import cutover_writer_view
from utils.auth_helpers import obtener_token, token_requerido, user_from_token
from utils.permissions import require_role
from utils.roles import is_authorized_superadmin_user
from utils.tenant_admin_access import can_manage_tenant_control_plane
from middleware.tenant_context import require_tenant
from models import (
    CatalogoItem,
    db,
    TenantProfile,
    User,
    TenantConfig,
    Role,
    UserRole,
    CategoriaTicket,
    IntegrationAccount,
    PymePedido,
    MarketOrder,
    PedidoConversacional,
    Order,
    MunicipioTicket,
    PymeTicket,
    EncEncuesta,
    EncRespuesta,
    TicketComentario,
    TicketRealtimeState,
    TenantTicket,
    Promocion,
)
from routes.catalogo import _formatear_producto
from routes.carrito import _product_query_for_tenant
from services.commerce_unified import dedupe_unified_orders, serialize_unified_order, summarize_unified_orders
from services.common_utils import parse_precio_flexible
from services.catalog_seed import ensure_seed_catalog
from services.catalog_inventory import inventory_columns_contract, inventory_contract, new_catalog_version
from services.embedding_service import embed_textos_llm
from services.pymes import tiene_archivo_catalogo
from services.qdrant_service import index_catalog_item
from services.tenant_factory import create_tenant_from_template, assign_number_to_tenant
from services.tenant_resolver import apply_tenant_alias
from services.survey_response_provenance import (
    SURVEY_RESPONSE_ORIGIN_REAL,
    SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
    SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    build_survey_response_provenance,
)
from services.employee_ticket_access import apply_employee_ticket_category_scope
from services.operational_heatmap_access import (
    EMPLOYEE_HEATMAP_K_MIN,
    EMPLOYEE_HEATMAP_COORDINATE_PRECISION,
    build_employee_aggregated_heatmap,
    employee_heatmap_scope_empty,
    is_employee_heatmap_viewer,
)
from services.tenant_ticket_scope import (
    municipio_ticket_belongs_to_tenant,
    municipio_ticket_scope_filter,
    scoped_municipio_ticket_query,
)
from services.plan_access import (
    FULL_INTEGRATION_PLANS,
    integration_access_payload,
    integration_plan_required_payload,
    normalize_plan,
    plan_allows_full_integrations,
)
from services.live_chat_schedule import build_live_chat_status, build_schedule_from_config
from services.operational_scoring import build_ticket_priority_score
from services.ticket_realtime_state import (
    build_ticket_collaboration_state,
    build_ticket_collaboration_states,
)
from services.twilio_tech_provider import build_twilio_tech_provider_contract
from services.employee_routing import (
    normalize_scope_list,
    tenant_operational_dimensions,
    workload_by_employee,
)

admin_tenant_bp = Blueprint('admin_tenant_bp', __name__)

TENANT_CREATION_ALLOWED_PLANS = {
    "free",
    "pro",
    "full",
    "enterprise",
    "premium",
    *FULL_INTEGRATION_PLANS,
}

TENANT_CREATION_PLAN_ALIASES = {
    "gratis": "free",
    "gratuito": "free",
    "demo": "free",
    "trial": "free",
    "basic": "free",
    "starter": "free",
}


def _normalize_tenant_creation_plan(raw_plan) -> str:
    normalized = normalize_plan(raw_plan) or "free"
    normalized = TENANT_CREATION_PLAN_ALIASES.get(normalized, normalized)
    if normalized not in TENANT_CREATION_ALLOWED_PLANS:
        allowed = ", ".join(sorted(TENANT_CREATION_ALLOWED_PLANS))
        raise ValueError(f"plan must be one of: {allowed}")
    return normalized


def _bool_from_payload(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "si", "sí", "s"}
    return False


def _optional_tenant_creation_actor() -> User | None:
    token = obtener_token()
    if not token:
        return None
    return user_from_token(token)


def _cors_preflight_response():
    response = jsonify({"status": "ok"})
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS,PUT,DELETE,PATCH")
    return response


def _sanitize_personalization_options_for_storage(raw_options):
    if raw_options is None:
        return None
    if not isinstance(raw_options, list):
        raise ValueError("personalization_options must be a list")

    sanitized = []
    allowed_types = {"text", "select", "multiselect", "number", "boolean"}
    for idx, option in enumerate(raw_options):
        if not isinstance(option, dict):
            raise ValueError("personalization_options entries must be objects")

        option_id = str(option.get("id") or option.get("key") or f"opt_{idx+1}").strip()
        label = str(option.get("label") or option.get("nombre") or "").strip()
        if not option_id or not label:
            raise ValueError("personalization options require id and label")

        option_type = str(option.get("type") or option.get("tipo") or "text").strip().lower()
        if option_type not in allowed_types:
            raise ValueError("invalid personalization option type")

        values = option.get("values") or option.get("opciones") or []
        values_out = []
        if values is not None:
            if not isinstance(values, list):
                raise ValueError("personalization option values must be a list")
            for value in values:
                if isinstance(value, dict):
                    value_text = str(value.get("value") or value.get("label") or "").strip()
                    if not value_text:
                        continue
                    try:
                        price_delta = float(value.get("price_delta", 0) or 0)
                    except (TypeError, ValueError):
                        raise ValueError("invalid price_delta in personalization option")
                    values_out.append({"value": value_text[:120], "price_delta": price_delta})
                else:
                    value_text = str(value).strip()
                    if value_text:
                        values_out.append({"value": value_text[:120], "price_delta": 0.0})

        out = {
            "id": option_id[:60],
            "label": label[:120],
            "type": option_type,
            "required": bool(option.get("required", False)),
            "values": values_out[:50],
        }

        if option.get("max_length") is not None:
            try:
                out["max_length"] = max(1, min(int(option.get("max_length")), 500))
            except (TypeError, ValueError):
                raise ValueError("invalid max_length in personalization option")

        if option.get("max_select") is not None:
            try:
                out["max_select"] = max(1, min(int(option.get("max_select")), 20))
            except (TypeError, ValueError):
                raise ValueError("invalid max_select in personalization option")

        help_text = str(option.get("help_text") or "").strip()
        if help_text:
            out["help_text"] = help_text[:200]

        sanitized.append(out)

    return sanitized


def _is_authorized_for_tenant(current_user: User, tenant: TenantProfile) -> bool:
    """Return True if ``current_user`` can manage the given tenant.

    Besides the explicit ``tenant_id`` match used in most flows, admins may be
    linked as the owning municipality/pyme user without ``tenant_id`` filled in.
    Allow those owners (and platform admins) to administer employees to avoid
    false 403 responses when legacy data lacks ``tenant_id``.
    """

    if not current_user or not tenant:
        return False

    if is_authorized_superadmin_user(current_user):
        return True

    if current_user.tenant_id and current_user.tenant_id == tenant.id:
        return True

    if getattr(current_user, "tenant_slug", None) and tenant.slug and current_user.tenant_slug.lower() == tenant.slug.lower():
        return True

    # Unconditional Owner Check (Strongest)
    # Handle int vs str comparison just in case
    if tenant.municipio_id and str(current_user.id) == str(tenant.municipio_id):
        return True
    if tenant.pyme_id and str(current_user.id) == str(tenant.pyme_id):
        return True

    # 3) fallback LEGACY municipio
    if getattr(current_user, "tipo_chat", None) == "municipio":
        # a) usuario tiene municipio_id apuntando al tenant (ID)
        if getattr(current_user, "municipio_id", None) and str(current_user.municipio_id) == str(tenant.id):
            return True
        # c) usuario es empleado del dueño (mismo municipio_id)
        if getattr(tenant, "municipio_id", None) and getattr(current_user, "municipio_id", None):
            if str(current_user.municipio_id) == str(tenant.municipio_id):
                return True

    # 4) fallback LEGACY pyme/empresa
    tipo_chat = getattr(current_user, "tipo_chat", "")
    if tipo_chat in ("pyme", "empresa"):
        if getattr(current_user, "empresa_id", None) and current_user.empresa_id == tenant.id:
            return True
        if getattr(current_user, "pyme_id", None) and current_user.pyme_id == tenant.id:
             return True
        # Check affiliation (employee/admin of same owner)
        if getattr(tenant, "pyme_id", None):
            owner_id = tenant.pyme_id
            if getattr(current_user, "empresa_id", None) == owner_id:
                return True
            if getattr(current_user, "pyme_id", None) == owner_id:
                return True

    return False


def _survey_response_counts_subquery(tenant_id: int):
    """Return one bounded aggregate row per survey for real/demo provenance."""

    return (
        db.session.query(
            EncRespuesta.encuesta_id.label("encuesta_id"),
            func.sum(
                case(
                    (EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL, 1),
                    else_=0,
                )
            ).label("real_count"),
            func.sum(
                case(
                    (
                        EncRespuesta.response_origin
                        == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
                        1,
                    ),
                    else_=0,
                )
            ).label("synthetic_count"),
            func.sum(
                case(
                    (
                        EncRespuesta.response_origin
                        == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
                        1,
                    ),
                    else_=0,
                )
            ).label("unverified_count"),
        )
        .filter(EncRespuesta.tenant_id == tenant_id)
        .group_by(EncRespuesta.encuesta_id)
        .subquery()
    )


def _build_tenant_dashboard_bundle_payload(
    tenant: TenantProfile,
    *,
    viewer: User,
    leads_limit: int = 100,
    surveys_limit: int = 20,
    unread_limit: int = 30,
    since_minutes: int = 1440,
) -> dict:
    now = datetime.now(timezone.utc)
    cutoff_unread = now - timedelta(minutes=since_minutes)

    lead_rows = []
    municipio_leads_query = apply_employee_ticket_category_scope(
        scoped_municipio_ticket_query(tenant),
        viewer,
        MunicipioTicket,
    )
    for ticket in (
        municipio_leads_query.order_by(
            MunicipioTicket.ultima_actividad.desc(),
            MunicipioTicket.id.desc(),
        )
        .limit(leads_limit)
        .all()
    ):
        lead_rows.append(("municipio", ticket))
    pyme_leads_query = apply_employee_ticket_category_scope(
        PymeTicket.query.filter_by(tenant_id=tenant.id),
        viewer,
        PymeTicket,
    )
    for ticket in (
        pyme_leads_query.order_by(PymeTicket.fecha.desc(), PymeTicket.id.desc())
        .limit(leads_limit)
        .all()
    ):
        lead_rows.append(("pyme", ticket))

    def _lead_rollup(query, model, activity_column):
        state_expr = func.lower(func.coalesce(model.estado, "nuevo"))
        total_expr = func.count(model.id)
        breached_expr = func.sum(
            case(
                (
                    and_(
                        activity_column < now - timedelta(minutes=30),
                        state_expr.notin_(
                            {
                                "ganado",
                                "perdido",
                                "cerrado",
                                "cerrada",
                                "cancelado",
                                "cancelada",
                                "resuelto",
                                "resuelta",
                            }
                        ),
                    ),
                    1,
                ),
                else_=0,
            )
        )
        rows = (
            query.with_entities(
                state_expr.label("stage"),
                total_expr.label("total"),
                breached_expr.label("sla_breached"),
            )
            .group_by(state_expr)
            .all()
        )
        return (
            sum(int(row.total or 0) for row in rows),
            {
                str(row.stage or "nuevo"): int(row.total or 0)
                for row in rows
            },
            sum(int(row.sla_breached or 0) for row in rows),
        )

    muni_total, muni_by_stage, muni_sla = _lead_rollup(
        municipio_leads_query,
        MunicipioTicket,
        func.coalesce(MunicipioTicket.ultima_actividad, MunicipioTicket.fecha),
    )
    pyme_total, pyme_by_stage, pyme_sla = _lead_rollup(
        pyme_leads_query,
        PymeTicket,
        PymeTicket.fecha,
    )
    total_leads = muni_total + pyme_total
    lead_items = []
    by_stage = dict(muni_by_stage)
    for stage, count in pyme_by_stage.items():
        by_stage[stage] = by_stage.get(stage, 0) + count
    sla_breached = muni_sla + pyme_sla
    total_active_viewers = 0
    total_unread_viewers = 0

    lead_collaboration = {
        "municipio": build_ticket_collaboration_states(
            ticket_type="municipio",
            ticket_ids=[
                ticket.id
                for ticket_type, ticket in lead_rows
                if ticket_type == "municipio"
            ],
        ),
        "pyme": build_ticket_collaboration_states(
            ticket_type="pyme",
            ticket_ids=[
                ticket.id
                for ticket_type, ticket in lead_rows
                if ticket_type == "pyme"
            ],
        ),
    }

    for ticket_type, ticket in lead_rows:
        details = _ticket_details(ticket)
        stage = str(details.get('lead_stage') or ticket.estado or 'nuevo').lower()
        last_seen = getattr(ticket, 'ultima_actividad', None) or ticket.fecha
        last_dt = last_seen if (last_seen and last_seen.tzinfo) else (last_seen.replace(tzinfo=timezone.utc) if last_seen else None)
        ticket_sla = bool(last_dt and (now - last_dt).total_seconds() > 1800 and stage not in {'ganado', 'perdido'})
        collaboration_state = lead_collaboration[ticket_type].get(
            int(ticket.id),
            {
                "active_viewers_count": 0,
                "idle_viewers_count": 0,
                "unread_viewer_count": 0,
                "operational_status": "healthy",
            },
        )
        priority_meta = build_ticket_priority_score(
            sla_breached_flag=ticket_sla,
            collaboration_state=collaboration_state,
            stage=stage,
        )
        priority_score = priority_meta["score"]
        total_active_viewers += collaboration_state.get('active_viewers_count', 0) or 0
        total_unread_viewers += collaboration_state.get('unread_viewer_count', 0) or 0
        lead_items.append({
            'ticket_type': ticket_type,
            'ticket_id': ticket.id,
            'nombre': getattr(ticket, 'nombre_vecino', None) or getattr(ticket, 'nombre_cliente', None),
            'categoria': getattr(ticket, 'categoria', None),
            'stage': stage,
            'status': ticket.estado,
            'sla_breached': ticket_sla,
            'last_seen': last_seen.isoformat() if last_seen else None,
            'collaboration_state': collaboration_state,
            'priority_score': priority_score,
            'priority_breakdown': priority_meta['breakdown'],
            'priority_reasons': priority_meta['reasons'],
        })

    lead_items.sort(
        key=lambda item: (
            item.get('priority_score') or 0,
            (item.get('sla_breached') is True),
            item.get('last_seen') or '',
        ),
        reverse=True,
    )

    response_counts = _survey_response_counts_subquery(tenant.id)
    survey_rows = (
        db.session.query(
            EncEncuesta,
            func.coalesce(response_counts.c.real_count, 0).label("real_count"),
            func.coalesce(response_counts.c.synthetic_count, 0).label("synthetic_count"),
            func.coalesce(response_counts.c.unverified_count, 0).label("unverified_count"),
            func.sum(func.coalesce(response_counts.c.real_count, 0))
            .over()
            .label("all_real_count"),
            func.sum(func.coalesce(response_counts.c.synthetic_count, 0))
            .over()
            .label("all_synthetic_count"),
            func.sum(func.coalesce(response_counts.c.unverified_count, 0))
            .over()
            .label("all_unverified_count"),
            func.count(EncEncuesta.id).over().label("all_survey_count"),
        )
        .outerjoin(response_counts, response_counts.c.encuesta_id == EncEncuesta.id)
        .filter(EncEncuesta.tenant_id == tenant.id)
        .order_by(EncEncuesta.updated_at.desc())
        .limit(surveys_limit)
        .all()
    )
    survey_items = []
    total_responses = int(survey_rows[0].all_real_count or 0) if survey_rows else 0
    total_synthetic_responses = (
        int(survey_rows[0].all_synthetic_count or 0) if survey_rows else 0
    )
    total_unverified_responses = (
        int(survey_rows[0].all_unverified_count or 0) if survey_rows else 0
    )
    total_surveys = int(survey_rows[0].all_survey_count or 0) if survey_rows else 0
    for row in survey_rows:
        survey = row[0]
        real_count = row.real_count
        responses_count = int(real_count or 0)
        survey_items.append({
            'id': survey.id,
            'slug': survey.slug,
            'titulo': survey.titulo,
            'estado': survey.estado,
            'tipo': survey.tipo,
            'respuestas': responses_count,
            'updated_at': survey.updated_at.isoformat() if survey.updated_at else None,
        })

    unread_items = []
    muni_unread_query = (
        db.session.query(TicketComentario.municipio_ticket_id, func.count(TicketComentario.id), func.max(TicketComentario.fecha))
        .join(MunicipioTicket, MunicipioTicket.id == TicketComentario.municipio_ticket_id)
        .filter(
            municipio_ticket_scope_filter(tenant),
            TicketComentario.es_admin.is_(False),
            TicketComentario.fecha >= cutoff_unread,
        )
    )
    muni_unread_scoped = apply_employee_ticket_category_scope(
        muni_unread_query,
        viewer,
        MunicipioTicket,
    ).group_by(TicketComentario.municipio_ticket_id)
    muni_unread_total = int(muni_unread_scoped.count())
    muni_unread = (
        muni_unread_scoped.order_by(func.max(TicketComentario.fecha).desc())
        .limit(unread_limit)
        .all()
    )
    muni_unread_collaboration = build_ticket_collaboration_states(
        ticket_type="municipio",
        ticket_ids=[int(ticket_id) for ticket_id, _count, _last_at in muni_unread],
    )
    for ticket_id, unread_count, last_at in muni_unread:
        collaboration_state = muni_unread_collaboration.get(int(ticket_id), {})
        unread_items.append({
            'ticket_type': 'municipio',
            'ticket_id': ticket_id,
            'unread_count': int(unread_count or 0),
            'last_message_at': last_at.isoformat() if last_at else None,
            'collaboration_state': collaboration_state,
        })

    pyme_unread_query = (
        db.session.query(TicketComentario.pyme_ticket_id, func.count(TicketComentario.id), func.max(TicketComentario.fecha))
        .join(PymeTicket, PymeTicket.id == TicketComentario.pyme_ticket_id)
        .filter(
            PymeTicket.tenant_id == tenant.id,
            TicketComentario.es_admin.is_(False),
            TicketComentario.fecha >= cutoff_unread,
        )
    )
    pyme_unread_scoped = apply_employee_ticket_category_scope(
        pyme_unread_query,
        viewer,
        PymeTicket,
    ).group_by(TicketComentario.pyme_ticket_id)
    pyme_unread_total = int(pyme_unread_scoped.count())
    pyme_unread = (
        pyme_unread_scoped.order_by(func.max(TicketComentario.fecha).desc())
        .limit(unread_limit)
        .all()
    )
    pyme_unread_collaboration = build_ticket_collaboration_states(
        ticket_type="pyme",
        ticket_ids=[int(ticket_id) for ticket_id, _count, _last_at in pyme_unread],
    )
    for ticket_id, unread_count, last_at in pyme_unread:
        collaboration_state = pyme_unread_collaboration.get(int(ticket_id), {})
        unread_items.append({
            'ticket_type': 'pyme',
            'ticket_id': ticket_id,
            'unread_count': int(unread_count or 0),
            'last_message_at': last_at.isoformat() if last_at else None,
            'collaboration_state': collaboration_state,
        })

    unread_items.sort(key=lambda item: item.get('last_message_at') or '', reverse=True)
    total_unread_tickets = muni_unread_total + pyme_unread_total

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()
    employee_ids = [int(employee.id) for employee in employees]
    workload_by_employee_id = {employee_id: 0 for employee_id in employee_ids}
    collaboration_by_employee_id = {
        employee_id: {
            "active": set(),
            "idle": set(),
            "unread": set(),
        }
        for employee_id in employee_ids
    }
    if employee_ids:
        active_states = {"nuevo", "pendiente", "en_proceso"}
        municipio_workload_query = db.session.query(
            MunicipioTicket.asignado_a_id,
            func.count(MunicipioTicket.id),
        ).filter(
            municipio_ticket_scope_filter(tenant),
            MunicipioTicket.asignado_a_id.in_(employee_ids),
            MunicipioTicket.estado.in_(active_states),
        )
        municipio_workload_rows = apply_employee_ticket_category_scope(
            municipio_workload_query,
            viewer,
            MunicipioTicket,
        ).group_by(MunicipioTicket.asignado_a_id).all()

        pyme_conditions = [PymeTicket.tenant_id == tenant.id]
        if getattr(tenant, "pyme_id", None):
            owner = db.session.get(User, tenant.pyme_id)
            if getattr(owner, "rubro_id", None):
                pyme_conditions.append(PymeTicket.rubro_id == owner.rubro_id)
        pyme_workload_query = db.session.query(
            PymeTicket.asignado_a_id,
            func.count(PymeTicket.id),
        ).filter(
            or_(*pyme_conditions),
            PymeTicket.asignado_a_id.in_(employee_ids),
            PymeTicket.estado.in_(active_states),
        )
        pyme_workload_rows = apply_employee_ticket_category_scope(
            pyme_workload_query,
            viewer,
            PymeTicket,
        ).group_by(PymeTicket.asignado_a_id).all()
        for employee_id, count in [
            *municipio_workload_rows,
            *pyme_workload_rows,
        ]:
            normalized_id = int(employee_id)
            workload_by_employee_id[normalized_id] = (
                workload_by_employee_id.get(normalized_id, 0) + int(count or 0)
            )

        municipio_realtime_query = (
            TicketRealtimeState.query.join(
                MunicipioTicket,
                (TicketRealtimeState.ticket_type == "municipio")
                & (TicketRealtimeState.ticket_id == MunicipioTicket.id),
            ).filter(
                TicketRealtimeState.viewer_user_id.in_(employee_ids),
                municipio_ticket_scope_filter(tenant),
            )
        )
        municipio_realtime_rows = apply_employee_ticket_category_scope(
            municipio_realtime_query,
            viewer,
            MunicipioTicket,
        ).all()
        pyme_realtime_query = (
            TicketRealtimeState.query.join(
                PymeTicket,
                (TicketRealtimeState.ticket_type == "pyme")
                & (TicketRealtimeState.ticket_id == PymeTicket.id),
            ).filter(
                TicketRealtimeState.viewer_user_id.in_(employee_ids),
                PymeTicket.tenant_id == tenant.id,
            )
        )
        pyme_realtime_rows = apply_employee_ticket_category_scope(
            pyme_realtime_query,
            viewer,
            PymeTicket,
        ).all()
        team_collaboration = {
            "municipio": build_ticket_collaboration_states(
                ticket_type="municipio",
                ticket_ids=[int(row.ticket_id) for row in municipio_realtime_rows],
            ),
            "pyme": build_ticket_collaboration_states(
                ticket_type="pyme",
                ticket_ids=[int(row.ticket_id) for row in pyme_realtime_rows],
            ),
        }
        for row in [*municipio_realtime_rows, *pyme_realtime_rows]:
            employee_id = int(row.viewer_user_id)
            ticket_key = (str(row.ticket_type), int(row.ticket_id))
            state = team_collaboration[str(row.ticket_type)].get(int(row.ticket_id), {})
            metrics = collaboration_by_employee_id[employee_id]
            if state.get("active_viewers_count", 0):
                metrics["active"].add(ticket_key)
            if state.get("idle_viewers_count", 0):
                metrics["idle"].add(ticket_key)
            if state.get("unread_viewer_count", 0):
                metrics["unread"].add(ticket_key)

    workload_items = []
    for emp in employees:
        collaboration_metrics = collaboration_by_employee_id.get(
            int(emp.id),
            {"active": set(), "idle": set(), "unread": set()},
        )
        workload_items.append({
            'employee_id': emp.id,
            'name': emp.name,
            'email': emp.email,
            'workload_open_tickets': int(workload_by_employee_id.get(int(emp.id), 0)),
            'scope': _employee_scope(emp),
            'active_ticket_views': len(collaboration_metrics['active']),
            'idle_ticket_views': len(collaboration_metrics['idle']),
            'unread_ticket_views': len(collaboration_metrics['unread']),
        })
    workload_items.sort(key=lambda item: item['workload_open_tickets'], reverse=True)

    recommended_actions = []
    if sla_breached:
        recommended_actions.append({
            'kind': 'review_sla',
            'priority': 'high',
            'message': 'Hay leads abiertos con SLA vencido o sin respuesta reciente.',
        })
    if unread_items:
        recommended_actions.append({
            'kind': 'reply_unread',
            'priority': 'high',
            'message': 'Hay conversaciones de tickets con mensajes sin leer.',
        })
    if workload_items and workload_items[0].get('workload_open_tickets', 0) >= 5:
        recommended_actions.append({
            'kind': 'rebalance_team',
            'priority': 'medium',
            'message': 'Conviene redistribuir tickets entre empleados.',
        })
    if not survey_items:
        recommended_actions.append({
            'kind': 'launch_survey',
            'priority': 'medium',
            'message': 'No hay encuestas recientes para medir feedback del tenant.',
        })

    return {
        'tenant': {
            'id': tenant.id,
            'slug': tenant.slug,
            'nombre': tenant.nombre,
            'tipo': tenant.tipo,
            'plan': tenant.plan,
            'is_active': bool(getattr(tenant, 'is_active', True)),
        },
        'summary': {
            'total_leads': total_leads,
            'sla_breached': sla_breached,
            'total_surveys': total_surveys,
            'total_survey_responses': total_responses,
            'tickets_with_unread': total_unread_tickets,
            'employees': len(workload_items),
            'active_viewers': total_active_viewers,
            'unread_viewers': total_unread_viewers,
        },
        'leads': {
            'total': total_leads,
            'by_stage': by_stage,
            'items': lead_items[:leads_limit],
            'items_partial': len(lead_items) < total_leads,
        },
        'surveys': {
            'total_surveys': total_surveys,
            'total_responses': total_responses,
            'response_provenance': build_survey_response_provenance(
                real_count=total_responses,
                synthetic_count=total_synthetic_responses,
                unverified_count=total_unverified_responses,
                mode='real',
            ),
            'items': survey_items,
        },
        'unread': {
            'since_minutes': since_minutes,
            'total_tickets_with_unread': total_unread_tickets,
            'items': unread_items[:unread_limit],
            'items_partial': len(unread_items) < total_unread_tickets,
        },
        'team': {
            'items': workload_items,
        },
        'recommended_actions': recommended_actions,
    }


def _build_tenant_heatmap_summary_payload(
    tenant: TenantProfile,
    *,
    viewer: User,
    limit_points: int = 1500,
) -> dict:
    def _normalized_label(value, fallback: str) -> str:
        label = str(value or fallback).strip().lower()
        return label or fallback

    effective_limit = max(1, min(int(limit_points or 1500), 5000))

    def _scoped_ticket_query(model):
        base_query = (
            scoped_municipio_ticket_query(tenant)
            if model is MunicipioTicket
            else model.query.filter_by(tenant_id=tenant.id)
        )
        return apply_employee_ticket_category_scope(
            base_query,
            viewer,
            model,
        )

    municipio_query = _scoped_ticket_query(MunicipioTicket)
    pyme_query = _scoped_ticket_query(PymeTicket)

    if is_employee_heatmap_viewer(viewer):
        def _ticket_cell_query(query, model):
            lat_cell = func.round(
                cast(model.latitud, Numeric),
                EMPLOYEE_HEATMAP_COORDINATE_PRECISION,
            )
            lng_cell = func.round(
                cast(model.longitud, Numeric),
                EMPLOYEE_HEATMAP_COORDINATE_PRECISION,
            )
            return (
                query.with_entities(
                    lat_cell.label('lat'),
                    lng_cell.label('lng'),
                    func.count(model.id).label('count'),
                )
                .filter(
                    model.latitud.isnot(None),
                    model.longitud.isnot(None),
                    model.latitud.between(-90, 90),
                    model.longitud.between(-180, 180),
                )
                .group_by(lat_cell, lng_cell)
            )

        ticket_cell_union = _ticket_cell_query(
            municipio_query,
            MunicipioTicket,
        ).union_all(
            _ticket_cell_query(pyme_query, PymeTicket)
        ).subquery()
        combined_cell_count = func.sum(ticket_cell_union.c.count)
        combined_cells = (
            db.session.query(
                ticket_cell_union.c.lat.label('lat'),
                ticket_cell_union.c.lng.label('lng'),
                combined_cell_count.label('count'),
            )
            .group_by(ticket_cell_union.c.lat, ticket_cell_union.c.lng)
            .subquery()
        )
        safe_cell_rows = (
            db.session.query(
                combined_cells.c.lat,
                combined_cells.c.lng,
                combined_cells.c.count,
            )
            .filter(combined_cells.c.count >= EMPLOYEE_HEATMAP_K_MIN)
            .order_by(combined_cells.c.count.desc())
            .limit(effective_limit + 1)
            .all()
        )
        cells_partial = len(safe_cell_rows) > effective_limit
        safe_cell_rows = safe_cell_rows[:effective_limit]
        exact_cells = [
            {
                'lat': float(lat),
                'lng': float(lng),
                'count': int(count or 0),
                'weight': int(count or 0),
            }
            for lat, lng, count in safe_cell_rows
        ]
        safe_cells_count, safe_records, suppressed_cells, suppressed_records = (
            db.session.query(
                func.coalesce(
                    func.sum(
                        case(
                            (combined_cells.c.count >= EMPLOYEE_HEATMAP_K_MIN, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                combined_cells.c.count >= EMPLOYEE_HEATMAP_K_MIN,
                                combined_cells.c.count,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (combined_cells.c.count < EMPLOYEE_HEATMAP_K_MIN, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                combined_cells.c.count < EMPLOYEE_HEATMAP_K_MIN,
                                combined_cells.c.count,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .one()
        )
        now = datetime.now(timezone.utc)
        employee_heatmap = build_employee_aggregated_heatmap(
            tenant,
            now,
            now,
            exact_payload={'cells': exact_cells},
            scope_empty=employee_heatmap_scope_empty(viewer),
        )
        employee_heatmap['privacy']['suppressed'].update({
            'cells': int(suppressed_cells or 0),
            'records': int(suppressed_records or 0),
            'low_cardinality_cells': int(suppressed_cells or 0),
        })
        employee_heatmap['summary'].update({
            'points': int(safe_records or 0),
            'cells': int(safe_cells_count or 0),
            'aggregated_observations': int(safe_records or 0),
            'suppressed_cells': int(suppressed_cells or 0),
            'suppressed_records': int(suppressed_records or 0),
            'cells_partial': cells_partial,
            'cells_returned': len(exact_cells),
        })
        safe_cells = employee_heatmap.get('cells') or []
        safe_points = [
            {
                'source': 'ticket_aggregate',
                'ticket_type': 'aggregate',
                'lat': cell['lat'],
                'lon': cell['lng'],
                'lng': cell['lng'],
                'count': cell['count'],
                'weight': cell['weight'],
                'privacy_mode': 'employee_aggregated',
            }
            for cell in safe_cells[:effective_limit]
        ]
        return {
            'tenant_id': tenant.id,
            'tenant_slug': tenant.slug,
            'total': int(
                (employee_heatmap.get('summary') or {}).get(
                    'aggregated_observations',
                    0,
                )
                or 0
            ),
            'top_categories': [],
            'top_zones': [],
            'hotspots': [],
            'hotspot_pairs': [],
            'heatmap_points': safe_points,
            'cells': safe_cells,
            'privacy': employee_heatmap.get('privacy'),
            'render_contract': employee_heatmap.get('render_contract'),
            'response_provenance': build_survey_response_provenance(
                real_count=0,
                synthetic_count=0,
                mode='real',
            ),
        }

    def _ticket_projection_and_rollup(
        query,
        model,
        *,
        ticket_type: str,
        zone_column,
        activity_column,
    ):
        category_expr = case(
            (
                func.length(func.trim(model.categoria)) > 0,
                func.lower(func.trim(model.categoria)),
            ),
            else_='sin_categoria',
        )
        zone_expr = case(
            (
                func.length(func.trim(zone_column)) > 0,
                func.lower(func.trim(zone_column)),
            ),
            else_='sin_zona',
        )
        total = int(query.with_entities(func.count(model.id)).scalar() or 0)
        pair_rows = (
            query.with_entities(
                category_expr.label('categoria'),
                zone_expr.label('zona'),
                func.count(model.id).label('count'),
            )
            .group_by(category_expr, zone_expr)
            .order_by(func.count(model.id).desc())
            .limit(effective_limit + 1)
            .all()
        )
        pairs_partial = len(pair_rows) > effective_limit
        pair_rows = pair_rows[:effective_limit]
        point_rows = (
            query.with_entities(
                model.id.label('ticket_id'),
                category_expr.label('categoria'),
                zone_expr.label('zona'),
                model.latitud.label('lat'),
                model.longitud.label('lon'),
                model.estado.label('status'),
                activity_column.label('activity_at'),
            )
            .filter(
                model.latitud.isnot(None),
                model.longitud.isnot(None),
                model.latitud.between(-90, 90),
                model.longitud.between(-180, 180),
            )
            .order_by(activity_column.desc(), model.id.desc())
            .limit(effective_limit + 1)
            .all()
        )
        points_partial = len(point_rows) > effective_limit
        projected_points = [
            {
                'source': 'ticket',
                'ticket_type': ticket_type,
                'ticket_id': int(row.ticket_id),
                'categoria': _normalized_label(row.categoria, 'sin_categoria'),
                'zona': _normalized_label(row.zona, 'sin_zona'),
                'lat': float(row.lat),
                'lon': float(row.lon),
                'status': row.status,
                '_sort_at': row.activity_at,
            }
            for row in point_rows[:effective_limit]
        ]
        return (
            total,
            pair_rows,
            projected_points,
            pairs_partial,
            points_partial,
        )

    municipio_total, municipio_pairs, municipio_points, municipio_pairs_partial, municipio_points_partial = (
        _ticket_projection_and_rollup(
            municipio_query,
            MunicipioTicket,
            ticket_type='municipio',
            zone_column=MunicipioTicket.distrito,
            activity_column=func.coalesce(
                MunicipioTicket.ultima_actividad,
                MunicipioTicket.fecha,
            ),
        )
    )
    pyme_total, pyme_pairs, pyme_points, pyme_pairs_partial, pyme_points_partial = (
        _ticket_projection_and_rollup(
            pyme_query,
            PymeTicket,
            ticket_type='pyme',
            zone_column=PymeTicket.direccion,
            activity_column=PymeTicket.fecha,
        )
    )

    by_categoria = {}
    by_zona = {}
    hotspots = {}
    points = [*municipio_points, *pyme_points]
    for categoria, zona, count in [*municipio_pairs, *pyme_pairs]:
        normalized_category = _normalized_label(categoria, 'sin_categoria')
        normalized_zone = _normalized_label(zona, 'sin_zona')
        normalized_count = int(count or 0)
        by_categoria[normalized_category] = (
            by_categoria.get(normalized_category, 0) + normalized_count
        )
        by_zona[normalized_zone] = by_zona.get(normalized_zone, 0) + normalized_count
        hotspot_key = f"{normalized_category}::{normalized_zone}"
        hotspots[hotspot_key] = hotspots.get(hotspot_key, 0) + normalized_count

    survey_category = case(
        (EncEncuesta.es_votacion_envivo.is_(True), "votacion"),
        else_="encuesta",
    )
    survey_zone = case(
        (
            func.length(func.trim(EncRespuesta.barrio)) > 0,
            func.lower(func.trim(EncRespuesta.barrio)),
        ),
        (
            func.length(func.trim(EncRespuesta.ciudad)) > 0,
            func.lower(func.trim(EncRespuesta.ciudad)),
        ),
        (
            func.length(func.trim(EncRespuesta.provincia)) > 0,
            func.lower(func.trim(EncRespuesta.provincia)),
        ),
        (
            func.length(func.trim(EncRespuesta.pais)) > 0,
            func.lower(func.trim(EncRespuesta.pais)),
        ),
        else_="sin_zona",
    )
    real_survey_count, unverified_survey_count, synthetic_survey_count = db.session.query(
        func.coalesce(
            func.sum(
                case(
                    (EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL, 1),
                    else_=0,
                )
            ),
            0,
        ),
        func.coalesce(
            func.sum(
                case(
                    (
                        EncRespuesta.response_origin
                        == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ),
        func.coalesce(
            func.sum(
                case(
                    (
                        EncRespuesta.response_origin
                        == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ),
    ).filter(EncRespuesta.tenant_id == tenant.id).one()
    real_survey_count = int(real_survey_count or 0)
    synthetic_survey_count = int(synthetic_survey_count or 0)
    unverified_survey_count = int(unverified_survey_count or 0)

    survey_dimensions = (
        db.session.query(
            survey_category.label("categoria"),
            survey_zone.label("zona"),
            func.count(EncRespuesta.id).label("count"),
        )
        .join(EncEncuesta, EncEncuesta.id == EncRespuesta.encuesta_id)
        .filter(
            EncRespuesta.tenant_id == tenant.id,
            EncEncuesta.tenant_id == tenant.id,
            EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL,
        )
        .group_by(survey_category, survey_zone)
        .order_by(func.count(EncRespuesta.id).desc())
        .limit(effective_limit + 1)
        .all()
    )
    survey_dimensions_partial = len(survey_dimensions) > effective_limit
    for categoria, zona, count in survey_dimensions[:effective_limit]:
        count = int(count or 0)
        by_categoria[categoria] = by_categoria.get(categoria, 0) + count
        by_zona[zona] = by_zona.get(zona, 0) + count
        hotspot_key = f"{categoria}::{zona}"
        hotspots[hotspot_key] = hotspots.get(hotspot_key, 0) + count

    survey_points = (
        db.session.query(
            EncRespuesta.id.label("response_id"),
            EncEncuesta.id.label("survey_id"),
            EncEncuesta.slug.label("survey_slug"),
            EncEncuesta.titulo.label("survey_title"),
            EncEncuesta.tipo.label("survey_tipo"),
            EncEncuesta.es_votacion_envivo.label("is_live_vote"),
            EncEncuesta.estado.label("status"),
            EncRespuesta.lat.label("lat"),
            EncRespuesta.lng.label("lng"),
            EncRespuesta.canal.label("channel"),
            EncRespuesta.barrio.label("barrio"),
            EncRespuesta.ciudad.label("ciudad"),
            EncRespuesta.provincia.label("provincia"),
            EncRespuesta.pais.label("pais"),
            EncRespuesta.submitted_at.label("submitted_at"),
            survey_category.label("categoria"),
            survey_zone.label("zona"),
        )
        .join(EncEncuesta, EncEncuesta.id == EncRespuesta.encuesta_id)
        .filter(
            EncRespuesta.tenant_id == tenant.id,
            EncEncuesta.tenant_id == tenant.id,
            EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL,
            EncRespuesta.lat.isnot(None),
            EncRespuesta.lng.isnot(None),
            EncRespuesta.lat.between(-90, 90),
            EncRespuesta.lng.between(-180, 180),
        )
        .order_by(EncRespuesta.submitted_at.desc(), EncRespuesta.id.desc())
        .limit(effective_limit + 1)
        .all()
    )
    survey_points_partial = len(survey_points) > effective_limit
    for survey_point in survey_points[:effective_limit]:
        points.append({
            'source': 'survey_response',
            'ticket_type': 'survey_response',
            'ticket_id': survey_point.response_id,
            'response_id': survey_point.response_id,
            'survey_id': survey_point.survey_id,
            'survey_slug': survey_point.survey_slug,
            'survey_title': survey_point.survey_title,
            'survey_tipo': survey_point.survey_tipo,
            'is_live_vote': bool(survey_point.is_live_vote),
            'categoria': survey_point.categoria,
            'zona': survey_point.zona,
            'lat': survey_point.lat,
            'lon': survey_point.lng,
            'lng': survey_point.lng,
            'status': survey_point.status,
            'weight': 1,
            'channel': survey_point.channel,
            'canal': survey_point.channel,
            'barrio': survey_point.barrio,
            'ciudad': survey_point.ciudad,
            'provincia': survey_point.provincia,
            'pais': survey_point.pais,
            'submitted_at': (
                survey_point.submitted_at.isoformat()
                if survey_point.submitted_at
                else None
            ),
            '_sort_at': survey_point.submitted_at,
        })

    def _sort_timestamp(item):
        value = item.get('_sort_at')
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    points.sort(key=_sort_timestamp, reverse=True)
    points = points[:effective_limit]
    for point in points:
        point.pop('_sort_at', None)
        point['lng'] = point.get('lng') if point.get('lng') is not None else point.get('lon')
        point['weight'] = point.get('weight') or 1

    top_categories = sorted(by_categoria.items(), key=lambda item: item[1], reverse=True)[:10]
    top_zones = sorted(by_zona.items(), key=lambda item: item[1], reverse=True)[:10]
    top_hotspots = sorted(hotspots.items(), key=lambda item: item[1], reverse=True)[:10]

    hotspot_items = [
        {
            'categoria': key.split('::', 1)[0],
            'zona': key.split('::', 1)[1],
            'count': value,
        }
        for key, value in top_hotspots
    ]

    return {
        'tenant_id': tenant.id,
        'tenant_slug': tenant.slug,
        'total': municipio_total + pyme_total + real_survey_count,
        'top_categories': [{'categoria': key, 'count': value} for key, value in top_categories],
        'top_zones': [{'zona': key, 'count': value} for key, value in top_zones],
        'hotspots': hotspot_items,
        'hotspot_pairs': hotspot_items,
        'heatmap_points': points,
        'materialization': {
            'point_limit': effective_limit,
            'points_returned': len(points),
            'partial': bool(
                municipio_pairs_partial
                or pyme_pairs_partial
                or municipio_points_partial
                or pyme_points_partial
                or survey_dimensions_partial
                or survey_points_partial
            ),
            'ticket_entities_materialized': 0,
        },
        'response_provenance': build_survey_response_provenance(
            real_count=real_survey_count,
            synthetic_count=synthetic_survey_count,
            unverified_count=unverified_survey_count,
            mode='real',
        ),
    }


def _build_employee_coverage_payload(tenant: TenantProfile) -> dict:
    supported_dimensions = tenant_operational_dimensions(tenant)
    category_map = {categoria: [] for categoria in supported_dimensions.get('categorias', [])}
    zone_map = {zona: [] for zona in supported_dimensions.get('zonas', [])}
    channel_map = {channel: [] for channel in supported_dimensions.get('channels', [])}
    permission_map = {}
    employees = []
    workloads = workload_by_employee(tenant)

    for emp in User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all():
        scope = _employee_scope(emp)
        employees.append({
            'employee_id': emp.id,
            'name': emp.name,
            'email': emp.email,
            'scope': scope,
            'workload_open_tickets': workloads.get(emp.id, 0),
        })
        for categoria in scope.get('categorias', []):
            category_map.setdefault(categoria, []).append({'employee_id': emp.id, 'name': emp.name})
        for zona in scope.get('zonas', []):
            zone_map.setdefault(zona, []).append({'employee_id': emp.id, 'name': emp.name})
        for channel in scope.get('channels', []):
            channel_map.setdefault(channel, []).append({'employee_id': emp.id, 'name': emp.name})
        for permiso in scope.get('permisos', []):
            permission_map.setdefault(permiso, []).append({'employee_id': emp.id, 'name': emp.name})

    total_dimensions = len(category_map) + len(zone_map) + len(channel_map)
    covered_dimensions = (
        sum(1 for value in category_map.values() if value)
        + sum(1 for value in zone_map.values() if value)
        + sum(1 for value in channel_map.values() if value)
    )

    return {
        'tenant_id': tenant.id,
        'tenant_slug': tenant.slug,
        'employees': employees,
        'supported_dimensions': supported_dimensions,
        'coverage': {
            'categorias': category_map,
            'zonas': zone_map,
            'channels': channel_map,
            'permisos': permission_map,
            'dimension_sources': supported_dimensions.get('sources') or {},
            'uncovered_categories': [key for key, value in category_map.items() if not value],
            'uncovered_zones': [key for key, value in zone_map.items() if not value],
            'uncovered_channels': [key for key, value in channel_map.items() if not value],
        },
        'summary': {
            'employees': len(employees),
            'coverage_rate': round((covered_dimensions / total_dimensions) * 100, 2) if total_dimensions else 100.0,
            'covered_dimensions': covered_dimensions,
            'total_dimensions': total_dimensions,
        },
    }


def _plan_allows_integrations(tenant: TenantProfile) -> bool:
    return plan_allows_full_integrations(tenant)


def _integration_plan_required_response(tenant: TenantProfile, feature_id: str = "catalog_management"):
    return (
        jsonify(integration_plan_required_payload(tenant, feature_id)),
        403,
    )


def _integration_plan_feature_id(integration_type: str | None, *, operation: str = "connect") -> str:
    normalized = str(integration_type or "").strip().lower().replace("-", "_")
    if normalized in {"whatsapp", "whatsapp_business", "whatsapp_business_platform", "twilio", "meta"}:
        return "whatsapp_sender_management"
    if normalized in {"mercadopago", "mercado_pago", "payments", "payment_gateway", "checkout"}:
        return "mercadopago_checkout"
    if normalized in {"mercadolibre", "mercado_libre", "tiendanube", "tienda_nube", "marketplace"}:
        return "marketplace_sync"
    if operation in {"sync", "preview"}:
        return "marketplace_sync"
    return "marketplace_sync"


def _resolve_admin_tenant(current_user: User, slug: str) -> TenantProfile | None:
    """Resolve only the tenant explicitly addressed by the admin URL.

    Membership is checked by each route after resolution so an existing tenant
    can produce a 403 without leaking its data. Never substitute the caller's
    own tenant when a different or unknown slug was requested: doing so turns a
    cross-tenant request into a successful response for the wrong resource.
    """

    resolved_slug = apply_tenant_alias(slug) or slug
    return TenantProfile.query.filter(
        func.lower(TenantProfile.slug) == str(resolved_slug).strip().lower()
    ).first()



@admin_tenant_bp.route('/api/admin/tenants/<slug>/live-chat/schedule', methods=['GET', 'PUT'])
@token_requerido
@require_tenant
def admin_tenant_live_chat_schedule(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    current_schedule = cfg.get('live_chat_schedule') if isinstance(cfg.get('live_chat_schedule'), dict) else {}

    if request.method == 'GET':
        status = build_live_chat_status(schedule_override=current_schedule if current_schedule else None)
        status['tenant_slug'] = tenant.slug
        status['source'] = 'tenant_config' if current_schedule else 'global_config'
        return jsonify(status)

    payload = request.get_json(silent=True) or {}
    candidate = {
        'enabled': bool(payload.get('enabled', True)),
        'days': payload.get('days', current_schedule.get('days', 'mon-fri')),
        'start_time': payload.get('start_time', current_schedule.get('start_time', '09:00')),
        'end_time': payload.get('end_time', current_schedule.get('end_time', '13:00')),
        'timezone': payload.get('timezone', current_schedule.get('timezone', 'America/Argentina/Buenos_Aires')),
    }

    # Validate candidate schedule by building a normalized schedule object.
    schedule_obj = build_schedule_from_config(candidate)
    cfg['live_chat_schedule'] = {
        'enabled': schedule_obj.enabled,
        'days': sorted(list(schedule_obj.days)),
        'start_time': schedule_obj.start_time.strftime('%H:%M'),
        'end_time': schedule_obj.end_time.strftime('%H:%M'),
        'timezone': getattr(schedule_obj.timezone, 'key', str(schedule_obj.timezone)),
    }
    tenant.configuracion = cfg
    db.session.commit()

    status = build_live_chat_status(schedule_override=cfg['live_chat_schedule'])
    status['tenant_slug'] = tenant.slug
    status['source'] = 'tenant_config'
    return jsonify(status)


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog', methods=['OPTIONS'])
def admin_catalog_options(slug):
    return _cors_preflight_response()


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog', methods=['GET'])
@token_requerido
@require_tenant
def admin_get_catalog(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    owner = tenant.municipio or tenant.pyme
    has_pdf = bool(owner and tiene_archivo_catalogo(owner.id))
    base_web = current_app.config.get("APP_BASE_URL", "https://chatboc.ar")
    base_api = current_app.config.get("API_BASE_URL", "https://api.chatboc.ar")
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    request_id = _request_id()

    response = jsonify({
        "contract_version": "tenant.catalog_admin.v1",
        "request_id": request_id,
        "tenant_slug": tenant.slug,
        "status": "published" if has_pdf else "missing",
        "catalog_version": cfg.get("catalog_version"),
        "view_url": f"{base_web.rstrip('/')}/t/{tenant.slug}/market",
        "download_url": f"{base_api}/api/public/tenants/{tenant.slug}/catalog/download?format=pdf",
        "download_url_json": f"{base_api}/api/public/tenants/{tenant.slug}/catalog/download?format=json",
        "links": {
            "draft_endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/draft",
            "items_endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "item_patch_template": f"/api/admin/tenants/{tenant.slug}/catalog/items/{{item_id}}",
            "publish_endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/publish",
            "promotions_endpoint": f"/api/pymes/{owner.id}/promociones" if owner else None,
            "bulk_import_v2": "/api/admin/catalog/import",
            "stock_only_import_v2": "/api/admin/catalog/import",
            "quality_endpoint": f"/api/v2/tenants/{tenant.slug}/catalog/quality",
        },
        "draft_endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/draft",
        "has_pdf": has_pdf,
        "inventory": {
            "contract_version": "catalog.inventory_ops.v1",
            "enabled": True,
            "catalog_version": cfg.get("catalog_version"),
            "last_inventory_update_at": cfg.get("catalog_last_inventory_update_at"),
            "low_stock_threshold": _tenant_inventory_threshold(tenant),
            "columns": inventory_columns_contract(),
            "rules": {
                "demo_mode": False,
                "chat_confirms_stock_only_after_backend_validation": True,
                "frontend_must_not_invent_availability": True,
            },
        },
        "promotions": _catalog_promotions_ops_contract(owner, tenant),
        "marketplace_readiness": _catalog_marketplace_readiness(owner, tenant, has_pdf=has_pdf, cfg=cfg),
        "frontend_contract": {
            "render_as": "tenant_catalog_inventory_admin",
            "primary_view": "catalog_and_inventory",
            "supports_inline_stock_edit": True,
            "supports_bulk_import": True,
            "supports_stock_only_import": True,
            "supports_quality_board": True,
            "supports_promotions_command_center": True,
            "supports_marketplace_readiness": True,
        },
    })
    response.headers["X-Request-Id"] = request_id
    return response


def _promotion_scopes_for_contract(promotion: Promocion) -> list[dict]:
    raw_scopes = promotion.alcances
    try:
        scopes = raw_scopes.all() if hasattr(raw_scopes, "all") else list(raw_scopes)
    except Exception:
        scopes = []

    out = []
    for scope in scopes:
        out.append(
            {
                "id": scope.id,
                "tipo_alcance": scope.tipo_alcance,
                "catalogo_item_id": scope.catalogo_item_id,
                "nombre_categoria": scope.nombre_categoria,
                "nombre_marca": scope.nombre_marca,
            }
        )
    return out


def _catalog_promotions_ops_contract(owner: User | None, tenant: TenantProfile) -> dict:
    if not owner:
        return {
            "contract_version": "tenant.catalog_promotions_ops.v1",
            "enabled": False,
            "reason_code": "missing_owner",
            "total": 0,
            "active": 0,
            "items": [],
        }

    query = Promocion.query.filter_by(pyme_user_id=owner.id)
    total = query.count()
    active = query.filter(Promocion.is_active.is_(True)).count()
    catalog_badges = CatalogoItem.query.filter(
        CatalogoItem.tenant_id == tenant.id,
        CatalogoItem.promocion_info.isnot(None),
    ).count()
    recent_promotions = query.order_by(Promocion.is_active.desc(), Promocion.created_at.desc()).limit(8).all()

    return {
        "contract_version": "tenant.catalog_promotions_ops.v1",
        "enabled": True,
        "total": total,
        "active": active,
        "inactive": max(total - active, 0),
        "catalog_items_with_promo_badge": catalog_badges,
        "endpoint": f"/api/pymes/{owner.id}/promociones",
        "create_endpoint": f"/api/pymes/{owner.id}/promociones",
        "activation_endpoint_template": f"/api/pymes/{owner.id}/promociones/{{promotion_id}}/activar",
        "deactivation_endpoint_template": f"/api/pymes/{owner.id}/promociones/{{promotion_id}}/desactivar",
        "supported_discount_types": [
            "PORCENTAJE_PRODUCTO",
            "PORCENTAJE_CATEGORIA",
            "PORCENTAJE_MARCA",
            "COMPRA_X_LLEVA_Y_PRODUCTOS",
            "CANTIDAD_MINIMA_DESCUENTO_FIJO_PRODUCTO",
            "CANTIDAD_MINIMA_DESCUENTO_PORCENTAJE_PRODUCTO",
            "TOTAL_CARRITO_DESCUENTO_PORCENTAJE",
            "TOTAL_CARRITO_DESCUENTO_FIJO",
        ],
        "recommended_quick_actions": [
            {
                "id": "cart_percent",
                "label": "Descuento por compra minima",
                "tipo_promocion": "TOTAL_CARRITO_DESCUENTO_PORCENTAJE",
            },
            {
                "id": "category_percent",
                "label": "Descuento por categoria",
                "tipo_promocion": "PORCENTAJE_CATEGORIA",
            },
            {
                "id": "product_percent",
                "label": "Descuento por producto",
                "tipo_promocion": "PORCENTAJE_PRODUCTO",
            },
        ],
        "items": [
            {
                "id": promotion.id,
                "nombre_promocion": promotion.nombre_promocion,
                "descripcion_publica": promotion.descripcion_publica,
                "tipo_promocion": promotion.tipo_promocion,
                "valor_descuento": promotion.valor_descuento,
                "monto_minimo_carrito": promotion.monto_minimo_carrito,
                "cantidad_minima_aplicable": promotion.cantidad_minima_aplicable,
                "is_active": promotion.is_active,
                "codigo_promocion": promotion.codigo_promocion,
                "fecha_inicio": promotion.fecha_inicio.isoformat() if promotion.fecha_inicio else None,
                "fecha_fin": promotion.fecha_fin.isoformat() if promotion.fecha_fin else None,
                "alcances": _promotion_scopes_for_contract(promotion),
            }
            for promotion in recent_promotions
        ],
        "frontend_contract": {
            "render_as": "catalog_promotions_command_center",
            "supports_cart_discount": True,
            "supports_category_discount": True,
            "supports_product_discount": True,
            "shows_in_marketplace_checkout": True,
        },
    }


def _catalog_marketplace_readiness(
    owner: User | None,
    tenant: TenantProfile,
    *,
    has_pdf: bool,
    cfg: dict,
) -> dict:
    if not owner:
        return {
            "contract_version": "tenant.marketplace_readiness.v1",
            "ready": False,
            "score": 0,
            "blockers": [
                {
                    "id": "missing_owner",
                    "label": "Tenant sin propietario operativo",
                    "severity": "blocker",
                    "next_action": "Vincular un usuario administrador al tenant.",
                }
            ],
            "warnings": [],
            "metrics": {"products_total": 0},
            "frontend_contract": {"render_as": "marketplace_readiness_panel"},
        }

    products = (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter(CatalogoItem.tenant_id == tenant.id, CatalogoItem.user_id == owner.id)
        .all()
    )
    total = len(products)
    with_image = sum(1 for item in products if bool(item.imagen_url))
    with_promo = sum(1 for item in products if bool(item.promocion_info))
    with_price = 0
    available = 0
    low_stock = 0
    threshold = _tenant_inventory_threshold(tenant)
    for item in products:
        _, price_value, _ = parse_precio_flexible(item.precio or "")
        if price_value is not None:
            with_price += 1
        if getattr(item, "disponible", True) is not False:
            available += 1
        try:
            quantity = float(str(item.cantidad or "0").replace(",", "."))
        except (TypeError, ValueError):
            quantity = None
        if quantity is not None and quantity <= threshold:
            low_stock += 1

    checkout_configured = bool(cfg.get("mercadopago_access_token"))
    blockers = []
    warnings = []

    if total == 0:
        blockers.append(
            {
                "id": "empty_catalog",
                "label": "Catalogo sin productos",
                "severity": "blocker",
                "next_action": "Cargar productos o importar un Excel antes de publicar el marketplace.",
            }
        )
    if total > 0 and available == 0:
        blockers.append(
            {
                "id": "no_available_products",
                "label": "No hay productos disponibles",
                "severity": "blocker",
                "next_action": "Activar disponibilidad o corregir stock de al menos un producto.",
            }
        )
    if not checkout_configured and tenant.tipo == "pyme":
        blockers.append(
            {
                "id": "checkout_not_configured",
                "label": "Checkout sin Mercado Pago configurado",
                "severity": "blocker",
                "next_action": f"Configurar /api/admin/tenants/{tenant.slug}/integrations/mercadopago.",
            }
        )

    if total and with_image < total:
        warnings.append(
            {
                "id": "missing_images",
                "label": f"{total - with_image} productos sin imagen",
                "severity": "warning",
                "next_action": "Agregar fotos para mejorar conversion en marketplace y WhatsApp.",
            }
        )
    if total and with_price < total:
        warnings.append(
            {
                "id": "missing_prices",
                "label": f"{total - with_price} productos sin precio interpretable",
                "severity": "warning",
                "next_action": "Completar precio para habilitar carrito, filtros y checkout claro.",
            }
        )
    if total and low_stock:
        warnings.append(
            {
                "id": "low_stock",
                "label": f"{low_stock} productos con stock bajo",
                "severity": "warning",
                "next_action": "Actualizar stock o marcar disponibilidad real antes de promocionar.",
            }
        )
    if total and with_promo == 0:
        warnings.append(
            {
                "id": "no_promotions",
                "label": "Sin promociones visibles",
                "severity": "info",
                "next_action": "Crear una promo para destacar productos en WhatsApp y widget.",
            }
        )
    if not has_pdf:
        warnings.append(
            {
                "id": "missing_pdf_catalog",
                "label": "Catalogo PDF no publicado",
                "severity": "info",
                "next_action": "Publicar PDF solo si el tenant necesita descarga tradicional.",
            }
        )

    score = 100
    score -= len(blockers) * 35
    score -= min(len(warnings) * 8, 32)
    if total:
        score -= int(((total - with_image) / total) * 12)
        score -= int(((total - with_price) / total) * 18)
    score = max(min(score, 100), 0)

    return {
        "contract_version": "tenant.marketplace_readiness.v1",
        "ready": not blockers and score >= 70,
        "score": score,
        "state": "ready" if not blockers and score >= 70 else ("blocked" if blockers else "needs_attention"),
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "products_total": total,
            "products_available": available,
            "products_with_images": with_image,
            "products_with_prices": with_price,
            "products_with_promotions": with_promo,
            "low_stock": low_stock,
            "checkout_configured": checkout_configured,
            "pdf_catalog_published": has_pdf,
        },
        "recommended_actions": [*(blockers[:3]), *(warnings[:3])],
        "frontend_contract": {
            "render_as": "marketplace_readiness_panel",
            "show_score_ring": True,
            "show_blockers_first": True,
            "show_quick_actions": True,
        },
    }


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/draft', methods=['OPTIONS'])
def admin_catalog_draft_options(slug):
    return _cors_preflight_response()


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/draft', methods=['PUT'])
@token_requerido
@require_tenant
def admin_save_catalog_draft(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    request_id = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or uuid.uuid4().hex).strip()
    saved_at = datetime.now(timezone.utc).isoformat()
    draft = {
        "contract_version": "tenant.catalog_draft.v1",
        "saved_at": saved_at,
        "source": payload.get("source") or "tenant_catalog_editor",
        "title": payload.get("title") or payload.get("titulo"),
        "description": payload.get("description") or payload.get("descripcion"),
        "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
        "raw": payload,
    }
    cfg["catalog_draft"] = draft
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")
    db.session.commit()

    response = jsonify(
        {
            "ok": True,
            "contract_version": "tenant.catalog_draft.v1",
            "tenant_slug": tenant.slug,
            "status": "draft",
            "saved_at": draft["saved_at"],
            "draft_endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/draft",
            "request_id": request_id,
        }
    )
    response.headers["X-Request-Id"] = request_id
    return response


def _request_id() -> str:
    return (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or f"req_{uuid.uuid4().hex}"
    )


def _tenant_inventory_threshold(tenant: TenantProfile) -> float:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    try:
        return float(cfg.get("inventory_low_stock_threshold", 5) or 5)
    except (TypeError, ValueError):
        return 5


def _item_inventory_payload(item: CatalogoItem, tenant: TenantProfile) -> dict:
    return inventory_contract(
        getattr(item, "cantidad", None),
        available=item.disponible is not False,
        low_stock_threshold=_tenant_inventory_threshold(tenant),
        source="catalogo_item",
        updated_at=getattr(item, "timestamp", None),
    )


def _catalog_version_for_tenant(tenant: TenantProfile) -> str:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    return cfg.get("catalog_version") or f"cat_{tenant.id}_unversioned"


def _attach_marketplace_inventory_aliases(
    product: dict,
    *,
    tenant: TenantProfile,
    item: CatalogoItem | None = None,
    catalog_version: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Expose frontend-friendly aliases without dropping legacy Spanish fields."""
    item_id = product.get("catalogo_item_id") or getattr(item, "id", None)
    product.setdefault("id", item_id)
    product.setdefault("name", product.get("nombre"))
    product.setdefault("description", product.get("descripcion") or product.get("descripcion_corta"))
    product.setdefault(
        "price",
        product.get("price_numeric")
        if product.get("price_numeric") is not None
        else product.get("precio") or product.get("precio_texto"),
    )
    product.setdefault("currency", product.get("moneda") or getattr(item, "moneda", None) or "ARS")
    product.setdefault("catalog_version", catalog_version or _catalog_version_for_tenant(tenant))
    if request_id:
        product.setdefault("request_id", request_id)
    return product


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/publish', methods=['OPTIONS'])
def admin_catalog_publish_options(slug):
    return _cors_preflight_response()


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/publish', methods=['POST'])
@token_requerido
@require_tenant
def admin_publish_catalog(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.json or {}
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    catalog_cfg = cfg.get("catalogo")
    if not isinstance(catalog_cfg, dict):
        catalog_cfg = {}

    title = payload.get("titulo") or payload.get("title")
    description = payload.get("descripcion") or payload.get("description")
    banner = (
        payload.get("banner")
        or payload.get("banner_url")
        or payload.get("banner_image_url")
    )
    message = (
        payload.get("mensaje_default")
        or payload.get("default_message")
        or payload.get("mensaje")
    )

    if title is not None:
        catalog_cfg["titulo"] = title
        catalog_cfg["title"] = title
    if description is not None:
        catalog_cfg["descripcion"] = description
        catalog_cfg["description"] = description
    if banner is not None:
        catalog_cfg["banner_image_url"] = banner
    if message is not None:
        catalog_cfg["mensaje_default"] = message

    enabled = payload.get("habilitado")
    if enabled is None:
        enabled = payload.get("catalogo_habilitado")
    if enabled is None:
        enabled = payload.get("enabled")
    if enabled is None:
        enabled = payload.get("catalog_enabled")
    if enabled is not None:
        enabled = bool(enabled)
        catalog_cfg["widget_visible"] = enabled
        catalog_cfg["catalogo_widget_visible"] = enabled
        cfg["widget_catalog_enabled"] = enabled

    is_public = payload.get("publico")
    if is_public is None:
        is_public = payload.get("public")
    if is_public is None:
        is_public = payload.get("is_public")
    if is_public is not None:
        catalog_cfg["publico"] = bool(is_public)
        catalog_cfg["public"] = bool(is_public)

    share_in_intent = payload.get("compartir_en_intencion")
    if share_in_intent is None:
        share_in_intent = payload.get("share_in_intent")
    if share_in_intent is None:
        share_in_intent = payload.get("share_in_intention")
    if share_in_intent is not None:
        catalog_cfg["compartir_en_intencion"] = bool(share_in_intent)

    prefer_pdf_whatsapp = payload.get("prefer_pdf_whatsapp")
    if prefer_pdf_whatsapp is None:
        prefer_pdf_whatsapp = payload.get("prefer_pdf_en_whatsapp")
    if prefer_pdf_whatsapp is not None:
        catalog_cfg["prefer_pdf_whatsapp"] = bool(prefer_pdf_whatsapp)

    cfg["catalogo"] = catalog_cfg
    tenant.configuracion = cfg
    db.session.commit()

    owner = tenant.municipio or tenant.pyme
    has_pdf = bool(owner and tiene_archivo_catalogo(owner.id))

    return jsonify({
        "status": "published" if has_pdf else "missing",
        "has_pdf": has_pdf,
        "catalogo": catalog_cfg,
    })


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/items/<int:item_id>', methods=['OPTIONS'])
def admin_catalog_item_options(slug, item_id):
    return _cors_preflight_response()


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/items/<int:item_id>', methods=['PATCH'])
@token_requerido
@require_tenant
def admin_update_catalog_item(current_user, slug, item_id: int):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    owner = tenant.municipio or tenant.pyme
    if not owner:
        return jsonify({"error": "Tenant owner not found"}), 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    product_query = _product_query_for_tenant(owner, tenant)
    item = product_query.filter(CatalogoItem.id == item_id).first()
    if not item:
        return jsonify({"error": "Catalog item not found"}), 404

    if "nombre" in payload:
        item.nombre = payload.get("nombre") or ""
    if "descripcion" in payload:
        item.descripcion = payload.get("descripcion")
    if "descripcion_corta" in payload:
        item.descripcion_corta = payload.get("descripcion_corta")
    if "promocion_info" in payload:
        item.promocion_info = payload.get("promocion_info")
    if "sku" in payload:
        item.sku = payload.get("sku")
    if "marca" in payload:
        item.marca = payload.get("marca")
    if "categoria" in payload:
        item.categoria = payload.get("categoria")
    if "moneda" in payload:
        item.moneda = payload.get("moneda")
    if "unidad" in payload:
        item.unidad = payload.get("unidad")
    if "disponible" in payload:
        item.disponible = bool(payload.get("disponible"))
    if "available_to_sell" in payload:
        item.disponible = bool(payload.get("available_to_sell"))

    metadata = item.extra_metadata if isinstance(item.extra_metadata, dict) else {}
    metadata = dict(metadata)

    image_url = payload.get("imagen_url") if "imagen_url" in payload else payload.get("image_url")
    if image_url is not None:
        image_url = str(image_url).strip()
        item.imagen_url = image_url or None
        metadata["image_status"] = "ready" if item.imagen_url else "missing"

    gallery_urls = payload.get("gallery_urls")
    if gallery_urls is None:
        gallery_urls = payload.get("imagenes") or payload.get("images")
    if gallery_urls is not None:
        if isinstance(gallery_urls, str):
            gallery_urls = [gallery_urls]
        if not isinstance(gallery_urls, list):
            return jsonify({"error": "gallery_urls must be a list"}), 400
        metadata["gallery_urls"] = [str(url).strip() for url in gallery_urls if str(url).strip()][:12]

    if "external_url" in payload:
        item.external_url = payload.get("external_url")
    if "checkout_type" in payload:
        item.checkout_type = str(payload.get("checkout_type") or "chatboc").strip()[:50] or "chatboc"

    if "extra_metadata" in payload and isinstance(payload.get("extra_metadata"), dict):
        metadata.update(payload.get("extra_metadata"))

    if "personalization_options" in payload:
        try:
            sanitized_options = _sanitize_personalization_options_for_storage(payload.get("personalization_options"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        metadata["personalization_options"] = sanitized_options
    item.extra_metadata = metadata

    if "precio" in payload:
        precio_raw = payload.get("precio")
        item.precio = str(precio_raw) if precio_raw is not None else item.precio
        _, precio_float, _ = parse_precio_flexible(precio_raw)
        item.precio_monetario = precio_float or 0.0

    if "cantidad" in payload or "stock" in payload or "stock_quantity" in payload:
        cantidad_raw = payload.get("cantidad")
        if cantidad_raw is None:
            cantidad_raw = payload.get("stock")
        if cantidad_raw is None:
            cantidad_raw = payload.get("stock_quantity")
        item.cantidad = str(cantidad_raw) if cantidad_raw is not None else item.cantidad
        metadata = item.extra_metadata if isinstance(item.extra_metadata, dict) else {}
        metadata = dict(metadata)
        metadata["inventory_source"] = payload.get("inventory_source") or "tenant_admin_inline_edit"
        metadata["stock_updated_at"] = datetime.now(timezone.utc).isoformat()
        item.extra_metadata = metadata

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    catalog_version = new_catalog_version(tenant.id)
    cfg["catalog_version"] = catalog_version
    cfg["catalog_last_inventory_update_at"] = datetime.now(timezone.utc).isoformat()
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")

    db.session.commit()

    text_parts = [
        item.nombre,
        item.descripcion or "",
        item.sku or "",
    ]
    if isinstance(item.extra_metadata, dict):
        text_parts.extend(
            str(value)
            for value in item.extra_metadata.values()
            if value and not isinstance(value, (list, dict))
        )
    texto = " ".join(part for part in text_parts if part).strip()
    embeddings = embed_textos_llm([texto]) if texto else []
    if embeddings and embeddings[0]:
        rubro_nombre = "general"
        if getattr(owner, "rubro", None) and owner.rubro.nombre:
            rubro_nombre = owner.rubro.nombre

        index_catalog_item(
            tenant.id,
            {
                "id": item.id,
                "nombre": item.nombre,
                "descripcion": item.descripcion,
                "precio": item.precio or item.precio_monetario or 0,
                "rubro": rubro_nombre,
                "stock": item.cantidad,
                "user_id": owner.id,
                "tenant_id": tenant.id,
                "sku": item.sku,
                "marca": item.marca,
                "categoria": item.categoria,
                "moneda": item.moneda,
                "unidad": item.unidad,
                "precio_por_caja": item.precio_por_caja,
                "unidad_por_caja": item.unidad_por_caja,
                "extra_metadata": item.extra_metadata or {},
            },
            embeddings[0],
        )

    request_id = _request_id()
    catalog_version = cfg.get("catalog_version") or _catalog_version_for_tenant(tenant)
    formatted = _formatear_producto(
        {
            "nombre": item.nombre,
            "categoria": item.categoria,
            "descripcion": item.descripcion,
            "sku": item.sku,
            "unidad": item.unidad,
            "precio_str": item.precio,
            "cantidad": item.cantidad,
            "marca": item.marca,
            "imagen_url": item.imagen_url,
            "descripcion_corta": item.descripcion_corta,
            "promocion_info": item.promocion_info,
            "precio_por_caja": item.precio_por_caja,
            "unidad_por_caja": item.unidad_por_caja,
            "moneda": item.moneda,
            "precio_float": item.precio_monetario,
            "extra_metadata": item.extra_metadata,
        }
    )
    formatted["inventory"] = _item_inventory_payload(item, tenant)
    formatted["stock_quantity"] = formatted["inventory"]["stock_quantity"]
    formatted["stock_status"] = formatted["inventory"]["stock_status"]
    formatted["available_to_sell"] = formatted["inventory"]["available_to_sell"]
    formatted["price_numeric"] = float(item.precio_monetario) if item.precio_monetario is not None else None
    _attach_marketplace_inventory_aliases(
        formatted,
        tenant=tenant,
        item=item,
        catalog_version=catalog_version,
        request_id=request_id,
    )
    return jsonify(
        {
            "contract_version": "tenant.catalog_item_update.v1",
            "request_id": request_id,
            "catalog_version": catalog_version,
            "item": formatted,
        }
    )

# --- Tenant Management ---

@admin_tenant_bp.route('/api/admin/tenants', methods=['POST'])
def create_tenant():
    """Crea un nuevo tenant desde una plantilla. Actúa como registro público."""
    data = request.json or {}
    try:
        requested_plan = _normalize_tenant_creation_plan(
            data.get("plan") or data.get("tenant_plan") or data.get("subscription_plan")
        )
        actor = _optional_tenant_creation_actor()
        actor_is_super_admin = bool(actor and is_authorized_superadmin_user(actor))
        if requested_plan != "free" and not actor_is_super_admin:
            return jsonify(
                {
                    "error": "plan_requires_super_admin",
                    "reason_code": "productive_plan_requires_super_admin",
                    "requested_plan": requested_plan,
                    "allowed_public_plan": "free",
                    "next_action": "crear_trial_free_o_autenticar_super_admin_para_activar_plan_productivo",
                    "frontend_contract": {
                        "render_as": "tenant_creation_plan_gate",
                        "allow_public_signup": True,
                        "public_plan": "free",
                        "requires_super_admin_for": sorted(plan for plan in TENANT_CREATION_ALLOWED_PLANS if plan != "free"),
                    },
                }
            ), 403
        auto_assign_whatsapp_number = _bool_from_payload(
            data.get("auto_assign_whatsapp_number", data.get("autoAssignWhatsappNumber"))
        )
        if auto_assign_whatsapp_number and not actor_is_super_admin:
            return jsonify(
                {
                    "error": "whatsapp_number_assignment_forbidden",
                    "reason_code": "super_admin_required",
                    "requested_plan": requested_plan,
                    "next_action": "continuar_sin_numero_o_solicitar_activacion_al_equipo_chatboc",
                    "frontend_contract": {
                        "render_as": "whatsapp_number_assignment_gate",
                        "allow_continue_without_number": True,
                        "requires_super_admin": True,
                        "requires_productive_plan": True,
                    },
                }
            ), 403
        if auto_assign_whatsapp_number and requested_plan not in FULL_INTEGRATION_PLANS:
            return jsonify(
                {
                    "error": "whatsapp_number_assignment_forbidden",
                    "reason_code": "plan_full_required",
                    "requested_plan": requested_plan,
                    "allowed_plans": sorted(FULL_INTEGRATION_PLANS),
                    "next_action": "activar_plan_productivo_antes_de_asignar_numero",
                    "frontend_contract": {
                        "render_as": "whatsapp_number_assignment_gate",
                        "allow_continue_without_number": True,
                        "requires_super_admin": True,
                        "requires_productive_plan": True,
                    },
                }
            ), 403
        tenant = create_tenant_from_template(
            nombre=data.get('nombre') or data.get('name'),
            slug=data.get('slug'),
            tipo=data.get('tipo') or data.get('type') or data.get('vertical'),
            template_key=data.get('template_key'),
            plan=requested_plan,
            auto_assign_whatsapp_number=auto_assign_whatsapp_number,
            owner_email=data.get('owner_email') or data.get('email_admin'),
            owner_password=data.get('owner_password'),
            allow_existing_owner=actor_is_super_admin,
            reset_existing_owner_password=actor_is_super_admin,
        )

        widget_token = None
        if tenant.configuracion and 'widget_tokens' in tenant.configuracion:
             widget_token = tenant.configuracion['widget_tokens'][0]

        owner = tenant.municipio or tenant.pyme

        return jsonify({
            "slug": tenant.slug,
            "plan": tenant.plan,
            "widget_token": widget_token,
            "id": tenant.id,
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "nombre": tenant.nombre,
                "tipo": tenant.tipo,
                "plan": tenant.plan,
                "owner_email": getattr(owner, "email", None),
                "owner_email_generated": bool((tenant.configuracion or {}).get("owner_email_generated")),
            },
            "whatsapp_onboarding": (tenant.configuracion or {}).get("whatsapp_onboarding"),
            "integration_access": integration_access_payload(tenant),
        }), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        current_app.logger.exception("Tenant creation failed")
        return jsonify({"error": str(e)}), 500

@admin_tenant_bp.route('/api/admin/tenants/<slug>/config', methods=['GET'])
@token_requerido
@require_tenant
def get_tenant_config_bundle(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    configs = TenantConfig.query.filter_by(tenant_id=tenant.id).all()
    # Nested structure: key -> channel -> value
    config_dict = {}
    for cfg in configs:
        k = cfg.key
        c = cfg.channel or 'default'
        if k not in config_dict:
            config_dict[k] = {}
        config_dict[k][c] = cfg.json_value

    integration_access = integration_access_payload(tenant)
    response = {
        "tenant": {
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": tenant.plan,
            "logo_url": tenant.logo_url,
            "whatsapp_sender_id": tenant.whatsapp_sender_id,
            "dispatch_email": getattr(tenant, "dispatch_email", None),
            "dispatch_phone": getattr(tenant, "dispatch_phone", None),
            "send_buyer_email": tenant.send_buyer_email,
            "send_dispatch_email": tenant.send_dispatch_email,
            "send_dispatch_whatsapp": tenant.send_dispatch_whatsapp,
            "theme_json": tenant.theme_json or {}
        },
        "configs": config_dict,
        "features": {
            "integrations": integration_access["enabled"],
            "widget_customization": integration_access["enabled"]
        },
        "integration_access": integration_access,
    }
    return jsonify(response)


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/items', methods=['GET', 'OPTIONS'])
@admin_tenant_bp.route('/admin/tenants/<slug>/catalog/items', methods=['GET', 'OPTIONS'])
@cutover_writer_view
@token_requerido
@require_tenant
def admin_tenant_catalog(current_user, slug):
    if request.method == 'OPTIONS':
        return jsonify({"ok": True}), 204

    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    owner = tenant.municipio or tenant.pyme
    if not owner:
        return jsonify([])

    ensure_seed_catalog(owner, tenant)
    categoria = request.args.get("categoria")
    search_text = request.args.get("q")
    modalidad = (request.args.get("modalidad") or "").strip().lower()
    only_available = request.args.get("disponible")
    promo_only = request.args.get("en_promocion")
    sort_key = (request.args.get("sort") or "nombre").strip().lower()
    price_min = request.args.get("precio_min")
    price_max = request.args.get("precio_max")
    request_id = _request_id()
    catalog_version = _catalog_version_for_tenant(tenant)

    query = _product_query_for_tenant(owner, tenant)
    if categoria:
        categoria_norm = categoria.strip().lower()
        if categoria_norm:
            query = query.filter(func.lower(CatalogoItem.categoria) == categoria_norm)

    if modalidad:
        query = query.filter(func.lower(CatalogoItem.modalidad) == modalidad)

    if only_available is not None:
        query = query.filter(CatalogoItem.disponible.is_(str(only_available).strip().lower() in {"1", "true", "si", "yes"}))

    if promo_only is not None and str(promo_only).strip().lower() in {"1", "true", "si", "yes"}:
        query = query.filter(CatalogoItem.promocion_info.isnot(None))

    try:
        if price_min not in (None, ""):
            query = query.filter(CatalogoItem.precio_monetario >= float(price_min))
        if price_max not in (None, ""):
            query = query.filter(CatalogoItem.precio_monetario <= float(price_max))
    except (TypeError, ValueError):
        return jsonify({"error": "Filtro de precio inválido"}), 400

    if sort_key == "precio_asc":
        query = query.order_by(CatalogoItem.precio_monetario.asc().nullslast(), func.lower(CatalogoItem.nombre))
    elif sort_key == "precio_desc":
        query = query.order_by(CatalogoItem.precio_monetario.desc().nullslast(), func.lower(CatalogoItem.nombre))
    elif sort_key == "updated_desc":
        query = query.order_by(CatalogoItem.timestamp.desc().nullslast(), func.lower(CatalogoItem.nombre))
    else:
        query = query.order_by(func.lower(CatalogoItem.nombre))

    items = query.all()

    productos = []
    for item in items:
        prod = _formatear_producto(
            {
                "nombre": item.nombre,
                "categoria": item.categoria,
                "descripcion": item.descripcion,
                "sku": item.sku,
                "unidad": item.unidad,
                "precio_str": item.precio,
                "cantidad": item.cantidad,
                "marca": item.marca,
                "imagen_url": item.imagen_url,
                "descripcion_corta": item.descripcion_corta,
                "promocion_info": item.promocion_info,
                "precio_por_caja": item.precio_por_caja,
                "unidad_por_caja": item.unidad_por_caja,
                "moneda": item.moneda,
                "precio_float": item.precio_monetario,
                "extra_metadata": item.extra_metadata,
            }
        )
        prod["catalogo_item_id"] = item.id
        prod["tenant_id"] = tenant.id
        prod["available"] = bool(item.disponible is not False)
        prod["inventory"] = _item_inventory_payload(item, tenant)
        prod["stock_quantity"] = prod["inventory"]["stock_quantity"]
        prod["stock_status"] = prod["inventory"]["stock_status"]
        prod["available_to_sell"] = prod["inventory"]["available_to_sell"]
        prod["price_numeric"] = float(item.precio_monetario) if item.precio_monetario is not None else None
        _attach_marketplace_inventory_aliases(
            prod,
            tenant=tenant,
            item=item,
            catalog_version=catalog_version,
            request_id=request_id,
        )
        prod["channel_availability"] = {
            "widget": True,
            "whatsapp": True,
            "phone": True,
        }
        productos.append(prod)

    if search_text:
        term = search_text.strip().lower()
        if term:
            filtrados = []
            for prod in productos:
                texto_busqueda = " ".join(
                    str(value or "")
                    for value in (
                        prod.get("nombre"),
                        prod.get("descripcion"),
                        prod.get("categoria"),
                        prod.get("promocion_info"),
                    )
                ).lower()
                if term in texto_busqueda:
                    filtrados.append(prod)
            productos = filtrados

    response = jsonify(productos)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Catalog-Version"] = catalog_version
    return response

@admin_tenant_bp.route('/api/admin/tenants/<slug>/config', methods=['PUT'])
@token_requerido
@require_tenant
def update_tenant_config_bundle(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check (same logic as GET and other admin tenant endpoints)
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403
    if not can_manage_tenant_control_plane(current_user, tenant):
        reason_code = "tenant_inactive" if getattr(tenant, "is_active", True) is not True else "tenant_admin_required"
        return jsonify({"error": "Unauthorized", "reason_code": reason_code}), 403

    data = request.json or {}

    # Update Tenant fields
    tenant_data = data.get('tenant', {})
    if 'nombre' in tenant_data: tenant.nombre = tenant_data['nombre']
    if 'logo_url' in tenant_data: tenant.logo_url = tenant_data['logo_url']
    if 'dispatch_email' in tenant_data: tenant.dispatch_email = tenant_data['dispatch_email']
    if 'dispatch_phone' in tenant_data: tenant.dispatch_phone = tenant_data['dispatch_phone']

    if 'send_buyer_email' in tenant_data: tenant.send_buyer_email = bool(tenant_data['send_buyer_email'])
    if 'send_dispatch_email' in tenant_data: tenant.send_dispatch_email = bool(tenant_data['send_dispatch_email'])
    if 'send_dispatch_whatsapp' in tenant_data: tenant.send_dispatch_whatsapp = bool(tenant_data['send_dispatch_whatsapp'])

    if 'theme_json' in tenant_data: tenant.theme_json = tenant_data['theme_json']

    # Update Configs
    # Expecting: "configs": { "menu": { "default": {...}, "widget": {...} } }
    # Or simplified: "menu": { ... } (updates default)
    # Let's support both for backward compat with my prev implementation if used.

    valid_keys = ['menu', 'contacts', 'links', 'widget']

    configs_in = data.get('configs', {})
    # Also merge top level keys if they match valid_keys (legacy support)
    for k in valid_keys:
        if k in data:
            if k not in configs_in:
                configs_in[k] = {}
            configs_in[k]['default'] = data[k] # Assume default channel if top level

    for key, channels_map in configs_in.items():
        if key not in valid_keys: continue

        if not isinstance(channels_map, dict):
             # Maybe raw json? Treat as default
             channels_map = {'default': channels_map}

        for channel, value in channels_map.items():
            chan_val = None if channel == 'default' else channel

            cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key=key, channel=chan_val).first()
            if cfg:
                cfg.json_value = value
            else:
                cfg = TenantConfig(tenant_id=tenant.id, key=key, channel=chan_val, json_value=value)
                db.session.add(cfg)

    db.session.commit()
    return jsonify({"message": "Config updated"})

@admin_tenant_bp.route('/api/admin/tenants/<slug>/assign-whatsapp-number', methods=['POST'])
@token_requerido
@require_tenant
def assign_whatsapp_number(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403
    if not can_manage_tenant_control_plane(current_user, tenant):
        reason_code = "tenant_inactive" if getattr(tenant, "is_active", True) is not True else "tenant_admin_required"
        return jsonify({"error": "Unauthorized", "reason_code": reason_code}), 403
    if not plan_allows_full_integrations(tenant):
        return _integration_plan_required_response(tenant, "whatsapp_sender_management")

    number = assign_number_to_tenant(tenant)
    if not number:
        return jsonify({"error": "No numbers available"}), 409

    db.session.commit()
    return jsonify({
        "assigned": True,
        "phone_number": number.phone_number,
        "sender_id": number.sender_id
    })



def _employee_scope(emp: User) -> dict:
    data = emp.accesibilidad if isinstance(emp.accesibilidad, dict) else {}
    scope = data.get('employee_scope') if isinstance(data.get('employee_scope'), dict) else {}
    categorias = scope.get('categorias') if isinstance(scope.get('categorias'), list) else []
    zonas = scope.get('zonas') if isinstance(scope.get('zonas'), list) else []
    permisos = scope.get('permisos') if isinstance(scope.get('permisos'), list) else []
    channels = scope.get('channels') if isinstance(scope.get('channels'), list) else []
    if not categorias and getattr(emp, "ticket_categorias", None):
        categorias = normalize_scope_list(emp.ticket_categorias)
    return {
        'categorias': normalize_scope_list(categorias),
        'zonas': normalize_scope_list(zonas),
        'permisos': normalize_scope_list(permisos),
        'channels': normalize_scope_list(channels),
    }


def _set_employee_scope(
    emp: User,
    *,
    categorias: list[str],
    zonas: list[str],
    permisos: list[str],
    channels: list[str] | None = None,
) -> None:
    data = emp.accesibilidad if isinstance(emp.accesibilidad, dict) else {}
    categorias_norm = normalize_scope_list(categorias)
    data['employee_scope'] = {
        'categorias': categorias_norm,
        'zonas': normalize_scope_list(zonas),
        'permisos': normalize_scope_list(permisos),
        'channels': normalize_scope_list(channels or []),
    }
    emp.accesibilidad = data
    emp.categorias_lista = categorias_norm
    flag_modified(emp, "accesibilidad")


def _scope_match_score(*, categoria: str, zona: str, scope: dict) -> int:
    score = 0
    cats = [str(c).strip().lower() for c in (scope.get('categorias') or []) if str(c).strip()]
    zones = [str(z).strip().lower() for z in (scope.get('zonas') or []) if str(z).strip()]
    if categoria and categoria in cats:
        score += 50
    if zona and zona in zones:
        score += 40
    if not cats and not zones:
        score += 10
    return score


def _employee_open_workload(
    tenant_id: int,
    employee_id: int,
    *,
    viewer: User | None = None,
) -> int:
    active_states = {'nuevo', 'pendiente', 'en_proceso'}
    tenant = db.session.get(TenantProfile, tenant_id)
    pyme_conditions = [PymeTicket.tenant_id == tenant_id]
    if tenant and getattr(tenant, "pyme_id", None):
        owner = db.session.get(User, tenant.pyme_id)
        if getattr(owner, "rubro_id", None):
            pyme_conditions.append(PymeTicket.rubro_id == owner.rubro_id)
    municipio_query = MunicipioTicket.query.filter(
        municipio_ticket_scope_filter(tenant),
        MunicipioTicket.asignado_a_id == employee_id,
        MunicipioTicket.estado.in_(list(active_states)),
    )
    pyme_query = PymeTicket.query.filter(
        or_(*pyme_conditions),
        PymeTicket.asignado_a_id == employee_id,
        PymeTicket.estado.in_(list(active_states)),
    )
    if viewer is not None:
        municipio_query = apply_employee_ticket_category_scope(
            municipio_query,
            viewer,
            MunicipioTicket,
        )
        pyme_query = apply_employee_ticket_category_scope(
            pyme_query,
            viewer,
            PymeTicket,
        )
    m_count = municipio_query.count()
    p_count = pyme_query.count()
    return int(m_count + p_count)


def _scope_has_permission(scope: dict, permission: str) -> bool:
    permisos = [str(p).strip().lower() for p in (scope.get('permisos') or []) if str(p).strip()]
    return permission.lower() in permisos


_BLOCKED_TENANT_ASSIGNABLE_ROLES = {"platform_admin", "super_admin"}
_DEFAULT_EMPLOYEE_PERMISSIONS = ["tickets_read", "tickets_update"]
_DEFAULT_EMPLOYEE_CHANNELS = ["web", "whatsapp"]


def _normalize_employee_roles(raw_roles) -> list[str]:
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]
    if not isinstance(raw_roles, list):
        raw_roles = ["empleado"]

    roles: list[str] = []
    seen: set[str] = set()
    for item in raw_roles:
        role = str(item or "").strip().lower()
        if not role or role in seen or role in _BLOCKED_TENANT_ASSIGNABLE_ROLES:
            continue
        seen.add(role)
        roles.append(role[:50])
    return roles or ["empleado"]


def _employee_role_names(emp: User, tenant: TenantProfile) -> list[str]:
    rows = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
    role_names = [ur.role.name for ur in rows if ur.role and ur.role.name]
    if not role_names and getattr(emp, "rol", None):
        role_names = [str(emp.rol)]
    return _normalize_employee_roles(role_names)


def _replace_employee_roles(emp: User, tenant: TenantProfile, raw_roles) -> list[str]:
    roles = _normalize_employee_roles(raw_roles)
    UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).delete(synchronize_session=False)
    for role_name in roles:
        role = Role.query.filter_by(name=role_name).first()
        if not role:
            role = Role(name=role_name, description=f"Rol {role_name}")
            db.session.add(role)
            db.session.flush()
        db.session.add(UserRole(user_id=emp.id, role_id=role.id, tenant_id=tenant.id))
    emp.rol = roles[0]
    return roles


def _parse_employee_categories_payload(tenant: TenantProfile, data: dict) -> tuple[list[CategoriaTicket], list[str]]:
    raw_categories = data.get('categories') or data.get('categorias') or []
    if isinstance(raw_categories, str):
        raw_categories = [raw_categories]
    if not isinstance(raw_categories, list):
        raw_categories = []

    category_ids = data.get("category_ids") or data.get("categoria_ids") or []
    if isinstance(category_ids, (str, int)):
        category_ids = [category_ids]
    if not isinstance(category_ids, list):
        category_ids = []

    parsed_ids: list[int] = []
    category_labels: list[str] = []

    for item in category_ids:
        if str(item or "").isdigit():
            parsed_ids.append(int(item))

    for item in raw_categories:
        if isinstance(item, dict):
            value = item.get("id")
            label = item.get("nombre") or item.get("label") or item.get("value") or item.get("name")
        else:
            value = item
            label = item
        if str(value or "").isdigit():
            parsed_ids.append(int(value))
        elif str(label or "").strip():
            category_labels.append(str(label).strip())

    valid_cats: list[CategoriaTicket] = []
    category_names: list[str] = []
    if parsed_ids:
        valid_cats = CategoriaTicket.query.filter(
            CategoriaTicket.id.in_(list(dict.fromkeys(parsed_ids))),
            CategoriaTicket.tenant_id == tenant.id,
        ).all()
        category_names.extend(cat.nombre for cat in valid_cats if cat.nombre)
    category_names.extend(category_labels)
    return valid_cats, normalize_scope_list(category_names)


def _serialize_admin_employee(emp: User, tenant: TenantProfile) -> dict:
    scope = _employee_scope(emp)
    return {
        "id": emp.id,
        "employee_id": emp.id,
        "name": emp.name,
        "email": emp.email,
        "role": emp.rol,
        "rol": emp.rol,
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "roles": _employee_role_names(emp, tenant),
        "categories": [cat.id for cat in getattr(emp, "categorias_ticket", [])],
        "categorias": [
            {"id": cat.id, "nombre": cat.nombre, "tipo": getattr(cat, "tipo", None)}
            for cat in getattr(emp, "categorias_ticket", [])
            if cat is not None
        ],
        "scope": scope,
        "employee_scope": scope,
        "created_at": emp.fecha_creacion.isoformat() if emp.fecha_creacion else None,
        "frontend_contract": {
            "render_as": "employee_crm_record",
            "editable_fields": ["name", "password", "roles", "scope"],
            "scope_dimensions": ["categorias", "zonas", "channels", "permisos"],
        },
    }

# --- Employee Management ---

@admin_tenant_bp.route('/api/admin/employees', methods=['POST'])
@token_requerido
@require_tenant
def create_employee(current_user):
    """
    Crea un nuevo empleado en el tenant actual.
    Requires X-Tenant header to resolve g.tenant_profile.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}
    email = str(data.get('email') or '').strip().lower()
    name = str(data.get('name') or '').strip()
    password = data.get('password')

    if not email or not password:
        return jsonify({'error': 'Missing email or password'}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'User already exists'}), 400

    user = User(
        email=email,
        name=name or email.split('@')[0],
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tipo_chat=tenant.tipo,
        es_empleado=True,
        rol='empleado'
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()

    assigned_roles = _replace_employee_roles(user, tenant, data.get('roles') or data.get('role') or ['empleado'])

    # Asignar categorías
    valid_cats, category_names = _parse_employee_categories_payload(tenant, data)
    if valid_cats:
        user.categorias_ticket = valid_cats

    scope_raw = data.get('scope') if isinstance(data.get('scope'), dict) else {}
    scope_categories = normalize_scope_list(scope_raw.get('categorias') or scope_raw.get('categories') or category_names)
    _set_employee_scope(
        user,
        categorias=scope_categories,
        zonas=normalize_scope_list(scope_raw.get('zonas') or scope_raw.get('zones')),
        permisos=normalize_scope_list(scope_raw.get('permisos') or scope_raw.get('permissions') or _DEFAULT_EMPLOYEE_PERMISSIONS),
        channels=normalize_scope_list(scope_raw.get('channels') or scope_raw.get('canales') or _DEFAULT_EMPLOYEE_CHANNELS),
    )

    db.session.commit()

    return jsonify({
        'contract_version': 'employee.admin_create.v1',
        'ok': True,
        'message': 'Employee created',
        'id': user.id,
        'employee': _serialize_admin_employee(user, tenant) | {'roles': assigned_roles},
        'coverage_endpoint': f'/api/admin/tenants/{tenant.slug}/employees/coverage',
        'routing_endpoint': '/api/v2/employee-routing',
    }), 201


@admin_tenant_bp.route('/api/admin/employees/<int:user_id>', methods=['PUT', 'PATCH'])
@token_requerido
@require_tenant
def update_employee_admin(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    user = User.query.filter_by(id=user_id, tenant_id=tenant.id, es_empleado=True).first()
    if not user:
        return jsonify({'error': 'Employee not found', 'reason_code': 'employee_not_found'}), 404

    data = request.get_json(silent=True) or {}

    if 'email' in data:
        new_email = str(data.get('email') or '').strip().lower()
        if new_email and new_email != user.email:
            return jsonify({
                'error': 'El email no puede modificarse una vez creado',
                'reason_code': 'employee_email_immutable',
            }), 400

    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if name:
            user.name = name

    if data.get('password'):
        user.set_password(str(data.get('password')))

    assigned_roles = None
    if 'roles' in data or 'role' in data:
        assigned_roles = _replace_employee_roles(user, tenant, data.get('roles') or data.get('role'))

    has_categories = any(key in data for key in ('categories', 'categorias', 'category_ids', 'categoria_ids'))
    category_names = _employee_scope(user).get('categorias') or []
    if has_categories:
        valid_cats, category_names = _parse_employee_categories_payload(tenant, data)
        user.categorias_ticket = valid_cats

    if isinstance(data.get('scope'), dict) or has_categories:
        scope_raw = data.get('scope') if isinstance(data.get('scope'), dict) else {}
        current_scope = _employee_scope(user)
        _set_employee_scope(
            user,
            categorias=normalize_scope_list(scope_raw.get('categorias') or scope_raw.get('categories') or category_names),
            zonas=normalize_scope_list(scope_raw.get('zonas') or scope_raw.get('zones') or current_scope.get('zonas')),
            permisos=normalize_scope_list(scope_raw.get('permisos') or scope_raw.get('permissions') or current_scope.get('permisos')),
            channels=normalize_scope_list(scope_raw.get('channels') or scope_raw.get('canales') or current_scope.get('channels')),
        )

    db.session.add(user)
    db.session.commit()
    employee_payload = _serialize_admin_employee(user, tenant)
    if assigned_roles is not None:
        employee_payload['roles'] = assigned_roles

    return jsonify({
        'contract_version': 'employee.admin_update.v1',
        'ok': True,
        'message': 'Employee updated',
        'employee': employee_payload,
        'coverage_endpoint': f'/api/admin/tenants/{tenant.slug}/employees/coverage',
        'routing_endpoint': '/api/v2/employee-routing',
    }), 200

@admin_tenant_bp.route('/api/admin/employees/<int:user_id>/roles', methods=['POST'])
@token_requerido
@require_tenant
def assign_role(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
         return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}
    role_name = data.get('role')

    if not role_name:
         return jsonify({'error': 'Missing role name'}), 400

    user = User.query.get(user_id)
    if not user or user.tenant_id != tenant.id:
        return jsonify({'error': 'User not found'}), 404
    if not user.es_empleado:
        user.es_empleado = True
        db.session.add(user)

    role = Role.query.filter_by(name=role_name).first()
    if not role:
        role = Role(name=role_name)
        db.session.add(role)
        db.session.commit()

    if not UserRole.query.filter_by(user_id=user.id, role_id=role.id, tenant_id=tenant.id).first():
        user_role = UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.id)
        db.session.add(user_role)
    db.session.commit()

    return jsonify({'message': f'Role {role_name} assigned'}), 200

@admin_tenant_bp.route('/api/admin/employees/<int:user_id>/categories', methods=['POST'])
@token_requerido
@require_tenant
def assign_categories(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
         return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}
    category_ids = data.get('category_ids', [])

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    valid_cats = CategoriaTicket.query.filter(
        CategoriaTicket.id.in_(category_ids),
        CategoriaTicket.tenant_id == tenant.id
    ).all()

    user.categorias_ticket = valid_cats
    scope = _employee_scope(user)
    _set_employee_scope(
        user,
        categorias=[cat.nombre for cat in valid_cats if cat.nombre],
        zonas=scope.get('zonas') or [],
        permisos=scope.get('permisos') or [],
        channels=scope.get('channels') or [],
    )
    db.session.commit()

    return jsonify({'message': 'Categories updated', 'count': len(valid_cats), 'scope': _employee_scope(user)}), 200


@admin_tenant_bp.route('/api/admin/employees/<int:user_id>/scope', methods=['PUT'])
@token_requerido
@require_tenant
def update_employee_scope(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    user = User.query.get(user_id)
    if not user or user.tenant_id != tenant.id or not user.es_empleado:
        return jsonify({'error': 'Employee not found'}), 404

    data = request.get_json(silent=True) or {}
    categorias = normalize_scope_list(data.get('categorias') or data.get('categories'))
    zonas = normalize_scope_list(data.get('zonas') or data.get('zones'))
    permisos = normalize_scope_list(data.get('permisos') or data.get('permissions'))
    channels = normalize_scope_list(data.get('channels') or data.get('canales'))

    _set_employee_scope(user, categorias=categorias, zonas=zonas, permisos=permisos, channels=channels)
    db.session.commit()
    return jsonify({'ok': True, 'employee_id': user.id, 'scope': _employee_scope(user)})


@admin_tenant_bp.route('/api/admin/tenants/<slug>/employees/suggest-assignee', methods=['POST'])
@token_requerido
@require_tenant
def suggest_assignee(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    payload = request.get_json(silent=True) or {}
    categoria = str(payload.get('categoria') or '').strip().lower()
    zona = str(payload.get('zona') or payload.get('distrito') or '').strip().lower()

    required_permission = str(payload.get('required_permission') or payload.get('permiso') or '').strip().lower()
    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()
    ranked = []
    for emp in employees:
        scope = _employee_scope(emp)
        if required_permission and not _scope_has_permission(scope, required_permission):
            continue
        base_score = _scope_match_score(categoria=categoria, zona=zona, scope=scope)
        if base_score <= 0:
            continue
        workload = _employee_open_workload(tenant.id, emp.id)
        final_score = max(base_score - min(workload * 5, 30), 1)
        ranked.append({
            'employee_id': emp.id,
            'name': emp.name,
            'email': emp.email,
            'score': final_score,
            'base_score': base_score,
            'workload_open_tickets': workload,
            'scope': scope,
        })

    ranked.sort(key=lambda r: (r['score'], -r['workload_open_tickets']), reverse=True)
    return jsonify({'ok': True, 'suggestions': ranked[:10]})



@admin_tenant_bp.route('/api/admin/tenants/<slug>/tickets/<ticket_type>/<int:ticket_id>/auto-assign', methods=['POST'])
@token_requerido
@require_tenant
def auto_assign_ticket(current_user, slug, ticket_type: str, ticket_id: int):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    ticket = MunicipioTicket.query.get(ticket_id) if ticket_type == 'municipio' else PymeTicket.query.get(ticket_id) if ticket_type == 'pyme' else None
    if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
        return jsonify({'error': 'Ticket no encontrado'}), 404

    categoria = str(getattr(ticket, 'categoria', None) or '').strip().lower()
    zona = str(getattr(ticket, 'distrito', None) or getattr(ticket, 'direccion', None) or '').strip().lower()

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()
    best = None
    best_score = -1
    payload = request.get_json(silent=True) or {}
    required_permission = str(payload.get('required_permission') or '').strip().lower()

    for emp in employees:
        scope = _employee_scope(emp)
        if required_permission and not _scope_has_permission(scope, required_permission):
            continue
        base_score = _scope_match_score(categoria=categoria, zona=zona, scope=scope)
        workload = _employee_open_workload(tenant.id, emp.id)
        score = max(base_score - min(workload * 5, 30), 0)
        if score > best_score:
            best_score = score
            best = (emp, scope, workload)

    if not best or best_score <= 0:
        return jsonify({'ok': False, 'assigned': False, 'reason': 'no_match'}), 200

    emp, scope, workload = best
    if hasattr(ticket, 'asignado_a_id'):
        ticket.asignado_a_id = emp.id
        ticket.asignado_en = datetime.now(timezone.utc)

    details = _ticket_details(ticket)
    timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []
    timeline.append({
        'at': datetime.now(timezone.utc).isoformat(),
        'by_user_id': current_user.id,
        'event': 'auto_assign_employee_scope',
        'employee_id': emp.id,
        'employee_name': emp.name,
        'score': best_score,
        'workload_open_tickets': workload,
        'categoria': categoria or None,
        'zona': zona or None,
    })
    details['lead_timeline'] = timeline[-100:]
    _save_ticket_details(ticket, details)
    if hasattr(ticket, 'ultima_actividad'):
        ticket.ultima_actividad = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'ok': True,
        'assigned': True,
        'ticket_id': ticket.id,
        'ticket_type': ticket_type,
        'employee': {'id': emp.id, 'name': emp.name, 'email': emp.email},
        'score': best_score,
        'workload_open_tickets': workload,
        'scope': scope,
    })


@admin_tenant_bp.route('/api/admin/tenants/<slug>/encuestas/overview', methods=['GET'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def tenant_surveys_overview(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    limit = max(1, min(int(request.args.get('limit', 50) or 50), 100))
    response_counts = _survey_response_counts_subquery(tenant.id)
    rows = (
        db.session.query(
            EncEncuesta,
            func.coalesce(response_counts.c.real_count, 0).label("real_count"),
            func.coalesce(response_counts.c.synthetic_count, 0).label("synthetic_count"),
            func.coalesce(response_counts.c.unverified_count, 0).label("unverified_count"),
        )
        .outerjoin(response_counts, response_counts.c.encuesta_id == EncEncuesta.id)
        .filter(EncEncuesta.tenant_id == tenant.id)
        .order_by(EncEncuesta.updated_at.desc())
        .limit(limit)
        .all()
    )

    items = []
    total_responses = 0
    total_synthetic_responses = 0
    total_unverified_responses = 0
    for encuesta, real_count, synthetic_count, unverified_count in rows:
        responses_count = int(real_count or 0)
        total_synthetic_responses += int(synthetic_count or 0)
        total_unverified_responses += int(unverified_count or 0)
        total_responses += responses_count
        items.append({
            'id': encuesta.id,
            'slug': encuesta.slug,
            'titulo': encuesta.titulo,
            'estado': encuesta.estado,
            'tipo': encuesta.tipo,
            'es_votacion_envivo': bool(encuesta.es_votacion_envivo),
            'mostrar_resultados_envivo': bool(encuesta.mostrar_resultados_envivo),
            'respuestas': responses_count,
            'inicio_at': encuesta.inicio_at.isoformat() if encuesta.inicio_at else None,
            'fin_at': encuesta.fin_at.isoformat() if encuesta.fin_at else None,
            'updated_at': encuesta.updated_at.isoformat() if encuesta.updated_at else None,
        })

    return jsonify({
        'tenant_id': tenant.id,
        'tenant_slug': tenant.slug,
        'total_surveys': len(items),
        'total_responses': total_responses,
        'response_provenance': build_survey_response_provenance(
            real_count=total_responses,
            synthetic_count=total_synthetic_responses,
            unverified_count=total_unverified_responses,
            mode='real',
        ),
        'items': items,
    })





@admin_tenant_bp.route('/api/admin/tenants/<slug>/tickets/unread-summary', methods=['GET'])
@token_requerido
@require_tenant
def tenant_unread_ticket_summary(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    since_minutes = max(5, min(int(request.args.get('since_minutes', 1440) or 1440), 7 * 24 * 60))
    limit = max(1, min(int(request.args.get('limit', 30) or 30), 200))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)

    items = []

    muni_rows = (
        db.session.query(TicketComentario.municipio_ticket_id, func.count(TicketComentario.id), func.max(TicketComentario.fecha))
        .join(MunicipioTicket, MunicipioTicket.id == TicketComentario.municipio_ticket_id)
        .filter(
            municipio_ticket_scope_filter(tenant),
            TicketComentario.es_admin.is_(False),
            TicketComentario.fecha >= cutoff,
        )
        .group_by(TicketComentario.municipio_ticket_id)
        .all()
    )
    for ticket_id, unread_count, last_at in muni_rows:
        collaboration_state = build_ticket_collaboration_state(ticket_type='municipio', ticket_id=ticket_id)
        items.append({
            'ticket_type': 'municipio',
            'ticket_id': ticket_id,
            'unread_count': int(unread_count or 0),
            'last_message_at': last_at.isoformat() if last_at else None,
            'collaboration_state': collaboration_state,
        })

    pyme_rows = (
        db.session.query(TicketComentario.pyme_ticket_id, func.count(TicketComentario.id), func.max(TicketComentario.fecha))
        .join(PymeTicket, PymeTicket.id == TicketComentario.pyme_ticket_id)
        .filter(
            PymeTicket.tenant_id == tenant.id,
            TicketComentario.es_admin.is_(False),
            TicketComentario.fecha >= cutoff,
        )
        .group_by(TicketComentario.pyme_ticket_id)
        .all()
    )
    for ticket_id, unread_count, last_at in pyme_rows:
        collaboration_state = build_ticket_collaboration_state(ticket_type='pyme', ticket_id=ticket_id)
        items.append({
            'ticket_type': 'pyme',
            'ticket_id': ticket_id,
            'unread_count': int(unread_count or 0),
            'last_message_at': last_at.isoformat() if last_at else None,
            'collaboration_state': collaboration_state,
        })

    items.sort(key=lambda x: (x['last_message_at'] or ''), reverse=True)
    return jsonify({
        'tenant_id': tenant.id,
        'tenant_slug': tenant.slug,
        'since_minutes': since_minutes,
        'total_tickets_with_unread': len(items),
        'items': items[:limit],
    })

@admin_tenant_bp.route('/api/admin/tenants/<slug>/employees/workload', methods=['GET'])
@token_requerido
@require_tenant
def tenant_employees_workload(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()
    items = []
    for emp in employees:
        scope = _employee_scope(emp)
        items.append({
            'employee_id': emp.id,
            'name': emp.name,
            'email': emp.email,
            'workload_open_tickets': _employee_open_workload(tenant.id, emp.id),
            'scope': scope,
        })

    items.sort(key=lambda x: x['workload_open_tickets'], reverse=True)
    return jsonify({'tenant_id': tenant.id, 'tenant_slug': tenant.slug, 'items': items})


@admin_tenant_bp.route('/api/admin/tenants/<slug>/dashboard-bundle', methods=['GET'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def tenant_dashboard_bundle(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    leads_limit = max(1, min(int(request.args.get('leads_limit', 100) or 100), 250))
    surveys_limit = max(1, min(int(request.args.get('surveys_limit', 20) or 20), 100))
    unread_limit = max(1, min(int(request.args.get('unread_limit', 30) or 30), 200))
    since_minutes = max(5, min(int(request.args.get('since_minutes', 1440) or 1440), 7 * 24 * 60))

    payload = _build_tenant_dashboard_bundle_payload(
        tenant,
        viewer=current_user,
        leads_limit=leads_limit,
        surveys_limit=surveys_limit,
        unread_limit=unread_limit,
        since_minutes=since_minutes,
    )
    payload['meta'] = {
        'leads_limit': leads_limit,
        'surveys_limit': surveys_limit,
        'unread_limit': unread_limit,
        'since_minutes': since_minutes,
    }
    return jsonify(payload)


@admin_tenant_bp.route('/api/admin/tenants/<slug>/heatmap-summary', methods=['GET'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def tenant_heatmap_summary(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    limit_points = max(100, min(int(request.args.get('limit_points', 1500) or 1500), 5000))
    payload = _build_tenant_heatmap_summary_payload(
        tenant,
        viewer=current_user,
        limit_points=limit_points,
    )
    payload['meta'] = {'limit_points': limit_points}
    return jsonify(payload)


@admin_tenant_bp.route('/api/admin/tenants/<slug>/employees/coverage', methods=['GET'])
@token_requerido
@require_tenant
def tenant_employee_coverage(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    return jsonify(_build_employee_coverage_payload(tenant))

@admin_tenant_bp.route('/api/admin/employees', methods=['GET'])
@token_requerido
@require_tenant
def list_current_tenant_employees(current_user):
    """
    List employees for the current tenant context.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()

    results = [_serialize_admin_employee(emp, tenant) for emp in employees]

    return jsonify(results)


# --- Integration Management ---

@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations', methods=['GET'])
@token_requerido
@require_tenant
def list_integrations(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, "marketplace_sync")

    integrations = IntegrationAccount.query.filter_by(tenant_id=tenant.id).all()

    # Mock status for known types if missing
    known_types = ["MercadoLibre", "TiendaNube", "WhatsApp", "MercadoPago"]
    result = {}

    # Fill from DB
    for integ in integrations:
        result[integ.type] = {
            "type": integ.type,
            "connected": integ.status == 'active',
            "lastSync": integ.last_sync_at.isoformat() if integ.last_sync_at else None,
            "account": integ.metadata_payload.get('account_name') if integ.metadata_payload else None
        }

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    mercadopago_token = cfg.get("mercadopago_access_token")
    if mercadopago_token:
        result["MercadoPago"] = {
            "type": "MercadoPago",
            "connected": True,
            "status": cfg.get("mercadopago_status") or "configured",
            "lastSync": cfg.get("mercadopago_tested_at"),
            "account": _mask_token(mercadopago_token),
        }

    # Fill missing
    for t in known_types:
        if t not in result:
            result[t] = {"type": t, "connected": False}

    return jsonify(list(result.values()))


@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/<string:integration_type>/connect', methods=['GET'])
@token_requerido
@require_tenant
def connect_integration(current_user, slug, integration_type):
    # Handle numeric index from legacy frontend
    if integration_type.isdigit():
        idx = int(integration_type)
        # Order matching list_integrations: ["MercadoLibre", "TiendaNube", "WhatsApp"]
        mapping = {
            0: 'mercadolibre',
            1: 'tiendanube',
            2: 'whatsapp'
        }
        if idx in mapping:
            integration_type = mapping[idx]
            current_app.logger.info(f"Mapped integration index {idx} to {integration_type}")

    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, _integration_plan_feature_id(integration_type))

    # Marketplace OAuth used to put the plain tenant id in ``state``.  Keep
    # those legacy entry points fail-closed until state is signed, expiring and
    # backed by a one-time nonce store.  The WhatsApp branch below uses its own
    # tenant-bound Tech Provider contract and is intentionally unaffected.
    if integration_type.lower() in {'tiendanube', 'mercadolibre'}:
        migration_flag_requested = bool(
            current_app.config.get("LEGACY_INTEGRATIONS_TRANSPORT_ENABLED", False)
        )
        response = jsonify(
            {
                "contract_version": "tenant.integration.legacy_transport_disabled.v1",
                "status": "disabled",
                "reason_code": (
                    "legacy_integration_secure_transport_unavailable"
                    if migration_flag_requested
                    else "legacy_oauth_connect_disabled"
                ),
                "retryable": False,
                "next_action": "configure_tenant_bound_signed_provider_adapter",
            }
        )
        response.status_code = 503 if migration_flag_requested else 404
        response.headers["Cache-Control"] = "no-store"
        return response

    if integration_type.lower() == 'whatsapp':
        contract = build_twilio_tech_provider_contract(tenant, current_app.config)
        embedded_signup = contract.get("embedded_signup") or {}
        setup_health = contract.get("setup_health") or {}
        env = ((contract.get("automation") or {}).get("env") or {})
        start_url = embedded_signup.get("start_url") or embedded_signup.get("url")

        payload = {
            "contract_version": "tenant.integration.connect.v2_adapter",
            "provider": "twilio_tech_provider",
            "legacy_integration_type": "whatsapp",
            "tenant": contract.get("tenant"),
            "status": contract.get("status"),
            "redirect_url": start_url,
            "url": start_url,
            "embedded_signup": embedded_signup,
            "status_endpoint": f"/api/v2/tenants/{tenant.slug}/integrations/whatsapp/status",
            "tech_provider_endpoint": f"/api/v2/tenants/{tenant.slug}/whatsapp/tech-provider",
            "completion_endpoint": embedded_signup.get("completion_endpoint"),
            "contract": contract,
            "frontend_contract": {
                "render_as": "twilio_tech_provider_onboarding",
                "legacy_adapter": True,
                "primary_action": (contract.get("frontend_contract") or {}).get("primary_action"),
                "show_twilio_console": False,
            },
        }
        if not env.get("ready") or not start_url:
            payload.update(
                {
                    "error": "platform_not_configured",
                    "reason_code": "missing_twilio_meta_platform_env",
                    "message": "Faltan variables de Twilio/Meta para iniciar WhatsApp productivo.",
                    "missing": env.get("missing") or [],
                    "setup_health": setup_health,
                }
            )
            return jsonify(payload), 409

        return jsonify(payload)

    return jsonify({"error": "Integration type not supported"}), 400



def _mask_token(raw: str | None) -> str | None:
    if not raw:
        return None
    value = str(raw)
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/mercadopago', methods=['GET'])
@token_requerido
@require_tenant
def get_mercadopago_credentials(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, "mercadopago_checkout")

    cfg = tenant.configuracion or {}
    token = cfg.get('mercadopago_access_token')
    status = cfg.get('mercadopago_status')
    tested_at = cfg.get('mercadopago_tested_at')

    return jsonify({
        "provider": "mercadopago",
        "configured": bool(token),
        "access_token_masked": _mask_token(token),
        "status": status or ("configured" if token else "missing"),
        "tested_at": tested_at,
    })


@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/mercadopago', methods=['POST'])
@token_requerido
@require_tenant
def set_mercadopago_credentials(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, "mercadopago_checkout")

    payload = request.get_json(silent=True) or {}
    access_token = (payload.get('access_token') or payload.get('token') or '').strip()
    if not access_token:
        return jsonify({"error": "access_token es requerido"}), 400

    cfg = tenant.configuracion or {}
    cfg['mercadopago_access_token'] = access_token
    cfg['mercadopago_status'] = 'configured'
    cfg['mercadopago_tested_at'] = None
    tenant.configuracion = cfg
    db.session.commit()

    return jsonify({
        "provider": "mercadopago",
        "configured": True,
        "access_token_masked": _mask_token(access_token),
        "status": "configured",
    })


@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/mercadopago/test', methods=['POST'])
@token_requerido
@require_tenant
def test_mercadopago_credentials(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, "mercadopago_checkout")

    cfg = tenant.configuracion or {}
    token = cfg.get('mercadopago_access_token')
    if not token:
        return jsonify({"error": "No hay token configurado"}), 400

    ok = False
    details = None
    try:
        resp = requests.get(
            'https://api.mercadopago.com/v1/account',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        ok = bool(resp.ok)
        if resp.ok:
            body = resp.json()
            details = {
                "id": body.get("id"),
                "site_id": body.get("site_id"),
                "email": body.get("email"),
            }
        else:
            details = {"status_code": resp.status_code}
    except Exception as exc:
        details = {"error": str(exc)}

    cfg['mercadopago_status'] = 'ok' if ok else 'error'
    cfg['mercadopago_tested_at'] = datetime.now(timezone.utc).isoformat()
    tenant.configuracion = cfg
    db.session.commit()

    return jsonify({
        "provider": "mercadopago",
        "ok": ok,
        "status": cfg['mercadopago_status'],
        "tested_at": cfg['mercadopago_tested_at'],
        "details": details,
    }), 200 if ok else 502

@admin_tenant_bp.route('/api/admin/tenants/<slug>/employees', methods=['GET'])
@token_requerido
@require_tenant
def list_employees_by_slug(current_user, slug):
    """
    List employees for a specific tenant slug (supports admin dashboard deep linking).
    """
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()

    results = [_serialize_admin_employee(emp, tenant) for emp in employees]

    return jsonify(results)

@admin_tenant_bp.route('/api/admin/tenants/<slug>/ticket-categories', methods=['GET'])
@admin_tenant_bp.route('/admin/tenants/<slug>/ticket-categories', methods=['GET']) # Alias without /api prefix to support legacy/broken frontend calls
@token_requerido
@require_tenant
def list_ticket_categories_by_slug(current_user, slug):
    """
    List ticket categories for a specific tenant.
    """
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check (Employees/Admins can list categories)
    if not _is_authorized_for_tenant(current_user, tenant) and current_user.rol != "empleado":
         # Allow employees to list categories to assign tickets?
         # Assuming yes for now, or just require tenant admin.
         # Actually standard auth check is fine.
         if not _is_authorized_for_tenant(current_user, tenant):
             return jsonify({'error': 'Unauthorized'}), 403

    categories = CategoriaTicket.query.filter_by(tenant_id=tenant.id).all()
    items = [{
        "id": c.id,
        "nombre": c.nombre,
        "tipo": c.tipo
    } for c in categories]
    if not items:
        dimensions = tenant_operational_dimensions(tenant)
        items = [
            {"id": None, "nombre": categoria, "tipo": "ticket", "source": "operational_dimensions"}
            for categoria in dimensions.get("categorias", [])
        ]

    return jsonify(items)

@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/<string:integration_type>/sync', methods=['POST'])
@token_requerido
@require_tenant
def sync_integration(current_user, slug, integration_type):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, _integration_plan_feature_id(integration_type, operation="sync"))

    if integration_type.lower() == 'mercadolibre':
        from services.integrations.mercadolibre import MercadoLibreService
        service = MercadoLibreService(tenant)
        result = service.sync_catalog()
        return jsonify(result)

    elif integration_type.lower() == 'tiendanube':
        from services.integrations.tiendanube import TiendaNubeService
        service = TiendaNubeService(tenant)
        result = service.import_products()
        return jsonify(result)

    return jsonify({"error": "Integration not supported"}), 400

@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/<string:integration_type>/preview', methods=['GET'])
@token_requerido
@require_tenant
def preview_integration_sync(current_user, slug, integration_type):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant, _integration_plan_feature_id(integration_type, operation="preview"))

    if integration_type.lower() == 'mercadolibre':
        from services.integrations.mercadolibre import MercadoLibreService
        service = MercadoLibreService(tenant)
        result = service.preview_sync()
        return jsonify(result)

    return jsonify({"error": "Integration not supported or preview unavailable"}), 400

@admin_tenant_bp.route('/api/admin/tenants/<slug>/orders', methods=['GET'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def list_tenant_orders(current_user, slug):
    """
    List orders for a specific tenant.
    """
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    # Fetch orders linked to this tenant (Unified View)
    status_filter = (request.args.get('status') or '').strip().lower() or None
    limit = max(1, min(int(request.args.get('limit', 50) or 50), 200))

    order_records = []

    legacy_scope = PymePedido.tenant_id == tenant.id
    if tenant.pyme_id:
        legacy_scope = or_(
            legacy_scope,
            (PymePedido.tenant_id.is_(None)) & (PymePedido.pyme_id == tenant.pyme_id),
        )
    legacy_query = PymePedido.query.filter(legacy_scope)
    if status_filter:
        legacy_query = legacy_query.filter(func.lower(PymePedido.estado) == status_filter)
    order_records.extend(legacy_query.order_by(PymePedido.fecha.desc()).limit(limit).all())

    market_query = MarketOrder.legacy_safe_query().filter(MarketOrder.tenant_id == tenant.id)
    if status_filter:
        market_query = market_query.filter(func.lower(MarketOrder.status) == status_filter)
    order_records.extend(market_query.order_by(MarketOrder.created_at.desc()).limit(limit).all())

    conversational_query = PedidoConversacional.query.filter(PedidoConversacional.tenant_id == tenant.id)
    if status_filter:
        conversational_query = conversational_query.filter(func.lower(PedidoConversacional.estado) == status_filter)
    order_records.extend(conversational_query.order_by(PedidoConversacional.created_at.desc()).limit(limit).all())

    canonical_query = Order.query.filter(Order.tenant_id == tenant.id)
    if status_filter:
        canonical_query = canonical_query.filter(func.lower(Order.status) == status_filter)
    order_records.extend(canonical_query.order_by(Order.created_at.desc()).limit(limit).all())

    results = dedupe_unified_orders([_serialize_tenant_order(record, tenant) for record in order_records])
    results.sort(key=lambda item: item.get('created_at') or '', reverse=True)
    page_results = results[:limit]
    summary = summarize_unified_orders(results, page_limit=limit)

    return jsonify({
        "orders": page_results,
        "count": len(page_results),
        "total": len(results),
        "sources": summary["sources"],
        "summary": summary,
    })


def _resolve_tenant_order_record(tenant: TenantProfile, order_id: str):
    raw_id = str(order_id or "").strip()
    if not raw_id:
        return None

    source_prefix = None
    source_id = raw_id
    if ":" in raw_id:
        source_prefix, source_id = raw_id.split(":", 1)
        source_prefix = source_prefix.strip().lower()
        source_id = source_id.strip()

    legacy_pyme_filter = tenant.pyme_id if tenant.pyme_id else -1

    if source_prefix == "order":
        return Order.query.filter_by(id=source_id, tenant_id=tenant.id).first()
    if source_prefix not in {None, "market", "conversational", "legacy"}:
        return None

    try:
        numeric_id = int(source_id)
    except (TypeError, ValueError):
        return Order.query.filter_by(id=source_id, tenant_id=tenant.id).first() if source_prefix is None else None

    if source_prefix == "market":
        return MarketOrder.legacy_safe_query().filter(MarketOrder.id == numeric_id, MarketOrder.tenant_id == tenant.id).first()
    if source_prefix == "conversational":
        return PedidoConversacional.query.filter_by(id=numeric_id, tenant_id=tenant.id).first()
    if source_prefix == "legacy":
        return PymePedido.query.filter(
            PymePedido.id == numeric_id,
            or_(
                PymePedido.tenant_id == tenant.id,
                (PymePedido.tenant_id.is_(None)) & (PymePedido.pyme_id == legacy_pyme_filter),
            ),
        ).first()

    return (
        Order.query.filter_by(id=source_id, tenant_id=tenant.id).first()
        or MarketOrder.legacy_safe_query().filter(MarketOrder.id == numeric_id, MarketOrder.tenant_id == tenant.id).first()
        or PedidoConversacional.query.filter_by(id=numeric_id, tenant_id=tenant.id).first()
        or PymePedido.query.filter(
            PymePedido.id == numeric_id,
            or_(
                PymePedido.tenant_id == tenant.id,
                (PymePedido.tenant_id.is_(None)) & (PymePedido.pyme_id == legacy_pyme_filter),
            ),
        ).first()
    )


def _apply_tenant_order_status(record, status: str) -> None:
    normalized = str(status or "").strip()
    if not normalized:
        return
    if isinstance(record, (MarketOrder, Order)):
        record.status = normalized
    elif isinstance(record, (PedidoConversacional, PymePedido)):
        record.estado = normalized


def _should_materialize_assisted_order(record, tenant: TenantProfile, status: str) -> bool:
    normalized = str(status or "").strip().lower()
    metadata = getattr(record, "metadata_payload", None)
    return (
        isinstance(record, PedidoConversacional)
        and normalized in {"confirmed", "confirmado"}
        and bool(getattr(tenant, "pyme_id", None))
        and isinstance(metadata, dict)
        and metadata.get("contract_version") == "marketplace.assisted_request.v1"
        and not metadata.get("materialized_order")
    )


def _admin_dict(value):
    return value if isinstance(value, dict) else {}


def _admin_list(value):
    return value if isinstance(value, list) else []


def _add_unique_blocker(blockers, code: str, message: str, **extra) -> None:
    if any(blocker.get("code") == code for blocker in blockers):
        return
    payload = {"code": code, "message": message}
    payload.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    blockers.append(payload)


def _assisted_order_confirmation_blockers(
    metadata,
    *,
    inventory_validation: dict | None = None,
) -> list[dict]:
    if not isinstance(metadata, dict):
        return [
            {
                "code": "missing_assisted_contract",
                "message": "No hay contrato asistido suficiente para crear un pedido operativo.",
            }
        ]

    blockers: list[dict] = []
    source = _admin_dict(metadata.get("source"))
    structured = _admin_dict(metadata.get("structured_extraction"))
    match_summary = _admin_dict(metadata.get("match_summary"))
    operator_pack = _admin_dict(metadata.get("operator_pack"))
    intake = _admin_dict(metadata.get("intake_experience"))
    crm_handoff = _admin_dict(metadata.get("crm_handoff"))
    draft = _admin_dict(metadata.get("crm_order_draft")) or _admin_dict(crm_handoff.get("draft_order"))
    confirmation = _admin_dict(draft.get("customer_confirmation"))

    if (
        metadata.get("extraction_error")
        or source.get("extraction_error")
        or structured.get("extraction_error")
        or str(source.get("provider_status") or metadata.get("provider_status") or "").strip().lower() in {"failed", "error"}
    ):
        _add_unique_blocker(
            blockers,
            "extraction_error",
            "La lectura IA de la solicitud fallo o quedo incompleta.",
        )

    if (
        match_summary.get("needs_operator_review") is True
        or operator_pack.get("needs_human_review") is True
        or intake.get("needs_operator_review") is True
        or metadata.get("needs_operator_review") is True
    ):
        _add_unique_blocker(
            blockers,
            "needs_operator_review",
            "La solicitud todavia requiere revision operativa.",
        )

    unmatched_items = (
        _admin_list(metadata.get("unmatched_items"))
        or _admin_list(draft.get("unmatched_items"))
        or _admin_list(structured.get("unmatched_items"))
    )
    if unmatched_items:
        _add_unique_blocker(
            blockers,
            "unmatched_items",
            "Hay articulos que no fueron vinculados con el catalogo.",
            count=len(unmatched_items),
        )

    catalog_candidates = _admin_list(metadata.get("catalog_candidates")) or _admin_list(draft.get("catalog_candidates"))
    if catalog_candidates:
        _add_unique_blocker(
            blockers,
            "catalog_candidates",
            "Hay candidatos de catalogo pendientes de resolucion.",
            count=len(catalog_candidates),
        )

    blocking_reasons = _admin_list(confirmation.get("blocking_reasons"))
    if blocking_reasons:
        _add_unique_blocker(
            blockers,
            "customer_confirmation_blocked",
            "La confirmacion del cliente tiene datos bloqueantes pendientes.",
            reasons=blocking_reasons[:6],
        )

    row_errors = (
        _admin_list(metadata.get("row_errors"))
        or _admin_list(structured.get("row_errors"))
        or _admin_list(draft.get("row_errors"))
    )
    if row_errors:
        _add_unique_blocker(
            blockers,
            "row_errors",
            "La extraccion contiene renglones con errores.",
            count=len(row_errors),
        )

    lines = _admin_list(draft.get("lines")) or _admin_list(metadata.get("detected_items"))
    if not lines:
        _add_unique_blocker(
            blockers,
            "missing_draft_lines",
            "No hay renglones validados para crear el pedido.",
        )
        return blockers

    review_states = {
        "needs_operator_review",
        "catalog_gap",
        "unmatched",
        "pending_resolution",
        "needs_catalog_resolution",
        "manual_review",
        "review_required",
        "ai_unavailable",
        "failed",
        "error",
    }
    for line in lines:
        if not isinstance(line, dict):
            _add_unique_blocker(
                blockers,
                "invalid_draft_line",
                "El borrador tiene renglones con estructura invalida.",
            )
            break
        state = str(
            line.get("confirmation_state")
            or line.get("status")
            or line.get("state")
            or ""
        ).strip().lower()
        if (
            line.get("needs_operator_review") is True
            or line.get("requires_operator_review") is True
            or state in review_states
            or _admin_list(line.get("catalog_candidates"))
        ):
            _add_unique_blocker(
                blockers,
                "line_needs_review",
                "Hay renglones del pedido que todavia no estan resueltos.",
                line_id=line.get("line_id") or line.get("id"),
                source_name=line.get("source_name") or line.get("name") or line.get("nombre"),
            )
            break

    if isinstance(inventory_validation, dict):
        blockers.extend(
            dict(reason)
            for reason in _admin_list(inventory_validation.get("blocking_reasons"))
            if isinstance(reason, dict)
        )

    return blockers


def _assisted_line_label(line: dict) -> str:
    return str(
        line.get("source_name")
        or line.get("name")
        or line.get("nombre")
        or line.get("sku")
        or ""
    ).strip()


def _catalog_match_payload(item: CatalogoItem) -> dict:
    price_value = parse_precio_flexible(getattr(item, "precio", None))
    return {
        "catalogo_item_id": item.id,
        "catalog_item_id": item.id,
        "product_id": item.id,
        "sku": item.sku,
        "nombre": item.nombre,
        "name": item.nombre,
        "precio": item.precio,
        "price": price_value,
        "moneda": getattr(item, "moneda", None) or "ARS",
        "unidad": getattr(item, "unidad", None),
    }


def _resolve_catalog_item_for_tenant(tenant: TenantProfile, catalog_item_id):
    try:
        numeric_id = int(catalog_item_id)
    except (TypeError, ValueError):
        return None
    query = CatalogoItem.query.filter(CatalogoItem.id == numeric_id)
    tenant_filters = [CatalogoItem.tenant_id == tenant.id]
    if getattr(tenant, "pyme_id", None):
        tenant_filters.append(CatalogoItem.user_id == tenant.pyme_id)
    return query.filter(or_(*tenant_filters)).first()


def _assisted_line_catalog_item_id(line: dict):
    catalog_match = _admin_dict(line.get("catalog_match"))
    raw_id = (
        line.get("catalog_item_id")
        or line.get("catalogo_item_id")
        or line.get("product_id")
        or catalog_match.get("catalog_item_id")
        or catalog_match.get("catalogo_item_id")
        or catalog_match.get("product_id")
    )
    try:
        parsed = int(raw_id)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _assisted_line_requested_quantity(line: dict) -> tuple[int | None, object]:
    raw_quantity = None
    for key in ("quantity", "cantidad", "qty", "unidades"):
        if line.get(key) not in (None, ""):
            raw_quantity = line.get(key)
            break
    if raw_quantity is None:
        return 1, 1
    if isinstance(raw_quantity, bool):
        return None, raw_quantity
    try:
        parsed = float(str(raw_quantity).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None, raw_quantity
    if parsed <= 0 or not parsed.is_integer():
        return None, raw_quantity
    return int(parsed), raw_quantity


def _assisted_order_inventory_validation(metadata: dict, tenant: TenantProfile) -> dict:
    crm_handoff = _admin_dict(metadata.get("crm_handoff"))
    draft = _admin_dict(metadata.get("crm_order_draft")) or _admin_dict(crm_handoff.get("draft_order"))
    lines = _admin_list(draft.get("lines")) or _admin_list(metadata.get("detected_items"))
    blocking_reasons: list[dict] = []
    grouped_items: dict[int, dict] = {}

    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        line_id = line.get("line_id") or line.get("id") or f"line-{index + 1}"
        source_name = _assisted_line_label(line) or None
        catalog_item_id = _assisted_line_catalog_item_id(line)
        if catalog_item_id is None:
            blocking_reasons.append(
                {
                    "code": "catalog_item_unresolved",
                    "message": "El renglon no esta vinculado a un producto vigente del catalogo.",
                    "line_id": line_id,
                    "source_name": source_name,
                }
            )
            continue

        quantity, raw_quantity = _assisted_line_requested_quantity(line)
        if quantity is None:
            blocking_reasons.append(
                {
                    "code": "invalid_requested_quantity",
                    "message": "La cantidad solicitada debe ser un entero positivo.",
                    "line_id": line_id,
                    "source_name": source_name,
                    "catalog_item_id": catalog_item_id,
                    "requested_quantity": raw_quantity,
                }
            )
            continue

        group = grouped_items.setdefault(
            catalog_item_id,
            {
                "catalog_item_id": catalog_item_id,
                "requested_quantity": 0,
                "line_ids": [],
                "source_names": [],
            },
        )
        group["requested_quantity"] += quantity
        group["line_ids"].append(line_id)
        if source_name:
            group["source_names"].append(source_name)

    validated_items: list[dict] = []
    for catalog_item_id, group in grouped_items.items():
        item = _resolve_catalog_item_for_tenant(tenant, catalog_item_id)
        if item is None:
            blocking_reasons.append(
                {
                    "code": "catalog_item_not_found",
                    "message": "El producto ya no existe o no pertenece al catalogo del tenant.",
                    **group,
                }
            )
            continue

        inventory = _item_inventory_payload(item, tenant)
        item_validation = {
            **group,
            "sku": item.sku,
            "name": item.nombre,
            "inventory": inventory,
        }
        validated_items.append(item_validation)
        stock_status = inventory.get("stock_status")
        stock_quantity = inventory.get("stock_quantity")
        blocker = None
        if stock_status == "not_available":
            blocker = (
                "catalog_item_not_available",
                "El producto no esta disponible para la venta.",
            )
        elif stock_status == "stock_unknown":
            blocker = (
                "stock_unknown",
                "El stock del producto no fue validado en el catalogo.",
            )
        elif stock_status == "out_of_stock":
            blocker = (
                "out_of_stock",
                "El producto no tiene stock disponible.",
            )
        elif stock_quantity is not None and group["requested_quantity"] > stock_quantity:
            blocker = (
                "insufficient_stock",
                "La cantidad solicitada supera el stock disponible.",
            )
        if blocker:
            blocking_reasons.append(
                {
                    "code": blocker[0],
                    "message": blocker[1],
                    **group,
                    "sku": item.sku,
                    "name": item.nombre,
                    "stock_quantity": stock_quantity,
                    "stock_status": stock_status,
                }
            )

    can_confirm = bool(lines) and not blocking_reasons
    return {
        "contract_version": "marketplace.assisted_inventory_validation.v1",
        "status": "validated" if can_confirm else "blocked",
        "stock_validated": can_confirm,
        "can_confirm_order": can_confirm,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "items": validated_items,
        "blocking_reasons": blocking_reasons,
    }


def _serialize_tenant_order(record, tenant: TenantProfile) -> dict:
    payload = serialize_unified_order(record)
    if not isinstance(record, PedidoConversacional):
        return payload

    metadata = _admin_dict(getattr(record, "metadata_payload", None))
    record_status = str(getattr(record, "estado", "") or "").strip().lower()
    if (
        not _uses_assisted_request_contract(metadata)
        or metadata.get("materialized_order")
        or record_status in {"confirmed", "confirmado"}
    ):
        return payload

    inventory_validation = _assisted_order_inventory_validation(metadata, tenant)
    payload["inventory_validation"] = inventory_validation

    assisted_request = dict(_admin_dict(payload.get("assisted_request")))
    if assisted_request:
        assisted_request["inventory_validation"] = inventory_validation
        payload["assisted_request"] = assisted_request

    review_card = dict(_admin_dict(payload.get("crm_review_card")))
    if review_card:
        review_card["inventory_validation"] = inventory_validation
        was_ready = review_card.get("operational_state") == "ready_for_order_creation"
        if was_ready and not inventory_validation["can_confirm_order"]:
            review_card["status"] = "needs_review"
            review_card["needs_operator_review"] = True
            review_card["operational_state"] = "needs_inventory_validation"
            actions = [dict(action) for action in _admin_list(review_card.get("operator_actions"))]
            for action in actions:
                if action.get("id") == "confirm_order_draft":
                    action["enabled"] = False
                    action["requires_review"] = True
                    action["disabled_reason"] = "inventory_validation_required"
            review_card["operator_actions"] = actions
        payload["crm_review_card"] = review_card
    return payload


def _resolution_matches_line(line: dict, resolution: dict) -> bool:
    line_id = str(line.get("line_id") or line.get("id") or "").strip()
    requested_line_id = str(resolution.get("line_id") or resolution.get("id") or "").strip()
    if requested_line_id and line_id == requested_line_id:
        return True

    source_name = str(resolution.get("source_name") or resolution.get("item") or resolution.get("label") or "").strip().lower()
    if source_name and _assisted_line_label(line).lower() == source_name:
        return True
    return False


def _customer_confirmation_from_resolved_draft(*, detected: int, matched: int, unmatched: int, has_contact: bool, needs_review: bool) -> dict:
    blocking_reasons = []
    if detected <= 0:
        blocking_reasons.append(
            {
                "id": "no_items_detected",
                "label": "No hay renglones confiables",
                "description": "Revisa el archivo o texto original antes de responder.",
            }
        )
    if unmatched > 0:
        blocking_reasons.append(
            {
                "id": "items_need_review",
                "label": "Hay articulos para revisar",
                "description": "Algunos renglones siguen sin asociarse al catalogo.",
            }
        )
    if not has_contact:
        blocking_reasons.append(
            {
                "id": "missing_contact",
                "label": "Falta contacto",
                "description": "Hace falta WhatsApp, email o telefono antes de confirmar.",
            }
        )
    if needs_review and not blocking_reasons:
        blocking_reasons.append(
            {
                "id": "operator_review_required",
                "label": "Requiere validacion",
                "description": "Confirma stock, precio y datos antes de avanzar.",
            }
        )

    confidence_score = round((matched / detected), 2) if detected else 0.0
    ready = not blocking_reasons and detected > 0
    return {
        "contract_version": "marketplace.customer_confirmation.v1",
        "status": "ready_for_customer_confirmation" if ready else "operator_review_required",
        "status_label": "Listo para confirmar" if ready else "Revision del equipo necesaria",
        "headline": "Borrador listo para confirmar" if ready else "Solicitud lista para revision",
        "description": (
            "Los renglones detectados coinciden con el catalogo y el equipo puede confirmar stock y precio."
            if ready
            else "Completa los datos pendientes antes de crear el pedido operativo."
        ),
        "confidence_level": "high" if ready else "medium" if matched > 0 else "low",
        "confidence_score": confidence_score,
        "matched": matched,
        "detected": detected,
        "blocking_reasons": blocking_reasons,
        "primary_action_id": "confirm_order_draft" if ready else "continue_by_whatsapp",
        "primary_action_label": "Confirmar borrador" if ready else "Continuar por WhatsApp o chat",
        "allowed_actions": ["confirm_order_draft", "open_tracking", "continue_by_whatsapp", "send_to_team"]
        if ready
        else ["open_tracking", "continue_by_whatsapp", "send_to_team"],
    }


def _uses_assisted_request_contract(metadata: dict) -> bool:
    contract_version = metadata.get("contract_version")
    return (
        contract_version == "marketplace.assisted_request.v1"
        or metadata.get("assisted_request_contract_version") == "marketplace.assisted_request.v1"
        or contract_version == "whatsapp.assisted_intake.v1"
        or metadata.get("mode") in {"order_note_upload", "whatsapp_order_note_upload"}
        or metadata.get("source_mode") in {"order_note_upload", "whatsapp_order_note_upload"}
    )


def _apply_assisted_catalog_resolutions(record, tenant: TenantProfile, resolutions):
    if not isinstance(record, PedidoConversacional):
        return None, (
            {
                "error": "unsupported_order_type",
                "message": "La resolucion de catalogo solo aplica a solicitudes asistidas.",
            },
            400,
        )
    if not isinstance(resolutions, list) or not resolutions:
        return None, (
            {
                "error": "invalid_catalog_resolutions",
                "message": "Envia catalog_resolutions como una lista con line_id y catalog_item_id.",
            },
            400,
        )

    metadata = dict(record.metadata_payload or {})
    if not _uses_assisted_request_contract(metadata):
        return None, (
            {
                "error": "unsupported_assisted_contract",
                "message": "La solicitud no usa el contrato de pedido asistido esperado.",
            },
            400,
        )

    crm_handoff = _admin_dict(metadata.get("crm_handoff"))
    draft = dict(_admin_dict(metadata.get("crm_order_draft")) or _admin_dict(crm_handoff.get("draft_order")))
    lines = [dict(line) for line in _admin_list(draft.get("lines")) if isinstance(line, dict)]
    if not lines:
        return None, (
            {
                "error": "missing_draft_lines",
                "message": "No hay renglones del borrador para resolver.",
            },
            422,
        )

    resolved_lines = []
    resolution_errors = []
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            resolution_errors.append({"code": "invalid_resolution", "message": "Cada resolucion debe ser un objeto."})
            continue
        item = _resolve_catalog_item_for_tenant(tenant, resolution.get("catalog_item_id") or resolution.get("catalogo_item_id"))
        if not item:
            resolution_errors.append(
                {
                    "code": "catalog_item_not_found",
                    "message": "El producto indicado no pertenece al catalogo del tenant.",
                    "catalog_item_id": resolution.get("catalog_item_id") or resolution.get("catalogo_item_id"),
                }
            )
            continue
        line = next((candidate for candidate in lines if _resolution_matches_line(candidate, resolution)), None)
        if not line:
            resolution_errors.append(
                {
                    "code": "line_not_found",
                    "message": "No se encontro el renglon a resolver.",
                    "line_id": resolution.get("line_id") or resolution.get("id"),
                }
            )
            continue

        catalog_payload = _catalog_match_payload(item)
        line.update(
            {
                "status": "catalog_matched",
                "confirmation_state": "ready",
                "confidence": "operator_confirmed",
                "customer_visible_status": "Producto confirmado en catalogo",
                "catalog_item_id": item.id,
                "catalogo_item_id": item.id,
                "catalog_match": catalog_payload,
                "sku": item.sku or line.get("sku"),
                "candidate_count": 0,
                "needs_operator_review": False,
                "resolved_by": "operator",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        resolved_lines.append(
            {
                "line_id": line.get("line_id"),
                "source_name": _assisted_line_label(line),
                "catalog_item_id": item.id,
                "sku": item.sku,
                "name": item.nombre,
            }
        )

    if not resolved_lines:
        return None, (
            {
                "error": "catalog_resolution_failed",
                "message": "No se pudo resolver ningun renglon con los datos enviados.",
                "errors": resolution_errors,
            },
            422,
        )

    unresolved_labels = [
        _assisted_line_label(line)
        for line in lines
        if line.get("needs_operator_review") is True
        or str(line.get("status") or "").strip().lower() in {"needs_catalog_resolution", "needs_review", "unmatched"}
    ]
    unresolved_labels = [label for label in unresolved_labels if label]
    detected = len(lines)
    matched = detected - len(unresolved_labels)
    unmatched = len(unresolved_labels)
    needs_review = bool(unmatched)
    contact = _admin_dict(metadata.get("contact"))
    has_contact = bool(contact.get("phone") or contact.get("telefono") or contact.get("email") or contact.get("name") or contact.get("nombre"))

    draft["lines"] = lines
    draft["summary"] = {
        **_admin_dict(draft.get("summary")),
        "detected": detected,
        "matched": matched,
        "unmatched": unmatched,
        "needs_operator_review": needs_review or not has_contact,
        "confirmation_status": "ready_for_customer_confirmation" if not needs_review and has_contact else "operator_review_required",
    }
    draft["customer_confirmation"] = _customer_confirmation_from_resolved_draft(
        detected=detected,
        matched=matched,
        unmatched=unmatched,
        has_contact=has_contact,
        needs_review=needs_review,
    )
    draft["recommended_next_step"] = "confirmar_pedido" if not needs_review and has_contact else "resolver_items_y_responder"

    metadata["crm_order_draft"] = draft
    metadata["crm_handoff"] = {**crm_handoff, "draft_order": draft}
    metadata["unmatched_items"] = unresolved_labels
    metadata["catalog_candidates"] = [
        group
        for group in _admin_list(metadata.get("catalog_candidates"))
        if str(group.get("item") or _assisted_line_label(_admin_dict(group.get("row"))) or "").strip() in unresolved_labels
    ]
    metadata["match_summary"] = {
        **_admin_dict(metadata.get("match_summary")),
        "matched": matched,
        "unmatched": unmatched,
        "detected": detected,
        "needs_operator_review": needs_review or not has_contact,
    }
    metadata["crm_state"] = "ready_for_confirmation" if not needs_review and has_contact else "pending_operator_review"
    metadata["catalog_resolution"] = {
        "contract_version": "marketplace.catalog_resolution.v1",
        "resolved_lines": resolved_lines,
        "errors": resolution_errors,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }

    operator_pack = dict(_admin_dict(metadata.get("operator_pack")))
    operator_pack["needs_human_review"] = needs_review or not has_contact
    operator_pack["priority"] = "normal" if not needs_review and has_contact else operator_pack.get("priority") or "high"
    operator_pack["recommended_next_step"] = draft["recommended_next_step"]
    metadata["operator_pack"] = operator_pack

    intake = dict(_admin_dict(metadata.get("intake_experience")))
    if intake:
        intake["needs_operator_review"] = needs_review or not has_contact
        metadata["intake_experience"] = intake

    record.metadata_payload = metadata
    flag_modified(record, "metadata_payload")
    if isinstance(record.items, list) and record.items:
        first_item = dict(record.items[0]) if isinstance(record.items[0], dict) else {}
        first_item.update(
            {
                "crm_order_draft": draft,
                "crm_handoff": metadata["crm_handoff"],
                "unmatched_items": unresolved_labels,
                "catalog_candidates": metadata["catalog_candidates"],
                "match_summary": metadata["match_summary"],
                "crm_state": metadata["crm_state"],
                "catalog_resolution": metadata["catalog_resolution"],
            }
        )
        record.items = [first_item, *record.items[1:]]
        flag_modified(record, "items")

    return metadata["catalog_resolution"], None


@admin_tenant_bp.route('/api/admin/tenants/<slug>/orders/<path:order_id>', methods=['GET', 'PATCH'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def tenant_order_detail(current_user, slug, order_id):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    record = _resolve_tenant_order_record(tenant, order_id)
    if not record:
        return jsonify({"error": "Order not found"}), 404

    if request.method == 'PATCH':
        payload = request.get_json(silent=True) or {}
        catalog_resolutions = payload.get("catalog_resolutions")
        if catalog_resolutions is not None:
            _, resolution_error = _apply_assisted_catalog_resolutions(record, tenant, catalog_resolutions)
            if resolution_error:
                error_payload, status_code = resolution_error
                return jsonify(error_payload), status_code
        status = payload.get("status")
        if status is not None:
            if _should_materialize_assisted_order(record, tenant, status):
                inventory_validation = _assisted_order_inventory_validation(
                    _admin_dict(getattr(record, "metadata_payload", None)),
                    tenant,
                )
                blockers = _assisted_order_confirmation_blockers(
                    getattr(record, "metadata_payload", None),
                    inventory_validation=inventory_validation,
                )
                if blockers:
                    return jsonify(
                        {
                            "error": "assisted_order_needs_review",
                            "message": (
                                "No se puede crear el pedido operativo hasta resolver catalogo, "
                                "inventario, datos bloqueantes o revision humana pendiente."
                            ),
                            "blocking_reasons": blockers,
                            "inventory_validation": inventory_validation,
                            "status": getattr(record, "estado", None),
                        }
                    ), 409
            _apply_tenant_order_status(record, status)
            if _should_materialize_assisted_order(record, tenant, status):
                from services.pedido_service import PedidoService

                materialized = PedidoService().create_from_conversational(record)
                if not materialized:
                    db.session.rollback()
                    return jsonify(
                        {
                            "error": "materialization_failed",
                            "message": (
                                "No se pudo confirmar el pedido operativo. "
                                "Revisa contacto, catalogo o datos minimos antes de confirmar."
                            ),
                        }
                    ), 422
                db.session.refresh(record)
            else:
                db.session.commit()
        else:
            db.session.commit()

    return jsonify(_serialize_tenant_order(record, tenant))



def _ticket_belongs_to_tenant(ticket, tenant: TenantProfile) -> bool:
    if not ticket or not tenant:
        return False
    if isinstance(ticket, MunicipioTicket):
        return municipio_ticket_belongs_to_tenant(ticket, tenant)
    if getattr(ticket, 'tenant_id', None) and ticket.tenant_id == tenant.id:
        return True
    owner = tenant.municipio or tenant.pyme
    owner_id = getattr(owner, 'id', None)
    if owner_id and getattr(ticket, 'municipio_id', None) == owner_id:
        return True
    if owner_id and getattr(ticket, 'pyme_id', None) == owner_id:
        return True
    return False


def _ticket_details(ticket) -> dict:
    import json
    details = getattr(ticket, 'detalles', None)
    if isinstance(details, dict):
        return dict(details)
    if isinstance(details, str) and details.strip():
        try:
            parsed = json.loads(details)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _save_ticket_details(ticket, details: dict):
    import json
    ticket.detalles = json.dumps(details, ensure_ascii=False)


def _lead_details(ticket) -> dict:
    if isinstance(ticket, TenantTicket):
        details = getattr(ticket, "datos_extra", None)
        return dict(details) if isinstance(details, dict) else {}
    return _ticket_details(ticket)


def _save_lead_details(ticket, details: dict):
    if isinstance(ticket, TenantTicket):
        ticket.datos_extra = dict(details or {})
        flag_modified(ticket, "datos_extra")
        return
    _save_ticket_details(ticket, details)


def _resolve_tenant_lead_ticket(ticket_type: str, ticket_id: int):
    normalized = str(ticket_type or "").strip().lower()
    if normalized == "municipio":
        return MunicipioTicket.query.get(ticket_id)
    if normalized == "pyme":
        return PymeTicket.query.get(ticket_id)
    if normalized == "tenant":
        return TenantTicket.query.get(ticket_id)
    return None


def _is_tenant_ticket_lead(ticket: TenantTicket) -> bool:
    details = _lead_details(ticket)
    category = str(getattr(ticket, "categoria", None) or "").strip().lower()
    if category in {"lead_capture", "marketplace_assisted_order", "commerce_assisted_intake"}:
        return True

    metadata_markers = {
        str(details.get("type") or "").strip().lower(),
        str(details.get("contract_version") or "").strip().lower(),
        str(details.get("intake_ticket_contract_version") or "").strip().lower(),
        str(details.get("assisted_request_contract_version") or "").strip().lower(),
    }
    return (
        "commerce_assisted_intake" in metadata_markers
        or "marketplace.commerce_intake_ticket.v1" in metadata_markers
        or "marketplace.assisted_request.v1" in metadata_markers
        or bool(details.get("pedido_conversacional_id"))
        or bool(details.get("conversational_order_id"))
    )


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
            continue
        if value not in ("", [], {}):
            return value
    return None


def _extract_lead_contact(ticket, details: dict) -> dict:
    contact = _admin_dict(details.get("contact"))
    customer_profile = _admin_dict(details.get("customer_profile"))
    assisted_request = _admin_dict(details.get("assisted_request"))
    crm_review_card = _admin_dict(details.get("crm_review_card"))
    review_contact = _admin_dict(crm_review_card.get("contact"))
    assisted_contact = _admin_dict(assisted_request.get("contact"))
    public_follow_up = _admin_dict(assisted_request.get("public_follow_up"))
    follow_up_contact = _admin_dict(public_follow_up.get("contact"))

    return {
        "nombre": _first_non_empty(
            getattr(ticket, "nombre_vecino", None),
            getattr(ticket, "nombre_cliente", None),
            details.get("contact_name"),
            details.get("nombre"),
            details.get("name"),
            contact.get("name"),
            contact.get("nombre"),
            customer_profile.get("name"),
            customer_profile.get("nombre"),
            assisted_contact.get("name"),
            assisted_contact.get("nombre"),
            review_contact.get("name"),
            follow_up_contact.get("name"),
        ),
        "telefono": _first_non_empty(
            getattr(ticket, "telefono_vecino", None),
            getattr(ticket, "telefono_cliente", None),
            details.get("contact_phone"),
            details.get("telefono"),
            details.get("phone"),
            contact.get("phone"),
            contact.get("telefono"),
            customer_profile.get("phone"),
            customer_profile.get("telefono"),
            assisted_contact.get("phone"),
            assisted_contact.get("telefono"),
            review_contact.get("phone"),
            follow_up_contact.get("phone"),
        ),
        "email": _first_non_empty(
            getattr(ticket, "email_vecino", None),
            getattr(ticket, "email_cliente", None),
            details.get("contact_email"),
            details.get("email"),
            contact.get("email"),
            customer_profile.get("email"),
            assisted_contact.get("email"),
            review_contact.get("email"),
            follow_up_contact.get("email"),
        ),
    }


def _lead_last_seen(ticket):
    return (
        getattr(ticket, "ultima_actividad", None)
        or getattr(ticket, "fecha", None)
        or getattr(ticket, "updated_at", None)
        or getattr(ticket, "created_at", None)
    )


def _tenant_lead_order_id(details: dict):
    linked_record = _admin_dict(details.get("linked_record"))
    crm_review_card = _admin_dict(details.get("crm_review_card"))
    assisted_request = _admin_dict(details.get("assisted_request"))
    crm_handoff = _admin_dict(assisted_request.get("crm_handoff"))
    candidates = (
        details.get("pedido_conversacional_id"),
        details.get("conversational_order_id"),
        linked_record.get("pedido_conversacional_id"),
        linked_record.get("id") if str(linked_record.get("source_model") or "").lower() == "pedidoconversacional" else None,
        crm_review_card.get("source_id"),
        crm_handoff.get("pedido_conversacional_id"),
    )
    for candidate in candidates:
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _serialize_tenant_lead_item(ticket, tenant: TenantProfile, now) -> dict:
    details = _lead_details(ticket)
    stage = str(details.get("lead_stage") or getattr(ticket, "estado", None) or "nuevo").strip().lower()
    last_seen = _lead_last_seen(ticket)
    last_dt = last_seen if (last_seen and last_seen.tzinfo) else (last_seen.replace(tzinfo=timezone.utc) if last_seen else None)
    sla_breached = bool(last_dt and (now - last_dt).total_seconds() > 1800 and stage not in {"ganado", "perdido"})
    contact = _extract_lead_contact(ticket, details)
    order_id = _tenant_lead_order_id(details)
    source_metadata = {
        "type": details.get("type"),
        "contract_version": details.get("contract_version") or details.get("intake_ticket_contract_version"),
        "assisted_request_contract_version": details.get("assisted_request_contract_version"),
        "pedido_conversacional_id": order_id,
        "needs_operator_review": details.get("needs_operator_review"),
    }
    return {
        "ticket_type": "tenant",
        "source_model": "TenantTicket",
        "ticket_id": ticket.id,
        "source_id": ticket.id,
        "nro": details.get("nro") or details.get("crm_id") or f"T-{ticket.id}",
        "nombre": contact.get("nombre"),
        "telefono": contact.get("telefono"),
        "email": contact.get("email"),
        "categoria": getattr(ticket, "categoria", None),
        "stage": stage,
        "status": getattr(ticket, "estado", None),
        "origen": getattr(ticket, "origen", None),
        "sla_breached": sla_breached,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "detail_endpoint": f"/api/v2/tickets/{ticket.id}",
        "order_endpoint": f"/api/admin/tenants/{tenant.slug}/orders/conversational:{order_id}" if order_id else None,
        "source_metadata": {k: v for k, v in source_metadata.items() if v not in (None, "", [], {})},
    }


@admin_tenant_bp.route('/api/admin/tenants/<slug>/leads', methods=['GET'])
@token_requerido
@require_tenant
def tenant_list_leads(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    limit = max(1, min(int(request.args.get('limit', 100) or 100), 250))
    stage_filter = str(request.args.get('stage') or '').strip().lower()

    m_query = scoped_municipio_ticket_query(tenant)
    p_query = PymeTicket.query.filter_by(tenant_id=tenant.id)
    t_query = TenantTicket.query.filter_by(tenant_id=tenant.id)
    rows = []
    for t in m_query.order_by(MunicipioTicket.ultima_actividad.desc()).limit(limit).all():
        rows.append(('municipio', t))
    for t in p_query.order_by(PymeTicket.fecha.desc()).limit(limit).all():
        rows.append(('pyme', t))
    for t in t_query.order_by(TenantTicket.updated_at.desc()).limit(limit).all():
        if _is_tenant_ticket_lead(t):
            rows.append(('tenant', t))

    items = []
    now = datetime.now(timezone.utc)
    for t_type, ticket in rows:
        if t_type == "tenant":
            item = _serialize_tenant_lead_item(ticket, tenant, now)
            if stage_filter and stage_filter != item.get("stage"):
                continue
            items.append(item)
            continue

        details = _lead_details(ticket)
        stage = str(details.get('lead_stage') or ticket.estado or 'nuevo').lower()
        if stage_filter and stage_filter != stage:
            continue
        last_seen = _lead_last_seen(ticket)
        last_dt = last_seen if (last_seen and last_seen.tzinfo) else (last_seen.replace(tzinfo=timezone.utc) if last_seen else None)
        sla_breached = bool(last_dt and (now - last_dt).total_seconds() > 1800 and stage not in {'ganado', 'perdido'})
        contact = _extract_lead_contact(ticket, details)
        items.append({
            'ticket_type': t_type,
            'source_model': 'MunicipioTicket' if t_type == 'municipio' else 'PymeTicket',
            'ticket_id': ticket.id,
            'source_id': ticket.id,
            'nro': getattr(ticket, 'nro_ticket', None) or getattr(ticket, 'nro_pedido', None),
            'nombre': contact.get("nombre"),
            'telefono': contact.get("telefono"),
            'email': contact.get("email"),
            'categoria': getattr(ticket, 'categoria', None),
            'stage': stage,
            'status': ticket.estado,
            'sla_breached': sla_breached,
            'last_seen': last_seen.isoformat() if last_seen else None,
        })

    items.sort(key=lambda it: ((it.get('sla_breached') is True), it.get('last_seen') or ''), reverse=True)
    return jsonify({'tenant_slug': tenant.slug, 'total': len(items), 'items': items[:limit]})


@admin_tenant_bp.route('/api/admin/tenants/<slug>/leads/<ticket_type>/<int:ticket_id>/stage', methods=['PATCH'])
@token_requerido
@require_tenant
def tenant_update_lead_stage(current_user, slug, ticket_type: str, ticket_id: int):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json(silent=True) or {}
    stage = str(payload.get('stage') or '').strip().lower()
    note = str(payload.get('note') or '').strip()
    allowed = {"nuevo", "contactado", "calificado", "demo_agendada", "propuesta_enviada", "ganado", "perdido"}
    if stage not in allowed:
        return jsonify({'error': 'stage inválido'}), 400

    ticket = _resolve_tenant_lead_ticket(ticket_type, ticket_id)
    if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
        return jsonify({'error': 'Lead no encontrado'}), 404

    details = _lead_details(ticket)
    timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []
    prev = details.get('lead_stage') or ticket.estado or 'nuevo'
    details['lead_stage'] = stage
    timeline.append({
        'at': datetime.now(timezone.utc).isoformat(),
        'by_user_id': current_user.id,
        'event': 'tenant_stage_update',
        'from': prev,
        'to': stage,
        'note': note or None,
    })
    details['lead_timeline'] = timeline[-100:]
    _save_lead_details(ticket, details)

    status_map = {
        'nuevo': 'nuevo', 'contactado': 'en_proceso', 'calificado': 'en_proceso',
        'demo_agendada': 'pendiente', 'propuesta_enviada': 'pendiente',
        'ganado': 'cerrado', 'perdido': 'cancelado',
    }
    ticket.estado = status_map.get(stage, 'en_proceso')
    if hasattr(ticket, 'ultima_actividad'):
        ticket.ultima_actividad = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'ok': True, 'ticket_id': ticket.id, 'ticket_type': ticket_type, 'lead_stage': stage, 'status': ticket.estado})


@admin_tenant_bp.route('/api/admin/tenants/<slug>/leads/bulk-stage', methods=['PATCH'])
@token_requerido
@require_tenant
def tenant_bulk_stage(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json(silent=True) or {}
    stage = str(payload.get('stage') or '').strip().lower()
    updates = payload.get('updates') if isinstance(payload.get('updates'), list) else []
    allowed = {"nuevo", "contactado", "calificado", "demo_agendada", "propuesta_enviada", "ganado", "perdido"}
    if stage not in allowed:
        return jsonify({'error': 'stage inválido'}), 400
    if not updates:
        return jsonify({'error': 'updates requerido'}), 400

    status_map = {
        'nuevo': 'nuevo', 'contactado': 'en_proceso', 'calificado': 'en_proceso',
        'demo_agendada': 'pendiente', 'propuesta_enviada': 'pendiente',
        'ganado': 'cerrado', 'perdido': 'cancelado',
    }
    changed = 0
    errors = []
    for row in updates[:200]:
        if not isinstance(row, dict):
            continue
        ticket_type = str(row.get('ticket_type') or '').strip().lower()
        note = str(row.get('note') or '').strip()
        try:
            ticket_id = int(row.get('ticket_id'))
        except Exception:
            errors.append({'ticket_type': ticket_type, 'ticket_id': row.get('ticket_id'), 'error': 'ticket_id inválido'})
            continue

        ticket = _resolve_tenant_lead_ticket(ticket_type, ticket_id)
        if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
            errors.append({'ticket_type': ticket_type, 'ticket_id': ticket_id, 'error': 'no encontrado'})
            continue

        details = _lead_details(ticket)
        timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []
        prev = details.get('lead_stage') or ticket.estado or 'nuevo'
        details['lead_stage'] = stage
        timeline.append({
            'at': datetime.now(timezone.utc).isoformat(),
            'by_user_id': current_user.id,
            'event': 'tenant_bulk_stage_update',
            'from': prev,
            'to': stage,
            'note': note or None,
        })
        details['lead_timeline'] = timeline[-100:]
        _save_lead_details(ticket, details)
        ticket.estado = status_map.get(stage, 'en_proceso')
        if hasattr(ticket, 'ultima_actividad'):
            ticket.ultima_actividad = datetime.now(timezone.utc)
        changed += 1

    db.session.commit()
    return jsonify({'ok': True, 'changed': changed, 'errors': errors})


@admin_tenant_bp.route('/api/admin/tenants/<slug>/leads/<ticket_type>/<int:ticket_id>/timeline', methods=['GET', 'POST'])
@token_requerido
@require_tenant
def tenant_lead_timeline(current_user, slug, ticket_type: str, ticket_id: int):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    ticket = _resolve_tenant_lead_ticket(ticket_type, ticket_id)
    if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
        return jsonify({'error': 'Lead no encontrado'}), 404

    details = _lead_details(ticket)
    timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []

    if request.method == 'GET':
        return jsonify({'ticket_id': ticket.id, 'ticket_type': ticket_type, 'timeline': timeline[-100:]})

    payload = request.get_json(silent=True) or {}
    note = str(payload.get('note') or '').strip()
    if not note:
        return jsonify({'error': 'note requerido'}), 400

    timeline.append({
        'at': datetime.now(timezone.utc).isoformat(),
        'by_user_id': current_user.id,
        'event': 'tenant_note',
        'note': note[:1000],
    })
    details['lead_timeline'] = timeline[-100:]
    _save_lead_details(ticket, details)
    if hasattr(ticket, 'ultima_actividad'):
        ticket.ultima_actividad = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'ok': True, 'ticket_id': ticket.id, 'ticket_type': ticket_type, 'timeline': details['lead_timeline']})

from flask import Blueprint, request, jsonify, g, current_app
import requests
from sqlalchemy import func
from datetime import datetime, timezone, timedelta

from utils.auth_helpers import token_requerido
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
)
from routes.catalogo import _formatear_producto
from routes.carrito import _product_query_for_tenant
from services.commerce_unified import dedupe_unified_orders, serialize_unified_order
from services.common_utils import parse_precio_flexible
from services.catalog_seed import ensure_seed_catalog
from services.embedding_service import embed_textos_llm
from services.pymes import tiene_archivo_catalogo
from services.qdrant_service import index_catalog_item
from services.tenant_factory import create_tenant_from_template, assign_number_to_tenant
from services.tenant_resolver import apply_tenant_alias
from services.live_chat_schedule import build_live_chat_status, build_schedule_from_config
from services.operational_scoring import build_ticket_priority_score
from services.ticket_realtime_state import build_ticket_collaboration_state

admin_tenant_bp = Blueprint('admin_tenant_bp', __name__)


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

    if current_user.rol in ("platform_admin", "super_admin"):
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


def _build_tenant_dashboard_bundle_payload(
    tenant: TenantProfile,
    *,
    leads_limit: int = 100,
    surveys_limit: int = 20,
    unread_limit: int = 30,
    since_minutes: int = 1440,
) -> dict:
    now = datetime.now(timezone.utc)
    cutoff_unread = now - timedelta(minutes=since_minutes)

    lead_rows = []
    for ticket in MunicipioTicket.query.filter_by(tenant_id=tenant.id).order_by(MunicipioTicket.ultima_actividad.desc()).all():
        lead_rows.append(("municipio", ticket))
    for ticket in PymeTicket.query.filter_by(tenant_id=tenant.id).order_by(PymeTicket.fecha.desc()).all():
        lead_rows.append(("pyme", ticket))

    lead_items = []
    by_stage = {}
    sla_breached = 0
    total_active_viewers = 0
    total_unread_viewers = 0

    for ticket_type, ticket in lead_rows:
        details = _ticket_details(ticket)
        stage = str(details.get('lead_stage') or ticket.estado or 'nuevo').lower()
        by_stage[stage] = by_stage.get(stage, 0) + 1
        last_seen = getattr(ticket, 'ultima_actividad', None) or ticket.fecha
        last_dt = last_seen if (last_seen and last_seen.tzinfo) else (last_seen.replace(tzinfo=timezone.utc) if last_seen else None)
        ticket_sla = bool(last_dt and (now - last_dt).total_seconds() > 1800 and stage not in {'ganado', 'perdido'})
        collaboration_state = build_ticket_collaboration_state(ticket_type=ticket_type, ticket_id=ticket.id)
        priority_meta = build_ticket_priority_score(
            sla_breached_flag=ticket_sla,
            collaboration_state=collaboration_state,
            stage=stage,
        )
        priority_score = priority_meta["score"]
        if ticket_sla:
            sla_breached += 1
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

    survey_rows = EncEncuesta.query.filter_by(tenant_id=tenant.id).order_by(EncEncuesta.updated_at.desc()).limit(surveys_limit).all()
    survey_items = []
    total_responses = 0
    for survey in survey_rows:
        responses_count = EncRespuesta.query.filter_by(encuesta_id=survey.id).count()
        total_responses += responses_count
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
    muni_unread = (
        db.session.query(TicketComentario.municipio_ticket_id, func.count(TicketComentario.id), func.max(TicketComentario.fecha))
        .join(MunicipioTicket, MunicipioTicket.id == TicketComentario.municipio_ticket_id)
        .filter(
            MunicipioTicket.tenant_id == tenant.id,
            TicketComentario.es_admin.is_(False),
            TicketComentario.fecha >= cutoff_unread,
        )
        .group_by(TicketComentario.municipio_ticket_id)
        .all()
    )
    for ticket_id, unread_count, last_at in muni_unread:
        collaboration_state = build_ticket_collaboration_state(ticket_type='municipio', ticket_id=ticket_id)
        unread_items.append({
            'ticket_type': 'municipio',
            'ticket_id': ticket_id,
            'unread_count': int(unread_count or 0),
            'last_message_at': last_at.isoformat() if last_at else None,
            'collaboration_state': collaboration_state,
        })

    pyme_unread = (
        db.session.query(TicketComentario.pyme_ticket_id, func.count(TicketComentario.id), func.max(TicketComentario.fecha))
        .join(PymeTicket, PymeTicket.id == TicketComentario.pyme_ticket_id)
        .filter(
            PymeTicket.tenant_id == tenant.id,
            TicketComentario.es_admin.is_(False),
            TicketComentario.fecha >= cutoff_unread,
        )
        .group_by(TicketComentario.pyme_ticket_id)
        .all()
    )
    for ticket_id, unread_count, last_at in pyme_unread:
        collaboration_state = build_ticket_collaboration_state(ticket_type='pyme', ticket_id=ticket_id)
        unread_items.append({
            'ticket_type': 'pyme',
            'ticket_id': ticket_id,
            'unread_count': int(unread_count or 0),
            'last_message_at': last_at.isoformat() if last_at else None,
            'collaboration_state': collaboration_state,
        })

    unread_items.sort(key=lambda item: item.get('last_message_at') or '', reverse=True)

    def _employee_collaboration_metrics(employee_id: int) -> dict:
        rows = TicketRealtimeState.query.filter_by(viewer_user_id=employee_id).all()
        active_ticket_views: set[tuple[str, int]] = set()
        idle_ticket_views: set[tuple[str, int]] = set()
        unread_ticket_views: set[tuple[str, int]] = set()
        for row in rows:
            collaboration_state = build_ticket_collaboration_state(ticket_type=row.ticket_type, ticket_id=row.ticket_id)
            if collaboration_state.get('active_viewers_count', 0):
                active_ticket_views.add((row.ticket_type, row.ticket_id))
            if collaboration_state.get('idle_viewers_count', 0):
                idle_ticket_views.add((row.ticket_type, row.ticket_id))
            if collaboration_state.get('unread_viewer_count', 0):
                unread_ticket_views.add((row.ticket_type, row.ticket_id))
        return {
            'active_ticket_views': len(active_ticket_views),
            'idle_ticket_views': len(idle_ticket_views),
            'unread_ticket_views': len(unread_ticket_views),
        }

    workload_items = []
    for emp in User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all():
        collaboration_metrics = _employee_collaboration_metrics(emp.id)
        workload_items.append({
            'employee_id': emp.id,
            'name': emp.name,
            'email': emp.email,
            'workload_open_tickets': _employee_open_workload(tenant.id, emp.id),
            'scope': _employee_scope(emp),
            **collaboration_metrics,
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
            'total_leads': len(lead_items),
            'sla_breached': sla_breached,
            'total_surveys': len(survey_items),
            'total_survey_responses': total_responses,
            'tickets_with_unread': len(unread_items),
            'employees': len(workload_items),
            'active_viewers': total_active_viewers,
            'unread_viewers': total_unread_viewers,
        },
        'leads': {
            'total': len(lead_items),
            'by_stage': by_stage,
            'items': lead_items[:leads_limit],
        },
        'surveys': {
            'total_surveys': len(survey_items),
            'total_responses': total_responses,
            'items': survey_items,
        },
        'unread': {
            'since_minutes': since_minutes,
            'total_tickets_with_unread': len(unread_items),
            'items': unread_items[:unread_limit],
        },
        'team': {
            'items': workload_items,
        },
        'recommended_actions': recommended_actions,
    }


def _build_tenant_heatmap_summary_payload(tenant: TenantProfile, *, limit_points: int = 1500) -> dict:
    rows = []
    for ticket in MunicipioTicket.query.filter_by(tenant_id=tenant.id).all():
        rows.append({
            'ticket_type': 'municipio',
            'ticket_id': ticket.id,
            'categoria': (ticket.categoria or 'sin_categoria').strip().lower(),
            'zona': (ticket.distrito or 'sin_zona').strip().lower(),
            'lat': ticket.latitud,
            'lon': ticket.longitud,
            'status': ticket.estado,
        })
    for ticket in PymeTicket.query.filter_by(tenant_id=tenant.id).all():
        rows.append({
            'ticket_type': 'pyme',
            'ticket_id': ticket.id,
            'categoria': (ticket.categoria or 'sin_categoria').strip().lower(),
            'zona': (getattr(ticket, 'direccion', None) or 'sin_zona').strip().lower(),
            'lat': ticket.latitud,
            'lon': ticket.longitud,
            'status': ticket.estado,
        })

    by_categoria = {}
    by_zona = {}
    hotspots = {}
    points = []
    for row in rows:
        by_categoria[row['categoria']] = by_categoria.get(row['categoria'], 0) + 1
        by_zona[row['zona']] = by_zona.get(row['zona'], 0) + 1
        hotspot_key = f"{row['categoria']}::{row['zona']}"
        hotspots[hotspot_key] = hotspots.get(hotspot_key, 0) + 1
        if row['lat'] is not None and row['lon'] is not None:
            points.append({
                'ticket_type': row['ticket_type'],
                'ticket_id': row['ticket_id'],
                'lat': row['lat'],
                'lon': row['lon'],
                'categoria': row['categoria'],
                'zona': row['zona'],
                'status': row['status'],
                'weight': 1,
            })

    top_categories = sorted(by_categoria.items(), key=lambda item: item[1], reverse=True)[:10]
    top_zones = sorted(by_zona.items(), key=lambda item: item[1], reverse=True)[:10]
    top_hotspots = sorted(hotspots.items(), key=lambda item: item[1], reverse=True)[:10]

    return {
        'tenant_id': tenant.id,
        'tenant_slug': tenant.slug,
        'total': len(rows),
        'top_categories': [{'categoria': key, 'count': value} for key, value in top_categories],
        'top_zones': [{'zona': key, 'count': value} for key, value in top_zones],
        'hotspots': [
            {
                'categoria': key.split('::', 1)[0],
                'zona': key.split('::', 1)[1],
                'count': value,
            }
            for key, value in top_hotspots
        ],
        'heatmap_points': points[:limit_points],
    }


def _build_employee_coverage_payload(tenant: TenantProfile) -> dict:
    category_map = {}
    zone_map = {}
    permission_map = {}
    employees = []

    for emp in User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all():
        scope = _employee_scope(emp)
        employees.append({
            'employee_id': emp.id,
            'name': emp.name,
            'email': emp.email,
            'scope': scope,
        })
        for categoria in scope.get('categorias', []):
            category_map.setdefault(categoria, []).append({'employee_id': emp.id, 'name': emp.name})
        for zona in scope.get('zonas', []):
            zone_map.setdefault(zona, []).append({'employee_id': emp.id, 'name': emp.name})
        for permiso in scope.get('permisos', []):
            permission_map.setdefault(permiso, []).append({'employee_id': emp.id, 'name': emp.name})

    return {
        'tenant_id': tenant.id,
        'tenant_slug': tenant.slug,
        'employees': employees,
        'coverage': {
            'categorias': category_map,
            'zonas': zone_map,
            'permisos': permission_map,
        },
    }


def _plan_allows_integrations(tenant: TenantProfile) -> bool:
    plan_key = (tenant.plan or "").strip().lower()
    return plan_key in ("pro", "full")


def _resolve_admin_tenant(current_user: User, slug: str) -> TenantProfile | None:
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if tenant and _is_authorized_for_tenant(current_user, tenant):
        return tenant

    tenant_hint = getattr(g, "tenant_profile", None)
    if tenant_hint and _is_authorized_for_tenant(current_user, tenant_hint):
        return tenant_hint

    tenant_from_user = (
        getattr(current_user, "tenant", None)
        or getattr(current_user, "tenant_profile", None)
        or getattr(current_user, "tenant_profile_municipio", None)
        or getattr(current_user, "tenant_profile_pyme", None)
    )
    if tenant_from_user and _is_authorized_for_tenant(current_user, tenant_from_user):
        return tenant_from_user

    return tenant



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

    return jsonify({
        "tenant_slug": tenant.slug,
        "status": "published" if has_pdf else "missing",
        "view_url": f"{base_web}/{tenant.slug}/catalogo",
        "download_url": f"{base_api}/api/public/tenants/{tenant.slug}/catalog/download?format=pdf",
        "download_url_json": f"{base_api}/api/public/tenants/{tenant.slug}/catalog/download?format=json",
        "has_pdf": has_pdf,
    })


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

    if "cantidad" in payload or "stock" in payload:
        cantidad_raw = payload.get("cantidad")
        if cantidad_raw is None:
            cantidad_raw = payload.get("stock")
        item.cantidad = str(cantidad_raw) if cantidad_raw is not None else item.cantidad

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

    return jsonify({"item": _formatear_producto(item)})

# --- Tenant Management ---

@admin_tenant_bp.route('/api/admin/tenants', methods=['POST'])
def create_tenant():
    """Crea un nuevo tenant desde una plantilla. Actúa como registro público."""
    data = request.json or {}
    try:
        tenant = create_tenant_from_template(
            nombre=data.get('nombre'),
            slug=data.get('slug'),
            tipo=data.get('tipo'),
            template_key=data.get('template_key', 'municipio_default'),
            plan=data.get('plan', 'full'),
            auto_assign_whatsapp_number=data.get('auto_assign_whatsapp_number', False),
            owner_email=data.get('owner_email'),
            owner_password=data.get('owner_password')
        )

        widget_token = None
        if tenant.configuracion and 'widget_tokens' in tenant.configuracion:
             widget_token = tenant.configuracion['widget_tokens'][0]

        return jsonify({
            "slug": tenant.slug,
            "widget_token": widget_token,
            "id": tenant.id
        }), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # Log error
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
            "integrations": _plan_allows_integrations(tenant),
            "widget_customization": _plan_allows_integrations(tenant)
        }
    }
    return jsonify(response)


@admin_tenant_bp.route('/api/admin/tenants/<slug>/catalog/items', methods=['GET', 'OPTIONS'])
@admin_tenant_bp.route('/admin/tenants/<slug>/catalog/items', methods=['GET', 'OPTIONS'])
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
        prod["price_numeric"] = float(item.precio_monetario) if item.precio_monetario is not None else None
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

    return jsonify(productos)

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
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
         return jsonify({'error': 'Unauthorized'}), 403

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
    return {
        'categorias': [str(c).strip() for c in categorias if str(c).strip()][:30],
        'zonas': [str(z).strip() for z in zonas if str(z).strip()][:30],
        'permisos': [str(p).strip() for p in permisos if str(p).strip()][:30],
    }


def _set_employee_scope(emp: User, *, categorias: list[str], zonas: list[str], permisos: list[str]) -> None:
    data = emp.accesibilidad if isinstance(emp.accesibilidad, dict) else {}
    data['employee_scope'] = {
        'categorias': categorias,
        'zonas': zonas,
        'permisos': permisos,
    }
    emp.accesibilidad = data


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


def _employee_open_workload(tenant_id: int, employee_id: int) -> int:
    active_states = {'nuevo', 'pendiente', 'en_proceso'}
    m_count = MunicipioTicket.query.filter(
        MunicipioTicket.tenant_id == tenant_id,
        MunicipioTicket.asignado_a_id == employee_id,
        MunicipioTicket.estado.in_(list(active_states)),
    ).count()
    p_count = PymeTicket.query.filter(
        PymeTicket.tenant_id == tenant_id,
        PymeTicket.asignado_a_id == employee_id,
        PymeTicket.estado.in_(list(active_states)),
    ).count()
    return int(m_count + p_count)


def _scope_has_permission(scope: dict, permission: str) -> bool:
    permisos = [str(p).strip().lower() for p in (scope.get('permisos') or []) if str(p).strip()]
    return permission.lower() in permisos

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
    email = data.get('email')
    name = data.get('name')
    password = data.get('password')

    if not email or not password:
        return jsonify({'error': 'Missing email or password'}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'User already exists'}), 400

    user = User(
        email=email,
        name=name or email.split('@')[0],
        tenant_id=tenant.id,
        es_empleado=True,
        rol='empleado'
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    # Asignar roles
    roles = data.get('roles', ['empleado'])
    for role_name in roles:
        role = Role.query.filter_by(name=role_name).first()
        if not role:
            # Auto-create basic roles if missing
            role = Role(name=role_name, description=f'Rol {role_name}')
            db.session.add(role)
            db.session.commit()

        if not UserRole.query.filter_by(user_id=user.id, role_id=role.id, tenant_id=tenant.id).first():
            ur = UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.id)
            db.session.add(ur)

    # Asignar categorías
    category_ids = data.get('categories', [])
    if category_ids:
        valid_cats = CategoriaTicket.query.filter(
            CategoriaTicket.id.in_(category_ids),
            CategoriaTicket.tenant_id == tenant.id
        ).all()
        user.categorias_ticket = valid_cats

    scope_raw = data.get('scope') if isinstance(data.get('scope'), dict) else {}
    _set_employee_scope(
        user,
        categorias=[str(v).strip() for v in (scope_raw.get('categorias') or []) if str(v).strip()][:30],
        zonas=[str(v).strip() for v in (scope_raw.get('zonas') or []) if str(v).strip()][:30],
        permisos=[str(v).strip() for v in (scope_raw.get('permisos') or []) if str(v).strip()][:30],
    )

    db.session.commit()

    assigned_roles = []
    for role_name in roles:
        if str(role_name).strip():
            assigned_roles.append(str(role_name).strip())

    return jsonify({
        'message': 'Employee created',
        'id': user.id,
        'employee': {
            'id': user.id,
            'name': user.name,
            'email': user.email,
            'tenant_id': tenant.id,
            'roles': assigned_roles,
            'categories': [cat.id for cat in getattr(user, 'categorias_ticket', [])],
            'scope': _employee_scope(user),
        },
    }), 201

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
    if not user:
        return jsonify({'error': 'User not found'}), 404

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
    db.session.commit()

    return jsonify({'message': 'Categories updated', 'count': len(valid_cats)}), 200


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
    categorias = [str(v).strip() for v in (data.get('categorias') or []) if str(v).strip()][:30]
    zonas = [str(v).strip() for v in (data.get('zonas') or []) if str(v).strip()][:30]
    permisos = [str(v).strip() for v in (data.get('permisos') or []) if str(v).strip()][:30]

    _set_employee_scope(user, categorias=categorias, zonas=zonas, permisos=permisos)
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
@require_tenant
def tenant_surveys_overview(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    limit = max(1, min(int(request.args.get('limit', 50) or 50), 100))
    rows = EncEncuesta.query.filter_by(tenant_id=tenant.id).order_by(EncEncuesta.updated_at.desc()).limit(limit).all()

    items = []
    total_responses = 0
    for encuesta in rows:
        responses_count = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count()
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
            MunicipioTicket.tenant_id == tenant.id,
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
@require_tenant
def tenant_heatmap_summary(current_user, slug):
    tenant = _resolve_admin_tenant(current_user, slug)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    limit_points = max(100, min(int(request.args.get('limit_points', 1500) or 1500), 5000))
    payload = _build_tenant_heatmap_summary_payload(tenant, limit_points=limit_points)
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

    results = []
    for emp in employees:
        # Load roles using the dynamic relationship or query
        user_roles = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
        role_names = [ur.role.name for ur in user_roles if ur.role]

        results.append({
            "id": emp.id,
            "name": emp.name,
            "email": emp.email,
            "roles": role_names,
            "created_at": emp.fecha_creacion.isoformat() if emp.fecha_creacion else None,
            "scope": _employee_scope(emp),
        })

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
        return (
            jsonify(
                {
                    "error": "plan_required",
                    "message": "Integraciones disponibles para planes Pro/Full.",
                }
            ),
            403,
        )

    integrations = IntegrationAccount.query.filter_by(tenant_id=tenant.id).all()

    # Mock status for known types if missing
    known_types = ["MercadoLibre", "TiendaNube", "WhatsApp"]
    result = {}

    # Fill from DB
    for integ in integrations:
        result[integ.type] = {
            "type": integ.type,
            "connected": integ.status == 'active',
            "lastSync": integ.last_sync_at.isoformat() if integ.last_sync_at else None,
            "account": integ.metadata_payload.get('account_name') if integ.metadata_payload else None
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
        return (
            jsonify(
                {
                    "error": "plan_required",
                    "message": "Integraciones disponibles para planes Pro/Full.",
                }
            ),
            403,
        )

    base_url = current_app.config.get("PUBLIC_BASE_URL", "https://chatboc.ar").rstrip("/")

    # Uses Platform Credentials (configured in Render/Env) to generate the OAuth URL.
    # The Client (Tenant Admin) clicks this URL, logs in to their account, and authorizes "Chatboc".
    # We use the GENERIC callback URL defined in routes/integrations.py to allow a single app registration.
    # The tenant context is preserved via the 'state' parameter (tenant.id).

    if integration_type.lower() == 'tiendanube':
        client_id = current_app.config.get("TIENDANUBE_CLIENT_ID")
        if not client_id:
            # Fallback for development or incomplete config - don't crash with 503
            current_app.logger.warning("TIENDANUBE_CLIENT_ID not set. Integration unavailable.")
            # Si estamos en modo desarrollo o no hay config, devolvemos un mock o un error 200 con mensaje
            # Para evitar 422 que rompe el frontend, devolvemos un error manejable o un mensaje de demo
            return jsonify({
                "error": "platform_not_configured",
                "message": "Falta TIENDANUBE_CLIENT_ID en el servidor. Contacte al administrador.",
                "demo_mode": True
            }), 200 # Cambiamos a 200 para que el frontend pueda manejarlo sin excepción

        redirect_uri = f"{base_url}/api/integrations/tiendanube/callback"
        # TiendaNube typically doesn't support 'state' in all docs, but standard OAuth does.
        # We assume standard behavior or fallback to direct if needed.
        auth_url = f"https://www.tiendanube.com/apps/authorize?client_id={client_id}&redirect_uri={redirect_uri}&state={tenant.id}"
        return jsonify({"redirect_url": auth_url})

    elif integration_type.lower() == 'mercadolibre':
        client_id = current_app.config.get("ML_APP_ID")
        if not client_id:
             current_app.logger.warning("ML_APP_ID not set. Integration unavailable.")
             return jsonify({
                "error": "platform_not_configured",
                "message": "Falta ML_APP_ID en el servidor. Contacte al administrador.",
                "demo_mode": True
            }), 200

        redirect_uri = f"{base_url}/api/integrations/mercadolibre/callback"
        # MercadoLibre supports 'state' perfectly.
        auth_url = f"https://auth.mercadolibre.com.ar/authorization?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}&state={tenant.id}"
        return jsonify({"redirect_url": auth_url})

    elif integration_type.lower() == 'whatsapp':
        # WhatsApp Cloud API / Embedded Signup flow
        # This usually requires a Facebook App ID and a specific config ID
        fb_app_id = current_app.config.get("FACEBOOK_APP_ID")
        if not fb_app_id:
             current_app.logger.warning("FACEBOOK_APP_ID not set. WhatsApp integration unavailable.")
             return jsonify({
                "error": "platform_not_configured",
                "message": "Falta FACEBOOK_APP_ID en el servidor. Contacte al administrador.",
                "demo_mode": True
            }), 200

        # Simplified flow: Redirect to a frontend page that handles the Embedded Signup
        # or return the config needed for the SDK.
        # For now, let's assume we return a setup URL or instruction.
        # Since the frontend calls 'connect', it expects a redirect_url.
        # If we are doing Embedded Signup, the frontend should trigger the popup.
        # If we are doing OAuth (less common for WA Business), we generate a URL.
        # Let's assume standard OAuth for now or a placeholder to stop the 400.

        redirect_uri = f"{base_url}/api/integrations/whatsapp/callback"
        auth_url = f"https://www.facebook.com/v17.0/dialog/oauth?client_id={fb_app_id}&redirect_uri={redirect_uri}&state={tenant.id}&scope=whatsapp_business_management,whatsapp_business_messaging"

        return jsonify({"redirect_url": auth_url})

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

    results = []
    for emp in employees:
        user_roles = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
        role_names = [ur.role.name for ur in user_roles if ur.role]

        results.append({
            "id": emp.id,
            "name": emp.name,
            "email": emp.email,
            "roles": role_names,
            "created_at": emp.fecha_creacion.isoformat() if emp.fecha_creacion else None,
            "scope": _employee_scope(emp),
        })

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

    return jsonify([{
        "id": c.id,
        "nombre": c.nombre,
        "tipo": c.tipo
    } for c in categories])

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
        return (
            jsonify(
                {
                    "error": "plan_required",
                    "message": "Integraciones disponibles para planes Pro/Full.",
                }
            ),
            403,
        )

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
        return (
            jsonify(
                {
                    "error": "plan_required",
                    "message": "Integraciones disponibles para planes Pro/Full.",
                }
            ),
            403,
        )

    if integration_type.lower() == 'mercadolibre':
        from services.integrations.mercadolibre import MercadoLibreService
        service = MercadoLibreService(tenant)
        result = service.preview_sync()
        return jsonify(result)

    return jsonify({"error": "Integration not supported or preview unavailable"}), 400

@admin_tenant_bp.route('/api/admin/tenants/<slug>/orders', methods=['GET'])
@token_requerido
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

    legacy_query = PymePedido.query.filter(
        (PymePedido.tenant_id == tenant.id) | (PymePedido.pyme_id == tenant.pyme_id)
    )
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

    results = dedupe_unified_orders([serialize_unified_order(record) for record in order_records])
    results.sort(key=lambda item: item.get('created_at') or '', reverse=True)

    return jsonify({
        "orders": results[:limit],
        "count": len(results[:limit]),
        "sources": sorted({item.get('source_model') for item in results[:limit] if item.get('source_model')}),
    })



def _ticket_belongs_to_tenant(ticket, tenant: TenantProfile) -> bool:
    if not ticket or not tenant:
        return False
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

    m_query = MunicipioTicket.query.filter_by(tenant_id=tenant.id)
    p_query = PymeTicket.query.filter_by(tenant_id=tenant.id)
    rows = []
    for t in m_query.order_by(MunicipioTicket.ultima_actividad.desc()).limit(limit).all():
        rows.append(('municipio', t))
    for t in p_query.order_by(PymeTicket.fecha.desc()).limit(limit).all():
        rows.append(('pyme', t))

    items = []
    now = datetime.now(timezone.utc)
    for t_type, ticket in rows:
        details = _ticket_details(ticket)
        stage = str(details.get('lead_stage') or ticket.estado or 'nuevo').lower()
        if stage_filter and stage_filter != stage:
            continue
        last_seen = getattr(ticket, 'ultima_actividad', None) or ticket.fecha
        last_dt = last_seen if (last_seen and last_seen.tzinfo) else (last_seen.replace(tzinfo=timezone.utc) if last_seen else None)
        sla_breached = bool(last_dt and (now - last_dt).total_seconds() > 1800 and stage not in {'ganado', 'perdido'})
        items.append({
            'ticket_type': t_type,
            'ticket_id': ticket.id,
            'nro': getattr(ticket, 'nro_ticket', None) or getattr(ticket, 'nro_pedido', None),
            'nombre': getattr(ticket, 'nombre_vecino', None) or getattr(ticket, 'nombre_cliente', None),
            'telefono': getattr(ticket, 'telefono_vecino', None) or getattr(ticket, 'telefono_cliente', None),
            'email': getattr(ticket, 'email_vecino', None) or getattr(ticket, 'email_cliente', None),
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

    ticket = MunicipioTicket.query.get(ticket_id) if ticket_type == 'municipio' else PymeTicket.query.get(ticket_id) if ticket_type == 'pyme' else None
    if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
        return jsonify({'error': 'Lead no encontrado'}), 404

    details = _ticket_details(ticket)
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
    _save_ticket_details(ticket, details)

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

        ticket = MunicipioTicket.query.get(ticket_id) if ticket_type == 'municipio' else PymeTicket.query.get(ticket_id) if ticket_type == 'pyme' else None
        if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
            errors.append({'ticket_type': ticket_type, 'ticket_id': ticket_id, 'error': 'no encontrado'})
            continue

        details = _ticket_details(ticket)
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
        _save_ticket_details(ticket, details)
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

    ticket = MunicipioTicket.query.get(ticket_id) if ticket_type == 'municipio' else PymeTicket.query.get(ticket_id) if ticket_type == 'pyme' else None
    if not ticket or not _ticket_belongs_to_tenant(ticket, tenant):
        return jsonify({'error': 'Lead no encontrado'}), 404

    details = _ticket_details(ticket)
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
    _save_ticket_details(ticket, details)
    if hasattr(ticket, 'ultima_actividad'):
        ticket.ultima_actividad = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'ok': True, 'ticket_id': ticket.id, 'ticket_type': ticket_type, 'timeline': details['lead_timeline']})

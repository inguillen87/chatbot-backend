from flask import Blueprint, jsonify, request, g, current_app
from models import (
    TenantProfile,
    User,
    db,
    generate_token,
    WhatsappNumero,
    Rubro,
    AdminAuditLog,
    TwilioNumber,
    ChatSessionContext,
    Conversacion,
    MunicipioTicket,
    PymeTicket,
    CatalogoItem,
    EncEncuesta,
    EncRespuesta,
    LlmInteractionLog,
)
from utils.auth_helpers import token_requerido
from services.operational_scoring import build_lead_portfolio_score
from utils.admin_decorators import super_admin_required
from sqlalchemy import desc, func
from datetime import datetime, timezone, timedelta
from services.tenant_management.folder_manager import ensure_tenant_folder_structure
from services.plan_config import apply_plan_to_user, get_plan_metadata
from services.user_service import assign_whatsapp_numbers
import jwt
import re
import unicodedata
import json

super_admin_bp = Blueprint('super_admin', __name__, url_prefix='/api/admin')

LEGACY_TENANT_SEEDS = {
    "mauricio@junin.com": {
        "slug": "junin",
        "nombre": "Municipalidad de Junin",
        "tipo": "municipio",
        "plan": "full",
    },
    "franco@cuatrofincas.com": {
        "slug": "cuatro-fincas",
        "nombre": "Bodega Cuatro Fincas",
        "tipo": "pyme",
        "plan": "full",
    },
}




_SUPPORTED_FRANCHISE_LANGUAGES = {"es", "en", "pt"}


def _default_franchise_profile(tenant: TenantProfile) -> dict:
    return {
        "white_label_enabled": True,
        "reseller_enabled": True,
        "target_markets": ["latam"],
        "default_language": "es",
        "supported_languages": ["es", "en", "pt"],
        "timezone": "America/Argentina/Buenos_Aires",
        "currency": "ARS",
        "country": "AR",
        "legal_entity_name": tenant.nombre,
        "partner_program": "standard",
    }


def _sanitize_franchise_profile(payload: dict, tenant: TenantProfile) -> tuple[dict, list[str]]:
    base = _default_franchise_profile(tenant)
    errors: list[str] = []

    if not isinstance(payload, dict):
        return base, ["Payload inválido"]

    for flag in ("white_label_enabled", "reseller_enabled"):
        if flag in payload:
            base[flag] = bool(payload.get(flag))

    if "target_markets" in payload:
        markets = payload.get("target_markets")
        if isinstance(markets, list):
            cleaned = [str(item).strip().lower() for item in markets if str(item).strip()]
            base["target_markets"] = list(dict.fromkeys(cleaned))[:12]

    if "default_language" in payload:
        lang = str(payload.get("default_language") or "").strip().lower()
        if lang in _SUPPORTED_FRANCHISE_LANGUAGES:
            base["default_language"] = lang
        else:
            errors.append("default_language inválido (usar es|en|pt)")

    if "supported_languages" in payload:
        langs = payload.get("supported_languages")
        if isinstance(langs, list) and langs:
            cleaned = []
            for item in langs:
                code = str(item).strip().lower()
                if code in _SUPPORTED_FRANCHISE_LANGUAGES:
                    cleaned.append(code)
            cleaned = list(dict.fromkeys(cleaned))
            if cleaned:
                base["supported_languages"] = cleaned
            else:
                errors.append("supported_languages inválido (usar es|en|pt)")

    for key in ("timezone", "currency", "country", "legal_entity_name", "partner_program"):
        if key in payload:
            value = str(payload.get(key) or "").strip()
            if value:
                base[key] = value

    if base.get("default_language") not in set(base.get("supported_languages") or []):
        base["supported_languages"] = list(dict.fromkeys([base.get("default_language")] + list(base.get("supported_languages") or [])))

    return base, errors


def _iso_datetime(value):
    if not value:
        return None
    try:
        return value.isoformat()
    except Exception:
        return None


def _build_tenant_health_snapshot(tenant: TenantProfile, *, cutoff: datetime) -> dict:
    owner = tenant.municipio or tenant.pyme
    owner_id = getattr(owner, "id", None)
    now = datetime.now(timezone.utc)

    municipio_tickets = MunicipioTicket.query.filter(
        MunicipioTicket.tenant_id == tenant.id,
        MunicipioTicket.fecha >= cutoff,
    ).all()
    pyme_tickets = PymeTicket.query.filter(
        PymeTicket.tenant_id == tenant.id,
        PymeTicket.fecha >= cutoff,
    ).all()
    tickets = municipio_tickets + pyme_tickets

    total_tickets = len(tickets)
    won = 0
    lost = 0
    open_tickets = 0
    assigned_tickets = 0
    sla_breached = 0
    unassigned_aging = 0

    for ticket in tickets:
        details = _ensure_ticket_details_dict(ticket)
        stage = str(details.get('lead_stage') or ticket.estado or 'nuevo').lower()
        if stage == 'ganado':
            won += 1
        elif stage == 'perdido':
            lost += 1
        else:
            open_tickets += 1

        if getattr(ticket, "asignado_a_id", None):
            assigned_tickets += 1

        last_seen = getattr(ticket, 'ultima_actividad', None) or getattr(ticket, 'fecha', None)
        if last_seen and stage not in {'ganado', 'perdido'}:
            dt = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
            age_seconds = (now - dt).total_seconds()
            if age_seconds > 1800:
                sla_breached += 1
            if not getattr(ticket, "asignado_a_id", None) and age_seconds > 8 * 3600:
                unassigned_aging += 1

    survey_rows = EncEncuesta.query.filter(
        EncEncuesta.tenant_id == tenant.id,
        EncEncuesta.updated_at >= cutoff,
    ).all()
    survey_count = len(survey_rows)
    survey_responses = 0
    for survey in survey_rows:
        survey_responses += EncRespuesta.query.filter_by(encuesta_id=survey.id).count()

    catalog_count = CatalogoItem.query.filter_by(tenant_id=tenant.id).count()
    conversations = 0
    if owner_id:
        conversations = Conversacion.query.filter(
            ((Conversacion.pyme_id == owner_id) | (Conversacion.user_id == owner_id)),
            Conversacion.timestamp >= cutoff,
        ).count()

    win_rate = round((won / total_tickets) * 100, 2) if total_tickets else 0.0
    response_rate = round(((total_tickets - sla_breached) / total_tickets) * 100, 2) if total_tickets else 0.0
    activity_score = min(conversations, 40)
    health_score = max(
        0.0,
        min(
            100.0,
            round(
                (win_rate * 0.45)
                + (min(survey_responses, 50) * 0.25)
                + (response_rate * 0.20)
                + (activity_score * 0.10)
                - (unassigned_aging * 3),
                2,
            ),
        ),
    )

    onboarding = {
        "branding": bool(tenant.logo_url or tenant.tema or tenant.theme_json),
        "widget": bool(tenant.widget_settings or tenant.widget_config or getattr(owner, "token", None)),
        "whatsapp": bool(tenant.whatsapp_sender_id),
        "catalog": catalog_count > 0,
        "surveys": survey_count > 0,
        "tracking": total_tickets > 0,
        "analytics": bool(total_tickets or survey_responses or conversations),
    }
    completed_steps = sum(1 for value in onboarding.values() if value)

    alerts = []
    if sla_breached:
        alerts.append("sla_breached")
    if unassigned_aging:
        alerts.append("unassigned_backlog")
    if catalog_count == 0 and tenant.tipo == "pyme":
        alerts.append("catalog_missing")
    if survey_count == 0:
        alerts.append("surveys_missing")
    if not onboarding["whatsapp"]:
        alerts.append("whatsapp_not_connected")

    return {
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "tenant_nombre": tenant.nombre,
        "tenant_tipo": tenant.tipo,
        "plan": tenant.plan,
        "is_active": bool(getattr(tenant, "is_active", True)),
        "owner": {
            "id": owner_id,
            "name": getattr(owner, "name", None),
            "email": getattr(owner, "email", None),
        },
        "metrics": {
            "total_tickets": total_tickets,
            "open_tickets": open_tickets,
            "won": won,
            "lost": lost,
            "assigned_tickets": assigned_tickets,
            "unassigned_tickets": max(total_tickets - assigned_tickets, 0),
            "sla_breached": sla_breached,
            "unassigned_aging": unassigned_aging,
            "survey_count": survey_count,
            "survey_responses": survey_responses,
            "catalog_items": catalog_count,
            "conversations_30d": conversations,
        },
        "health": {
            "score": health_score,
            "win_rate": win_rate,
            "response_rate": response_rate,
            "alerts": alerts,
        },
        "onboarding": {
            "completed_steps": completed_steps,
            "total_steps": len(onboarding),
            "checklist": onboarding,
        },
        "meta": {
            "last_updated_at": _iso_datetime(getattr(tenant, "updated_at", None)),
            "created_at": _iso_datetime(getattr(tenant, "created_at", None)),
            "cutoff": _iso_datetime(cutoff),
        },
    }


def _build_strategic_overview_payload(*, since_days: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    mt_rows = MunicipioTicket.query.filter(MunicipioTicket.fecha >= cutoff).all()
    pt_rows = PymeTicket.query.filter(PymeTicket.fecha >= cutoff).all()
    all_rows = [("municipio", t) for t in mt_rows] + [("pyme", t) for t in pt_rows]

    by_stage = {}
    by_tenant = {}
    sla_breached = 0

    now = datetime.now(timezone.utc)
    for _ticket_type, ticket in all_rows:
        details = _ensure_ticket_details_dict(ticket)
        stage = str(details.get('lead_stage') or ticket.estado or 'nuevo').lower()
        by_stage[stage] = by_stage.get(stage, 0) + 1

        tenant_key = str(getattr(ticket, 'tenant_id', None) or 'sin_tenant')
        if tenant_key not in by_tenant:
            by_tenant[tenant_key] = {
                'tenant_id': None if tenant_key == 'sin_tenant' else int(tenant_key),
                'total': 0,
                'won': 0,
                'lost': 0,
                'open': 0,
            }
        by_tenant[tenant_key]['total'] += 1
        if stage == 'ganado':
            by_tenant[tenant_key]['won'] += 1
        elif stage == 'perdido':
            by_tenant[tenant_key]['lost'] += 1
        else:
            by_tenant[tenant_key]['open'] += 1

        last_seen = getattr(ticket, 'ultima_actividad', None) or getattr(ticket, 'fecha', None)
        if last_seen:
            dt = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
            if (now - dt).total_seconds() > 1800 and stage not in {'ganado', 'perdido'}:
                sla_breached += 1

    total = len(all_rows)
    won = by_stage.get('ganado', 0)
    lost = by_stage.get('perdido', 0)
    open_total = max(total - won - lost, 0)
    win_rate = round((won / total) * 100, 2) if total else 0.0

    by_tenant_rows = list(by_tenant.values())
    for item in by_tenant_rows:
        tenant_total = item.get('total') or 0
        tenant_won = item.get('won') or 0
        item['win_rate'] = round((tenant_won / tenant_total) * 100, 2) if tenant_total else 0.0
    by_tenant_rows.sort(key=lambda item: (item.get('open', 0), item.get('total', 0)), reverse=True)

    alerts = []
    if sla_breached:
        alerts.append({
            'kind': 'sla_breached',
            'severity': 'high',
            'count': sla_breached,
            'message': 'Hay leads abiertos sin respuesta reciente.',
        })
    if open_total > max(won + lost, 1):
        alerts.append({
            'kind': 'backlog_growth',
            'severity': 'medium',
            'count': open_total,
            'message': 'El backlog abierto supera a los leads cerrados del período.',
        })

    return {
        'since_days': since_days,
        'totals': {
            'total_leads': total,
            'open_leads': open_total,
            'won': won,
            'lost': lost,
            'sla_breached': sla_breached,
            'win_rate': win_rate,
        },
        'by_stage': by_stage,
        'by_tenant': by_tenant_rows,
        'portfolio': {
            'top_open_tenants': by_tenant_rows[:5],
            'active_tenants': len([item for item in by_tenant_rows if item.get('total')]),
        },
        'alerts': alerts,
    }


def _build_realtime_ai_payload(*, minutes: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    sessions = ChatSessionContext.query.filter(ChatSessionContext.last_updated >= cutoff).all()
    llm_logs = LlmInteractionLog.query.filter(LlmInteractionLog.created_at >= cutoff).all()

    total_llm = len(llm_logs)
    pending = sum(1 for x in llm_logs if str(getattr(x, 'status', '')).lower() == 'pending_review')
    converted = sum(1 for x in llm_logs if str(getattr(x, 'status', '')).lower() == 'converted_to_faq')
    rejected = sum(1 for x in llm_logs if str(getattr(x, 'status', '')).lower() == 'rejected')

    mt_total = MunicipioTicket.query.filter(MunicipioTicket.fecha >= cutoff).count()
    pt_total = PymeTicket.query.filter(PymeTicket.fecha >= cutoff).count()

    lead_rows = MunicipioTicket.query.filter(MunicipioTicket.fecha >= cutoff).all() + PymeTicket.query.filter(PymeTicket.fecha >= cutoff).all()
    assigned = sum(1 for ticket in lead_rows if getattr(ticket, 'asignado_a_id', None))
    unassigned = max(len(lead_rows) - assigned, 0)

    return {
        'minutes': minutes,
        'active_sessions': len(sessions),
        'llm': {
            'total': total_llm,
            'pending_review': pending,
            'converted_to_faq': converted,
            'rejected': rejected,
        },
        'tickets': {
            'municipio': mt_total,
            'pyme': pt_total,
            'total': mt_total + pt_total,
            'assigned': assigned,
            'unassigned': unassigned,
        },
    }


def _build_tenant_health_payload(*, since_days: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    rows = []
    for tenant in TenantProfile.query.all():
        snapshot = _build_tenant_health_snapshot(tenant, cutoff=cutoff)
        rows.append({
            'tenant_id': snapshot['tenant_id'],
            'tenant_slug': snapshot['tenant_slug'],
            'tenant_tipo': snapshot['tenant_tipo'],
            'tenant_nombre': snapshot['tenant_nombre'],
            'plan': snapshot['plan'],
            'is_active': snapshot['is_active'],
            'total_tickets': snapshot['metrics']['total_tickets'],
            'open_tickets': snapshot['metrics']['open_tickets'],
            'won': snapshot['metrics']['won'],
            'lost': snapshot['metrics']['lost'],
            'sla_breached': snapshot['metrics']['sla_breached'],
            'survey_count': snapshot['metrics']['survey_count'],
            'survey_responses': snapshot['metrics']['survey_responses'],
            'catalog_items': snapshot['metrics']['catalog_items'],
            'health_score': snapshot['health']['score'],
            'win_rate': snapshot['health']['win_rate'],
            'response_rate': snapshot['health']['response_rate'],
            'alerts': snapshot['health']['alerts'],
            'onboarding_completion': snapshot['onboarding']['completed_steps'],
            'onboarding_total_steps': snapshot['onboarding']['total_steps'],
        })

    rows.sort(key=lambda r: (r['health_score'], -r['sla_breached']), reverse=True)
    return {'since_days': since_days, 'total_tenants': len(rows), 'items': rows}


def _build_heatmap_categories_payload(*, since_days: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    rows = []
    for ticket in MunicipioTicket.query.filter(MunicipioTicket.fecha >= cutoff).all():
        rows.append({
            'tenant_id': ticket.tenant_id,
            'categoria': (ticket.categoria or 'sin_categoria').strip().lower(),
            'zona': (ticket.distrito or 'sin_zona').strip().lower(),
            'lat': ticket.latitud,
            'lon': ticket.longitud,
            'tipo': 'municipio',
        })
    for ticket in PymeTicket.query.filter(PymeTicket.fecha >= cutoff).all():
        rows.append({
            'tenant_id': ticket.tenant_id,
            'categoria': (ticket.categoria or 'sin_categoria').strip().lower(),
            'zona': (getattr(ticket, 'direccion', None) or 'sin_zona').strip().lower(),
            'lat': ticket.latitud,
            'lon': ticket.longitud,
            'tipo': 'pyme',
        })

    by_categoria = {}
    by_zona = {}
    points = []
    for row in rows:
        by_categoria[row['categoria']] = by_categoria.get(row['categoria'], 0) + 1
        by_zona[row['zona']] = by_zona.get(row['zona'], 0) + 1
        if row['lat'] is not None and row['lon'] is not None:
            points.append({
                'lat': row['lat'],
                'lon': row['lon'],
                'weight': 1,
                'categoria': row['categoria'],
                'zona': row['zona'],
                'tipo': row['tipo'],
                'tenant_id': row['tenant_id'],
            })

    top_categories = sorted(by_categoria.items(), key=lambda it: it[1], reverse=True)[:20]
    top_zones = sorted(by_zona.items(), key=lambda it: it[1], reverse=True)[:20]
    return {
        'since_days': since_days,
        'total': len(rows),
        'top_categories': [{'categoria': k, 'count': v} for k, v in top_categories],
        'top_zones': [{'zona': k, 'count': v} for k, v in top_zones],
        'heatmap_points': points[:3000],
    }



def _compute_franchise_readiness(tenant: TenantProfile) -> dict:
    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    profile = config.get("franchise_profile") if isinstance(config.get("franchise_profile"), dict) else _default_franchise_profile(tenant)

    checks = {
        "white_label_enabled": bool(profile.get("white_label_enabled")),
        "reseller_enabled": bool(profile.get("reseller_enabled")),
        "languages_configured": bool(profile.get("supported_languages")),
        "default_language_valid": str(profile.get("default_language") or "") in set(profile.get("supported_languages") or []),
        "currency_defined": bool(str(profile.get("currency") or "").strip()),
        "country_defined": bool(str(profile.get("country") or "").strip()),
        "timezone_defined": bool(str(profile.get("timezone") or "").strip()),
        "partner_program_defined": bool(str(profile.get("partner_program") or "").strip()),
        "brand_domain_configured": bool(str(tenant.dominio or "").strip()),
        "logo_configured": bool(str(tenant.logo_url or "").strip()),
        "payments_configured": bool(str((config.get("mercadopago_access_token") or "")).strip()),
        "whatsapp_sender_configured": bool(str(tenant.whatsapp_sender_id or "").strip()),
    }

    total = len(checks)
    passed = sum(1 for value in checks.values() if value)
    score = round((passed / total) * 100, 2) if total else 0.0

    missing = [key for key, value in checks.items() if not value]
    status = "ready" if score >= 85 else "in_progress" if score >= 60 else "basic"

    return {
        "score": score,
        "status": status,
        "checks": checks,
        "missing": missing,
        "profile": profile,
    }



_PLAYBOOK_TASKS_BY_CHECK = {
    "white_label_enabled": {
        "title": "Activar modo white-label",
        "description": "Habilitar branding white-label y revisión legal/comercial para subdistribución.",
        "priority": "high",
        "owner": "producto",
    },
    "reseller_enabled": {
        "title": "Activar canal reseller",
        "description": "Definir esquema de partners, márgenes y gobierno operativo.",
        "priority": "high",
        "owner": "comercial",
    },
    "languages_configured": {
        "title": "Configurar idiomas comerciales",
        "description": "Completar catálogo de idiomas objetivo (es/en/pt) para portal y panel.",
        "priority": "high",
        "owner": "producto",
    },
    "default_language_valid": {
        "title": "Corregir idioma por defecto",
        "description": "Alinear default_language con supported_languages del tenant.",
        "priority": "medium",
        "owner": "producto",
    },
    "currency_defined": {
        "title": "Definir moneda operativa",
        "description": "Configurar currency para pricing y reportes del país objetivo.",
        "priority": "high",
        "owner": "finanzas",
    },
    "country_defined": {
        "title": "Definir país objetivo",
        "description": "Configurar country para localización, compliance y go-to-market.",
        "priority": "high",
        "owner": "comercial",
    },
    "timezone_defined": {
        "title": "Definir zona horaria",
        "description": "Configurar timezone para SLA, turnos y analítica local.",
        "priority": "medium",
        "owner": "operaciones",
    },
    "partner_program_defined": {
        "title": "Definir partner program",
        "description": "Seleccionar programa de partnership/franquicia para este tenant.",
        "priority": "high",
        "owner": "comercial",
    },
    "brand_domain_configured": {
        "title": "Configurar dominio de marca",
        "description": "Asignar dominio productivo del tenant para despliegue white-label.",
        "priority": "high",
        "owner": "infra",
    },
    "logo_configured": {
        "title": "Subir identidad visual",
        "description": "Configurar logo oficial y lineamientos de marca.",
        "priority": "medium",
        "owner": "marketing",
    },
    "payments_configured": {
        "title": "Conectar pagos por tenant",
        "description": "Configurar token de pagos (MercadoPago u otro proveedor) y validarlo.",
        "priority": "high",
        "owner": "finanzas",
    },
    "whatsapp_sender_configured": {
        "title": "Configurar canal WhatsApp",
        "description": "Asignar sender oficial para operaciones omnicanal del tenant.",
        "priority": "medium",
        "owner": "operaciones",
    },
}


def _build_franchise_playbook(tenant: TenantProfile) -> dict:
    readiness = _compute_franchise_readiness(tenant)
    tasks = []
    for check_key in readiness.get("missing") or []:
        template = _PLAYBOOK_TASKS_BY_CHECK.get(check_key)
        if not template:
            continue
        tasks.append({"check": check_key, **template})

    priority_order = {"high": 0, "medium": 1, "low": 2}
    tasks.sort(key=lambda item: priority_order.get(item.get("priority"), 99))

    return {
        "status": readiness.get("status"),
        "score": readiness.get("score"),
        "next_actions": tasks,
        "estimated_phases": {
            "phase_1": [t for t in tasks if t.get("priority") == "high"],
            "phase_2": [t for t in tasks if t.get("priority") == "medium"],
            "phase_3": [t for t in tasks if t.get("priority") == "low"],
        },
    }


def _critical_readiness_gaps(readiness: dict) -> list[str]:
    missing = readiness.get("missing") if isinstance(readiness, dict) else []
    if not isinstance(missing, list):
        return []

    critical = []
    for check in missing:
        task = _PLAYBOOK_TASKS_BY_CHECK.get(check)
        if task and task.get("priority") == "high":
            critical.append(check)
    return critical



_LEAD_URGENT_KEYWORDS = {
    "urgente", "emergencia", "incendio", "explosion", "accidente", "fuga", "gas", "inseguridad",
    "hoy", "ya", "ahora", "rápido", "rapido", "cotizacion", "presupuesto", "comprar", "demo"
}


def _lead_relevance_score(*, open_tickets: int, latest_message: str, last_seen: datetime | None, has_contact: bool) -> int:
    score = 0
    if open_tickets > 0:
        score += min(open_tickets * 20, 60)

    text = str(latest_message or "").strip().lower()
    if any(k in text for k in _LEAD_URGENT_KEYWORDS):
        score += 30

    if has_contact:
        score += 15

    if last_seen:
        now = datetime.now(timezone.utc)
        last_dt = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
        age_hours = max(0, (now - last_dt).total_seconds() / 3600)
        if age_hours <= 1:
            score += 25
        elif age_hours <= 24:
            score += 15
        elif age_hours <= 72:
            score += 8

    return int(score)




def _normalize_plan_key(raw_plan: str | None) -> str:
    if not raw_plan:
        return "gratis"
    normalized = str(raw_plan).strip().lower()
    if normalized in {"free", "demo"}:
        normalized = "gratis"
    if get_plan_metadata(normalized) is None:
        return "gratis"
    return normalized

def _log_admin_action(user_id: int, action: str, target: str, details: dict = None):
    try:
        log = AdminAuditLog(
            admin_user_id=user_id,
            action=action,
            target_object=target,
            details=details or {},
            ip_address=request.remote_addr
        )
        db.session.add(log)
        # Note: We rely on the caller's commit or commit here if safe.
        # Since most routes commit at the end, we can let the route handle it,
        # or commit immediately if we want logs even on failure.
        # Here we trust the caller transaction for atomicity.
    except Exception as e:
        current_app.logger.error(f"Failed to create audit log: {e}")


def _slugify(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    decomposed = unicodedata.normalize("NFKD", text)
    sanitized = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    sanitized = re.sub(r"[^a-z0-9]+", "-", sanitized).strip("-")
    return sanitized or None


def _candidate_slug_for_user(user: User) -> str | None:
    seed = LEGACY_TENANT_SEEDS.get((user.email or "").strip().lower())
    if seed:
        return _slugify(seed.get("slug"))
    if getattr(user, "tenant_slug", None):
        return _slugify(user.tenant_slug)
    email = (user.email or "").strip().lower()
    if (user.tipo_chat or "").lower() == "municipio" and "@" in email:
        domain = email.split("@")[-1]
        if domain and domain not in {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com"}:
            domain_root = domain.split(".")[0]
            if domain_root:
                return _slugify(domain_root)
    candidate = user.nombre_empresa or user.name or email
    if "@" in str(candidate):
        candidate = str(candidate).split("@")[0]
    return _slugify(str(candidate))


def _ensure_unique_slug(base_slug: str | None) -> str:
    base = base_slug or "tenant"
    slug = base
    counter = 1
    while TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower()).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _slug_conflicts(desired_slug: str, tenant_id: int | None = None) -> bool:
    if not desired_slug:
        return False
    query = TenantProfile.query.filter(func.lower(TenantProfile.slug) == desired_slug.lower())
    if tenant_id:
        query = query.filter(TenantProfile.id != tenant_id)
    return query.first() is not None


def _maybe_create_tenant_for_admin(user: User) -> TenantProfile | None:
    seed = LEGACY_TENANT_SEEDS.get((user.email or "").strip().lower())
    existing = None
    if user.tenant_id:
        existing = TenantProfile.query.get(user.tenant_id)
    if not existing:
        candidate_slug = _slugify(getattr(user, "tenant_slug", None))
        if candidate_slug:
            existing = TenantProfile.query.filter(
                func.lower(TenantProfile.slug) == candidate_slug.lower()
            ).first()
    if not existing:
        existing = TenantProfile.query.filter(
            (TenantProfile.municipio_id == user.id) | (TenantProfile.pyme_id == user.id)
        ).first()
    if seed and existing:
        owned_by_user = user.id in {existing.municipio_id, existing.pyme_id}
        if not owned_by_user:
            existing = None
    if existing:
        user.tenant_id = existing.id
        user.tenant_slug = existing.slug
        if seed:
            desired_slug = seed["slug"]
            if desired_slug and not _slug_conflicts(desired_slug, tenant_id=existing.id):
                existing.slug = desired_slug
            elif desired_slug:
                current_app.logger.warning(
                    "SA:seed tenant slug '%s' is already in use; keeping '%s' for user %s",
                    desired_slug,
                    existing.slug,
                    user.email,
                )
            existing.nombre = seed["nombre"]
            existing.tipo = seed["tipo"]
            existing.plan = _normalize_plan_key(seed["plan"])
        if existing.tipo == "municipio":
            existing.municipio_id = user.id
            existing.pyme_id = None
            if not user.municipio_id:
                user.municipio_id = user.id
        if existing.tipo == "pyme":
            existing.pyme_id = user.id
            existing.municipio_id = None
            if not user.pyme_id:
                user.pyme_id = user.id
        return existing

    if user.rol not in {"admin", "admin_pyme"}:
        return None

    tipo = (seed.get("tipo") if seed else None) or (
        user.tipo_chat or ("municipio" if user.municipio_id else "pyme")
    )
    tipo = tipo.lower()
    if tipo not in {"municipio", "pyme"}:
        return None

    seed_slug = seed.get("slug") if seed else None
    slug = _ensure_unique_slug(_candidate_slug_for_user(user) or seed_slug or f"tenant-{user.id}")
    nombre = seed.get("nombre") if seed else None
    nombre = nombre or user.nombre_empresa or user.name or slug.replace("-", " ").title()
    plan = seed.get("plan") if seed else user.plan
    tenant = TenantProfile(
        slug=slug,
        nombre=nombre,
        tipo=tipo,
        plan=_normalize_plan_key(plan),
        is_active=True,
        municipio_id=user.id if tipo == "municipio" else None,
        pyme_id=user.id if tipo == "pyme" else None,
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    user.tenant_slug = slug
    if tipo == "municipio" and not user.municipio_id:
        user.municipio_id = user.id
    if tipo == "pyme" and not user.pyme_id:
        user.pyme_id = user.id
    return tenant


def _bootstrap_missing_tenants() -> int:
    candidates = User.query.filter(User.rol.in_(["admin", "admin_pyme"])).all()
    created_count = 0
    for user in candidates:
        tenant = _maybe_create_tenant_for_admin(user)
        if tenant and tenant.id:
            created_count += 1
    if created_count:
        db.session.commit()
    return created_count


def _normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.lower().startswith("whatsapp:"):
        normalized = normalized.split("whatsapp:", 1)[1].strip()
    return normalized or None


def _normalize_number_status(raw_status: str | None) -> str:
    if not raw_status:
        return "available"
    status = str(raw_status).strip().lower()
    allowed = {"available", "reserved", "assigned", "verified", "disabled"}
    return status if status in allowed else "available"


def _infer_phone_prefix(phone_number: str | None) -> str | None:
    if not phone_number:
        return None
    match = re.match(r"^\+?\d{1,4}", phone_number.strip())
    return match.group(0) if match else None


def _serialize_twilio_number(number: TwilioNumber) -> dict:
    tenant = number.tenant
    status = number.status or "available"
    return {
        "id": number.id,
        "phone_number": number.phone_number,
        "sender_id": number.sender_id,
        "status": status,
        "prefix": _infer_phone_prefix(number.phone_number),
        "city": None,
        "state": None,
        "tenant_id": number.tenant_id,
        "tenant_slug": tenant.slug if tenant else None,
        "tenant_nombre": tenant.nombre if tenant else None,
    }


def _delete_rows_by_column(table, column_name: str, ids: list[int]) -> int:
    if not ids or column_name not in table.c:
        return 0
    result = db.session.execute(table.delete().where(table.c[column_name].in_(ids)))
    return result.rowcount or 0


def _purge_tenant_records(tenant: TenantProfile) -> dict:
    tenant_id = tenant.id
    deleted = {"tenant_id": tenant_id, "tables": {}}
    for table in db.metadata.sorted_tables:
        if table.name == TenantProfile.__tablename__:
            continue
        table_deleted = 0
        if "tenant_id" in table.c:
            table_deleted += _delete_rows_by_column(table, "tenant_id", [tenant_id])
        if "municipio_id" in table.c:
            table_deleted += _delete_rows_by_column(table, "municipio_id", [tenant_id])
        if "pyme_id" in table.c:
            table_deleted += _delete_rows_by_column(table, "pyme_id", [tenant_id])
        if table_deleted:
            deleted["tables"][table.name] = table_deleted
    return deleted


def _purge_users(user_ids: list[int]) -> dict:
    deleted = {"users": len(user_ids), "tables": {}}
    if not user_ids:
        return deleted
    for table in db.metadata.sorted_tables:
        if table.name == User.__tablename__:
            continue
        if "user_id" not in table.c:
            continue
        deleted["tables"][table.name] = _delete_rows_by_column(table, "user_id", user_ids)
    User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    return deleted

@super_admin_bp.route('/tenants', methods=['GET'])
@token_requerido
@super_admin_required
def list_tenants(current_user):
    _bootstrap_missing_tenants()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    query = TenantProfile.query.order_by(desc(TenantProfile.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    tenants_data = []
    for tenant in pagination.items:
        # IMPROVED LOGIC: Prefer tenant plan, fall back to owner if needed.
        owner = tenant.municipio or tenant.pyme
        plan = tenant.plan or (owner.plan if owner else None) or "gratis"
        plan = _normalize_plan_key(plan)

        # IMPROVED LOGIC: Status is derived from is_active field.
        status = "active" if tenant.is_active else "inactive"

        tenants_data.append({
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": plan,
            "status": status,
            "is_active": tenant.is_active,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
            "owner_email": owner.email if owner else None  # Added as requested
        })

    return jsonify({
        "tenants": tenants_data,
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": page
    })

@super_admin_bp.route('/tenants/<string:slug>/franchise-profile', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_franchise_profile(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    profile = config.get("franchise_profile") if isinstance(config.get("franchise_profile"), dict) else None
    if not profile:
        profile = _default_franchise_profile(tenant)

    return jsonify({
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": tenant.plan,
        },
        "franchise_profile": profile,
        "supported_language_codes": sorted(_SUPPORTED_FRANCHISE_LANGUAGES),
    })


@super_admin_bp.route('/tenants/<string:slug>/franchise-profile', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_franchise_profile(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    payload = request.get_json(silent=True) or {}

    profile, errors = _sanitize_franchise_profile(payload, tenant)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    config["franchise_profile"] = profile
    tenant.configuracion = config
    db.session.add(tenant)

    _log_admin_action(current_user.id, "update_franchise_profile", slug, {"profile": profile})
    db.session.commit()

    return jsonify({"ok": True, "franchise_profile": profile})


@super_admin_bp.route('/tenants/<string:slug>/franchise-readiness', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_franchise_readiness(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    readiness = _compute_franchise_readiness(tenant)
    return jsonify({
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": tenant.plan,
        },
        "readiness": readiness,
    })


@super_admin_bp.route('/tenants/<string:slug>/franchise-playbook', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_franchise_playbook(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    playbook = _build_franchise_playbook(tenant)
    return jsonify({
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": tenant.plan,
        },
        "playbook": playbook,
    })


@super_admin_bp.route('/tenants/franchise-compare', methods=['GET'])
@token_requerido
@super_admin_required
def compare_tenants_franchise_readiness(current_user):
    tenant_type = str(request.args.get("tipo") or "").strip().lower()
    status_filter = str(request.args.get("status") or "").strip().lower()

    query = TenantProfile.query.filter(TenantProfile.is_active.is_(True))
    if tenant_type in {"pyme", "municipio"}:
        query = query.filter(TenantProfile.tipo == tenant_type)

    ranking = []
    for tenant in query.all():
        readiness = _compute_franchise_readiness(tenant)
        if status_filter and readiness.get("status") != status_filter:
            continue

        ranking.append({
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "nombre": tenant.nombre,
                "tipo": tenant.tipo,
                "plan": tenant.plan,
            },
            "score": readiness.get("score"),
            "status": readiness.get("status"),
            "critical_gaps": _critical_readiness_gaps(readiness),
            "missing": readiness.get("missing") or [],
        })

    ranking.sort(key=lambda item: (-(item.get("score") or 0), item["tenant"].get("slug") or ""))

    return jsonify({
        "items": ranking,
        "total": len(ranking),
        "filters": {
            "tipo": tenant_type or None,
            "status": status_filter or None,
        },
    })


@super_admin_bp.route('/tenants/<string:slug>/metrics', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_metrics(current_user, slug):
    """
    Returns summarized CRM metrics for a specific tenant:
    - Messages received
    - Orders placed
    - Tickets/Claims created
    - Basic heatmap data (aggregated points)
    """
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()

    # 1. Message Volume (Conversations or NotificationLog)
    # Using Conversacion for inbound/outbound estimation
    # Filter last 30 days
    since = datetime.now(timezone.utc) - timedelta(days=30)

    # Note: Conversacion table uses 'pyme_id' or 'user_id' which maps to User, not directly tenant_id often.
    # We resolve the owner user ID.
    owner_id = getattr(tenant, 'pyme_id', None) or getattr(tenant, 'municipio_id', None)

    msg_count = 0
    if owner_id:
        # Count user interactions (questions)
        from models import Conversacion
        msg_count = Conversacion.query.filter(
            (Conversacion.pyme_id == owner_id) | (Conversacion.user_id == owner_id),
            Conversacion.timestamp >= since
        ).count()

    # 2. Orders (MarketOrder)
    from models import MarketOrder
    order_count = MarketOrder.legacy_safe_count(
        MarketOrder.tenant_id == tenant.id,
        MarketOrder.created_at >= since,
    )

    # 3. Claims/Tickets
    ticket_count = 0
    if tenant.tipo == 'municipio':
        from models import MunicipioTicket
        ticket_count = MunicipioTicket.query.filter_by(tenant_id=tenant.id).filter(
            MunicipioTicket.fecha >= since
        ).count()
    else:
        from models import PymeTicket
        ticket_count = PymeTicket.query.filter_by(tenant_id=tenant.id).filter(
            PymeTicket.fecha >= since
        ).count()

    return jsonify({
        "period": "30d",
        "messages": msg_count,
        "orders": order_count,
        "tickets": ticket_count,
        "plan": tenant.plan
    })

@super_admin_bp.route('/tenants', methods=['POST'])
@token_requerido
@super_admin_required
def create_tenant(current_user):
    data = request.get_json() or {}
    slug = _slugify(data.get('slug'))
    nombre = data.get('nombre')
    tipo = data.get('tipo', 'pyme')
    email_admin = data.get('email_admin')

    if not slug or not nombre or not email_admin:
        return jsonify({"error": "Faltan datos (slug, nombre, email_admin)"}), 400

    if _slug_conflicts(slug):
        return jsonify({"error": "Slug ya existe"}), 409

    # Create Owner User if not exists
    owner = User.query.filter_by(email=email_admin).first()
    if not owner:
        owner = User(
            email=email_admin,
            name=f"Admin {nombre}",
            rol="admin",
            tipo_chat=tipo,
            token=generate_token()
        )
        owner.set_password("changeme") # Default password, should be changed
        db.session.add(owner)
        db.session.flush() # Get ID

    # Create Tenant
    tenant = TenantProfile(
        slug=slug,
        nombre=nombre,
        tipo=tipo,
        plan=_normalize_plan_key(data.get('plan')),
        is_active=True,
        municipio_id=owner.id if tipo == 'municipio' else None,
        pyme_id=owner.id if tipo == 'pyme' else None
    )
    db.session.add(tenant)

    _log_admin_action(current_user.id, "create_tenant", slug, {"nombre": nombre, "tipo": tipo, "owner_email": email_admin})

    db.session.commit()

    # Ensure Professional Folder Structure
    try:
        folder_path = ensure_tenant_folder_structure(slug, nombre, tipo)
        current_app.logger.info(f"Created professional folder structure for new tenant {slug} at {folder_path}")
    except Exception as e:
        current_app.logger.error(f"Failed to create folder structure for {slug}: {e}")
        # Proceed, don't fail the request, but log it.

    return jsonify({"message": "Tenant creado", "id": tenant.id, "slug": tenant.slug}), 201

@super_admin_bp.route('/tenants/<string:slug>', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_detail(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme

    return jsonify({
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "plan": tenant.plan,
        "is_active": getattr(tenant, 'is_active', True),
        "owner_email": owner.email if owner else None,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "whatsapp_sender_id": tenant.whatsapp_sender_id
    })

@super_admin_bp.route('/tenants/<string:slug>', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_full(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json() or {}

    if 'slug' in data:
        desired_slug = _slugify(data.get('slug'))
        if not desired_slug:
            return jsonify({"error": "Slug inválido"}), 400
        if _slug_conflicts(desired_slug, tenant_id=tenant.id):
            return jsonify({"error": "Slug ya existe"}), 409
        if tenant.slug != desired_slug:
            tenant.slug = desired_slug
            User.query.filter(User.tenant_id == tenant.id).update(
                {"tenant_slug": desired_slug},
                synchronize_session=False,
            )

    if 'nombre' in data: tenant.nombre = data['nombre']
    if 'plan' in data:
        old_plan = tenant.plan
        normalized_plan = _normalize_plan_key(data['plan'])
        tenant.plan = normalized_plan

        _log_admin_action(current_user.id, "change_plan", slug, {"old": old_plan, "new": normalized_plan})

        users_to_update = set()

        # Method 1: Find the owner via TenantProfile's FK and their employees
        owner = tenant.municipio or tenant.pyme
        if owner:
            users_to_update.add(owner)
            if owner.id: # safety check
                employees = User.query.filter(User.empresa_id == owner.id).all()
                for emp in employees:
                    users_to_update.add(emp)

        # Method 2: Find all users directly linked via User.tenant_id
        direct_members = User.query.filter(User.tenant_id == tenant.id).all()
        for member in direct_members:
            users_to_update.add(member)

        # Method 3 (Fallback for demo/legacy tenants): Find users whose pyme_id/municipio_id points to this tenant's ID
        if tenant.tipo == 'pyme':
            fallback_members = User.query.filter(User.pyme_id == tenant.id).all()
            for member in fallback_members:
                users_to_update.add(member)
        elif tenant.tipo == 'municipio':
            fallback_members = User.query.filter(User.municipio_id == tenant.id).all()
            for member in fallback_members:
                users_to_update.add(member)

        if not users_to_update:
            current_app.logger.warning(f"SA:update_plan: No associated users found for tenant {tenant.slug}. Plan will not be propagated.")
        else:
             current_app.logger.info(f"SA:update_plan: Found {len(users_to_update)} users to update for tenant {tenant.slug}.")

        for user in users_to_update:
            apply_plan_to_user(user, normalized_plan)
            current_app.logger.info(f"Applied plan '{normalized_plan}' to user {user.id} ({user.email}) for tenant {tenant.slug}")

    if 'is_active' in data:
        old_active = tenant.is_active
        tenant.is_active = bool(data['is_active'])
        _log_admin_action(current_user.id, "toggle_active", slug, {"old": old_active, "new": tenant.is_active})

    if 'whatsapp_sender_id' in data: tenant.whatsapp_sender_id = data['whatsapp_sender_id']

    # Handle domain, etc if needed
    if 'dominio' in data: tenant.dominio = data['dominio']

    try:
        db.session.commit()
        return jsonify({
            "message": "Tenant updated successfully",
            "slug": tenant.slug,
            "is_active": tenant.is_active,
            "plan_applied": tenant.plan
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@super_admin_bp.route('/tenants/<string:slug>', methods=['DELETE'])
@token_requerido
@super_admin_required
def delete_tenant_soft(current_user, slug):
    """Soft delete (deactivate) tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    tenant.is_active = False
    _log_admin_action(current_user.id, "deactivate_tenant", slug)
    db.session.commit()
    return jsonify({"message": "Tenant deactivated successfully"})


@super_admin_bp.route('/tenants/<string:slug>/purge', methods=['DELETE'])
@token_requerido
@super_admin_required
def delete_tenant_hard(current_user, slug):
    """Hard delete a tenant and its records."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json(silent=True) or {}
    confirm = data.get("confirm")
    purge_users = data.get("purge_users", True)

    if not confirm:
        return jsonify({"error": "Confirmación requerida (confirm=true)"}), 400

    owner_ids = {tenant.municipio_id, tenant.pyme_id}
    owner_ids.discard(None)
    user_ids = set(
        u.id
        for u in User.query.filter(
            (User.tenant_id == tenant.id)
            | (User.tenant_slug == tenant.slug)
            | (User.municipio_id == tenant.id)
            | (User.pyme_id == tenant.id)
        ).all()
    )
    user_ids.update(owner_ids)

    purge_summary = _purge_tenant_records(tenant)
    user_summary = {}
    if purge_users and user_ids:
        user_summary = _purge_users(sorted(user_ids))
    else:
        User.query.filter(User.id.in_(user_ids)).update(
            {"tenant_id": None, "tenant_slug": None},
            synchronize_session=False,
        )
        User.query.filter(User.municipio_id == tenant.id).update(
            {"municipio_id": None},
            synchronize_session=False,
        )
        User.query.filter(User.pyme_id == tenant.id).update(
            {"pyme_id": None},
            synchronize_session=False,
        )

    db.session.delete(tenant)
    _log_admin_action(
        current_user.id,
        "purge_tenant",
        slug,
        {"purge_users": purge_users, "user_ids": sorted(user_ids)},
    )
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("SA:purge_tenant failed for %s", slug)
        return jsonify({"error": "No se pudo eliminar el tenant", "details": str(exc)}), 500
    return jsonify(
        {
            "message": "Tenant eliminado definitivamente",
            "tenant_id": tenant.id,
            "purge": purge_summary,
            "users": user_summary,
        }
    )

@super_admin_bp.route('/tenants/<string:slug>/activate', methods=['POST'])
@token_requerido
@super_admin_required
def activate_tenant(current_user, slug):
    """Re-activate tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    tenant.is_active = True
    _log_admin_action(current_user.id, "activate_tenant", slug)
    db.session.commit()
    return jsonify({"message": "Tenant activated successfully"})

# Deprecated but kept for backward compatibility if frontend uses it
@super_admin_bp.route('/tenants/<string:slug>/status', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_status(current_user, slug):
    return update_tenant_full(current_user, slug)

@super_admin_bp.route('/tenants/<string:slug>/impersonate', methods=['POST'])
@token_requerido
@super_admin_required
def impersonate_tenant(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme

    if not owner:
        return jsonify({"error": "Tenant sin owner"}), 400

    # Generate short-lived token for owner
    payload = {
        'user_id': owner.id,
        'rol': owner.rol,
        'tipo_chat': owner.tipo_chat,
        'empresa_id': owner.empresa_id,
        'municipio_id': owner.municipio_id,
        'tenant_slug': tenant.slug,
        'impersonated_by': current_user.id,
        'exp': datetime.now(timezone.utc) + timedelta(minutes=60)
    }
    token = jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    _log_admin_action(current_user.id, "impersonate_tenant", slug, {"target_user_id": owner.id})

    redirect_url = f"/perfil?tenant_slug={tenant.slug}&tenant={tenant.slug}"
    return jsonify({"token": token, "redirect_url": redirect_url})

@super_admin_bp.route('/tenants/<string:slug>/admin-user', methods=['POST'])
@token_requerido
@super_admin_required
def create_tenant_admin(current_user, slug):
    """Create or link an admin user to the tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json() or {}
    email = data.get('email')
    password = data.get('password')
    name = data.get('name')

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos"}), 400

    existing_user = User.query.filter_by(email=email).first()

    if existing_user:
        # Check if already linked
        if existing_user.tenant_id == tenant.id:
            return jsonify({"message": "Usuario ya existe y está vinculado al tenant", "user_id": existing_user.id}), 200
        # If user exists but not linked, we might link them, but be careful of overriding
        return jsonify({"error": "El usuario ya existe pero no está vinculado a este tenant. Use endpoints de actualización."}), 409

    # Create new user
    rubro = Rubro.query.filter_by(clave="default").first() or Rubro.query.first()

    new_user = User(
        email=email,
        name=name or f"Admin {tenant.nombre}",
        rol=f"admin_{tenant.tipo}", # admin_pyme or admin_municipio
        tipo_chat=tenant.tipo,
        tenant_id=tenant.id,
        rubro_id=rubro.id if rubro else None,
        email_verified=True,
        acepto_terminos=True,
        token=generate_token()
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.flush()

    # Link as owner if missing
    if tenant.tipo == 'pyme' and not tenant.pyme_id:
        tenant.pyme_id = new_user.id
    elif tenant.tipo == 'municipio' and not tenant.municipio_id:
        tenant.municipio_id = new_user.id

    _log_admin_action(current_user.id, "create_admin_user", slug, {"new_user_email": email})
    db.session.commit()
    return jsonify({"message": "Admin user created successfully", "user_id": new_user.id}), 201

@super_admin_bp.route('/tenants/<string:slug>/password', methods=['PUT'])
@token_requerido
@super_admin_required
def reset_tenant_password(current_user, slug):
    """Reset password for the tenant's owner/admin."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme
    data = request.get_json() or {}
    new_password = data.get('password')

    if not owner:
        return jsonify({"error": "Tenant no tiene usuario propietario asignado"}), 404

    if not new_password:
        return jsonify({"error": "Password requerido"}), 400

    owner.set_password(new_password)
    _log_admin_action(current_user.id, "reset_password", slug, {"target_user_id": owner.id})
    db.session.commit()
    return jsonify({"message": "Contraseña actualizada correctamente"})

@super_admin_bp.route('/tenants/<string:slug>/whatsapp', methods=['PUT'])
@token_requerido
@super_admin_required
def configure_tenant_whatsapp(current_user, slug):
    """Configure WhatsApp number mapping for the tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme
    data = request.get_json() or {}
    number = data.get('number') # Expected format: +549...

    if not owner:
        return jsonify({"error": "Tenant no tiene usuario propietario asignado"}), 404

    if not number:
        return jsonify({"error": "Número de WhatsApp requerido"}), 400

    # 1. Update Tenant Profile
    tenant.whatsapp_sender_id = f"whatsapp:{number}" # Ensure Twilio format

    # 2. Update/Create WhatsappNumero mapping
    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number).first()
    if mapping:
        # Reassign if needed
        if mapping.user_id != owner.id:
            mapping.user_id = owner.id
            mapping.is_active = True
    else:
        mapping = WhatsappNumero(
            numero_whatsapp=number,
            user_id=owner.id,
            is_active=True
        )
        db.session.add(mapping)

    _log_admin_action(current_user.id, "configure_whatsapp", slug, {"number": number})
    db.session.commit()
    return jsonify({"message": "WhatsApp configurado correctamente", "number": number})


@super_admin_bp.route('/whatsapp/numbers', methods=['GET'])
@token_requerido
@super_admin_required
def list_whatsapp_numbers(current_user):
    status = request.args.get("status")
    tenant_slug = request.args.get("tenant_slug")
    prefix = request.args.get("prefix")
    city = request.args.get("city")
    state = request.args.get("state")

    query = TwilioNumber.query
    if status:
        query = query.filter_by(status=_normalize_number_status(status))
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            return jsonify({"error": "Tenant no encontrado"}), 404
        query = query.filter(TwilioNumber.tenant_id == tenant.id)
    if prefix:
        query = query.filter(TwilioNumber.phone_number.startswith(prefix))
    if city or state:
        current_app.logger.info(
            "SA:whatsapp:list: city/state filter requested but not stored in TwilioNumber: %s/%s",
            city,
            state,
        )

    numbers = query.order_by(TwilioNumber.phone_number.asc()).all()
    return jsonify({
        "numbers": [_serialize_twilio_number(number) for number in numbers],
        "total": len(numbers),
    })


@super_admin_bp.route('/whatsapp/numbers', methods=['POST'])
@token_requerido
@super_admin_required
def create_whatsapp_number(current_user):
    data = request.get_json() or {}
    phone_number = _normalize_phone(data.get("phone_number") or data.get("number"))
    sender_id = data.get("sender_id")
    status = _normalize_number_status(data.get("status"))
    tenant_slug = data.get("tenant_slug")

    if not phone_number or not sender_id:
        return jsonify({"error": "phone_number y sender_id requeridos"}), 400

    if TwilioNumber.query.filter_by(phone_number=phone_number).first():
        return jsonify({"error": "Número ya existe"}), 409

    tenant = None
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            return jsonify({"error": "Tenant no encontrado"}), 404

    number = TwilioNumber(
        phone_number=phone_number,
        sender_id=sender_id,
        status=status,
        tenant_id=tenant.id if tenant else None,
    )
    db.session.add(number)
    _log_admin_action(
        current_user.id,
        "create_whatsapp_number",
        tenant_slug or "",
        {"phone_number": phone_number, "status": status},
    )
    db.session.commit()
    return jsonify({"message": "Número creado", "number": _serialize_twilio_number(number)}), 201


@super_admin_bp.route('/whatsapp/numbers/reserve', methods=['POST'])
@token_requerido
@super_admin_required
def reserve_whatsapp_number(current_user):
    data = request.get_json() or {}
    number_id = data.get("number_id")
    tenant_slug = data.get("tenant_slug")

    if not number_id:
        return jsonify({"error": "number_id requerido"}), 400

    number = TwilioNumber.query.get(number_id)
    if not number:
        return jsonify({"error": "Número no encontrado"}), 404

    if number.status not in {"available", "reserved"}:
        return jsonify({"error": "Número no disponible"}), 409

    tenant = None
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            return jsonify({"error": "Tenant no encontrado"}), 404

    number.status = "reserved"
    number.tenant_id = tenant.id if tenant else number.tenant_id

    _log_admin_action(
        current_user.id,
        "reserve_whatsapp_number",
        tenant_slug or "",
        {"number_id": number.id, "phone_number": number.phone_number},
    )
    db.session.commit()
    return jsonify({"message": "Número reservado", "number": _serialize_twilio_number(number)})


@super_admin_bp.route('/whatsapp/numbers/release', methods=['POST'])
@token_requerido
@super_admin_required
def release_whatsapp_number(current_user):
    data = request.get_json() or {}
    number_id = data.get("number_id")

    if not number_id:
        return jsonify({"error": "number_id requerido"}), 400

    number = TwilioNumber.query.get(number_id)
    if not number:
        return jsonify({"error": "Número no encontrado"}), 404

    number.status = "available"
    number.tenant_id = None

    _log_admin_action(
        current_user.id,
        "release_whatsapp_number",
        "",
        {"number_id": number.id, "phone_number": number.phone_number},
    )
    db.session.commit()
    return jsonify({"message": "Número liberado", "number": _serialize_twilio_number(number)})


@super_admin_bp.route('/whatsapp/numbers/assign', methods=['POST'])
@token_requerido
@super_admin_required
def assign_whatsapp_number_to_tenant(current_user):
    data = request.get_json() or {}
    tenant_slug = data.get("tenant_slug")
    number_id = data.get("number_id")

    if not tenant_slug or not number_id:
        return jsonify({"error": "tenant_slug y number_id requeridos"}), 400

    tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
    if not tenant:
        return jsonify({"error": "Tenant no encontrado"}), 404

    number = TwilioNumber.query.get(number_id)
    if not number:
        return jsonify({"error": "Número no encontrado"}), 404

    if number.status not in {"available", "reserved", "assigned"}:
        return jsonify({"error": "Número no disponible"}), 409
    if number.status == "assigned" and number.tenant_id != tenant.id:
        return jsonify({"error": "Número ya asignado a otro tenant"}), 409

    number.status = "assigned"
    number.tenant_id = tenant.id
    tenant.whatsapp_sender_id = number.sender_id

    owner = tenant.municipio or tenant.pyme
    if owner:
        assign_whatsapp_numbers(owner, [_normalize_phone(number.phone_number)], activate=True, commit=False)

    _log_admin_action(
        current_user.id,
        "assign_whatsapp_number",
        tenant_slug,
        {"number_id": number.id, "phone_number": number.phone_number},
    )
    db.session.commit()
    return jsonify({
        "message": "Número asignado correctamente",
        "number": _serialize_twilio_number(number),
    })


@super_admin_bp.route('/whatsapp/numbers/register', methods=['POST'])
@token_requerido
@super_admin_required
def register_external_whatsapp_number(current_user):
    data = request.get_json() or {}
    tenant_slug = data.get("tenant_slug")
    number_raw = data.get("number")
    sender_id = data.get("sender_id")
    status = _normalize_number_status(data.get("status") or "verified")

    if not tenant_slug or not number_raw:
        return jsonify({"error": "tenant_slug y number requeridos"}), 400

    tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
    if not tenant:
        return jsonify({"error": "Tenant no encontrado"}), 404

    number = _normalize_phone(number_raw)
    if not number:
        return jsonify({"error": "Número inválido"}), 400

    sender_value = sender_id or f"whatsapp:{number}"
    tenant.whatsapp_sender_id = sender_value
    owner = tenant.municipio or tenant.pyme
    if owner:
        assign_whatsapp_numbers(owner, [number], activate=True, commit=False)

    twilio_number = TwilioNumber.query.filter_by(phone_number=number).first()
    if not twilio_number:
        twilio_number = TwilioNumber(
            phone_number=number,
            sender_id=sender_value,
            status=status,
            tenant_id=tenant.id,
        )
        db.session.add(twilio_number)
    else:
        twilio_number.sender_id = sender_value
        twilio_number.status = status
        twilio_number.tenant_id = tenant.id

    _log_admin_action(
        current_user.id,
        "register_external_whatsapp",
        tenant_slug,
        {"number": number, "sender_id": tenant.whatsapp_sender_id, "status": status},
    )
    db.session.commit()
    return jsonify({
        "message": "Número registrado correctamente",
        "tenant_slug": tenant.slug,
        "whatsapp_sender_id": tenant.whatsapp_sender_id,
        "number": _serialize_twilio_number(twilio_number),
    })




@super_admin_bp.route('/catalog/quality', methods=['GET'])
@token_requerido
@super_admin_required
def catalog_quality_queue(current_user):
    tenant_slug = str(request.args.get('tenant_slug') or '').strip().lower()
    limit = max(1, min(int(request.args.get('limit', 100) or 100), 250))

    query = CatalogoItem.query
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            return jsonify({"error": "Tenant no encontrado"}), 404
        query = query.filter(CatalogoItem.tenant_id == tenant.id)

    rows = query.order_by(desc(CatalogoItem.timestamp)).limit(limit * 3).all()
    items = []
    for item in rows:
        meta = item.extra_metadata if isinstance(item.extra_metadata, dict) else {}
        confidence = meta.get('confidence_score')
        review_required = bool(meta.get('review_required'))
        quality_issues = meta.get('quality_issues') if isinstance(meta.get('quality_issues'), list) else []
        if not review_required and not quality_issues:
            continue
        items.append({
            "catalog_item_id": item.id,
            "tenant_id": item.tenant_id,
            "user_id": item.user_id,
            "nombre": item.nombre,
            "categoria": item.categoria,
            "precio": item.precio,
            "confidence_score": confidence,
            "review_required": review_required,
            "quality_issues": quality_issues,
            "updated_at": item.timestamp.isoformat() if item.timestamp else None,
        })
        if len(items) >= limit:
            break

    avg_conf = round(sum(float(it.get('confidence_score') or 0) for it in items) / len(items), 3) if items else 0.0
    return jsonify({
        "total": len(items),
        "avg_confidence": avg_conf,
        "items": items,
    })

@super_admin_bp.route('/leads/interactions', methods=['GET'])
@token_requerido
@super_admin_required
def list_leads_interactions(current_user):
    limit = max(1, min(int(request.args.get('limit', 50) or 50), 200))
    tenant_slug_filter = str(request.args.get('tenant_slug') or '').strip().lower()

    contexts = ChatSessionContext.query.order_by(desc(ChatSessionContext.last_updated)).limit(600).all()
    items = []

    for ctx in contexts:
        context_data = ctx.context_data if isinstance(ctx.context_data, dict) else {}
        lead_profile = context_data.get('lead_profile') if isinstance(context_data.get('lead_profile'), dict) else {}

        profile_name = (lead_profile.get('nombre') or context_data.get('profile_name') or '').strip()
        profile_email = (lead_profile.get('email') or '').strip().lower()
        profile_phone = (lead_profile.get('telefono') or '').strip()
        tenant_slug = (lead_profile.get('tenant_slug') or '').strip().lower()

        if tenant_slug_filter and tenant_slug_filter != tenant_slug:
            continue

        if not (profile_name or profile_email or profile_phone or ctx.anon_id):
            continue

        conv_query = Conversacion.query
        if ctx.user_id:
            conv_query = conv_query.filter(Conversacion.user_id == ctx.user_id)
        elif ctx.anon_id:
            conv_query = conv_query.filter(Conversacion.session_id == ctx.anon_id)
        else:
            continue

        recent_conversations = conv_query.order_by(desc(Conversacion.timestamp)).limit(5).all()
        latest_question = recent_conversations[0].pregunta if recent_conversations else (lead_profile.get('mensaje') or '')

        mt_query = MunicipioTicket.query
        pt_query = PymeTicket.query
        if ctx.user_id:
            mt_query = mt_query.filter(MunicipioTicket.user_id == ctx.user_id)
            pt_query = pt_query.filter(PymeTicket.user_id == ctx.user_id)
        elif ctx.anon_id:
            mt_query = mt_query.filter(MunicipioTicket.anon_id == ctx.anon_id)
            pt_query = pt_query.filter(PymeTicket.anon_id == ctx.anon_id)

        open_statuses = {'nuevo', 'abierto', 'pendiente', 'en_proceso'}
        open_tickets = mt_query.filter(MunicipioTicket.estado.in_(list(open_statuses))).count() + pt_query.filter(PymeTicket.estado.in_(list(open_statuses))).count()

        last_seen = ctx.last_updated or (recent_conversations[0].timestamp if recent_conversations else None)
        has_contact = bool(profile_email or profile_phone)
        relevance = _lead_relevance_score(
            open_tickets=open_tickets,
            latest_message=latest_question,
            last_seen=last_seen,
            has_contact=has_contact,
        )
        now = datetime.now(timezone.utc)
        last_dt = last_seen if (last_seen and last_seen.tzinfo) else (last_seen.replace(tzinfo=timezone.utc) if last_seen else None)
        age_seconds = (now - last_dt).total_seconds() if last_dt else 0
        sla_breached = bool(last_dt and age_seconds > 1800 and open_tickets > 0)
        lead_priority = build_lead_portfolio_score(
            latest_message=latest_question,
            has_email=bool(profile_email),
            has_phone=bool(profile_phone),
            open_tickets=open_tickets,
            last_seen=last_seen,
        )

        items.append({
            'chat_session_id': ctx.chat_session_id,
            'anon_id': ctx.anon_id,
            'user_id': ctx.user_id,
            'tenant_slug': tenant_slug or None,
            'lead': {
                'nombre': profile_name or None,
                'email': profile_email or None,
                'telefono': profile_phone or None,
                'interes': lead_profile.get('interes'),
                'mensaje': lead_profile.get('mensaje'),
            },
            'latest_question': latest_question,
            'recent_questions': [c.pregunta for c in recent_conversations if c.pregunta][:3],
            'open_tickets': open_tickets,
            'last_seen': last_seen.isoformat() if last_seen else None,
            'relevance_score': relevance,
            'sla_breached': sla_breached,
            'lead_score': lead_priority['score'],
            'lead_score_breakdown': lead_priority['breakdown'],
            'lead_score_reasons': lead_priority['reasons'],
        })

    items.sort(key=lambda item: ((item.get('relevance_score') or 0), item.get('last_seen') or ''), reverse=True)
    items = items[:limit]

    top_questions = []
    question_rows = (
        db.session.query(Conversacion.pregunta, func.count(Conversacion.id).label('count'))
        .filter(Conversacion.pregunta.isnot(None))
        .filter(func.length(func.trim(Conversacion.pregunta)) > 2)
        .group_by(Conversacion.pregunta)
        .order_by(desc('count'))
        .limit(10)
        .all()
    )
    for question, count in question_rows:
        top_questions.append({'question': question, 'count': int(count or 0)})

    return jsonify({'items': items, 'total': len(items), 'top_questions': top_questions})


LEAD_STAGE_ALLOWED = {"nuevo", "contactado", "calificado", "demo_agendada", "propuesta_enviada", "ganado", "perdido"}


def _load_ticket_for_lead(ticket_type: str, ticket_id: int):
    if ticket_type == "municipio":
        return MunicipioTicket.query.get(ticket_id)
    if ticket_type == "pyme":
        return PymeTicket.query.get(ticket_id)
    return None


def _map_lead_stage_to_ticket_status(stage: str) -> str:
    mapping = {
        "nuevo": "nuevo",
        "contactado": "en_proceso",
        "calificado": "en_proceso",
        "demo_agendada": "pendiente",
        "propuesta_enviada": "pendiente",
        "ganado": "cerrado",
        "perdido": "cancelado",
    }
    return mapping.get(stage, "en_proceso")


def _ensure_ticket_details_dict(ticket) -> dict:
    details = ticket.detalles
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


@super_admin_bp.route('/leads/<ticket_type>/<int:ticket_id>/stage', methods=['PATCH'])
@token_requerido
@super_admin_required
def super_admin_update_lead_stage(current_user, ticket_type: str, ticket_id: int):
    payload = request.get_json(silent=True) or {}
    stage = str(payload.get('stage') or '').strip().lower()
    note = str(payload.get('note') or '').strip()

    if stage not in LEAD_STAGE_ALLOWED:
        return jsonify({"error": "stage inválido"}), 400

    ticket = _load_ticket_for_lead(ticket_type, ticket_id)
    if not ticket:
        return jsonify({"error": "Lead no encontrado"}), 404

    details = _ensure_ticket_details_dict(ticket)
    timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []
    previous_stage = details.get('lead_stage') or ticket.estado or 'nuevo'

    details['lead_stage'] = stage
    timeline.append({
        'at': datetime.now(timezone.utc).isoformat(),
        'by_user_id': current_user.id,
        'event': 'stage_update',
        'from': previous_stage,
        'to': stage,
        'note': note or None,
    })
    details['lead_timeline'] = timeline[-100:]

    ticket.estado = _map_lead_stage_to_ticket_status(stage)
    ticket.detalles = json.dumps(details, ensure_ascii=False)
    ticket.ultima_actividad = datetime.now(timezone.utc)

    _log_admin_action(current_user.id, 'lead_stage_update', f'{ticket_type}:{ticket_id}', {
        'stage': stage,
        'previous_stage': previous_stage,
        'note': note or None,
    })
    db.session.commit()
    return jsonify({'ok': True, 'ticket_id': ticket.id, 'ticket_type': ticket_type, 'lead_stage': stage, 'ticket_status': ticket.estado})


@super_admin_bp.route('/leads/bulk-stage', methods=['PATCH'])
@token_requerido
@super_admin_required
def super_admin_bulk_stage(current_user):
    payload = request.get_json(silent=True) or {}
    stage = str(payload.get('stage') or '').strip().lower()
    updates = payload.get('updates') if isinstance(payload.get('updates'), list) else []

    if stage not in LEAD_STAGE_ALLOWED:
        return jsonify({"error": "stage inválido"}), 400
    if not updates:
        return jsonify({"error": "updates requerido"}), 400

    changed = 0
    errors = []
    for row in updates[:200]:
        if not isinstance(row, dict):
            continue
        t_type = str(row.get('ticket_type') or '').strip().lower()
        t_id = row.get('ticket_id')
        note = str(row.get('note') or '').strip()
        try:
            t_id = int(t_id)
        except Exception:
            errors.append({'ticket_type': t_type, 'ticket_id': row.get('ticket_id'), 'error': 'ticket_id inválido'})
            continue

        ticket = _load_ticket_for_lead(t_type, t_id)
        if not ticket:
            errors.append({'ticket_type': t_type, 'ticket_id': t_id, 'error': 'no encontrado'})
            continue

        details = _ensure_ticket_details_dict(ticket)
        timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []
        prev = details.get('lead_stage') or ticket.estado or 'nuevo'
        details['lead_stage'] = stage
        timeline.append({
            'at': datetime.now(timezone.utc).isoformat(),
            'by_user_id': current_user.id,
            'event': 'bulk_stage_update',
            'from': prev,
            'to': stage,
            'note': note or None,
        })
        details['lead_timeline'] = timeline[-100:]
        ticket.estado = _map_lead_stage_to_ticket_status(stage)
        ticket.detalles = json.dumps(details, ensure_ascii=False)
        ticket.ultima_actividad = datetime.now(timezone.utc)
        changed += 1

    _log_admin_action(current_user.id, 'lead_bulk_stage_update', 'bulk', {'stage': stage, 'changed': changed, 'errors': len(errors)})
    db.session.commit()
    return jsonify({'ok': True, 'changed': changed, 'errors': errors})


@super_admin_bp.route('/leads/<ticket_type>/<int:ticket_id>/timeline', methods=['GET', 'POST'])
@token_requerido
@super_admin_required
def super_admin_lead_timeline(current_user, ticket_type: str, ticket_id: int):
    ticket = _load_ticket_for_lead(ticket_type, ticket_id)
    if not ticket:
        return jsonify({"error": "Lead no encontrado"}), 404

    details = _ensure_ticket_details_dict(ticket)
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
        'event': 'note',
        'note': note[:1000],
    })
    details['lead_timeline'] = timeline[-100:]
    ticket.detalles = json.dumps(details, ensure_ascii=False)
    ticket.ultima_actividad = datetime.now(timezone.utc)

    _log_admin_action(current_user.id, 'lead_note_add', f'{ticket_type}:{ticket_id}', {'note': note[:200]})
    db.session.commit()
    return jsonify({'ok': True, 'ticket_id': ticket.id, 'ticket_type': ticket_type, 'timeline': details['lead_timeline']})


@super_admin_bp.route('/leads/playbooks/run', methods=['POST'])
@token_requerido
@super_admin_required
def run_lead_playbooks(current_user):
    payload = request.get_json(silent=True) or {}
    only_sla_breached = bool(payload.get('only_sla_breached', True))
    limit = max(1, min(int(payload.get('limit', 50) or 50), 200))
    dry_run = bool(payload.get('dry_run', False))

    now = datetime.now(timezone.utc)
    candidates = []

    m_tickets = MunicipioTicket.query.order_by(desc(MunicipioTicket.ultima_actividad)).limit(limit * 4).all()
    p_tickets = PymeTicket.query.order_by(desc(PymeTicket.fecha)).limit(limit * 4).all()

    for ticket_type, rows in (("municipio", m_tickets), ("pyme", p_tickets)):
        for t in rows:
            last_seen = getattr(t, 'ultima_actividad', None) or getattr(t, 'fecha', None)
            if not last_seen:
                continue
            dt = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
            age_seconds = (now - dt).total_seconds()
            if only_sla_breached and age_seconds <= 1800:
                continue

            details = _ensure_ticket_details_dict(t)
            stage = str(details.get('lead_stage') or t.estado or 'nuevo').lower()
            if stage in {'ganado', 'perdido'}:
                continue

            name = getattr(t, 'nombre_vecino', None) or getattr(t, 'nombre_cliente', None) or 'Cliente'
            phone = getattr(t, 'telefono_vecino', None) or getattr(t, 'telefono_cliente', None)
            email = getattr(t, 'email_vecino', None) or getattr(t, 'email_cliente', None)
            categoria = getattr(t, 'categoria', None) or 'servicio'

            actions = []
            if phone:
                actions.append({
                    'channel': 'whatsapp',
                    'message': f"Hola {name}, soy del equipo comercial de Chatboc. Vimos tu interés en {categoria}. ¿Te comparto propuesta y próximos pasos?",
                    'status': 'queued' if not dry_run else 'preview',
                })
            if email:
                actions.append({
                    'channel': 'email',
                    'subject': 'Seguimiento de tu interés en Chatboc',
                    'message': f"Hola {name}, gracias por tu interés en Chatboc para {categoria}. ¿Coordinamos una demo de 15 minutos?",
                    'status': 'queued' if not dry_run else 'preview',
                })

            if not actions:
                continue

            if not dry_run:
                timeline = details.get('lead_timeline') if isinstance(details.get('lead_timeline'), list) else []
                timeline.append({
                    'at': now.isoformat(),
                    'by_user_id': current_user.id,
                    'event': 'playbook_followup_queued',
                    'channels': [a['channel'] for a in actions],
                    'sla_breached': age_seconds > 1800,
                })
                details['lead_timeline'] = timeline[-100:]
                t.detalles = json.dumps(details, ensure_ascii=False)

            candidates.append({
                'ticket_type': ticket_type,
                'ticket_id': t.id,
                'stage': stage,
                'sla_breached': age_seconds > 1800,
                'actions': actions,
            })
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    if not dry_run:
        _log_admin_action(current_user.id, 'lead_playbooks_run', 'playbooks', {
            'count': len(candidates),
            'only_sla_breached': only_sla_breached,
        })
        db.session.commit()

    return jsonify({'ok': True, 'dry_run': dry_run, 'count': len(candidates), 'items': candidates})


@super_admin_bp.route('/leads/strategic-overview', methods=['GET'])
@token_requerido
@super_admin_required
def leads_strategic_overview(current_user):
    since_days = max(1, min(int(request.args.get('since_days', 30) or 30), 365))
    return jsonify(_build_strategic_overview_payload(since_days=since_days))




@super_admin_bp.route('/analytics/realtime-ai', methods=['GET'])
@token_requerido
@super_admin_required
def super_admin_realtime_ai_metrics(current_user):
    minutes = max(5, min(int(request.args.get('minutes', 60) or 60), 24 * 60))
    return jsonify(_build_realtime_ai_payload(minutes=minutes))


@super_admin_bp.route('/encuestas/overview', methods=['GET'])
@token_requerido
@super_admin_required
def super_admin_surveys_overview(current_user):
    since_days = max(1, min(int(request.args.get('since_days', 30) or 30), 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    encuestas = EncEncuesta.query.filter(EncEncuesta.updated_at >= cutoff).all()
    by_tenant = {}
    total_responses = 0
    for enc in encuestas:
        count_resp = EncRespuesta.query.filter_by(encuesta_id=enc.id).count()
        total_responses += count_resp
        key = str(enc.tenant_id)
        if key not in by_tenant:
            by_tenant[key] = {'tenant_id': enc.tenant_id, 'surveys': 0, 'responses': 0, 'live_votings': 0}
        by_tenant[key]['surveys'] += 1
        by_tenant[key]['responses'] += count_resp
        if enc.es_votacion_envivo:
            by_tenant[key]['live_votings'] += 1

    return jsonify({
        'since_days': since_days,
        'total_surveys': len(encuestas),
        'total_responses': total_responses,
        'by_tenant': list(by_tenant.values()),
    })



@super_admin_bp.route('/analytics/tenant-health', methods=['GET'])
@token_requerido
@super_admin_required
def super_admin_tenant_health(current_user):
    since_days = max(1, min(int(request.args.get('since_days', 30) or 30), 365))
    return jsonify(_build_tenant_health_payload(since_days=since_days))


@super_admin_bp.route('/tenants/<string:slug>/profile-360', methods=['GET'])
@token_requerido
@super_admin_required
def super_admin_tenant_profile_360(current_user, slug):
    since_days = max(1, min(int(request.args.get('since_days', 30) or 30), 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    snapshot = _build_tenant_health_snapshot(tenant, cutoff=cutoff)

    return jsonify({
        'since_days': since_days,
        'tenant': {
            'id': tenant.id,
            'slug': tenant.slug,
            'nombre': tenant.nombre,
            'tipo': tenant.tipo,
            'plan': tenant.plan,
            'dominio': tenant.dominio,
            'logo_url': tenant.logo_url,
            'whatsapp_sender_id': tenant.whatsapp_sender_id,
            'is_active': bool(getattr(tenant, 'is_active', True)),
        },
        'owner': snapshot['owner'],
        'health': snapshot['health'],
        'metrics': snapshot['metrics'],
        'onboarding': snapshot['onboarding'],
        'meta': snapshot['meta'],
    })

@super_admin_bp.route('/analytics/heatmap-categories-zones', methods=['GET'])
@token_requerido
@super_admin_required
def super_admin_heatmap_categories_zones(current_user):
    since_days = max(1, min(int(request.args.get('since_days', 30) or 30), 365))
    return jsonify(_build_heatmap_categories_payload(since_days=since_days))


@super_admin_bp.route('/analytics/executive-summary', methods=['GET'])
@token_requerido
@super_admin_required
def super_admin_executive_summary(current_user):
    since_days = max(1, min(int(request.args.get('since_days', 30) or 30), 365))
    minutes = max(5, min(int(request.args.get('minutes', 60) or 60), 24 * 60))

    strategic = _build_strategic_overview_payload(since_days=since_days)
    tenant_health = _build_tenant_health_payload(since_days=since_days)
    realtime = _build_realtime_ai_payload(minutes=minutes)
    heatmap = _build_heatmap_categories_payload(since_days=since_days)

    risky_tenants = [
        item for item in tenant_health.get('items', [])
        if item.get('sla_breached') or item.get('health_score', 0) < 50
    ][:5]
    healthiest_tenants = tenant_health.get('items', [])[:5]

    recommended_actions = []
    if strategic.get('alerts'):
        recommended_actions.append({
            'kind': 'review_backlog',
            'priority': 'high',
            'message': 'Revisar backlog/SLA desde strategic-overview.',
        })
    if realtime.get('tickets', {}).get('unassigned'):
        recommended_actions.append({
            'kind': 'assign_unassigned_tickets',
            'priority': 'high',
            'message': 'Hay tickets recientes sin asignar en el monitoreo realtime.',
        })
    if risky_tenants:
        recommended_actions.append({
            'kind': 'contact_risky_tenants',
            'priority': 'medium',
            'message': 'Hay tenants con health score bajo o SLA roto.',
        })

    return jsonify({
        'since_days': since_days,
        'minutes': minutes,
        'strategic_overview': strategic,
        'tenant_health': {
            'total_tenants': tenant_health.get('total_tenants', 0),
            'top_healthy': healthiest_tenants,
            'top_risky': risky_tenants,
        },
        'realtime': realtime,
        'heatmap': {
            'top_categories': heatmap.get('top_categories', [])[:5],
            'top_zones': heatmap.get('top_zones', [])[:5],
            'total_points': len(heatmap.get('heatmap_points', [])),
        },
        'recommended_actions': recommended_actions,
    })

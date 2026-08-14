from __future__ import annotations
from flask import Blueprint, jsonify, request, g, abort, current_app, url_for
from sqlalchemy import or_, and_, func
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

from models import MunicipioPost, CatalogoItem, TenantTicket, MarketOrder, MarketOrderItem, OrderEvent, User, TenantProfile, TenantFollower, WidgetConfig, EncEncuesta, EncRespuesta, PointsTransaction, Promocion, SugerenciaCiudadano, Notification, AdminAuditLog
from extensions import db
from services.tenant_resolver import resolve_tenant_only, TenantResolutionError
from services.rewards import recompensas_service
from services.demo_experience_contract import build_demo_experience_contract
from routes.public_resolver import _build_widget_embed_payload, _canonical_widget_token
from utils.auth_decorators import require_auth_optional, require_auth
from routes.catalogo import _formatear_producto
from services.encuestas_service import (
    EncuestaError,
    get_public_encuesta_by_id,
    list_public_encuestas_for_tenant,
    resolve_survey_submission_id,
    save_respuesta,
    serialize_public_encuesta,
    survey_response_receipt_contract,
)
from services.survey_eligibility import SURVEY_ELIGIBILITY_CREDENTIAL_HEADER
from services.survey_tenant_scope import (
    SURVEY_TENANT_SCOPE_CONTRACT_VERSION,
    SurveyTenantScopeError,
    resolve_survey_tenant_scope_id,
)

portal_api_bp = Blueprint('portal_api', __name__)


@portal_api_bp.errorhandler(SurveyTenantScopeError)
def _handle_survey_tenant_scope_error(error: SurveyTenantScopeError):
    return (
        jsonify(
            {
                "contract_version": SURVEY_TENANT_SCOPE_CONTRACT_VERSION,
                "ok": False,
                "reason_code": error.reason_code,
                "retryable": False,
                "action_hint": "contact_tenant_administrator",
                "error": "La participacion ciudadana no esta disponible temporalmente.",
            }
        ),
        503,
    )

def _resolve_context(tenant_slug):
    try:
        # Includes lazy demo creation if applicable
        tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
    except TenantResolutionError:
        abort(404, "Tenant no encontrado")

    g.tenant = tenant
    return tenant

def _get_owner_id(tenant):
    owner = tenant.municipio or tenant.pyme
    return owner.id if owner else None


def _survey_scope_ids(tenants: list[TenantProfile]) -> list[int]:
    return list(
        dict.fromkeys(resolve_survey_tenant_scope_id(tenant) for tenant in tenants)
    )


def _order_status_label(status: str | None) -> str:
    mapping = {
        "pending": "Pendiente",
        "confirmed": "Confirmado",
        "paid": "Pagado",
        "preparing": "En preparación",
        "ready": "Listo para retirar",
        "shipped": "En camino",
        "delivered": "Entregado",
        "cancelled": "Cancelado",
    }
    key = (status or "pending").strip().lower()
    return mapping.get(key, key.replace("_", " ").capitalize())


def _serialize_order_event(event: OrderEvent) -> dict:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return {
        "id": event.id,
        "type": event.type,
        "label": payload.get("label") or _order_status_label(payload.get("status") if event.type == "status_changed" else event.type),
        "status": payload.get("status"),
        "message": payload.get("message"),
        "at": event.created_at.isoformat() if event.created_at else None,
    }


def _to_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _resolve_tracking_stage(status: str | None) -> str:
    key = (status or "pending").strip().lower()
    if key in {"delivered"}:
        return "delivered"
    if key in {"shipped", "ready"}:
        return "shipped"
    if key in {"cancelled"}:
        return "cancelled"
    return "preparing"


def _estimate_eta(order: MarketOrder, stage: str, timeline: list[dict]) -> str | None:
    if stage == "delivered":
        delivered = [ev for ev in timeline if (ev.get("status") or "").strip().lower() == "delivered"]
        if delivered:
            return delivered[-1].get("at")
        return _to_iso(order.updated_at) or _to_iso(order.created_at)

    if stage == "cancelled":
        return None

    if order.metadata_payload and isinstance(order.metadata_payload, dict):
        eta = order.metadata_payload.get("eta") or order.metadata_payload.get("estimated_delivery_at")
        if isinstance(eta, str) and eta.strip():
            return eta

    base = order.created_at or datetime.now(timezone.utc)
    delta = timedelta(hours=2) if stage == "shipped" else timedelta(hours=24)
    return (base + delta).isoformat()


def _portal_benefits(tenant: TenantProfile) -> list[dict]:
    slug = (tenant.slug or "").replace("-", " ").title() or "Comunidad"
    return [
        {"id": "free_shipping", "title": "Envío gratis", "description": f"Envío bonificado en compras de {slug}", "cost_points": 400, "type": "shipping"},
        {"id": "discount_10", "title": "10% OFF", "description": "Descuento aplicable en tu próximo pedido", "cost_points": 700, "type": "discount"},
        {"id": "gift_pack", "title": "Pack regalo", "description": "Canjeá un kit promocional sujeto a stock", "cost_points": 1200, "type": "gift"},
    ]


def _normalize_limit(raw_value, default=20, max_limit=100) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, max_limit))






def _supported_languages() -> list[dict]:
    return [
        {"code": "es", "label": "Español", "locale": "es-AR"},
        {"code": "en", "label": "English", "locale": "en-US"},
        {"code": "pt", "label": "Português", "locale": "pt-BR"},
    ]


def _portal_demo_menu_for_tenant(tenant: TenantProfile) -> list[dict]:
    tipo = (getattr(tenant, "tipo", "") or "").strip().lower()
    if tipo == "municipio":
        return [
            {"id": "demo_reclamo", "label": "Crear reclamo", "intent": "iniciar_reclamo"},
            {"id": "demo_sugerencia", "label": "Crear sugerencia", "intent": "enviar_sugerencia"},
            {"id": "demo_estado", "label": "Ver estado ticket", "intent": "consultar_ticket"},
            {"id": "demo_heatmap", "label": "Ver mapa de calor", "intent": "analytics_heatmap"},
            {"id": "demo_encuesta", "label": "Responder encuesta", "intent": "encuestas_publicas"},
        ]
    return [
        {"id": "demo_catalogo", "label": "Ver catálogo", "intent": "ver_catalogo"},
        {"id": "demo_pedido", "label": "Crear pedido", "intent": "crear_pedido"},
        {"id": "demo_estado_pedido", "label": "Ver estado pedido", "intent": "estado_pedido"},
        {"id": "demo_pdf", "label": "Subir PDF catálogo", "intent": "subir_catalogo_pdf"},
        {"id": "demo_excel", "label": "Subir Excel catálogo", "intent": "subir_catalogo_excel"},
    ]


def _portal_twilio_trial_contract() -> dict:
    phrase = "join brief-yesterday"
    number_e164 = "+14155238886"
    return {
        "enabled": True,
        "display_number": "+1 (415) 523-8886",
        "number_e164": number_e164,
        "join_phrase": phrase,
        "wa_deeplink": f"https://wa.me/{number_e164[1:]}?text={quote_plus(phrase)}",
        "cta_label": "Activar prueba WhatsApp",
        "limits": {
            "max_messages": 10,
            "upgrade_required_for": [
                "qdrant_catalogo_completo",
                "automatizaciones_enterprise",
            ],
            "upgrade_message": "Límite demo alcanzado. Activá plan Full para continuar.",
        },
    }


def _tenant_demo_activation_state(tenant: TenantProfile) -> dict:
    cfg = tenant.configuracion or {}
    demo_cfg = cfg.get("demo_trial") if isinstance(cfg.get("demo_trial"), dict) else {}
    activation_count = int(demo_cfg.get("activation_count") or 0)
    return {
        "active": bool(demo_cfg.get("active")),
        "activated_at": demo_cfg.get("activated_at"),
        "expires_at": demo_cfg.get("expires_at"),
        "activation_count": activation_count,
        "max_activations": 1,
        "can_activate": activation_count < 1 or bool(demo_cfg.get("active")),
        "last_actor_user_id": demo_cfg.get("last_actor_user_id"),
    }


def _activate_tenant_demo_trial(tenant: TenantProfile, actor_user_id: int | None) -> dict:
    now = datetime.now(timezone.utc)
    cfg = tenant.configuracion.copy() if isinstance(tenant.configuracion, dict) else {}
    demo_cfg = cfg.get("demo_trial") if isinstance(cfg.get("demo_trial"), dict) else {}
    activation_count = int(demo_cfg.get("activation_count") or 0)
    if activation_count >= 1 and not bool(demo_cfg.get("active")):
        return {
            "ok": False,
            "error": "demo_activation_limit_reached",
            "message": "La activación demo ya fue utilizada para este tenant.",
            "state": _tenant_demo_activation_state(tenant),
        }

    if not bool(demo_cfg.get("active")):
        activation_count += 1

    demo_cfg.update(
        {
            "active": True,
            "activated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
            "activation_count": activation_count,
            "last_actor_user_id": actor_user_id,
        }
    )
    cfg["demo_trial"] = demo_cfg
    tenant.configuracion = cfg
    db.session.add(tenant)
    db.session.commit()
    return {"ok": True, "state": _tenant_demo_activation_state(tenant)}


def _notify_superadmin_hot_lead(tenant: TenantProfile, actor: User | None, activation_state: dict) -> None:
    super_admins = User.query.filter_by(rol="super_admin").all()
    if not super_admins:
        return

    actor_name = getattr(actor, "name", None) or getattr(actor, "email", None) or "Usuario"
    body = (
        f"Hot lead SaaS: '{tenant.nombre}' ({tenant.slug}) activó demo WhatsApp. "
        f"Actor: {actor_name}. Activaciones: {activation_state.get('activation_count')}/{activation_state.get('max_activations')}. "
        f"Estado: {'activo' if activation_state.get('active') else 'inactivo'}."
    )

    for sa in super_admins:
        idempotency_key = f"sales_hot_lead:{tenant.id}:{activation_state.get('activation_count')}:{sa.id}"
        existing = Notification.query.filter_by(tenant_id=tenant.id, idempotency_key=idempotency_key).first()
        if existing:
            continue
        db.session.add(
            Notification(
                tenant_id=tenant.id,
                user_id=sa.id,
                channel="in_app",
                recipient=(sa.email or f"superadmin-{sa.id}@chatboc.local"),
                subject="Lead caliente: demo activado",
                body=body,
                status="sent",
                idempotency_key=idempotency_key,
                metadata_json={
                    "event": "tenant_demo_whatsapp_activated",
                    "tenant_slug": tenant.slug,
                    "tenant_type": tenant.tipo,
                    "actor_user_id": getattr(actor, "id", None),
                    "activation_state": activation_state,
                },
            )
        )

    if actor:
        db.session.add(
            AdminAuditLog(
                admin_user_id=actor.id,
                action="tenant_demo_whatsapp_activated",
                target_object=tenant.slug,
                details={
                    "tenant_id": tenant.id,
                    "tenant_type": tenant.tipo,
                    "activation_state": activation_state,
                    "sales_signal": "hot_lead",
                },
                ip_address=request.headers.get("X-Forwarded-For") or request.remote_addr,
            )
        )
    db.session.commit()

def _classify_points_source(tipo: str | None) -> str:
    key = (tipo or "").strip().lower()
    if not key:
        return "otros"
    if "compra" in key or "order" in key or "pedido" in key:
        return "compras"
    if "encuesta" in key:
        return "encuestas"
    if "vot" in key:
        return "votaciones"
    if "suger" in key:
        return "sugerencias"
    if "reclamo" in key or "ticket" in key:
        return "reclamos"
    if "redeem" in key or "canje" in key:
        return "canjes"
    return "participacion"


def _build_points_breakdown(points_tx: list[PointsTransaction]) -> dict:
    breakdown = {
        "participacion": 0,
        "compras": 0,
        "encuestas": 0,
        "votaciones": 0,
        "sugerencias": 0,
        "reclamos": 0,
        "canjes": 0,
        "otros": 0,
    }
    for tx in points_tx:
        bucket = _classify_points_source(getattr(tx, "tipo", None))
        breakdown[bucket] = breakdown.get(bucket, 0) + int(tx.delta or 0)
    return breakdown


def _resolve_followed_tenants(user: User, current_tenant: TenantProfile) -> list[TenantProfile]:
    followed_ids = [
        follower.tenant_id
        for follower in TenantFollower.query.filter_by(user_id=user.id).all()
        if follower.tenant_id
    ]
    tenant_ids = list(dict.fromkeys([current_tenant.id] + followed_ids))
    if not tenant_ids:
        return [current_tenant]
    return TenantProfile.query.filter(TenantProfile.id.in_(tenant_ids)).all()



def _is_truthy(raw_value) -> bool:
    if raw_value is None:
        return False
    return str(raw_value).strip().lower() in {"1", "true", "yes", "si", "on"}


def _resolve_portal_scope(user: User, current_tenant: TenantProfile, include_network: bool) -> tuple[list[TenantProfile], list[int], list[int]]:
    tenants = _resolve_followed_tenants(user, current_tenant) if include_network else [current_tenant]
    tenant_ids = [tenant.id for tenant in tenants if tenant and tenant.id]
    owner_ids = [_get_owner_id(tenant) for tenant in tenants if _get_owner_id(tenant)]
    return tenants, tenant_ids, owner_ids

def _serialize_portal_order(order: MarketOrder, include_timeline: bool = False) -> dict:
    items = [
        {
            "id": item.id,
            "product_id": item.product_id,
            "name": item.name_snapshot or (item.product.nombre if item.product else None),
            "quantity": item.quantity,
            "price_monetary": float(item.price_monetary or 0),
            "price_points": item.price_points,
            "currency": item.currency,
            "modalidad": item.modalidad,
        }
        for item in (order.items or [])
    ]

    latest_event = order.events.order_by(OrderEvent.created_at.desc()).first() if hasattr(order, "events") else None
    timeline = []
    if include_timeline and hasattr(order, "events"):
        timeline = [
            _serialize_order_event(ev)
            for ev in order.events.order_by(OrderEvent.created_at.asc()).all()
        ]

    stage = _resolve_tracking_stage(order.status)

    return {
        "id": order.id,
        "status": order.status,
        "status_label": _order_status_label(order.status),
        "total": float(order.total_monetary or 0),
        "total_points": int(order.total_points or 0),
        "currency": order.currency or "ARS",
        "date": order.created_at.isoformat() if order.created_at else None,
        "items_count": len(items),
        "items": items if include_timeline else None,
        "tracking": {
            "stage": stage,
            "eta": _estimate_eta(order, stage, timeline),
            "has_timeline": bool(timeline),
            "latest_event": _serialize_order_event(latest_event) if latest_event else None,
            "timeline": timeline,
        },
    }


def _serialize_portal_claim(ticket: TenantTicket) -> dict:
    datos_extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    return {
        "id": str(ticket.id),
        "title": ticket.categoria or "Reclamo",
        "description": ticket.descripcion,
        "status": ticket.estado,
        "channel": ticket.origen,
        "priority": datos_extra.get("priority") or datos_extra.get("sla_status") or "normal",
        "date": _to_iso(ticket.created_at),
        "updated_at": _to_iso(ticket.updated_at),
    }


def _serialize_points_transaction(tx: PointsTransaction) -> dict:
    metadata = tx.metadata_payload if isinstance(tx.metadata_payload, dict) else {}
    return {
        "id": tx.id,
        "fecha": _to_iso(tx.created_at),
        "tipo": tx.tipo,
        "delta": tx.delta,
        "saldo_final": tx.saldo_final,
        "benefit_id": metadata.get("benefit_id"),
        "benefit_title": metadata.get("benefit_title"),
        "detalle": metadata.get("detalle", tx.tipo),
    }


def _portal_membership_tier(balance: int) -> dict:
    if balance >= 2500:
        return {
            "id": "black",
            "label": "Black",
            "theme": "obsidian",
            "next_tier_points": None,
        }
    if balance >= 1200:
        return {
            "id": "gold",
            "label": "Gold",
            "theme": "gold",
            "next_tier_points": 2500 - balance,
        }
    if balance >= 400:
        return {
            "id": "silver",
            "label": "Silver",
            "theme": "graphite",
            "next_tier_points": 1200 - balance,
        }
    return {
        "id": "starter",
        "label": "Starter",
        "theme": "indigo",
        "next_tier_points": 400 - balance,
    }


def _portal_promotions(tenant: TenantProfile, *, limit: int = 6) -> list[dict]:
    owner = tenant.municipio or tenant.pyme
    owner_id = getattr(owner, "id", None)
    cards: list[dict] = []

    if owner_id:
        promos = (
            Promocion.query.filter_by(pyme_user_id=owner_id, is_active=True)
            .order_by(Promocion.updated_at.desc())
            .limit(limit)
            .all()
        )
        for promo in promos:
            cards.append(
                {
                    "id": promo.id,
                    "title": promo.nombre_promocion,
                    "description": promo.descripcion_publica or promo.tipo_promocion,
                    "type": "structured",
                    "discount_type": promo.tipo_promocion,
                    "discount_value": promo.valor_descuento,
                    "starts_at": _to_iso(promo.fecha_inicio),
                    "ends_at": _to_iso(promo.fecha_fin),
                    "code": promo.codigo_promocion,
                }
            )

    remaining = max(0, limit - len(cards))
    if remaining:
        items = (
            CatalogoItem.query.filter(
                CatalogoItem.tenant_id == tenant.id,
                CatalogoItem.promocion_info.isnot(None),
            )
            .order_by(CatalogoItem.timestamp.desc())
            .limit(remaining * 2)
            .all()
        )
        seen_titles = {card["title"] for card in cards}
        for item in items:
            if item.nombre in seen_titles:
                continue
            cards.append(
                {
                    "id": f"catalog:{item.id}",
                    "title": item.nombre,
                    "description": item.promocion_info,
                    "type": "catalog",
                    "category": item.categoria,
                    "image_url": item.imagen_url,
                    "price_label": item.precio,
                }
            )
            seen_titles.add(item.nombre)
            if len(cards) >= limit:
                break

    return cards[:limit]


def _portal_available_surveys(tenant: TenantProfile, user: User, *, limit: int = 6) -> list[dict]:
    survey_scope_id = resolve_survey_tenant_scope_id(tenant)
    surveys = list_public_encuestas_for_tenant(survey_scope_id, limit=max(limit * 2, 10))
    answered_ids = {
        enc_id
        for (enc_id,) in db.session.query(EncRespuesta.encuesta_id)
        .filter(
            EncRespuesta.user_id == user.id,
            EncRespuesta.tenant_id == survey_scope_id,
        )
        .all()
    }

    items: list[dict] = []
    for encuesta, slug in surveys:
        if encuesta.id in answered_ids:
            continue
        serialized = serialize_public_encuesta(encuesta)
        items.append(
            {
                "id": encuesta.id,
                "slug": slug,
                "title": serialized.get("titulo") or encuesta.titulo,
                "description": serialized.get("descripcion") or encuesta.descripcion,
                "kind": serialized.get("tipo") or encuesta.tipo,
                "estimated_reward_points": 150 if not getattr(encuesta, "es_votacion_envivo", False) else 75,
                "link": f"/portal/encuestas/{slug}",
            }
        )
        if len(items) >= limit:
            break
    return items

def _generate_notifications(user, tenant, limit=10):
    notifications = []

    # 1. Ticket Updates (Last 7 days) - Only if user is logged in
    if user:
        recent_tickets = TenantTicket.query.filter(
            TenantTicket.user_id == user.id,
            TenantTicket.tenant_id == tenant.id,
            TenantTicket.updated_at >= datetime.now(timezone.utc) - timedelta(days=7)
        ).order_by(TenantTicket.updated_at.desc()).limit(limit).all()

        for t in recent_tickets:
            notifications.append({
                "id": f"ticket_update_{t.id}_{int(t.updated_at.timestamp())}",
                "title": "Actualización de Reclamo",
                "message": f"Tu reclamo #{t.id} está en estado: {t.estado}",
                "severity": "info",
                "date": t.updated_at.isoformat(),
                "read": False, # Mock until read state is tracked
                "actionLabel": "Ver Reclamo",
                "actionHref": f"/{tenant.slug}/pedidos/{t.id}"
            })

    # 2. New Content (News/Events - Last 3 days)
    # This is relevant for all users, but personalized if we had subscriptions
    owner_id = _get_owner_id(tenant)
    if owner_id:
        recent_posts = MunicipioPost.query.filter(
            MunicipioPost.municipio_id == owner_id,
            MunicipioPost.fecha_publicacion >= datetime.now(timezone.utc) - timedelta(days=3)
        ).order_by(MunicipioPost.fecha_publicacion.desc()).limit(limit).all()

        for p in recent_posts:
            type_label = "Evento" if p.tipo_post == 'evento' else "Noticia"
            route_segment = 'eventos' if p.tipo_post == 'evento' else 'noticias'
            notifications.append({
                "id": f"post_new_{p.id}",
                "title": f"Nuevo {type_label}: {p.titulo}",
                "message": p.subtitulo or (p.descripcion[:50] + "..." if p.descripcion else ""),
                "severity": "success",
                "date": p.fecha_publicacion.isoformat(),
                "read": False,
                "actionLabel": "Ver",
                "actionHref": f"/{tenant.slug}/{route_segment}/{p.id}"
            })

    # Sort combined
    notifications.sort(key=lambda x: x['date'], reverse=True)
    return notifications[:limit]

def _get_theme_config(tenant):
    """Resolve theme configuration merging TenantProfile and WidgetConfig."""
    theme = tenant.tema or {}
    if not isinstance(theme, dict):
        theme = {}

    # Merge with widget config if available (often has the primary colors set by admin)
    if tenant.widget_config:
        if tenant.widget_config.primary_color:
            theme["primaryColor"] = tenant.widget_config.primary_color
        if tenant.widget_config.accent_color:
            theme["secondaryColor"] = tenant.widget_config.accent_color

    return theme

def _get_executive_summary(tenant, user):
    """Generate mock executive data for demo tenants to showcase analytics."""
    if not tenant or not tenant.tipo or tenant.tipo.lower() != 'pyme':
        return None

    # Mock data varying slightly by tenant name hash to look persistent but random
    base_seed = sum(ord(c) for c in tenant.slug)

    return {
        "stockValue": 1500000 + (base_seed * 100),
        "totalUnits": 350 + (base_seed % 50),
        "inventoryDays": 12 + (base_seed % 5),
        "criticalItems": [
            {"name": "Item A - Reponer", "stock": 2},
            {"name": "Item B - Bajo", "stock": 5}
        ],
        "stockFlow": [
            {"date": "2023-10-01", "value": 100},
            {"date": "2023-10-02", "value": 120},
            {"date": "2023-10-03", "value": 115},
            {"date": "2023-10-04", "value": 140}
        ]
    }

@portal_api_bp.route('/content', methods=['GET'])
@require_auth_optional
def get_content(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    owner_id = _get_owner_id(tenant)
    user = getattr(g, "viewer", None)

    # 1. News (limit 5)
    news_items = []
    if owner_id:
        news_query = MunicipioPost.query.filter(
            MunicipioPost.municipio_id == owner_id,
            MunicipioPost.tipo_post != 'evento'
        ).order_by(MunicipioPost.fecha_publicacion.desc()).limit(5)
        news_items = news_query.all()

    # 2. Events (limit 5 upcoming)
    events_items = []
    if owner_id:
        events_query = MunicipioPost.query.filter(
            MunicipioPost.municipio_id == owner_id,
            MunicipioPost.tipo_post == 'evento',
            MunicipioPost.fecha_evento_inicio >= datetime.now(timezone.utc)
        ).order_by(MunicipioPost.fecha_evento_inicio.asc()).limit(5)
        events_items = events_query.all()

    # 3. Notifications (Dynamic)
    notifications = _generate_notifications(user, tenant, limit=5)

    # 4. Loyalty Summary
    loyalty_summary = {
        "points": 0,
        "level": "Estándar",
        "surveysCompleted": 0,
        "suggestionsShared": 0,
        "claimsFiled": 0,
        "enabled": True # Feature flag
    }
    if user:
        loyalty_summary["points"] = recompensas_service().obtener_saldo(user)
        loyalty_summary["claimsFiled"] = TenantTicket.query.filter_by(user_id=user.id, tenant_id=tenant.id).count()

    # 5. Activities (Recent tickets)
    activities = []
    if user:
        recent_tickets = TenantTicket.query.filter_by(
            user_id=user.id, tenant_id=tenant.id
        ).order_by(TenantTicket.updated_at.desc()).limit(5).all()

        for t in recent_tickets:
            activities.append({
                "id": str(t.id),
                "type": "RECLAMO",
                "description": t.descripcion or t.categoria or "Sin descripción",
                "date": t.updated_at.isoformat() if t.updated_at else None,
                "status": t.estado,
                "statusType": "info",
                "link": f"/portal/pedidos/{t.id}"
            })

    # 6. Catalog (Top items)
    catalog_items = CatalogoItem.query.filter(
        CatalogoItem.tenant_id == tenant.id,
        or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None))
    ).limit(6).all()

    catalog_data = []
    for item in catalog_items:
        prod = _formatear_producto({
            "nombre": item.nombre,
            "categoria": item.categoria,
            "descripcion": item.descripcion,
            "precio_str": item.precio,
            "moneda": item.moneda,
            "modalidad": item.modalidad,
            "imagen_url": item.imagen_url
        })
        price_label = prod.get('precio_texto')
        if not price_label:
            if prod.get('moneda') == 'PTS':
                price_label = f"{prod.get('precio_puntos')} PTS"
            else:
                price_label = f"${prod.get('precio_unitario')}"

        try:
            numeric_price = float(prod.get('precio_unitario') or 0)
        except (ValueError, TypeError):
            numeric_price = 0

        catalog_data.append({
            "id": str(item.id),
            "title": item.nombre,
            "description": item.descripcion,
            "category": item.categoria,
            "priceLabel": price_label,
            "price": numeric_price,
            "status": "available",
            "imageUrl": prod.get('imagen_url')
        })

    # 7. Surveys
    surveys_data = []
    survey_scope_id = resolve_survey_tenant_scope_id(tenant)
    encuestas = list_public_encuestas_for_tenant(survey_scope_id, limit=3)
    for enc, slug in encuestas:
        surveys_data.append({
            "id": str(enc.id),
            "title": enc.titulo,
            "link": f"/portal/encuestas/{slug}"
        })

    # 8. Theme and Settings
    theme_config = _get_theme_config(tenant)
    user_settings = {}
    if user and user.accesibilidad:
        user_settings = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}

    # 9. Executive Summary (Mock)
    executive_summary = _get_executive_summary(tenant, user)

    return jsonify({
        "notifications": notifications,
        "news": [{
            "id": str(n.id),
            "title": n.titulo,
            "summary": n.subtitulo or (n.descripcion[:100] if n.descripcion else ""),
            "body": n.descripcion,
            "cover_url": n.imagen_url,
            "publicado_at": n.fecha_publicacion.isoformat(),
            "tags": n.tags or [],
            "category": "General",
            "featured": False,
            "link": f"/{tenant.slug}/noticias/{n.id}",
            # Compat keys
            "coverUrl": n.imagen_url,
            "date": n.fecha_publicacion.isoformat(),
        } for n in news_items],
        "events": [{
            "id": str(e.id),
            "title": e.titulo,
            "descripcion": e.descripcion,
            "starts_at": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "ends_at": e.fecha_evento_fin.isoformat() if e.fecha_evento_fin else None,
            "cover_url": e.imagen_url,
            "lugar": e.ubicacion,
            "status": "inscripcion",
            "link": f"/{tenant.slug}/eventos/{e.id}",
            # Compat keys
            "date": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "location": e.ubicacion,
            "coverUrl": e.imagen_url,
            "spots": 0,
            "registered": 0
        } for e in events_items],
        "catalog": catalog_data,
        "surveys": surveys_data,
        "loyaltySummary": loyalty_summary,
        "activities": activities,
        "executiveSummary": executive_summary,
        "config": {
            "theme": theme_config,
            "userPreferences": user_settings,
            "features": {
                "animations": True,
                "dark_mode": True
            }
        }
    })

@portal_api_bp.route('/settings', methods=['GET', 'PUT'])
@require_auth
def portal_settings(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    if request.method == 'PUT':
        data = request.get_json(silent=True) or {}

        # Update user preferences (stored in 'accesibilidad' column for now)
        prefs = user.accesibilidad or {}
        if not isinstance(prefs, dict):
            prefs = {}

        # Allow updating specific keys
        if 'theme_mode' in data: # 'dark', 'light', 'system'
            prefs['theme_mode'] = data['theme_mode']
        if 'notifications' in data:
            prefs['notifications'] = data['notifications']
        if 'animations_enabled' in data:
            prefs['animations_enabled'] = bool(data['animations_enabled'])
        if 'language' in data:
            language = str(data.get('language') or '').strip().lower()
            allowed = {item['code'] for item in _supported_languages()}
            if language in allowed:
                prefs['language'] = language

        user.accesibilidad = prefs
        db.session.add(user)
        db.session.commit()

        return jsonify({
            "success": True,
            "preferences": prefs
        })

    # GET
    prefs = user.accesibilidad or {}
    if not isinstance(prefs, dict):
        prefs = {}

    # Defaults
    if 'theme_mode' not in prefs:
        prefs['theme_mode'] = 'system'
    if 'animations_enabled' not in prefs:
        prefs['animations_enabled'] = True
    if 'language' not in prefs:
        prefs['language'] = 'es'

    return jsonify({
        "preferences": prefs,
        "tenantTheme": _get_theme_config(tenant),
        "available_languages": _supported_languages(),
    })

@portal_api_bp.route('/news', methods=['GET'])
@require_auth_optional
def get_news(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    owner_id = _get_owner_id(tenant)

    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)

    if not owner_id:
        return jsonify({"data": [], "meta": {"total": 0, "page": page, "limit": limit}})

    query = MunicipioPost.query.filter(
        MunicipioPost.municipio_id == owner_id,
        MunicipioPost.tipo_post != 'evento'
    ).order_by(MunicipioPost.fecha_publicacion.desc())

    pagination = query.paginate(page=page, per_page=limit, error_out=False)

    data = []
    for n in pagination.items:
        data.append({
            "id": str(n.id),
            "title": n.titulo,
            "summary": n.subtitulo or (n.descripcion[:150] if n.descripcion else ""),
            "body": n.descripcion,
            "cover_url": n.imagen_url,
            "publicado_at": n.fecha_publicacion.isoformat(),
            "tags": n.tags or [],
            "category": "General",
            "author": "Admin",
            "featured": False,
            "link": f"/{tenant.slug}/noticias/{n.id}",
            # Compat
            "coverUrl": n.imagen_url,
            "date": n.fecha_publicacion.isoformat(),
            "content": n.descripcion,
        })

    return jsonify({
        "data": data,
        "meta": {
            "total": pagination.total,
            "page": page,
            "limit": limit
        }
    })

@portal_api_bp.route('/events', methods=['GET'])
@require_auth_optional
def get_events(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    owner_id = _get_owner_id(tenant)

    if not owner_id:
        return jsonify({"data": []})

    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)
    status = request.args.get('status', 'upcoming')

    query = MunicipioPost.query.filter(
        MunicipioPost.municipio_id == owner_id,
        MunicipioPost.tipo_post == 'evento'
    )

    if status == 'upcoming':
        query = query.filter(MunicipioPost.fecha_evento_inicio >= datetime.now(timezone.utc))
        query = query.order_by(MunicipioPost.fecha_evento_inicio.asc())
    elif status == 'past':
        query = query.filter(MunicipioPost.fecha_evento_inicio < datetime.now(timezone.utc))
        query = query.order_by(MunicipioPost.fecha_evento_inicio.desc())

    pagination = query.paginate(page=page, per_page=limit, error_out=False)

    data = []
    for e in pagination.items:
        data.append({
            "id": str(e.id),
            "title": e.titulo,
            "descripcion": e.descripcion,
            "starts_at": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "ends_at": e.fecha_evento_fin.isoformat() if e.fecha_evento_fin else None,
            "cover_url": e.imagen_url,
            "lugar": e.ubicacion,
            "spots": 0,
            "registered": 0,
            "status": "inscripcion",
            "user_registered": False,
            "link": f"/{tenant.slug}/eventos/{e.id}",
            # Compat
            "date": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "location": e.ubicacion,
            "coverUrl": e.imagen_url,
        })

    return jsonify({
        "data": data,
        "meta": {
            "total": pagination.total,
            "page": page,
            "limit": limit
        }
    })

@portal_api_bp.route('/catalog', methods=['GET'])
@require_auth_optional
def get_catalog(tenant_slug):
    tenant = _resolve_context(tenant_slug)

    items = CatalogoItem.query.filter(
        CatalogoItem.tenant_id == tenant.id,
        or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None))
    ).all()

    data = []
    for item in items:
        prod = _formatear_producto({
            "nombre": item.nombre,
            "categoria": item.categoria,
            "descripcion": item.descripcion,
            "precio_str": item.precio,
            "moneda": item.moneda,
            "modalidad": item.modalidad,
            "imagen_url": item.imagen_url
        })

        price_label = prod.get('precio_texto')
        if not price_label:
            if prod.get('moneda') == 'PTS':
                price_label = f"{prod.get('precio_puntos')} PTS"
            else:
                price_label = f"${prod.get('precio_unitario')}"

        data.append({
            "id": str(item.id),
            "title": item.nombre,
            "description": item.descripcion,
            "category": item.categoria,
            "imageUrl": prod.get('imagen_url'),
            "priceLabel": price_label,
            "status": "available",
            "formSchema": {},
            "link": f"/{tenant.slug}/productos/{item.id}"
        })

    return jsonify({"data": data})

@portal_api_bp.route('/notifications', methods=['GET'])
@require_auth
def get_notifications(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    notifications = _generate_notifications(user, tenant, limit=20)
    return jsonify(notifications)

@portal_api_bp.route('/notifications/<string:notif_id>/read', methods=['POST'])
@require_auth
def mark_notification_read(tenant_slug, notif_id):
    _resolve_context(tenant_slug)
    # Placeholder logic - in future implement read state storage in DB
    return jsonify({"success": True})

@portal_api_bp.route('/profile', methods=['GET'])
@require_auth
def get_profile(tenant_slug):
    _resolve_context(tenant_slug)
    user = g.viewer

    points = recompensas_service().obtener_saldo(user)

    # Retrieve user preferences
    prefs = user.accesibilidad or {}

    return jsonify({
        "id": str(user.id),
        "name": user.name,
        "email": user.email,
        "telefono": user.telefono,
        "points": points,
        "level": "Standard",
        "preferences": prefs,
        "language": (prefs or {}).get("language", "es"),
        "available_languages": _supported_languages(),
    })

@portal_api_bp.route('/orders', methods=['GET'])
@require_auth
def get_orders(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    orders = MarketOrder.legacy_safe_query().filter_by(
        tenant_id=tenant.id,
        user_id=user.id
    ).order_by(MarketOrder.created_at.desc()).all()

    status_filter = (request.args.get("status") or "").strip().lower()
    if status_filter:
        orders = [o for o in orders if (o.status or "").strip().lower() == status_filter]

    return jsonify([_serialize_portal_order(o, include_timeline=False) for o in orders])

@portal_api_bp.route('/orders/<int:order_id>', methods=['GET'])
@require_auth
def get_order_detail(tenant_slug, order_id: int):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    order = MarketOrder.legacy_safe_query().filter_by(tenant_id=tenant.id, user_id=user.id, id=order_id).first()
    if not order:
        return jsonify({"error": "Order not found"}), 404

    return jsonify(_serialize_portal_order(order, include_timeline=True))


@portal_api_bp.route('/orders', methods=['POST'])
@require_auth
def create_order(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    data = request.get_json(silent=True) or {}

    items_data = data.get("items") or []
    if not items_data:
        return jsonify({"error": "No items provided"}), 400

    order = MarketOrder(
        tenant_id=tenant.id,
        user_id=user.id,
        status="pending",
        total_monetary=data.get("total"),
        contact_name=user.name,
        contact_phone=user.telefono
    )
    db.session.add(order)

    for item in items_data:
        db.session.add(MarketOrderItem(
            order=order,
            product_id=item.get("product_id"),
            quantity=item.get("quantity", 1),
            price_monetary=item.get("price")
        ))

    db.session.flush()
    db.session.add(
        OrderEvent(
            market_order_id=order.id,
            type="created",
            payload={
                "status": order.status,
                "label": _order_status_label(order.status),
                "message": "Pedido creado",
                "channel": "portal",
            },
        )
    )
    db.session.commit()
    return jsonify({"id": order.id, "status": order.status, "status_label": _order_status_label(order.status)}), 201

@portal_api_bp.route('/claims', methods=['GET'])
@require_auth
def get_claims(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    tickets = TenantTicket.query.filter_by(
        user_id=user.id,
        tenant_id=tenant.id
    ).order_by(TenantTicket.updated_at.desc()).all()

    return jsonify([{
        "id": str(t.id),
        "title": t.categoria or "Reclamo",
        "description": t.descripcion,
        "status": t.estado,
        "date": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat()
    } for t in tickets])

@portal_api_bp.route('/surveys/history', methods=['GET'])
@require_auth
def get_surveys_history(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    limit = _normalize_limit(request.args.get('limit'), default=20, max_limit=100)
    include_network = _is_truthy(request.args.get('include_network'))

    tenants, tenant_ids, _owner_ids = _resolve_portal_scope(user, tenant, include_network)
    if not tenant_ids:
        return jsonify([])
    survey_scope_ids = _survey_scope_ids(tenants)

    encuestas = (
        db.session.query(EncRespuesta, EncEncuesta)
        .join(EncEncuesta, EncEncuesta.id == EncRespuesta.encuesta_id)
        .filter(
            and_(
                EncRespuesta.user_id == user.id,
                EncRespuesta.tenant_id.in_(survey_scope_ids),
            )
        )
        .order_by(EncRespuesta.submitted_at.desc())
        .limit(limit)
        .all()
    )

    return jsonify([
        {
            "id": str(respuesta.id),
            "tenant_id": respuesta.tenant_id,
            "encuesta_id": respuesta.encuesta_id,
            "encuesta_slug": encuesta.slug if encuesta else None,
            "titulo": encuesta.titulo if encuesta else None,
            "tipo": encuesta.tipo if encuesta else None,
            "es_votacion": bool(encuesta.es_votacion_envivo) if encuesta else False,
            "submitted_at": _to_iso(respuesta.submitted_at),
        }
        for respuesta, encuesta in encuestas
    ])


@portal_api_bp.route('/history', methods=['GET'])
@require_auth
def get_portal_history(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    limit = _normalize_limit(request.args.get('limit'), default=20, max_limit=100)
    include_network = _is_truthy(request.args.get('include_network'))
    tenants, tenant_ids, owner_ids = _resolve_portal_scope(user, tenant, include_network)

    if not tenant_ids:
        return jsonify({"claims": [], "orders": [], "points": [], "surveys": [], "suggestions": [], "summary": {"counts": {}, "points_breakdown": {}}, "timeline": []})
    survey_scope_ids = _survey_scope_ids(tenants)

    claims = TenantTicket.query.filter(
        TenantTicket.user_id == user.id,
        TenantTicket.tenant_id.in_(tenant_ids),
    ).order_by(TenantTicket.updated_at.desc()).limit(limit).all()

    orders = MarketOrder.legacy_safe_query().filter(
        MarketOrder.user_id == user.id,
        MarketOrder.tenant_id.in_(tenant_ids),
    ).order_by(MarketOrder.created_at.desc()).limit(limit).all()

    points_tx = PointsTransaction.query.filter(
        PointsTransaction.user_id == user.id,
        PointsTransaction.tenant_id.in_(tenant_ids),
    ).order_by(PointsTransaction.created_at.desc()).limit(limit).all()

    encuestas = (
        db.session.query(EncRespuesta, EncEncuesta)
        .join(EncEncuesta, EncEncuesta.id == EncRespuesta.encuesta_id)
        .filter(
            and_(
                EncRespuesta.user_id == user.id,
                EncRespuesta.tenant_id.in_(survey_scope_ids),
            )
        )
        .order_by(EncRespuesta.submitted_at.desc())
        .limit(limit)
        .all()
    )

    sugerencias = []
    if owner_ids:
        sugerencias = SugerenciaCiudadano.query.filter(
            SugerenciaCiudadano.user_id == user.id,
            SugerenciaCiudadano.municipio_id.in_(owner_ids),
        ).order_by(SugerenciaCiudadano.fecha.desc()).limit(limit).all()

    points_breakdown = _build_points_breakdown(points_tx)
    tenant_by_id = {t.id: t for t in tenants}

    timeline = []
    for t in claims:
        timeline.append({
            "type": "claim",
            "id": t.id,
            "tenant_id": t.tenant_id,
            "tenant_slug": tenant_by_id.get(t.tenant_id).slug if tenant_by_id.get(t.tenant_id) else None,
            "title": t.categoria or "Reclamo",
            "status": t.estado,
            "at": _to_iso(t.updated_at) or _to_iso(t.created_at),
            "payload": {
                "description": t.descripcion,
                "created_at": _to_iso(t.created_at),
            },
        })

    for order in orders:
        serialized = _serialize_portal_order(order, include_timeline=False)
        timeline.append({
            "type": "order",
            "id": order.id,
            "tenant_id": order.tenant_id,
            "tenant_slug": tenant_by_id.get(order.tenant_id).slug if tenant_by_id.get(order.tenant_id) else None,
            "title": f"Pedido #{order.id}",
            "status": serialized.get("status"),
            "at": serialized.get("date"),
            "payload": serialized,
        })

    for tx in points_tx:
        timeline.append({
            "type": "points",
            "id": tx.id,
            "tenant_id": tx.tenant_id,
            "tenant_slug": tenant_by_id.get(tx.tenant_id).slug if tenant_by_id.get(tx.tenant_id) else None,
            "title": (tx.metadata_payload or {}).get("benefit_title") or tx.tipo,
            "status": "earned" if (tx.delta or 0) >= 0 else "redeemed",
            "at": _to_iso(tx.created_at),
            "payload": {
                "tipo": tx.tipo,
                "delta": tx.delta,
                "saldo_final": tx.saldo_final,
                "detalle": (tx.metadata_payload or {}).get("detalle", tx.tipo),
            },
        })

    for respuesta, encuesta in encuestas:
        timeline.append({
            "type": "survey",
            "id": respuesta.id,
            "tenant_id": respuesta.tenant_id,
            "tenant_slug": tenant_by_id.get(respuesta.tenant_id).slug if tenant_by_id.get(respuesta.tenant_id) else None,
            "title": encuesta.titulo if encuesta else "Encuesta",
            "status": "submitted",
            "at": _to_iso(respuesta.submitted_at),
            "payload": {
                "encuesta_id": respuesta.encuesta_id,
                "encuesta_slug": encuesta.slug if encuesta else None,
                "encuesta_tipo": encuesta.tipo if encuesta else None,
                "es_votacion": bool(encuesta.es_votacion_envivo) if encuesta else False,
            },
        })

    for sug in sugerencias:
        timeline.append({
            "type": "suggestion",
            "id": sug.id,
            "title": sug.categoria or "Sugerencia",
            "status": sug.estado,
            "at": _to_iso(sug.fecha),
            "payload": {
                "texto": sug.texto_sugerencia,
            },
        })

    timeline.sort(key=lambda item: item.get("at") or "", reverse=True)

    return jsonify({
        "scope": {
            "include_network": include_network,
            "tenant_ids": tenant_ids,
            "tenant_slugs": [t.slug for t in tenants],
        },
        "claims": [{
            "id": str(t.id),
            "tenant_id": t.tenant_id,
            "tenant_slug": tenant_by_id.get(t.tenant_id).slug if tenant_by_id.get(t.tenant_id) else None,
            "title": t.categoria or "Reclamo",
            "description": t.descripcion,
            "status": t.estado,
            "date": _to_iso(t.created_at),
            "updated_at": _to_iso(t.updated_at),
        } for t in claims],
        "orders": [{**_serialize_portal_order(o, include_timeline=False), "tenant_id": o.tenant_id, "tenant_slug": tenant_by_id.get(o.tenant_id).slug if tenant_by_id.get(o.tenant_id) else None} for o in orders],
        "points": [{
            "id": tx.id,
            "tenant_id": tx.tenant_id,
            "tenant_slug": tenant_by_id.get(tx.tenant_id).slug if tenant_by_id.get(tx.tenant_id) else None,
            "fecha": _to_iso(tx.created_at),
            "tipo": tx.tipo,
            "delta": tx.delta,
            "saldo_final": tx.saldo_final,
            "detalle": (tx.metadata_payload or {}).get("detalle", tx.tipo),
        } for tx in points_tx],
        "surveys": [{
            "id": str(respuesta.id),
            "tenant_id": respuesta.tenant_id,
            "tenant_slug": tenant_by_id.get(respuesta.tenant_id).slug if tenant_by_id.get(respuesta.tenant_id) else None,
            "encuesta_id": respuesta.encuesta_id,
            "encuesta_slug": encuesta.slug if encuesta else None,
            "titulo": encuesta.titulo if encuesta else None,
            "tipo": encuesta.tipo if encuesta else None,
            "es_votacion": bool(encuesta.es_votacion_envivo) if encuesta else False,
            "submitted_at": _to_iso(respuesta.submitted_at),
        } for respuesta, encuesta in encuestas],
        "suggestions": [{
            "id": str(sug.id),
            "categoria": sug.categoria,
            "estado": sug.estado,
            "texto": sug.texto_sugerencia,
            "submitted_at": _to_iso(sug.fecha),
        } for sug in sugerencias],
        "summary": {
            "counts": {
                "claims": len(claims),
                "orders": len(orders),
                "surveys": len(encuestas),
                "suggestions": len(sugerencias),
                "points_movements": len(points_tx),
            },
            "points_breakdown": points_breakdown,
        },
        "timeline": timeline[: max(limit * 5, 25)],
    })


@portal_api_bp.route('/dashboard', methods=['GET'])
@require_auth
def get_portal_dashboard(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    include_network = _is_truthy(request.args.get('include_network'))
    tenants, tenant_ids, _owner_ids = _resolve_portal_scope(user, tenant, include_network)

    if not tenant_ids:
        return jsonify({"summary": {}, "points": {"current": recompensas_service().obtener_saldo(user), "breakdown": {}}, "tenants_followed": 0})
    survey_scope_ids = _survey_scope_ids(tenants)

    claims_count = TenantTicket.query.filter(
        TenantTicket.user_id == user.id,
        TenantTicket.tenant_id.in_(tenant_ids),
    ).count()
    orders_count = MarketOrder.legacy_safe_count(
        MarketOrder.user_id == user.id,
        MarketOrder.tenant_id.in_(tenant_ids),
    )
    surveys_count = EncRespuesta.query.filter(
        EncRespuesta.user_id == user.id,
        EncRespuesta.tenant_id.in_(survey_scope_ids),
    ).count()

    points_tx = PointsTransaction.query.filter(
        PointsTransaction.user_id == user.id,
        PointsTransaction.tenant_id.in_(tenant_ids),
    ).order_by(PointsTransaction.created_at.desc()).limit(200).all()

    return jsonify({
        "scope": {"include_network": include_network, "tenant_ids": tenant_ids},
        "summary": {
            "claims": claims_count,
            "orders": orders_count,
            "surveys": surveys_count,
        },
        "points": {
            "current": recompensas_service().obtener_saldo(user),
            "breakdown": _build_points_breakdown(points_tx),
            "movements_count": len(points_tx),
        },
        "tenants_followed": max(0, len(tenant_ids) - 1),
    })


@portal_api_bp.route('/premium-bundle', methods=['GET'])
@portal_api_bp.route('/dashboard-bundle', methods=['GET'])
@require_auth
def get_portal_premium_bundle(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    include_network = _is_truthy(request.args.get('include_network'))
    limit = _normalize_limit(request.args.get('limit'), default=6, max_limit=20)
    tenants, tenant_ids, owner_ids = _resolve_portal_scope(user, tenant, include_network)
    survey_scope_ids = _survey_scope_ids(tenants)
    tenant_by_id = {item.id: item for item in tenants}

    claims = (
        TenantTicket.query.filter(
            TenantTicket.user_id == user.id,
            TenantTicket.tenant_id.in_(tenant_ids),
        )
        .order_by(TenantTicket.updated_at.desc())
        .limit(limit)
        .all()
    )
    orders = (
        MarketOrder.legacy_safe_query().filter(
            MarketOrder.user_id == user.id,
            MarketOrder.tenant_id.in_(tenant_ids),
        )
        .order_by(MarketOrder.created_at.desc())
        .limit(limit)
        .all()
    )
    points_tx = (
        PointsTransaction.query.filter(
            PointsTransaction.user_id == user.id,
            PointsTransaction.tenant_id.in_(tenant_ids),
        )
        .order_by(PointsTransaction.created_at.desc())
        .limit(max(limit * 4, 20))
        .all()
    )
    survey_history = (
        db.session.query(EncRespuesta, EncEncuesta)
        .join(EncEncuesta, EncEncuesta.id == EncRespuesta.encuesta_id)
        .filter(
            and_(
                EncRespuesta.user_id == user.id,
                EncRespuesta.tenant_id.in_(survey_scope_ids),
            )
        )
        .order_by(EncRespuesta.submitted_at.desc())
        .limit(limit)
        .all()
    )
    redeems = [tx for tx in points_tx if (tx.delta or 0) < 0][:limit]
    suggestions = []
    if owner_ids:
        suggestions = (
            SugerenciaCiudadano.query.filter(
                SugerenciaCiudadano.user_id == user.id,
                SugerenciaCiudadano.municipio_id.in_(owner_ids),
            )
            .order_by(SugerenciaCiudadano.fecha.desc())
            .limit(limit)
            .all()
        )

    available_surveys = _portal_available_surveys(tenant, user, limit=limit)
    rewards_balance = recompensas_service().obtener_saldo(user)
    tier = _portal_membership_tier(int(rewards_balance or 0))
    points_breakdown = _build_points_breakdown(points_tx)
    benefits = []
    for benefit in _portal_benefits(tenant):
        benefits.append(
            {
                **benefit,
                "eligible": rewards_balance >= benefit["cost_points"],
                "points_missing": max(0, benefit["cost_points"] - rewards_balance),
            }
        )
    promotions = _portal_promotions(tenant, limit=limit)

    total_spent = sum(float(order.total_monetary or 0) for order in orders)
    pending_claims = sum(1 for claim in claims if (claim.estado or "").lower() not in {"resuelto", "cerrado", "closed"})
    active_orders = sum(1 for order in orders if (order.status or "").lower() not in {"delivered", "cancelled"})
    timeline = []
    for order in orders:
        serialized = _serialize_portal_order(order, include_timeline=False)
        timeline.append(
            {
                "type": "order",
                "at": serialized.get("date"),
                "title": f"Pedido #{order.id}",
                "status": serialized.get("status"),
                "tenant_slug": tenant_by_id.get(order.tenant_id).slug if tenant_by_id.get(order.tenant_id) else None,
                "payload": serialized,
            }
        )
    for claim in claims:
        timeline.append(
            {
                "type": "claim",
                "at": _to_iso(claim.updated_at) or _to_iso(claim.created_at),
                "title": claim.categoria or "Reclamo",
                "status": claim.estado,
                "tenant_slug": tenant_by_id.get(claim.tenant_id).slug if tenant_by_id.get(claim.tenant_id) else None,
                "payload": _serialize_portal_claim(claim),
            }
        )
    for tx in points_tx[:limit]:
        timeline.append(
            {
                "type": "points",
                "at": _to_iso(tx.created_at),
                "title": (tx.metadata_payload or {}).get("detalle", tx.tipo),
                "status": "earned" if (tx.delta or 0) >= 0 else "redeemed",
                "tenant_slug": tenant_by_id.get(tx.tenant_id).slug if tenant_by_id.get(tx.tenant_id) else None,
                "payload": _serialize_points_transaction(tx),
            }
        )
    for respuesta, encuesta in survey_history:
        timeline.append(
            {
                "type": "survey",
                "at": _to_iso(respuesta.submitted_at),
                "title": encuesta.titulo if encuesta else "Encuesta",
                "status": "submitted",
                "tenant_slug": tenant_by_id.get(respuesta.tenant_id).slug if tenant_by_id.get(respuesta.tenant_id) else None,
                "payload": {
                    "encuesta_id": respuesta.encuesta_id,
                    "encuesta_slug": encuesta.slug if encuesta else None,
                    "kind": encuesta.tipo if encuesta else None,
                },
            }
        )
    timeline.sort(key=lambda item: item.get("at") or "", reverse=True)

    return jsonify(
        {
            "scope": {
                "include_network": include_network,
                "tenant_ids": tenant_ids,
                "tenant_slugs": [item.slug for item in tenants],
            },
            "member": {
                "id": str(user.id),
                "name": user.name,
                "email": user.email,
                "telefono": user.telefono,
                "tenant_slug": tenant.slug,
                "tenant_name": tenant.nombre,
                "segment": tenant.tipo,
            },
            "club": {
                "style": "private_club",
                "label": "Portal Privado",
                "tier": tier,
                "current_points": rewards_balance,
                "points_breakdown": points_breakdown,
                "tenants_followed": max(0, len(tenant_ids) - 1),
            },
            "orders": {
                "items": [_serialize_portal_order(order, include_timeline=False) for order in orders],
                "active_count": active_orders,
                "total_count": len(orders),
                "total_spent": round(total_spent, 2),
            },
            "claims": {
                "items": [_serialize_portal_claim(claim) for claim in claims],
                "open_count": pending_claims,
                "total_count": len(claims),
            },
            "promotions": {
                "items": promotions,
                "benefits_preview": benefits[:3],
            },
            "surveys": {
                "available": available_surveys,
                "history": [
                    {
                        "id": str(respuesta.id),
                        "tenant_id": respuesta.tenant_id,
                        "tenant_slug": tenant_by_id.get(respuesta.tenant_id).slug if tenant_by_id.get(respuesta.tenant_id) else None,
                        "encuesta_id": respuesta.encuesta_id,
                        "encuesta_slug": encuesta.slug if encuesta else None,
                        "title": encuesta.titulo if encuesta else None,
                        "kind": encuesta.tipo if encuesta else None,
                        "submitted_at": _to_iso(respuesta.submitted_at),
                    }
                    for respuesta, encuesta in survey_history
                ],
                "pending_count": len(available_surveys),
                "answered_count": len(survey_history),
            },
            "rewards": {
                "benefits": benefits,
                "redeems": [_serialize_points_transaction(tx) for tx in redeems],
                "wallet": {
                    "current_points": rewards_balance,
                    "earned_total": sum(max(0, int(tx.delta or 0)) for tx in points_tx),
                    "redeemed_total": abs(sum(min(0, int(tx.delta or 0)) for tx in points_tx)),
                },
            },
            "suggestions": [
                {
                    "id": str(item.id),
                    "categoria": item.categoria,
                    "estado": item.estado,
                    "texto": item.texto_sugerencia,
                    "submitted_at": _to_iso(item.fecha),
                }
                for item in suggestions
            ],
            "history": {
                "timeline": timeline[: max(limit * 4, 20)],
                "counts": {
                    "orders": len(orders),
                    "claims": len(claims),
                    "points": len(points_tx),
                    "surveys": len(survey_history),
                    "suggestions": len(suggestions),
                },
            },
            "highlights": [
                {
                    "id": "orders",
                    "title": "Compras activas",
                    "value": active_orders,
                    "tone": "brand",
                },
                {
                    "id": "claims",
                    "title": "Reclamos abiertos",
                    "value": pending_claims,
                    "tone": "warning" if pending_claims else "success",
                },
                {
                    "id": "points",
                    "title": "Puntos disponibles",
                    "value": rewards_balance,
                    "tone": "accent",
                },
            ],
            "quick_actions": [
                {"id": "go_orders", "label": "Ver pedidos", "href": "/portal/pedidos", "icon": "package"},
                {"id": "go_claims", "label": "Ver reclamos", "href": "/portal/reclamos", "icon": "life-buoy"},
                {"id": "go_rewards", "label": "Canjear puntos", "href": "/portal/puntos", "icon": "gift"},
                {"id": "go_surveys", "label": "Responder encuestas", "href": "/portal/encuestas", "icon": "clipboard"},
            ],
            "modules": [
                {"id": "orders", "title": "Compras", "badge_count": active_orders, "cta": "/portal/pedidos"},
                {"id": "claims", "title": "Reclamos", "badge_count": pending_claims, "cta": "/portal/reclamos"},
                {"id": "promotions", "title": "Promos", "badge_count": len(promotions), "cta": "/portal/promociones"},
                {"id": "rewards", "title": "Puntos y canjes", "badge_count": rewards_balance, "cta": "/portal/puntos"},
                {"id": "surveys", "title": "Encuestas", "badge_count": len(available_surveys), "cta": "/portal/encuestas"},
            ],
        }
    )


@portal_api_bp.route('/i18n', methods=['GET'])
@require_auth
def get_portal_i18n(tenant_slug):
    _resolve_context(tenant_slug)
    user = g.viewer
    prefs = user.accesibilidad or {}
    if not isinstance(prefs, dict):
        prefs = {}

    return jsonify({
        "current_language": prefs.get("language", "es"),
        "available_languages": _supported_languages(),
    })


@portal_api_bp.route('/network/feed', methods=['GET'])
@require_auth
def get_portal_network_feed(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    limit = _normalize_limit(request.args.get('limit'), default=20, max_limit=100)

    tenants = _resolve_followed_tenants(user, tenant)
    owner_ids = [
        _get_owner_id(t)
        for t in tenants
        if _get_owner_id(t)
    ]
    if not owner_ids:
        return jsonify({"items": [], "tenants": []})

    posts = MunicipioPost.query.filter(
        MunicipioPost.municipio_id.in_(owner_ids)
    ).order_by(MunicipioPost.fecha_publicacion.desc()).limit(limit).all()

    tenant_by_owner = { _get_owner_id(t): t for t in tenants if _get_owner_id(t) }

    items = []
    for post in posts:
        source_tenant = tenant_by_owner.get(post.municipio_id)
        post_type = "event" if post.tipo_post == "evento" else "news"
        items.append({
            "id": post.id,
            "type": post_type,
            "title": post.titulo,
            "summary": post.subtitulo or (post.descripcion[:120] if post.descripcion else ""),
            "date": _to_iso(post.fecha_publicacion),
            "tenant": {
                "id": source_tenant.id if source_tenant else None,
                "slug": source_tenant.slug if source_tenant else None,
                "name": source_tenant.nombre if source_tenant else None,
                "tipo": source_tenant.tipo if source_tenant else None,
            },
            "link": f"/{source_tenant.slug}/{'eventos' if post_type == 'event' else 'noticias'}/{post.id}" if source_tenant else None,
        })

    return jsonify({
        "items": items,
        "tenants": [
            {
                "id": t.id,
                "slug": t.slug,
                "name": t.nombre,
                "tipo": t.tipo,
            }
            for t in tenants
        ],
    })


@portal_api_bp.route('/benefits', methods=['GET'])
@require_auth
def get_portal_benefits(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    balance = recompensas_service().obtener_saldo(user)
    benefits = []
    for benefit in _portal_benefits(tenant):
        benefits.append({
            **benefit,
            "eligible": balance >= benefit["cost_points"],
            "points_missing": max(0, benefit["cost_points"] - balance),
        })

    return jsonify({
        "current_points": balance,
        "benefits": benefits,
    })


@portal_api_bp.route('/redeems', methods=['GET'])
@require_auth
def get_portal_redeems(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    limit = _normalize_limit(request.args.get('limit'), default=20, max_limit=100)
    history = PointsTransaction.query.filter_by(
        user_id=user.id,
        tenant_id=tenant.id
    ).filter(PointsTransaction.delta < 0).order_by(PointsTransaction.created_at.desc()).limit(limit).all()

    return jsonify([
        {
            "id": tx.id,
            "fecha": _to_iso(tx.created_at),
            "tipo": tx.tipo,
            "delta": tx.delta,
            "saldo_final": tx.saldo_final,
            "benefit_id": (tx.metadata_payload or {}).get("benefit_id"),
            "benefit_title": (tx.metadata_payload or {}).get("benefit_title"),
            "detalle": (tx.metadata_payload or {}).get("detalle", tx.tipo),
        }
        for tx in history
    ])


@portal_api_bp.route('/integration', methods=['GET'])
def get_integration_info(tenant_slug):
    """
    Returns integration details for the tenant: widget script, catalog URL, etc.
    This helps the 'Integration' page in the frontend populate its data.
    """
    tenant = _resolve_context(tenant_slug)
    owner = tenant.municipio or tenant.pyme

    # Base URL for catalog/portal
    # Logic similar to pwa_public but simpler
    portal_url = f"https://chatboc.ar/{tenant.slug}"
    widget_token = _canonical_widget_token(tenant, None)
    widget_payload = _build_widget_embed_payload(tenant, widget_token)
    widget_script = widget_payload.get("embed_snippet")

    demo_menu = _portal_demo_menu_for_tenant(tenant)
    demo_experience = build_demo_experience_contract(
        tenant_type=tenant.tipo,
        rubro_label=(tenant.nombre or tenant.tipo),
        max_messages=10,
    )
    return jsonify({
        "slug": tenant.slug,
        "name": tenant.nombre,
        "portalUrl": portal_url,
        "widgetScript": widget_script,
        "widget": widget_payload,
        "embed_snippet": widget_script,
        "builder_config": widget_payload.get("builder_config", {}),
        "embed_attributes": widget_payload.get("attributes", {}),
        "catalogUrl": f"{portal_url}/market",
        "qrCodeUrl": f"https://api.qrserver.com/v1/create-qr-code/?size=150x150&data={portal_url}",
        "whatsappLink": f"https://wa.me/{owner.telefono if owner and owner.telefono else ''}",
        "demoOnboarding": {
            "tenant_type": tenant.tipo,
            "twilio_trial": _portal_twilio_trial_contract(),
            "activation_state": _tenant_demo_activation_state(tenant),
            "activation_endpoint": f"/api/v1/portal/{tenant.slug}/integration/demo/activate-whatsapp",
            "quick_menu": demo_menu,
            "experience_blueprint": demo_experience,
            "feature_flags": {
                "audio_enabled": True,
                "image_enabled": True,
                "tickets_enabled": True,
                "orders_enabled": True,
                "suggestions_enabled": True,
                "heatmap_enabled": True,
                "catalog_pdf_enabled": True,
                "catalog_excel_enabled": True,
                "qdrant_demo_enabled": True,
            },
        },
    })


@portal_api_bp.route('/integration/demo/activate-whatsapp', methods=['POST'])
@require_auth
def activate_demo_whatsapp(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    owner = tenant.municipio or tenant.pyme
    owner_id = getattr(owner, "id", None)
    if not user or (getattr(user, "tenant_id", None) != tenant.id and getattr(user, "id", None) != owner_id):
        return jsonify({"error": "forbidden", "message": "No autorizado para activar demo en este tenant."}), 403

    result = _activate_tenant_demo_trial(tenant, getattr(user, "id", None))
    if not result.get("ok"):
        return jsonify(result), 403
    _notify_superadmin_hot_lead(tenant, user, result.get("state") or {})

    trial = _portal_twilio_trial_contract()
    return jsonify(
        {
            "ok": True,
            "message": "Demo WhatsApp activado en tiempo real para este tenant.",
            "twilio_trial": trial,
            "activation_state": result.get("state"),
        }
    ), 200

@portal_api_bp.route('/redeem', methods=['POST'])
@require_auth
def redeem_points(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer
    data = request.get_json(silent=True) or {}

    benefit_id = (data.get('benefit_id') or '').strip()
    if not benefit_id:
        return jsonify({"error": "Benefit ID required"}), 400

    benefits_map = {benefit["id"]: benefit for benefit in _portal_benefits(tenant)}
    benefit = benefits_map.get(benefit_id)
    if not benefit:
        return jsonify({"error": "Benefit not found"}), 404

    rewards = recompensas_service()
    cost = int(benefit["cost_points"])
    ok = rewards.canjear_puntos(
        user,
        tenant,
        cost,
        tipo="portal_redeem",
        metadata={
            "benefit_id": benefit_id,
            "benefit_title": benefit["title"],
            "detalle": f"Canje portal: {benefit['title']}",
        },
    )
    if not ok:
        current_points = rewards.obtener_saldo(user)
        return jsonify({"error": "Insufficient points", "current_points": current_points, "required_points": cost}), 400

    db.session.refresh(user)
    return jsonify({
        "success": True,
        "message": "Benefit redeemed",
        "benefit": benefit,
        "new_balance": rewards.obtener_saldo(user)
    })

@portal_api_bp.route('/loyalty', methods=['GET'])
@require_auth
def get_loyalty_info(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    rewards = recompensas_service()
    balance = rewards.obtener_saldo(user)

    history = PointsTransaction.query.filter_by(
        user_id=user.id,
        tenant_id=tenant.id
    ).order_by(PointsTransaction.created_at.desc()).limit(20).all()

    transactions = []
    for t in history:
        transactions.append({
            "fecha": t.created_at.isoformat() if t.created_at else None,
            "tipo": t.tipo,
            "delta": t.delta,
            "detalle": (t.metadata_payload or {}).get("detalle", t.tipo)
        })

    return jsonify({
        "current_points": balance,
        "transactions": transactions
    })

@portal_api_bp.route('/surveys/<slug>/responses', methods=['POST'])
@require_auth
def submit_portal_survey_response(tenant_slug, slug):
    tenant = _resolve_context(tenant_slug)
    viewer = g.viewer
    data = request.get_json(silent=True) or {}
    try:
        submission_id = resolve_survey_submission_id(
            data,
            header_value=request.headers.get("Idempotency-Key"),
            required=True,
        )
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    # Request context for fingerprinting
    request_ctx = {
        "ip": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
        "anon_id": request.headers.get("X-Anon-Id"),
        "canal": "portal"
    }

    try:
        # Note: save_respuesta expects PUBLIC SLUG.
        preferred_tenant_id = resolve_survey_tenant_scope_id(tenant)
        respuesta = save_respuesta(
            slug,
            data,
            request_ctx,
            preferred_tenant_id=preferred_tenant_id,
            require_tenant_match=True,
            authenticated_user=viewer,
            submission_id=submission_id,
            eligibility_credential=request.headers.get(
                SURVEY_ELIGIBILITY_CREDENTIAL_HEADER
            ),
            eligibility_transport="http",
        )
        response_payload = {
            "contract_version": "surveys.public_response.v2",
            "ok": True,
            "success": True,
            "persisted": True,
            "replayed": bool(getattr(respuesta, "submission_replayed", False)),
            "respuesta_id": respuesta.id,
            "response_id": respuesta.id,
            "instrument_revision": getattr(respuesta, "instrument_revision", None),
        }
        receipt_contract = survey_response_receipt_contract(respuesta)
        if receipt_contract is not None:
            response_payload["idempotency"] = receipt_contract
        from services.survey_governance import response_governance_contract

        response_payload["governance"] = response_governance_contract(respuesta)
        return (
            jsonify(response_payload),
            200 if response_payload["replayed"] else 201,
        )
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error submitting survey from portal")
        return jsonify({"error": "Error interno"}), 500

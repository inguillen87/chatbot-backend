from __future__ import annotations
from flask import Blueprint, jsonify, request, g, abort, current_app, url_for
from sqlalchemy import or_, and_
from datetime import datetime, timezone, timedelta

from models import MunicipioPost, CatalogoItem, TenantTicket, MarketOrder, MarketOrderItem, OrderEvent, User, TenantProfile, TenantFollower, WidgetConfig, EncEncuesta, EncRespuesta, PointsTransaction, SugerenciaCiudadano
from extensions import db
from services.tenant_resolver import resolve_tenant_only, TenantResolutionError
from services.rewards import recompensas_service
from routes.public_resolver import _build_widget_embed_payload, _canonical_widget_token
from utils.auth_decorators import require_auth_optional, require_auth
from routes.catalogo import _formatear_producto
from services.encuestas_service import list_public_encuestas_for_tenant, serialize_public_encuesta, get_public_encuesta_by_id

portal_api_bp = Blueprint('portal_api', __name__)

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
    if tenant.encuestas_tenant_id:
        encuestas = list_public_encuestas_for_tenant(tenant.encuestas_tenant_id, limit=3)
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

    return jsonify({
        "preferences": prefs,
        "tenantTheme": _get_theme_config(tenant)
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
        "preferences": prefs
    })

@portal_api_bp.route('/orders', methods=['GET'])
@require_auth
def get_orders(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    orders = MarketOrder.query.filter_by(
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

    order = MarketOrder.query.filter_by(tenant_id=tenant.id, user_id=user.id, id=order_id).first()
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

    # Not implemented: tracking user survey completions in a dedicated table.
    # For now returning empty list.
    return jsonify([])


@portal_api_bp.route('/history', methods=['GET'])
@require_auth
def get_portal_history(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    user = g.viewer

    limit = _normalize_limit(request.args.get('limit'), default=20, max_limit=100)

    claims = TenantTicket.query.filter_by(
        user_id=user.id,
        tenant_id=tenant.id
    ).order_by(TenantTicket.updated_at.desc()).limit(limit).all()

    orders = MarketOrder.query.filter_by(
        tenant_id=tenant.id,
        user_id=user.id
    ).order_by(MarketOrder.created_at.desc()).limit(limit).all()

    points_tx = PointsTransaction.query.filter_by(
        user_id=user.id,
        tenant_id=tenant.id
    ).order_by(PointsTransaction.created_at.desc()).limit(limit).all()

    encuestas = (
        db.session.query(EncRespuesta, EncEncuesta)
        .join(EncEncuesta, EncEncuesta.id == EncRespuesta.encuesta_id)
        .filter(
            and_(
                EncRespuesta.user_id == user.id,
                EncRespuesta.tenant_id == tenant.id,
            )
        )
        .order_by(EncRespuesta.submitted_at.desc())
        .limit(limit)
        .all()
    )

    owner_id = _get_owner_id(tenant)
    sugerencias = []
    if owner_id:
        sugerencias = SugerenciaCiudadano.query.filter_by(
            user_id=user.id,
            municipio_id=owner_id,
         ).order_by(SugerenciaCiudadano.fecha.desc()).limit(limit).all()

    points_breakdown = _build_points_breakdown(points_tx)

    timeline = []
    for t in claims:
        timeline.append({
            "type": "claim",
            "id": t.id,
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
            "title": f"Pedido #{order.id}",
            "status": serialized.get("status"),
            "at": serialized.get("date"),
            "payload": serialized,
        })

    for tx in points_tx:
        timeline.append({
            "type": "points",
            "id": tx.id,
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
        "claims": [{
            "id": str(t.id),
            "title": t.categoria or "Reclamo",
            "description": t.descripcion,
            "status": t.estado,
            "date": _to_iso(t.created_at),
            "updated_at": _to_iso(t.updated_at),
        } for t in claims],
        "orders": [_serialize_portal_order(o, include_timeline=False) for o in orders],
        "points": [{
            "id": tx.id,
            "fecha": _to_iso(tx.created_at),
            "tipo": tx.tipo,
            "delta": tx.delta,
            "saldo_final": tx.saldo_final,
            "detalle": (tx.metadata_payload or {}).get("detalle", tx.tipo),
        } for tx in points_tx],
        "surveys": [{
            "id": str(respuesta.id),
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
        "whatsappLink": f"https://wa.me/{owner.telefono if owner and owner.telefono else ''}"
    })

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
    _resolve_context(tenant_slug) # Ensure tenant context
    # user = g.viewer # Responses logic typically uses user_id from payload or infers it

    from services.encuestas_service import save_respuesta, EncuestaError

    data = request.get_json(silent=True) or {}

    # Inject authenticated user info if not present
    if 'user_id' not in data and g.viewer:
        data['user_id'] = g.viewer.id

    # Request context for fingerprinting
    request_ctx = {
        "ip": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
        "anon_id": request.headers.get("X-Anon-Id"),
        "canal": "portal"
    }

    try:
        # Note: save_respuesta expects PUBLIC SLUG.
        save_respuesta(slug, data, request_ctx)
        return jsonify({"success": True}), 201
    except EncuestaError as e:
        return jsonify({"error": e.message}), e.status_code
    except Exception as e:
        current_app.logger.exception("Error submitting survey from portal")
        return jsonify({"error": "Error interno"}), 500

from __future__ import annotations
from flask import Blueprint, jsonify, request, g, abort, current_app, url_for
from sqlalchemy import or_
from datetime import datetime, timezone, timedelta

from models import MunicipioPost, CatalogoItem, TenantTicket, MarketOrder, MarketOrderItem, User, TenantProfile, WidgetConfig, EncEncuesta
from extensions import db
from services.tenant_resolver import resolve_tenant_only, TenantResolutionError
from services.rewards import recompensas_service
from utils.auth_decorators import require_auth_optional, require_auth
from routes.catalogo import _formatear_producto
from services.encuestas_service import list_public_encuestas_for_tenant, serialize_public_encuesta, get_public_encuesta_by_id

portal_api_bp = Blueprint('portal_api', __name__)

def _resolve_context(tenant_slug):
    try:
        tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
    except TenantResolutionError:
        abort(404, "Tenant no encontrado")

    g.tenant = tenant
    return tenant

def _get_owner_id(tenant):
    owner = tenant.municipio or tenant.pyme
    return owner.id if owner else None

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

    return jsonify([{
        "id": o.id,
        "status": o.status,
        "total": float(o.total_monetary or 0),
        "date": o.created_at.isoformat(),
        "items_count": len(o.items)
    } for o in orders])

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

    db.session.commit()
    return jsonify({"id": order.id, "status": order.status}), 201

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


@portal_api_bp.route('/integration', methods=['GET'])
def get_integration_info(tenant_slug):
    """
    Returns integration details for the tenant: widget script, catalog URL, etc.
    This helps the 'Integration' page in the frontend populate its data.
    """
    tenant = _resolve_context(tenant_slug)
    owner = _get_owner_id(tenant)

    # Base URL for catalog/portal
    # Logic similar to pwa_public but simpler
    portal_url = f"https://chatboc.ar/{tenant.slug}"
    widget_script = '<script src="https://chatboc.ar/widget.js" data-tenant="{}"></script>'.format(tenant.slug)

    return jsonify({
        "slug": tenant.slug,
        "name": tenant.nombre,
        "portalUrl": portal_url,
        "widgetScript": widget_script,
        "catalogUrl": f"{portal_url}/market",
        "qrCodeUrl": f"https://api.qrserver.com/v1/create-qr-code/?size=150x150&data={portal_url}",
        "whatsappLink": f"https://wa.me/{owner.telefono if owner and owner.telefono else ''}"
    })

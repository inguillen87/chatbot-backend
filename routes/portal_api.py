from __future__ import annotations
from flask import Blueprint, jsonify, request, g, abort
from sqlalchemy import or_
from datetime import datetime, timezone, timedelta

from models import MunicipioPost, CatalogoItem, TenantTicket
from services.tenant_resolver import resolve_tenant_only, TenantResolutionError
from services.rewards import recompensas_service
from utils.auth_decorators import require_auth_optional, require_auth
from routes.catalogo import _formatear_producto

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

    # 1. Ticket Updates (Last 7 days)
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
    owner_id = _get_owner_id(tenant)
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

@portal_api_bp.route('/content', methods=['GET'])
@require_auth_optional
def get_content(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    owner_id = _get_owner_id(tenant)
    user = getattr(g, "viewer", None)

    # 1. News (limit 5)
    news_query = MunicipioPost.query.filter(
        MunicipioPost.municipio_id == owner_id,
        MunicipioPost.tipo_post != 'evento'
    ).order_by(MunicipioPost.fecha_publicacion.desc()).limit(5)
    news_items = news_query.all()

    # 2. Events (limit 5 upcoming)
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
        "surveysCompleted": 0,
        "suggestionsShared": 0,
        "claimsFiled": 0
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
                "statusType": "info"
            })

    return jsonify({
        "notifications": notifications,
        "news": [{
            "id": str(n.id),
            "title": n.titulo,
            "summary": n.subtitulo or (n.descripcion[:100] if n.descripcion else ""),
            "coverUrl": n.imagen_url,
            "date": n.fecha_publicacion.isoformat(),
            "category": "General",
            "featured": False,
            "link": f"/{tenant.slug}/noticias/{n.id}"
        } for n in news_items],
        "events": [{
            "id": str(e.id),
            "title": e.titulo,
            "date": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "location": e.ubicacion,
            "status": "inscripcion",
            "coverUrl": e.imagen_url,
            "link": f"/{tenant.slug}/eventos/{e.id}"
        } for e in events_items],
        "loyaltySummary": loyalty_summary,
        "activities": activities
    })

@portal_api_bp.route('/news', methods=['GET'])
@require_auth_optional
def get_news(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    owner_id = _get_owner_id(tenant)

    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)

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
            "content": n.descripcion,
            "coverUrl": n.imagen_url,
            "date": n.fecha_publicacion.isoformat(),
            "category": "General",
            "author": "Admin",
            "featured": False,
            "link": f"/{tenant.slug}/noticias/{n.id}"
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
            "description": e.descripcion,
            "coverUrl": e.imagen_url,
            "date": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "location": e.ubicacion,
            "spots": 0,
            "registered": 0,
            "status": "inscripcion",
            "user_registered": False,
            "link": f"/{tenant.slug}/eventos/{e.id}"
        })

    return jsonify({
        "data": data
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
    # Placeholder logic - in future implement read state storage
    return jsonify({"success": True})

@portal_api_bp.route('/profile', methods=['GET'])
@require_auth
def get_profile(tenant_slug):
    _resolve_context(tenant_slug)
    user = g.viewer

    points = recompensas_service().obtener_saldo(user)

    return jsonify({
        "id": str(user.id),
        "name": user.name,
        "email": user.email,
        "points": points,
        "level": "Standard",
        "preferences": {
            "notifications_email": True,
            "notifications_push": False
        }
    })

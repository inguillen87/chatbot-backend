from __future__ import annotations
from flask import Blueprint, jsonify, request, g, abort
from sqlalchemy import func, or_
from datetime import datetime, timezone

from models import TenantProfile, MunicipioPost, CatalogoItem, User, TenantTicket, EncEncuesta
from services.tenant_resolver import resolve_tenant_only, TenantResolutionError
from services.rewards import recompensas_service
from utils.auth_decorators import require_auth_optional, require_auth
from routes.catalogo import _formatear_producto

portal_api_bp = Blueprint('portal_api', __name__, url_prefix='/api/v1/portal/<tenant_slug>')

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

    # 3. Notifications (Mock/Placeholder or Ticket Updates)
    notifications = []
    # If we had a notification model, we'd query it here for 'user'

    # 4. Loyalty Summary
    loyalty = {
        "points": 0,
        "surveysCompleted": 0,
        "suggestionsShared": 0,
        "claimsFiled": 0
    }
    if user:
        loyalty["points"] = recompensas_service().obtener_saldo(user)
        # Count other metrics if models allow
        loyalty["claimsFiled"] = TenantTicket.query.filter_by(user_id=user.id, tenant_id=tenant.id).count()

    # 5. Activities (Recent tickets)
    activities = []
    if user:
        recent_tickets = TenantTicket.query.filter_by(
            user_id=user.id, tenant_id=tenant.id
        ).order_by(TenantTicket.updated_at.desc()).limit(5).all()

        for t in recent_tickets:
            activities.append({
                "id": str(t.id),
                "type": "RECLAMO", # or TICKET
                "description": t.descripcion or t.categoria,
                "date": t.updated_at.isoformat() if t.updated_at else None,
                "status": t.estado,
                "statusType": "info" # map status to severity
            })

    return jsonify({
        "notifications": notifications,
        "news": [{
            "id": str(n.id),
            "title": n.titulo,
            "summary": n.subtitulo or n.descripcion[:100],
            "coverUrl": n.imagen_url,
            "date": n.fecha_publicacion.isoformat(),
            "category": "General", # or from tags
            "featured": False,
            "link": f"/portal/noticias/{n.id}"
        } for n in news_items],
        "events": [{
            "id": str(e.id),
            "title": e.titulo,
            "date": e.fecha_evento_inicio.isoformat() if e.fecha_evento_inicio else None,
            "location": e.ubicacion,
            "status": "inscripcion", # logic needed
            "coverUrl": e.imagen_url
        } for e in events_items],
        "loyaltySummary": loyalty,
        "activities": activities
    })

@portal_api_bp.route('/news', methods=['GET'])
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
            "summary": n.subtitulo or n.descripcion[:150],
            "content": n.descripcion, # Assuming content is in description
            "coverUrl": n.imagen_url,
            "date": n.fecha_publicacion.isoformat(),
            "category": "General",
            "author": "Admin", # Placeholder
            "featured": False
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
            "spots": 0, # Not in model
            "registered": 0, # Not in model
            "status": "inscripcion",
            "user_registered": False
        })

    return jsonify({
        "data": data
    })

@portal_api_bp.route('/catalog', methods=['GET'])
def get_catalog(tenant_slug):
    tenant = _resolve_context(tenant_slug)
    owner_id = _get_owner_id(tenant)

    items = CatalogoItem.query.filter(
        CatalogoItem.tenant_id == tenant.id,
        or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None))
    ).all()

    data = []
    for item in items:
        # Reuse existing formatter logic if possible, or build custom
        # Spec: id, title, description, category, imageUrl, priceLabel, status, formSchema

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
            "formSchema": {}
        })

    return jsonify({"data": data})

@portal_api_bp.route('/notifications', methods=['GET'])
@require_auth
def get_notifications(tenant_slug):
    _resolve_context(tenant_slug)
    # Placeholder
    return jsonify([])

@portal_api_bp.route('/notifications/<string:notif_id>/read', methods=['POST'])
@require_auth
def mark_notification_read(tenant_slug, notif_id):
    _resolve_context(tenant_slug)
    # Placeholder logic
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
        "level": "Standard", # Logic needed
        "preferences": {
            "notifications_email": True,
            "notifications_push": False
        }
    })

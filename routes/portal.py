from flask import Blueprint, render_template, abort, g, current_app
from sqlalchemy import func, or_
from models import TenantProfile, MunicipioPost, EncEncuesta, CatalogoItem, User
from services.tenant_resolver import resolve_tenant_only, TenantResolutionError
from services.catalog_seed import ensure_seed_catalog
from routes.catalogo import _formatear_producto

portal_bp = Blueprint('portal', __name__, url_prefix='/market')

def _resolve_tenant(slug: str) -> TenantProfile:
    try:
        tenant = resolve_tenant_only(tenant_slug=slug, require_explicit_slug=True)
    except TenantResolutionError:
        abort(404, "Tenant no encontrado")

    g.tenant_profile = tenant
    return tenant

def _get_owner(tenant: TenantProfile) -> User:
    owner = tenant.municipio or tenant.pyme
    if not owner:
        abort(404, "Tenant sin propietario configurado")
    return owner

@portal_bp.route('/<slug>')
@portal_bp.route('/<slug>/')
def portal_view(slug: str):
    tenant = _resolve_tenant(slug)
    owner = _get_owner(tenant)

    # Ensure catalog seed if empty (for demos)
    ensure_seed_catalog(owner, tenant)

    # 1. Catalog Items
    query = CatalogoItem.query.filter(
        CatalogoItem.tenant_id == tenant.id,
        CatalogoItem.user_id == owner.id,
        or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None))
    ).order_by(CatalogoItem.categoria, CatalogoItem.nombre)

    all_items = query.all()

    # Organize by Category
    catalog_sections = {}
    canjes = []

    for item in all_items:
        prod = _formatear_producto({
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
            "moneda": item.moneda,
            "modalidad": item.modalidad
        })
        prod['id'] = item.id

        # Check if it's a reward/canje
        if prod.get('moneda') == 'PTS' or prod.get('modalidad') == 'canje':
            canjes.append(prod)
        else:
            cat = prod.get('categoria') or 'General'
            if cat not in catalog_sections:
                catalog_sections[cat] = []
            catalog_sections[cat].append(prod)

    # Convert sections to list
    sections_list = [{'title': k, 'items': v} for k, v in catalog_sections.items()]

    # 2. News (Noticias)
    news_query = MunicipioPost.query.filter(
        MunicipioPost.municipio_id == owner.id,
        MunicipioPost.tipo_post != 'evento'
    ).order_by(MunicipioPost.fecha_publicacion.desc()).limit(10)
    news = news_query.all()

    # 3. Events (Eventos)
    events_query = MunicipioPost.query.filter(
        MunicipioPost.municipio_id == owner.id,
        MunicipioPost.tipo_post == 'evento'
    ).order_by(MunicipioPost.fecha_evento_inicio.asc()).limit(10)
    events = events_query.all()

    # 4. Surveys (Encuestas)
    surveys_query = EncEncuesta.query.filter(
        EncEncuesta.tenant_id == tenant.id,
        EncEncuesta.estado == 'publicada'
    ).order_by(EncEncuesta.created_at.desc()).limit(5)
    surveys = surveys_query.all()

    # Hero Subtitle
    hero_subtitle = "Tu portal digital para conectar, comprar y participar."
    if tenant.tipo == 'municipio':
        hero_subtitle = "Gestión municipal, eventos, noticias y beneficios para vecinos."

    # Features (Static for now, could be dynamic)
    features = [
        "Atención por WhatsApp y Web",
        "Catálogo Digital y Pedidos",
        "Eventos y Noticias Locales",
        "Encuestas y Participación",
        "Soporte 24/7 con IA"
    ]

    return render_template(
        'portal.html',
        tenant=tenant,
        tenant_name=tenant.nombre,
        tenant_slug=tenant.slug,
        tenant_tipo=tenant.tipo,
        hero_subtitle=hero_subtitle,
        catalog_sections=sections_list,
        canjes=canjes,
        news=news,
        events=events,
        surveys=surveys,
        features=features,
        cart_preview=[], # JS will load this
        cart_totals={}
    )

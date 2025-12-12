"""Public endpoints for the progressive web app multi-tenant experience."""

from __future__ import annotations

from typing import Dict, List, Tuple

from flask import Blueprint, abort, g, jsonify, make_response, request, session
from sqlalchemy import func

from models import CatalogoItem, CatalogoModalidad, MunicipioPost, TenantProfile, User, WidgetConfig, WidgetSettings
from middleware import require_tenant
from services.encuestas_service import (
    EncuestaError,
    get_public_encuesta,
    list_public_encuestas_for_tenant,
    save_respuesta,
    serialize_public_encuesta,
)
from services.catalog_seed import ensure_seed_catalog
from services.common_utils import parse_precio_flexible
from routes.catalogo import _formatear_producto
from services.rewards_demo import reward_profile_for_tenant
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
# Import common cart logic to support Persistent X-Anon-Id and unified cart behavior
from routes.carrito import (
    _get_or_create_db_cart,
    _db_cart_summary,
    _product_query_for_tenant,
    _pricing_snapshot,
)
from models import MarketCartItem
from database import db


pwa_public_bp = Blueprint("pwa_public", __name__, url_prefix="/api/pwa/public")
public_api_bp = Blueprint("public_api", __name__, url_prefix="/api/public")
pwa_tenant_info_bp = Blueprint("pwa_tenant_info", __name__)


@pwa_tenant_info_bp.route("/api/pwa/tenant-info", methods=["GET", "OPTIONS"])
def api_pwa_tenant_info():
    """Alias JSON de /api/public/tenant-profile para el PWA del widget."""
    from routes.public_resolver import tenant_profile as tenant_profile_view

    if request.method == "OPTIONS":
        return "", 204

    return tenant_profile_view()


@pwa_tenant_info_bp.route("/pwa/tenant-info", methods=["GET", "OPTIONS"])
def pwa_tenant_info_alias():
    """Alias sin prefijo /api usado por algunos embeds del widget."""
    from routes.public_resolver import tenant_profile as tenant_profile_view

    if request.method == "OPTIONS":
        return "", 204

    return tenant_profile_view()


def _require_tenant() -> TenantProfile:
    tenant = getattr(g, "tenant_profile", None)

    if tenant is None:
        widget_token = (
            request.args.get("widget_token")
            or request.headers.get("X-Widget-Token")
            or request.args.get("entityToken")
            or request.headers.get("X-Entity-Token")
        )
        tenant_slug = (
            request.args.get("tenant")
            or request.args.get("slug")
            or request.args.get("tenant_slug")
        )
        host_hint = request.headers.get("X-Forwarded-Host") or request.host

        try:
            tenant = resolve_tenant_only(
                widget_token=widget_token,
                tenant_slug=tenant_slug,
                host=host_hint,
                require_explicit_slug=False,
            )
        except TenantResolutionError:
            tenant = None
        else:
            g.tenant_profile = tenant
            g.tenant_profile_slug = tenant.slug

    if tenant is None:
        abort(
            make_response(
                jsonify(
                    {
                        "error": "Tenant no encontrado",
                        "detail": "Revisa el slug o la URL del widget; no se pudo resolver el tenant para el carrito público.",
                    }
                ),
                404,
            )
        )
    return tenant


def _public_cart_base_url(tenant: TenantProfile) -> str:
    """Return the base URL to be used when sharing/embedding the public cart.

    Preference order (all optional, no migrations required):
    1) ``configuracion.public_cart_base_url`` or ``configuracion.public_base_url``: scheme+host base.
    2) ``configuracion.public_cart_host`` or ``tenant.dominio`` as host, keeping the request scheme.
    3) Current request host (``request.url_root``).
    """

    cfg = tenant.configuracion or {}

    base_url = cfg.get("public_cart_base_url") or cfg.get("public_base_url")
    if isinstance(base_url, str) and base_url.strip():
        return base_url.strip().rstrip("/")

    host = cfg.get("public_cart_host") or tenant.dominio
    if isinstance(host, str) and host.strip():
        scheme = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
        return f"{scheme}://{host.strip().strip('/')}"

    return (request.url_root or "").rstrip("/")


def _public_cart_path(tenant: TenantProfile) -> str:
    cfg = tenant.configuracion or {}
    explicit_path = cfg.get("public_cart_path")
    if isinstance(explicit_path, str) and explicit_path.strip():
        return explicit_path.lstrip("/")

    slug_alias = cfg.get("public_cart_slug") or tenant.slug
    prefix = (cfg.get("public_cart_prefix") or "m").rstrip("/")
    suffix = cfg.get("public_cart_suffix")
    normalized_suffix = str(suffix).strip("/") if isinstance(suffix, str) else ""

    # Estilo profesional tipo ``chatboc.ar/m/<slug>`` por defecto.
    if normalized_suffix:
        return f"{prefix}/{slug_alias}/{normalized_suffix}"
    return f"{prefix}/{slug_alias}"


def _build_public_cart_url(tenant: TenantProfile) -> Tuple[str, str, str]:
    cfg = tenant.configuracion or {}
    override_url = cfg.get("public_cart_url")
    if isinstance(override_url, str) and override_url.strip():
        cleaned = override_url.strip()
        return cleaned, cleaned, "override"

    base_url = _public_cart_base_url(tenant).rstrip("/")
    path = _public_cart_path(tenant)
    full_url = f"{base_url}/{path}" if path else base_url
    return full_url, base_url, path


def _resolve_encuestas_tenant_id(tenant: TenantProfile) -> int | None:
    if tenant.encuestas_tenant_id:
        return tenant.encuestas_tenant_id
    if tenant.municipio_id:
        return tenant.municipio_id
    if tenant.pyme_id:
        return tenant.pyme_id
    return None


def _posts_query_for_tenant(tenant: TenantProfile):
    owner_id = tenant.municipio_id or tenant.pyme_id
    if not owner_id:
        return MunicipioPost.query.filter(False)
    return MunicipioPost.query.filter(MunicipioPost.municipio_id == owner_id)


def _tenant_owner(tenant: TenantProfile) -> User | None:
    return tenant.municipio or tenant.pyme


def _require_owner(tenant: TenantProfile) -> User:
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario configurado")
    return owner


def _lookup_catalog_item(owner: User, tenant: TenantProfile, payload: Dict[str, object]) -> CatalogoItem | None:
    identifier = payload.get("catalogo_item_id") or payload.get("item_id")
    sku = payload.get("sku")
    nombre = payload.get("nombre")

    query = _product_query_for_tenant(owner, tenant)

    if identifier is not None:
        try:
            identifier = int(identifier)
        except (TypeError, ValueError):
            identifier = None
        else:
            item = query.filter(CatalogoItem.id == identifier).first()
            if item:
                return item

    if sku:
        sku_text = str(sku).strip().lower()
        if sku_text:
            item = query.filter(func.lower(CatalogoItem.sku) == sku_text).first()
            if item:
                return item

    if nombre:
        nombre_text = str(nombre).strip().lower()
        if nombre_text:
            return query.filter(func.lower(CatalogoItem.nombre) == nombre_text).first()

    return None


def _normalize_quantity(value: object, default: int = 1, min_value: int = 1) -> int:
    try:
        cantidad = int(value)
    except (TypeError, ValueError):
        cantidad = default
    return max(cantidad, min_value)


def _coerce_item_id(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@pwa_public_bp.get("/catalog")
def public_catalog():
    tenant = _require_tenant()
    owner = _require_owner(tenant)
    ensure_seed_catalog(owner, tenant)

    categoria = request.args.get("categoria")
    search_text = request.args.get("q")

    # Use the shared query helper to ensure tenant filtering matches cart logic
    query = _product_query_for_tenant(owner, tenant)

    if categoria:
        categoria_norm = categoria.strip().lower()
        if categoria_norm:
            query = query.filter(func.lower(CatalogoItem.categoria) == categoria_norm)

    items = query.order_by(func.lower(CatalogoItem.nombre)).all()

    productos: List[Dict[str, object]] = []
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
            }
        )
        prod["catalogo_item_id"] = item.id
        prod["tenant_id"] = tenant.id
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


@pwa_public_bp.get("/rewards")
def public_rewards():
    tenant = _require_tenant()
    return jsonify(reward_profile_for_tenant(tenant.id, 0.0))


@pwa_public_bp.get("/cart/url")
def public_cart_url():
    tenant = _require_tenant()
    full_url, base_url, path = _build_public_cart_url(tenant)
    return jsonify(
        {
            "cart_url": full_url,
            "base_url": base_url,
            "path": path,
            "tenant_slug": tenant.slug,
        }
    )


@pwa_public_bp.get("/cart")
def public_cart_summary():
    tenant = _require_tenant()
    owner = _require_owner(tenant)
    ensure_seed_catalog(owner, tenant)

    cart = _get_or_create_db_cart(tenant, getattr(g, 'user', None), create_if_missing=False)
    if not cart:
         return jsonify({
            "tenant_id": tenant.id,
            "items": [],
            "items_count": 0,
            "total_estimado": 0.0,
            "badge_count": 0,
            "recompensas_demo": reward_profile_for_tenant(tenant.id, 0.0),
        })

    return jsonify(_db_cart_summary(cart, owner))


@pwa_public_bp.post("/cart/add")
def public_cart_add():
    tenant = _require_tenant()
    owner = _require_owner(tenant)
    ensure_seed_catalog(owner, tenant)

    payload = request.get_json(silent=True) or {}
    item = _lookup_catalog_item(owner, tenant, payload)
    if not item:
        return jsonify({"error": "Producto no encontrado"}), 404

    cantidad = _normalize_quantity(payload.get("cantidad", 1))

    # Use persistent cart logic (X-Anon-Id or User)
    cart = _get_or_create_db_cart(tenant, getattr(g, 'user', None), create_if_missing=True)
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está cerrado"}), 400

    cart_item = cart.items.filter(MarketCartItem.product_id == item.id).first()
    pricing = _pricing_snapshot(item)

    if cart_item:
        cart_item.quantity += cantidad
    else:
        cart_item = MarketCartItem(
            cart_id=cart.id,
            product_id=item.id,
            quantity=cantidad,
            price_text=pricing.get("price_text"),
            price_monetary=pricing.get("price_monetary"),
            price_points=pricing.get("price_points"),
            currency=pricing.get("currency"),
            modalidad=pricing.get("modalidad"),
            name_snapshot=item.nombre,
        )
        db.session.add(cart_item)

    db.session.commit()
    return jsonify(_db_cart_summary(cart, owner, event="add"))


@pwa_public_bp.post("/cart/update")
def public_cart_update():
    tenant = _require_tenant()
    owner = _require_owner(tenant)
    payload = request.get_json(silent=True) or {}
    item_id = _coerce_item_id(payload.get("catalogo_item_id") or payload.get("item_id"))
    if item_id is None:
        return jsonify({"error": "catalogo_item_id requerido"}), 400

    cantidad = _normalize_quantity(payload.get("cantidad", 0), default=0, min_value=0)

    cart = _get_or_create_db_cart(tenant, getattr(g, 'user', None), create_if_missing=False)
    if not cart:
        return jsonify({"error": "Carrito no encontrado"}), 404

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id).first()
    if not cart_item:
        return jsonify({'error': 'Item no encontrado en el carrito'}), 404

    if cantidad <= 0:
        db.session.delete(cart_item)
    else:
        cart_item.quantity = cantidad

    db.session.commit()
    return jsonify(_db_cart_summary(cart, owner, event="update"))


@pwa_public_bp.post("/cart/remove")
def public_cart_remove():
    tenant = _require_tenant()
    owner = _require_owner(tenant)
    payload = request.get_json(silent=True) or {}
    item_id = _coerce_item_id(payload.get("catalogo_item_id") or payload.get("item_id"))
    if item_id is None:
        return jsonify({"error": "catalogo_item_id requerido"}), 400

    cart = _get_or_create_db_cart(tenant, getattr(g, 'user', None), create_if_missing=False)
    if not cart:
        return jsonify({"error": "Carrito no encontrado"}), 404

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id).first()
    if cart_item:
        db.session.delete(cart_item)
        db.session.commit()
        return jsonify(_db_cart_summary(cart, owner, event="remove"))

    return jsonify({"error": "Item no encontrado en el carrito"}), 404


@pwa_public_bp.post("/cart/clear")
def public_cart_clear():
    tenant = _require_tenant()
    owner = _require_owner(tenant)

    cart = _get_or_create_db_cart(tenant, getattr(g, 'user', None), create_if_missing=False)
    if cart:
        cart.items.delete()
        db.session.commit()

    return jsonify(_db_cart_summary(cart, owner, event="clear") if cart else {
        "tenant_id": tenant.id, "items": [], "items_count": 0, "total_estimado": 0.0,
        "ui_signals": {"event": "clear", "animation": "cart-burst", "badge": 0},
    })


@pwa_public_bp.get("/tenant")
def tenant_info():
    tenant = _require_tenant()
    return jsonify(tenant.to_public_dict())


@pwa_public_bp.get("/surveys")
def list_surveys():
    tenant = _require_tenant()
    tenant_id = _resolve_encuestas_tenant_id(tenant)
    if not tenant_id:
        return jsonify([])

    encuestas: List[Tuple[object, str]] = list_public_encuestas_for_tenant(tenant_id, limit=25)
    payload = [
        serialize_public_encuesta(encuesta, slug_publico=slug)
        for encuesta, slug in encuestas
    ]
    return jsonify(payload)


@pwa_public_bp.get("/surveys/<slug>")
def get_survey(slug: str):
    tenant = _require_tenant()
    tenant_id = _resolve_encuestas_tenant_id(tenant)
    try:
        encuesta = get_public_encuesta(slug)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    if tenant_id and encuesta.tenant_id != tenant_id:
        abort(404, description="Encuesta no encontrada")

    return jsonify(serialize_public_encuesta(encuesta, slug_publico=slug))


@pwa_public_bp.post("/surveys/<slug>/respond")
def respond_survey(slug: str):
    tenant = _require_tenant()
    tenant_id = _resolve_encuestas_tenant_id(tenant)
    try:
        encuesta = get_public_encuesta(slug)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    if tenant_id and encuesta.tenant_id != tenant_id:
        abort(404, description="Encuesta no encontrada")

    payload = request.get_json(silent=True) or {}
    request_ctx = {
        "ip": request.headers.get("X-Forwarded-For") or request.remote_addr,
        "anon_id": (
            request.headers.get("X-Anon-Id")
            or request.headers.get("Anon-Id")
            or request.cookies.get("Anon-Id")
            or request.cookies.get("anon_id")
        ),
        "canal": "pwa",
    }
    try:
        respuesta = save_respuesta(slug, payload, request_ctx)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify({"id": respuesta.id})


@pwa_public_bp.get("/news")
def list_news():
    tenant = _require_tenant()
    query = _posts_query_for_tenant(tenant).filter(MunicipioPost.tipo_post != "evento")
    items = (
        query.order_by(MunicipioPost.fecha_publicacion.desc())
        .limit(50)
        .all()
    )
    return jsonify([item.to_dict() for item in items])


@public_api_bp.get("/tenants/<tenant_slug>/widget-config")
def public_tenant_widget_config(tenant_slug: str):
    from services.tenant_resolver import resolve_tenant_only, TenantResolutionError

    try:
        tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=False)
    except TenantResolutionError:
        abort(404, "Tenant no encontrado")

    owner = _tenant_owner(tenant)
    cfg = tenant.configuracion or {}

    # Merge theme: WidgetConfig > Tenant.tema > Defaults
    theme = tenant.tema or {}
    if not isinstance(theme, dict):
        theme = {}

    widget_cfg = tenant.widget_config
    widget_settings = tenant.widget_settings

    # Robust Defaults for Theme Config to prevent Frontend Crashes
    DEFAULT_THEME_CONFIG = {
        "mode": "light",
        "light": {
            "primary": "#3B82F6",
            "secondary": "#ffffff",
            "background": "#ffffff",
            "text": "#000000"
        },
        "dark": {
            "primary": "#2563EB",
            "secondary": "#1f2937",
            "background": "#111827",
            "text": "#ffffff"
        }
    }

    # Use deepcopy to avoid mutating the global DEFAULT_THEME_CONFIG across requests
    import copy
    theme_config = copy.deepcopy(DEFAULT_THEME_CONFIG)

    # 1. Resolve Legacy Colors (Priority: WidgetSettings > WidgetConfig > Tenant.tema > Default)
    legacy_primary = None
    legacy_secondary = None

    if widget_settings:
        try:
            legacy_primary = getattr(widget_settings, "primary_color", None) or legacy_primary
            legacy_secondary = getattr(widget_settings, "secondary_color", None) or legacy_secondary
        except Exception:
            pass

    if not legacy_primary and widget_cfg and widget_cfg.primary_color:
        legacy_primary = widget_cfg.primary_color
    if not legacy_secondary and widget_cfg and widget_cfg.accent_color:
        legacy_secondary = widget_cfg.accent_color

    if not legacy_primary and isinstance(tenant.tema, dict) and tenant.tema.get("primaryColor"):
         legacy_primary = tenant.tema.get("primaryColor")
    if not legacy_secondary and isinstance(tenant.tema, dict) and tenant.tema.get("secondaryColor"):
         legacy_secondary = tenant.tema.get("secondaryColor")

    # 2. Populate theme_config from Legacy Colors if not explicitly provided
    # This ensures that older tenants who haven't set up the new theme config still get their brand colors
    # applied to the new variable system (light/dark modes).

    has_explicit_theme_config = False
    if widget_settings:
        try:
            raw_theme_config = getattr(widget_settings, "theme_config", None)
            has_explicit_theme_config = isinstance(raw_theme_config, dict) and raw_theme_config
        except Exception:
            pass

    if has_explicit_theme_config:
        # Re-fetch safely
        ws_config = getattr(widget_settings, "theme_config", {})
        theme_config["mode"] = ws_config.get("mode", theme_config["mode"])
        if isinstance(ws_config.get("light"), dict):
            theme_config["light"].update(ws_config["light"])
        if isinstance(ws_config.get("dark"), dict):
            theme_config["dark"].update(ws_config["dark"])
    else:
        # Auto-generate theme config from legacy colors
        if legacy_primary:
            theme_config["light"]["primary"] = legacy_primary
            theme_config["dark"]["primary"] = legacy_primary # Simple mapping for now
            theme_config["light"]["text"] = "#000000" # Ensure readability

        if legacy_secondary:
            theme_config["light"]["secondary"] = legacy_secondary
            theme_config["dark"]["secondary"] = legacy_secondary

    # 3. Update the legacy 'theme' object for backward compatibility
    if legacy_primary:
        theme["primaryColor"] = legacy_primary
    if legacy_secondary:
        theme["secondaryColor"] = legacy_secondary

    theme["config"] = theme_config

    # Features logic replicated from auth helper to avoid circular imports
    features = {}
    widget_features = cfg.get("widget_features")
    if isinstance(widget_features, dict):
        features.update(widget_features)

    catalog_enabled = cfg.get("widget_catalog_enabled")
    if isinstance(catalog_enabled, bool):
        features.setdefault("catalog_enabled", catalog_enabled)
    else:
        features.setdefault("catalog_enabled", (tenant.tipo or "").lower() == "pyme")

    loyalty_enabled = cfg.get("widget_loyalty_enabled")
    if isinstance(loyalty_enabled, bool):
        features.setdefault("loyalty_enabled", loyalty_enabled)

    contact = {}
    if owner:
        if owner.telefono:
            contact["whatsapp"] = owner.telefono
        if owner.email:
            contact["email"] = owner.email

    # Prepare interaction config (CTAs)
    interaction = {}
    cta_messages = []
    default_open = False

    if widget_settings:
        try:
            # Defensive access to support legacy schemas or pending migrations
            cta_msgs_raw = getattr(widget_settings, "cta_messages", None)
            if cta_msgs_raw:
                # Ensure cta_messages is a list to prevent frontend map() crashes
                if isinstance(cta_msgs_raw, list):
                    interaction["cta_messages"] = cta_msgs_raw
                    cta_messages = cta_msgs_raw
                else:
                    # If malformed (e.g. dict or string), wrap or ignore
                    pass

            w_title = getattr(widget_settings, "welcome_title", None)
            if w_title:
                interaction["welcome_title"] = w_title

            w_subtitle = getattr(widget_settings, "welcome_subtitle", None)
            if w_subtitle:
                interaction["welcome_subtitle"] = w_subtitle

            d_open = getattr(widget_settings, "default_open", None)
            if d_open is not None:
                interaction["default_open"] = d_open
                default_open = d_open
        except Exception:
            # Fallback if widget_settings causes DB errors (e.g. missing columns)
            pass

    # Legacy fallback for welcome message
    if not interaction.get("welcome_title") and widget_cfg and widget_cfg.welcome_message:
        interaction["welcome_title"] = widget_cfg.welcome_message

    # Resolve entity token (widgetToken)
    entity_token = None
    if owner:
        try:
            from routes.auth import _resolve_owner_token
            entity_token = _resolve_owner_token(owner)
        except (ImportError, Exception):
            # If auth module fails to import or resolution fails, ignore and use fallback
            entity_token = None

    # Fallback for tenants without an owner (e.g. Vercel previews or headless demos)
    # Ensures frontend socket initialization doesn't crash on "No entityToken"
    if not entity_token:
        entity_token = tenant.slug

    return jsonify({
        "slug": tenant.slug,
        "name": tenant.nombre,
        "logo_url": tenant.logo_url or (widget_cfg.logo_url if widget_cfg else "") or "",
        "theme": theme,
        "theme_config": theme_config,
        "features": features,
        "contact": contact,
        "interaction": interaction,
        "cta_messages": cta_messages,
        "default_open": default_open,
        "entityToken": entity_token, # Added for frontend socket initialization
        "widgetToken": entity_token  # Alias for compatibility
    })


@public_api_bp.get("/tenant")
def public_tenant_info():
    tenant = _require_tenant()
    return jsonify({
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "logo_url": tenant.logo_url,
        "tipo": tenant.tipo,
        "tema": tenant.tema or {}
    })


@public_api_bp.get("/news")
def public_news():
    tenant = _require_tenant()
    query = _posts_query_for_tenant(tenant).filter(MunicipioPost.tipo_post != "evento")
    items = (
        query.order_by(MunicipioPost.fecha_publicacion.desc())
        .limit(50)
        .all()
    )
    return jsonify([{
        "id": str(item.id),
        "title": item.titulo,
        "summary": item.subtitulo or (item.descripcion[:100] + "..."),
        "body": item.descripcion,
        "cover_url": item.imagen_url,
        "publicado_at": item.fecha_publicacion.isoformat() if item.fecha_publicacion else None,
        "tags": item.tags
    } for item in items])


@public_api_bp.get("/events")
def public_events():
    tenant = _require_tenant()
    query = _posts_query_for_tenant(tenant).filter(MunicipioPost.tipo_post == "evento")
    items = (
        query.order_by(MunicipioPost.fecha_evento_inicio.asc().nullslast())
        .limit(50)
        .all()
    )
    return jsonify([{
        "id": str(item.id),
        "title": item.titulo,
        "descripcion": item.descripcion,
        "cover_url": item.imagen_url,
        "starts_at": item.fecha_evento_inicio.isoformat() if item.fecha_evento_inicio else None,
        "ends_at": item.fecha_evento_fin.isoformat() if item.fecha_evento_fin else None,
        "lugar": item.ubicacion
    } for item in items])


@public_api_bp.get("/encuestas")
def public_surveys():
    # Reuse existing logic which is standardized
    return list_surveys()


@pwa_public_bp.get("/events")
def list_events():
    tenant = _require_tenant()
    query = _posts_query_for_tenant(tenant).filter(MunicipioPost.tipo_post == "evento")
    items = (
        query.order_by(MunicipioPost.fecha_evento_inicio.asc().nullslast())
        .limit(100)
        .all()
    )
    return jsonify([item.to_dict() for item in items])

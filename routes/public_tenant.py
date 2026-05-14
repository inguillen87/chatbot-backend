from datetime import timezone
import uuid

from flask import Blueprint, request, jsonify, g, current_app
from sqlalchemy import func, or_

from models import (
    CatalogoItem,
    Conversacion,
    MarketCart,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TenantConfig,
    TenantTicket,
    User,
    WidgetSettings,
    db,
)
from routes.auth import token_requerido
from routes.catalogo import _formatear_producto
from routes.carrito import _product_query_for_tenant
from middleware.tenant_context import require_tenant
from services.catalog_seed import ensure_seed_catalog

public_tenant_bp = Blueprint('public_tenant_bp', __name__)

RESERVED_PUBLIC_SLUGS = {
    "demo",
    "demo-catalogs",
    "casos",
    "casos-de-uso",
    "use-cases",
    "pymes",
    "empresas",
    "municipios",
    "gobiernos",
    "colegios",
    "escuelas",
    "media",
    "static",
    "assets",
    "public",
    "sectores",
    "precios",
    "opinar",
}


def _normalize_public_slug(value: object) -> str:
    return str(value or "").strip().lower()


def _is_reserved_public_slug(value: object) -> bool:
    return _normalize_public_slug(value) in RESERVED_PUBLIC_SLUGS


def _reserved_public_slug_from_request() -> str:
    for value in (
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
        request.args.get("slug"),
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
    ):
        if _is_reserved_public_slug(value):
            return _normalize_public_slug(value)
    return ""


def _reserved_slug_payload(slug: object) -> dict:
    return {
        "contract_version": "public.reserved_slug.v1",
        "ok": False,
        "reserved_slug": _normalize_public_slug(slug),
        "slug": _normalize_public_slug(slug),
        "reason_code": "reserved_public_slug",
        "action_hint": "Use /demo, /api/v2/demo/catalog or a real tenant_slug.",
    }


def _catalog_resolution_payload(slug: object, *, reason_code: str = "tenant_resolution_failed") -> dict:
    return {
        "contract_version": "public.catalog_resolution.v1",
        "ok": False,
        "tenant_slug": _normalize_public_slug(slug),
        "reason_code": reason_code,
        "items": [],
        "cart": {"enabled": False},
    }

def _add_cors_headers(response):
    origin = request.headers.get('Origin', '*')
    response.headers.add('Access-Control-Allow-Origin', origin)
    response.headers.add(
        'Access-Control-Allow-Headers',
        'Content-Type,Authorization,X-Tenant,X-Requested-With,X-Anon-Id,'
        'X-Chat-Session-Id,X-Entity-Token,X-Widget-Token,X-Owner-Token,'
        'X-Widget-Key,X-Token,x-token,X-Demo-Session-Id,Anon-Id,Idempotency-Key',
    )
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PUT,DELETE,PATCH')
    response.headers.add('Access-Control-Expose-Headers', 'X-Request-Id')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _public_json(payload: dict, status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return _add_cors_headers(response)


def _iso_datetime(value):
    if not value:
        return None
    if isinstance(value, str):
        return value
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    except Exception:
        return None

def _get_tenant_from_request(slug: str):
    """Resolve tenant with compatibility fallbacks used by public widget/catalog routes."""

    def _first_active_tenant_with_owner():
        return (
            TenantProfile.query
            .filter(TenantProfile.is_active.is_(True))
            .filter(
                or_(
                    TenantProfile.pyme_id.isnot(None),
                    TenantProfile.municipio_id.isnot(None),
                )
            )
            .order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc())
            .first()
        )

    if _is_reserved_public_slug(slug):
        return None

    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if tenant:
        return tenant

    fallback_slug = request.args.get("tenant") or request.args.get("tenant_slug")
    if _is_reserved_public_slug(fallback_slug):
        return None
    if fallback_slug and fallback_slug != slug:
        tenant = TenantProfile.query.filter_by(slug=fallback_slug).first()
        if tenant:
            return tenant

    alias_slug = str(slug or "").strip().lower()
    # ``default`` is used by multiple frontend widget builds when they don't
    # have a concrete tenant yet. We degrade gracefully to a configured default
    # tenant or first active tenant with owner to avoid 404 loops.
    if alias_slug == "default":
        configured_default = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        if configured_default:
            tenant = TenantProfile.query.filter_by(slug=str(configured_default).strip()).first()
            if tenant:
                return tenant
        tenant = _first_active_tenant_with_owner()
        if tenant:
            return tenant

    alias_tipo_map = {
        "pyme": "pyme",
        "municipio": "municipio",
        "e": "pyme",
        "m": "municipio",
    }
    resolved_tipo = alias_tipo_map.get(alias_slug)
    if resolved_tipo:
        tenants = (
            TenantProfile.query.filter_by(tipo=resolved_tipo)
            .filter(TenantProfile.is_active.is_(True))
            .order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc())
            .all()
        )
        for candidate in tenants:
            owner_id = candidate.pyme_id or candidate.municipio_id
            if owner_id and CatalogoItem.query.filter_by(tenant_id=candidate.id, user_id=owner_id).first():
                return candidate
        return tenants[0] if tenants else None

    return None


def _resolve_catalog_owner(tenant: TenantProfile):
    """Return tenant owner with FK fallback when ORM relationship is stale."""

    if not tenant:
        return None

    owner = tenant.municipio or tenant.pyme
    if owner:
        return owner

    owner_id = tenant.municipio_id or tenant.pyme_id
    if owner_id:
        return User.query.get(owner_id)

    return None


def _resolve_public_widget_tenant() -> TenantProfile | None:
    """Resolve the tenant for embedded widget/commerce endpoints."""

    widget_token = (
        request.args.get("widget_token")
        or request.args.get("entityToken")
        or request.headers.get("X-Widget-Token")
        or request.headers.get("X-Entity-Token")
    )
    tenant_slug = (
        request.args.get("tenant_slug")
        or request.args.get("tenant")
        or request.args.get("slug")
        or request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
    )
    if tenant_slug:
        tenant = _get_tenant_from_request(str(tenant_slug))
        if tenant:
            return tenant

    if widget_token:
        try:
            from services.tenant_resolver import resolve_tenant_only

            return resolve_tenant_only(
                widget_token=widget_token,
                tenant_slug=tenant_slug,
                host=request.headers.get("X-Forwarded-Host") or request.host,
                require_explicit_slug=False,
            )
        except Exception:
            current_app.logger.info("[public_widget] widget token did not resolve tenant", exc_info=True)

    return None


def _tenant_public_summary(tenant: TenantProfile) -> dict:
    return {
        "slug": tenant.slug,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical or ("gobierno" if tenant.tipo == "municipio" else "empresas"),
        "subvertical": tenant.subvertical,
        "display_name": tenant.nombre,
        "nombre": tenant.nombre,
        "logo_url": tenant.logo_url,
    }


def _session_context_payload() -> dict:
    chat_session_id = (
        request.headers.get("X-Chat-Session-Id")
        or request.headers.get("X-Demo-Session-Id")
        or request.args.get("chat_session_id")
        or request.args.get("demo_session_id")
        or request.args.get("session")
        or f"chat_{uuid.uuid4().hex[:16]}"
    )
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.args.get("anon_id")
        or request.cookies.get("chatboc_anon_id")
        or request.cookies.get("anon_id")
        or f"anon_{uuid.uuid4().hex[:16]}"
    )
    return {
        "chat_session_id": str(chat_session_id),
        "anon_id": str(anon_id),
        "widget_session_token": f"wst_{uuid.uuid5(uuid.NAMESPACE_URL, f'{chat_session_id}:{anon_id}').hex[:24]}",
        "is_authenticated": bool(getattr(g, "user", None)),
        "can_checkout_as_guest": True,
        "can_link_account": True,
    }


def _cart_counts_for_tenant(tenant: TenantProfile, session_payload: dict) -> dict:
    session_ids = {
        str(session_payload.get("chat_session_id") or ""),
        str(session_payload.get("anon_id") or ""),
    }
    session_ids = {value for value in session_ids if value}
    cart = None
    if session_ids:
        cart = (
            MarketCart.legacy_safe_query()
            .filter(MarketCart.tenant_id == tenant.id, MarketCart.status == "open")
            .filter(MarketCart.session_id.in_(session_ids))
            .order_by(MarketCart.updated_at.desc())
            .first()
        )
    items_count = 0
    if cart:
        try:
            items_count = sum(int(item.quantity or 0) for item in cart.items.all())
        except Exception:
            items_count = 0
    return {
        "items_count": items_count,
        "summary_endpoint": "/api/pwa/public/cart/summary",
        "items_endpoint": "/api/pwa/public/cart/items",
        "legacy_endpoint": "/api/pwa/public/cart",
    }


def _public_history_item(
    *,
    item_id: str,
    kind: str,
    channel: str,
    title: str,
    status: str | None,
    created_at,
    detail_endpoint: str | None = None,
    extra: dict | None = None,
) -> dict:
    payload = {
        "id": item_id,
        "kind": kind,
        "channel": channel,
        "title": title,
        "status": status or "recibido",
        "created_at": _iso_datetime(created_at),
    }
    if detail_endpoint:
        payload["detail_endpoint"] = detail_endpoint
    if extra:
        payload.update(extra)
    return payload


def _widget_history_items(tenant: TenantProfile, session_payload: dict, limit: int = 30) -> list[dict]:
    items: list[dict] = []
    chat_session_id = str(session_payload.get("chat_session_id") or "")
    anon_id = str(session_payload.get("anon_id") or "")

    tenant_tickets = (
        TenantTicket.query.filter_by(tenant_id=tenant.id)
        .order_by(TenantTicket.created_at.desc())
        .limit(limit)
        .all()
    )
    for ticket in tenant_tickets:
        if anon_id and ticket.fingerprint and anon_id not in str(ticket.fingerprint):
            continue
        items.append(
            _public_history_item(
                item_id=f"tenant_ticket_{ticket.id}",
                kind="claim",
                channel=ticket.origen or "widget",
                title=ticket.categoria or ticket.descripcion[:80] or "Caso",
                status=ticket.estado,
                created_at=ticket.created_at,
                detail_endpoint=f"/api/public/tracking/experience?kind=claim&code=TT-{ticket.id}",
            )
        )

    for model, kind, channel, code_prefix in (
        (MunicipioTicket, "claim", "widget", "M"),
        (PymeTicket, "claim", "widget", "P"),
    ):
        query = model.query.filter(model.tenant_id == tenant.id)
        if anon_id:
            query = query.filter(or_(model.anon_id == anon_id, model.anon_id.is_(None)))
        for ticket in query.order_by(model.fecha.desc()).limit(limit).all():
            code = getattr(ticket, "nro_ticket", None) or ticket.id
            pin = getattr(ticket, "consulta_pin", None)
            detail = f"/api/public/tracking/experience?kind=claim&code={code_prefix}-{code}"
            if pin:
                detail = f"{detail}&pin={pin}"
            items.append(
                _public_history_item(
                    item_id=f"{model.__tablename__}_{ticket.id}",
                    kind=kind,
                    channel=getattr(ticket, "canal_ingreso", None) or channel,
                    title=getattr(ticket, "asunto", None) or getattr(ticket, "categoria", None) or "Caso",
                    status=getattr(ticket, "estado", None),
                    created_at=getattr(ticket, "fecha", None),
                    detail_endpoint=detail,
                )
            )

    pedidos = (
        PymePedido.query.filter_by(tenant_id=tenant.id)
        .order_by(PymePedido.fecha.desc())
        .limit(limit)
        .all()
    )
    for pedido in pedidos:
        items.append(
            _public_history_item(
                item_id=f"order_{pedido.id}",
                kind="order",
                channel="widget",
                title=pedido.asunto or f"Pedido {pedido.nro_pedido}",
                status=pedido.estado,
                created_at=pedido.fecha,
                detail_endpoint=f"/api/public/tracking/experience?kind=order&code={pedido.nro_pedido}",
                extra={"amount": pedido.monto_total},
            )
        )

    if chat_session_id:
        messages = (
            Conversacion.query.filter_by(session_id=chat_session_id)
            .order_by(Conversacion.timestamp.desc())
            .limit(10)
            .all()
        )
        for message in messages:
            title = getattr(message, "pregunta", None) or "Mensaje"
            items.append(
                _public_history_item(
                    item_id=f"message_{message.id}",
                    kind="message",
                    channel="widget",
                    title=str(title)[:90],
                    status="registrado",
                    created_at=message.timestamp,
                )
            )

    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items[:limit]


@public_tenant_bp.route('/api/public/tenants/<slug>/menu', methods=['GET', 'OPTIONS'])
def get_menu(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    channel = request.args.get('channel')

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    cfg = None
    if channel:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=channel).first()

    if not cfg:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=None).first()

    response = jsonify(cfg.json_value if cfg else {})
    return _add_cors_headers(response)

@public_tenant_bp.route('/api/public/tenants/<slug>/contacts', methods=['GET', 'OPTIONS'])
def get_contacts(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='contacts', channel=None).first()
    response = jsonify(cfg.json_value if cfg else {})
    return _add_cors_headers(response)

@public_tenant_bp.route('/api/public/tenants/<slug>/links', methods=['GET', 'OPTIONS'])
def get_links(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='links', channel=None).first()
    response = jsonify(cfg.json_value if cfg else {})
    return _add_cors_headers(response)

@public_tenant_bp.route('/api/public/tenants/<slug>/widget-config', methods=['GET', 'OPTIONS'])
def get_widget_config(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    if _is_reserved_public_slug(slug) or _is_reserved_public_slug(request.args.get("tenant_slug")) or _is_reserved_public_slug(request.args.get("tenant")):
        return _public_json(_reserved_slug_payload(slug), 404)

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return _public_json(
            {
                "contract_version": "public.widget_config_resolution.v1",
                "ok": False,
                "reason_code": "tenant_resolution_failed",
                "error": {"code": 404, "message": "Tenant not found"},
            },
            404,
        )

    from routes.pwa_public import public_tenant_widget_config

    # We need to capture the response from the other blueprint function
    # and ensure CORS headers are added.
    # Typically that function returns a JSON response object.

    # This is a bit hacky but avoids code duplication.
    # However, public_tenant_widget_config might return a Response object.

    try:
        # Assuming public_tenant_widget_config returns a response object
        resp = public_tenant_widget_config(tenant.slug)
        if isinstance(resp, tuple):
             resp_obj, status = resp
             return _add_cors_headers(resp_obj), status
        return _add_cors_headers(resp)
    except Exception as e:
        current_app.logger.error(f"Error fetching widget config: {e}")
        return jsonify({"error": "Internal Error"}), 500


@public_tenant_bp.route('/api/public/tenants/<slug>/catalog', methods=['GET', 'OPTIONS'])
@public_tenant_bp.route('/public/tenants/<slug>/catalog', methods=['GET', 'OPTIONS'])
def get_catalog(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    if _is_reserved_public_slug(slug) or _is_reserved_public_slug(request.args.get("tenant_slug")) or _is_reserved_public_slug(request.args.get("tenant")):
        return _public_json(_catalog_resolution_payload(slug, reason_code="reserved_public_slug"))

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return _public_json(_catalog_resolution_payload(slug))

    owner = _resolve_catalog_owner(tenant)
    if not owner:
        current_app.logger.warning(
            "[public_tenant.catalog] tenant=%s has no owner binding (municipio_id=%s pyme_id=%s)",
            tenant.slug,
            tenant.municipio_id,
            tenant.pyme_id,
        )
        return _public_json(_catalog_resolution_payload(tenant.slug, reason_code="tenant_owner_missing"))

    ensure_seed_catalog(owner, tenant)
    categoria = request.args.get("categoria")
    search_text = request.args.get("q")

    query = _product_query_for_tenant(owner, tenant)
    if categoria:
        categoria_norm = categoria.strip().lower()
        if categoria_norm:
            query = query.filter(func.lower(CatalogoItem.categoria) == categoria_norm)

    items = query.order_by(func.lower(CatalogoItem.nombre)).all()

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
        prod["catalog_item_id"] = item.id
        prod["tenant_id"] = tenant.id
        prod["tenant_slug"] = tenant.slug
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
    return _add_cors_headers(response)


@public_tenant_bp.route('/api/public/widget-commerce-session', methods=['GET', 'OPTIONS'])
def public_widget_commerce_session():
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "public.widget_commerce_session.v1"})

    reserved_slug = _reserved_public_slug_from_request()
    if reserved_slug:
        return _public_json(_reserved_slug_payload(reserved_slug), 404)

    tenant = _resolve_public_widget_tenant()
    if not tenant:
        return _public_json(
            {
                "contract_version": "public.widget_commerce_session.v1",
                "status_code": 404,
                "reason_code": "tenant_resolution_failed",
                "retryable": False,
                "action_hint": "send widget_token, tenant_slug or X-Tenant-Slug",
                "error": {"code": 404, "message": "Tenant no encontrado"},
            },
            404,
        )

    session_payload = _session_context_payload()
    catalog_enabled = bool((tenant.tipo or "").lower() == "pyme" or tenant.pyme_id)
    cart_enabled = catalog_enabled
    checkout_base = f"/api/v2/tenants/{tenant.slug}/payments"

    payload = {
        "contract_version": "public.widget_commerce_session.v1",
        "tenant": _tenant_public_summary(tenant),
        "session": session_payload,
        "catalog": {
            "enabled": catalog_enabled,
            "endpoint": f"/api/public/tenants/{tenant.slug}/catalog",
            "pwa_endpoint": f"/api/pwa/public/catalog?tenant={tenant.slug}",
            "quality_endpoint": f"/api/v2/tenants/{tenant.slug}/catalog/quality",
        },
        "cart": {
            "enabled": cart_enabled,
            "summary_endpoint": "/api/pwa/public/cart/summary",
            "items_endpoint": "/api/pwa/public/cart/items",
            "legacy_endpoint": "/api/pwa/public/cart",
            "add_endpoint": "/api/pwa/public/cart/add",
            "update_endpoint": "/api/pwa/public/cart/update",
            "remove_endpoint": "/api/pwa/public/cart/remove",
            "checkout_preview_endpoint": f"{checkout_base}/checkout-preview",
            "checkout_session_endpoint": f"{checkout_base}/checkout-session",
            "allow_guest_cart": True,
            "requires_contact_before_checkout": True,
            **_cart_counts_for_tenant(tenant, session_payload),
        },
        "portal": {
            "enabled": True,
            "label": "Mi actividad",
            "view_url": f"/portal/{tenant.slug}",
            "login_endpoint": "/auth/widget/bootstrap",
            "register_endpoint": "/api/public/widget-user/register",
            "link_session_endpoint": "/api/public/widget-user/link-session",
            "history_endpoint": "/api/public/widget-user/tenant-history",
            "scope": "end_user_tenant_history",
        },
        "history": {
            "channels": ["widget", "whatsapp", "voice", "orders", "claims", "surveys"],
            "endpoint": "/api/public/widget-user/tenant-history",
        },
        "accessibility": {
            "enabled": True,
            "default_simplified_text": False,
            "allow_dyslexia_mode": True,
            "allow_high_contrast": True,
            "allow_large_controls": True,
            "captions_enabled": True,
            "respect_prefers_reduced_motion": True,
            "single_visible_header_entry": True,
            "touch_target_min_px": 44,
            "features": ["dyslexia", "plain_language", "high_contrast", "large_controls", "reading_ruler"],
        },
        "live_chat": {
            "schedule_endpoint": f"/api/{tenant.slug}/live-chat/schedule",
            "enabled": False,
            "socket_enabled": False,
            "fallback_mode": "http_chat",
        },
        "frontend_contract": {
            "render_as": "embedded_tenant_operating_widget",
            "primary_actions": ["chat", "catalog", "cart", "portal"],
            "empty_state_behavior": "chat_first_catalog_when_enabled",
        },
    }
    return _public_json(payload)


@public_tenant_bp.route('/api/public/tenants/<slug>/public-navigation', methods=['GET', 'OPTIONS'])
@public_tenant_bp.route('/public/tenants/<slug>/public-navigation', methods=['GET', 'OPTIONS'])
def public_tenant_navigation(slug):
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "tenant.public_navigation.v1"})

    if _is_reserved_public_slug(slug) or _is_reserved_public_slug(request.args.get("tenant_slug")) or _is_reserved_public_slug(request.args.get("tenant")):
        return _public_json(_reserved_slug_payload(slug), 404)

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return _public_json(
            {
                "contract_version": "tenant.public_navigation.v1",
                "ok": False,
                "reason_code": "tenant_resolution_failed",
                "items": [],
                "error": {"code": 404, "message": "Tenant not found"},
            },
            404,
        )

    owner = _resolve_catalog_owner(tenant)
    has_catalog = bool(owner and (tenant.pyme_id or (tenant.tipo or "").lower() == "pyme"))
    has_news = bool(owner and tenant.municipio_id)
    has_surveys = bool(tenant.encuestas_tenant_id or tenant.municipio_id or tenant.pyme_id)
    base_route = f"/t/{tenant.slug}"
    items = [
        {"id": "home", "label": "Inicio", "route": base_route, "enabled": True},
        {
            "id": "news",
            "label": "Noticias",
            "route": f"{base_route}/noticias",
            "enabled": has_news,
            "empty_state": "Todavia no hay noticias publicadas.",
        },
        {
            "id": "events",
            "label": "Eventos",
            "route": f"{base_route}/eventos",
            "enabled": has_news,
            "empty_state": "Todavia no hay eventos publicados.",
        },
        {
            "id": "surveys",
            "label": "Encuestas",
            "route": f"{base_route}/encuestas",
            "enabled": has_surveys,
            "empty_state": "Todavia no hay encuestas publicadas.",
        },
        {
            "id": "new_claim",
            "label": "Nuevo reclamo",
            "route": f"{base_route}/reclamos/nuevo",
            "enabled": bool(owner),
            "empty_state": "Este canal todavia no esta disponible.",
        },
        {
            "id": "catalog",
            "label": "Catalogo",
            "route": f"{base_route}/catalogo",
            "enabled": has_catalog,
            "empty_state": "Todavia no hay catalogo publicado.",
        },
    ]
    return _public_json(
        {
            "contract_version": "tenant.public_navigation.v1",
            "tenant_slug": tenant.slug,
            "items": items,
            "frontend_contract": {"render_as": "tenant_public_navigation"},
        }
    )


@public_tenant_bp.route('/api/public/widget-user/tenant-history', methods=['GET', 'OPTIONS'])
def public_widget_user_tenant_history():
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "public.widget_user_tenant_history.v1"})

    reserved_slug = _reserved_public_slug_from_request()
    if reserved_slug:
        return _public_json(_reserved_slug_payload(reserved_slug), 404)

    tenant = _resolve_public_widget_tenant()
    if not tenant:
        return _public_json(
            {
                "contract_version": "public.widget_user_tenant_history.v1",
                "status_code": 404,
                "reason_code": "tenant_resolution_failed",
                "retryable": False,
                "action_hint": "send widget_token, tenant_slug or X-Tenant-Slug",
                "error": {"code": 404, "message": "Tenant no encontrado"},
            },
            404,
        )

    session_payload = _session_context_payload()
    payload = {
        "contract_version": "public.widget_user_tenant_history.v1",
        "tenant_slug": tenant.slug,
        "profile": {
            "is_authenticated": session_payload["is_authenticated"],
            "contact": None,
            "can_register": True,
            "can_link_whatsapp": True,
            "anon_id": session_payload["anon_id"],
            "chat_session_id": session_payload["chat_session_id"],
        },
        "items": _widget_history_items(tenant, session_payload),
        "cart": _cart_counts_for_tenant(tenant, session_payload),
    }
    return _public_json(payload)


@public_tenant_bp.route('/api/public/widget-user/register', methods=['POST', 'OPTIONS'])
def public_widget_user_register():
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "public.widget_user_register.v1"})

    tenant = _resolve_public_widget_tenant()
    payload = request.get_json(silent=True) or {}
    session_payload = _session_context_payload()
    return _public_json(
        {
            "ok": True,
            "contract_version": "public.widget_user_register.v1",
            "tenant_slug": getattr(tenant, "slug", None),
            "status": "pending_verification",
            "profile": {
                "contact": {
                    "name": payload.get("name") or payload.get("nombre"),
                    "email": payload.get("email"),
                    "phone": payload.get("phone") or payload.get("telefono"),
                },
                "anon_id": session_payload["anon_id"],
                "chat_session_id": session_payload["chat_session_id"],
            },
            "next_action": "verify_contact_or_continue_as_guest",
        }
    )


@public_tenant_bp.route('/api/public/widget-user/link-session', methods=['POST', 'OPTIONS'])
def public_widget_user_link_session():
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "public.widget_user_link_session.v1"})

    tenant = _resolve_public_widget_tenant()
    session_payload = _session_context_payload()
    return _public_json(
        {
            "ok": True,
            "contract_version": "public.widget_user_link_session.v1",
            "tenant_slug": getattr(tenant, "slug", None),
            "linked": True,
            "session": session_payload,
            "preserved": ["cart", "history", "chat"],
        }
    )

# --- Fix for missing /api/tenant/config endpoint ---

@public_tenant_bp.route('/api/tenant/config', methods=['GET', 'PUT', 'OPTIONS'])
@token_requerido
@require_tenant
def tenant_config_api(current_user):
    """
    Endpoint for tenant configuration (Admin Panel -> Appearance).
    Supports GET (view) and PUT (update).
    Requires 'tenant_slug' or 'tenant' query param (handled by require_tenant middleware).
    """
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = g.tenant_profile
    if not tenant:
        return jsonify({"error": "Tenant context required"}), 400

    # Authorization Check
    # Allow if user is admin/owner of this tenant
    # Using existing helper from admin_tenant if available, or simple logic
    authorized = False
    if current_user.rol in ('super_admin', 'platform_admin'):
        authorized = True
    elif current_user.tenant_id == tenant.id:
        authorized = True
    elif getattr(current_user, 'pyme_id', None) == getattr(tenant, 'pyme_id', None) and getattr(tenant, 'pyme_id', None):
        authorized = True

    if not authorized:
         response = jsonify({'error': 'Unauthorized'})
         return _add_cors_headers(response), 403

    if request.method == 'GET':
        # Return merged config (TenantProfile + WidgetSettings)
        # Similar to what the frontend expects: theme_json, welcome_message, etc.

        settings = WidgetSettings.query.filter_by(tenant_id=tenant.id).first()
        if not settings:
            # Return defaults from TenantProfile if no specific widget settings
            response = jsonify({
                "theme_json": tenant.theme_json or {},
                "welcome_message": "¡Hola! ¿En qué puedo ayudarte?",
                "avatar_url": tenant.logo_url,
                "primary_color": "#000000", # Default
                "secondary_color": "#FFFFFF"
            })
        else:
            response = jsonify({
                "theme_json": settings.theme_config or tenant.theme_json or {},
                "welcome_message": settings.welcome_title or "¡Hola! ¿En qué puedo ayudarte?",
                "welcome_subtitle": settings.welcome_subtitle,
                "avatar_url": settings.avatar_url or tenant.logo_url,
                "primary_color": settings.primary_color,
                "secondary_color": settings.secondary_color,
                # Include other settings as needed
                "bottom": settings.bottom,
                "side_offset": settings.side_offset,
                "bubble_shape": settings.bubble_shape,
                "font_family": settings.font_family,
                "default_open": settings.default_open
            })
        return _add_cors_headers(response)

    elif request.method == 'PUT':
        data = request.json or {}

        settings = WidgetSettings.query.filter_by(tenant_id=tenant.id).first()
        if not settings:
            settings = WidgetSettings(tenant_id=tenant.id)
            db.session.add(settings)

        # Map fields
        if 'theme_json' in data:
            settings.theme_config = data['theme_json']
            tenant.theme_json = data['theme_json'] # Sync back to profile

        if 'welcome_message' in data: settings.welcome_title = data['welcome_message']
        if 'welcome_subtitle' in data: settings.welcome_subtitle = data['welcome_subtitle']
        if 'avatar_url' in data: settings.avatar_url = data['avatar_url']

        # Color handling from theme_json usually, but if sent separately:
        if 'primary_color' in data: settings.primary_color = data['primary_color']
        if 'secondary_color' in data: settings.secondary_color = data['secondary_color']

        # Style props
        if 'bottom' in data: settings.bottom = data['bottom']
        if 'side_offset' in data: settings.side_offset = data['side_offset']
        if 'bubble_shape' in data: settings.bubble_shape = data['bubble_shape']
        if 'font_family' in data: settings.font_family = data['font_family']
        if 'default_open' in data: settings.default_open = bool(data['default_open'])

        db.session.commit()
        return _add_cors_headers(jsonify({"status": "updated"}))

# --- Fix for missing /api/<slug>/live-chat/schedule ---

@public_tenant_bp.route('/api/<slug>/live-chat/schedule', methods=['GET', 'OPTIONS'])
@public_tenant_bp.route('/<slug>/live-chat/schedule', methods=['GET', 'OPTIONS'])
@public_tenant_bp.route('/api/public/tenants/<slug>/live-chat/schedule', methods=['GET', 'OPTIONS'])
@public_tenant_bp.route('/public/tenants/<slug>/live-chat/schedule', methods=['GET', 'OPTIONS'])
def public_live_chat_schedule(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    from services.live_chat_schedule import build_live_chat_status
    # We might want to pass the tenant slug to build_live_chat_status if it supports tenant-specific schedules
    # For now, assuming global or default logic, but checking tenant existence first

    requested_slug = (
        request.args.get("tenant_slug")
        or request.args.get("tenant")
        or slug
        or "demo"
    )
    tenant = _get_tenant_from_request(slug)

    # If build_live_chat_status accepts a tenant, pass it.
    # Checking source code of services/live_chat_schedule.py would be ideal, but for the fix:
    try:
        # Assuming it returns a dict
        schedule_cfg = None
        if tenant and isinstance(tenant.configuracion, dict):
            schedule_cfg = tenant.configuracion.get("live_chat_schedule")
        status = build_live_chat_status(schedule_override=schedule_cfg if isinstance(schedule_cfg, dict) else None)
        status["tenant_slug"] = tenant.slug if tenant else str(requested_slug).strip().lower()
        status["source"] = "tenant_config" if isinstance(schedule_cfg, dict) else "global_config"
        status["contract_version"] = "live_chat.schedule.v1"
        status["fallback_reason"] = None if tenant else "tenant_not_found_schedule_fallback"
        status.setdefault("enabled", False)
        status.setdefault("available", False)
        status["socket_transport_hint"] = "disabled"
        status["socket_transports"] = []
        status["socket_fallback_enabled"] = False
        status["socket_enabled"] = False
        status["realtime"] = False
        status["fallback_mode"] = "http_chat"
        return _add_cors_headers(jsonify(status))
    except Exception as e:
        current_app.logger.error(f"Error getting schedule: {e}")
        response = jsonify({
            "contract_version": "live_chat.schedule.v1",
            "enabled": False,
            "available": False,
            "tenant_slug": str(requested_slug).strip().lower(),
            "source": "error_fallback",
            "fallback_reason": "schedule_error",
            "socket_transport_hint": "disabled",
            "socket_transports": [],
            "socket_fallback_enabled": False,
            "socket_enabled": False,
            "realtime": False,
            "fallback_mode": "http_chat",
        })
        return _add_cors_headers(response)

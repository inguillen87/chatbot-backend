from datetime import datetime, timezone
from io import BytesIO
import re
import uuid

from flask import Blueprint, request, jsonify, g, current_app, send_file
from sqlalchemy import false, func, or_

from models import (
    CatalogoItem,
    ChatSessionContext,
    Conversacion,
    MarketCart,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TenantFollower,
    TenantProfile,
    TenantConfig,
    TenantTicket,
    User,
    WebAuthnCredential,
    WidgetSettings,
    db,
)
from routes.auth import token_requerido
from routes.catalogo import _formatear_producto
from routes.carrito import _product_query_for_tenant
from middleware.tenant_context import require_tenant
from services.catalog_seed import ensure_seed_catalog
from services.encuestas_service import list_public_encuestas_for_tenant
from services.commerce_contracts import build_checkout_experience_payload
from services.marketplace_analytics import track_marketplace_event
from utils.roles import is_authorized_superadmin_user
from services.public_market_catalog import (
    build_public_market_catalog_contract,
    public_market_api_contract,
    public_market_assisted_intake,
)
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_full_integrations,
)
from services.tenant_resolver import tenant_slug_from_public_referrer, tenant_slug_lookup_candidates
from services.user_merge import merge_anon_into_user

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


def _canonical_public_slug(value: object) -> str:
    candidates = tenant_slug_lookup_candidates(str(value or ""))
    return candidates[-1] if candidates else _normalize_public_slug(value)


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


def _catalog_download_error(slug: object, reason_code: str, *, status_code: int = 404):
    return _public_json(
        {
            "contract_version": "public.catalog_download.v1",
            "ok": False,
            "tenant_slug": _normalize_public_slug(slug),
            "reason_code": reason_code,
            "error": {"code": status_code, "message": "Catalog download unavailable"},
        },
        status_code,
    )


def _catalog_download_filename(tenant: TenantProfile, fmt: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(tenant.slug or "catalogo").strip().lower()).strip("-")
    return f"catalogo-{slug or 'tenant'}.{fmt}"


def _build_catalog_download_contract(tenant: TenantProfile, owner: User) -> dict:
    ensure_seed_catalog(owner, tenant)
    return build_public_market_catalog_contract(
        tenant,
        owner,
        base_web_url=current_app.config.get("APP_BASE_URL", "https://chatboc.ar"),
        categoria=request.args.get("categoria"),
        q=request.args.get("q"),
        precio_min=request.args.get("precio_min"),
        precio_max=request.args.get("precio_max"),
        en_promocion=request.args.get("en_promocion"),
        sort=request.args.get("sort"),
    )


def _truncate_pdf_text(value: object, max_chars: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _write_catalog_pdf(payload: dict, tenant: TenantProfile) -> BytesIO:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _, height = A4
    left = 18 * mm
    top = height - 18 * mm
    line_height = 6 * mm
    y = top

    def new_page_if_needed(lines: int = 1) -> None:
        nonlocal y
        if y - (lines * line_height) < 18 * mm:
            pdf.showPage()
            y = top

    def draw_line(text: object, *, font: str = "Helvetica", size: int = 10) -> None:
        nonlocal y
        safe_text = _truncate_pdf_text(text, 190)
        new_page_if_needed()
        pdf.setFont(font, size)
        pdf.drawString(left, y, safe_text)
        y -= line_height

    pdf.setTitle(f"Catalogo {tenant.nombre or tenant.slug}")
    draw_line(f"Catalogo publico - {tenant.nombre or tenant.slug}", font="Helvetica-Bold", size=16)
    draw_line(f"Tenant: {tenant.slug}", size=9)
    draw_line(f"Generado: {datetime.now(timezone.utc).isoformat()}", size=8)
    y -= 3 * mm

    products = payload.get("products") or []
    draw_line(f"Productos visibles: {len(products)}", font="Helvetica-Bold", size=11)
    if not products:
        draw_line("El catalogo no tiene productos visibles. El marketplace mantiene carga asistida activa.", size=10)
    for index, product in enumerate(products, start=1):
        price = product.get("precio") or product.get("precio_str") or product.get("price") or ""
        category = product.get("categoria") or product.get("category") or "Sin categoria"
        stock = product.get("stock_status") or product.get("cantidad") or ""
        title = product.get("nombre") or product.get("name") or f"Producto {index}"
        draw_line(f"{index}. {title}", font="Helvetica-Bold", size=10)
        draw_line(f"Categoria: {category} | Precio: {price} | Stock: {stock}", size=9)
        description = product.get("descripcion") or product.get("description") or product.get("descripcion_corta")
        if description:
            draw_line(description, size=9)
        y -= 1 * mm

    pdf.save()
    buffer.seek(0)
    return buffer


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


def _public_widget_plan_required_payload(tenant: TenantProfile, contract_version: str) -> tuple[dict, int]:
    return integration_plan_required_payload(
        tenant,
        "widget_embed",
        contract_version=contract_version,
        render_as="integration_locked",
        hide_embed_copy=True,
        hide_widget_session=True,
    ), 403


def _public_widget_integration_allowed(tenant: TenantProfile, contract_version: str) -> tuple[bool, dict | None, int | None]:
    if plan_allows_full_integrations(tenant):
        return True, None, None
    payload, status = _public_widget_plan_required_payload(tenant, contract_version)
    return False, payload, status


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

    slug_candidates = tenant_slug_lookup_candidates(slug) or (_normalize_public_slug(slug),)
    for candidate in slug_candidates:
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == candidate.lower()).first()
        if tenant:
            return tenant

    if _is_reserved_public_slug(slug):
        return None

    fallback_slug = request.args.get("tenant") or request.args.get("tenant_slug")
    if fallback_slug and fallback_slug != slug:
        for candidate in tenant_slug_lookup_candidates(fallback_slug):
            tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == candidate.lower()).first()
            if tenant:
                return tenant
    if _is_reserved_public_slug(fallback_slug):
        return None

    alias_slug = str((slug_candidates[-1] if slug_candidates else slug) or "").strip().lower()
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
    referrer_slug = tenant_slug_from_public_referrer()
    if referrer_slug and (
        not tenant_slug or _canonical_public_slug(referrer_slug) != _canonical_public_slug(tenant_slug)
    ):
        tenant_slug = referrer_slug
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
    demo_session_id = (
        request.headers.get("X-Demo-Session-Id")
        or request.args.get("demo_session_id")
        or ""
    )
    chat_session_id = (
        request.headers.get("X-Chat-Session-Id")
        or request.args.get("chat_session_id")
        or request.args.get("session")
        or demo_session_id
        or f"chat_{uuid.uuid4().hex[:16]}"
    )
    chat_session_id = str(chat_session_id)
    if len(chat_session_id) > 36 or (chat_session_id.startswith("eyJ") and "." in chat_session_id):
        chat_session_id = f"sid_{uuid.uuid5(uuid.NAMESPACE_URL, chat_session_id).hex[:28]}"
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.args.get("anon_id")
        or request.cookies.get("chatboc_anon_id")
        or request.cookies.get("anon_id")
        or f"anon_{uuid.uuid4().hex[:16]}"
    )
    return {
        "chat_session_id": chat_session_id,
        "demo_session_id": str(demo_session_id) if demo_session_id else None,
        "anon_id": str(anon_id),
        "widget_session_token": f"wst_{uuid.uuid5(uuid.NAMESPACE_URL, f'{chat_session_id}:{anon_id}').hex[:24]}",
        "is_authenticated": bool(getattr(g, "viewer", None)),
        "can_checkout_as_guest": True,
        "can_link_account": True,
    }


def _history_session_context(
    tenant: TenantProfile,
    session_payload: dict,
    viewer: User | None,
) -> ChatSessionContext | None:
    """Validate that a public session bearer belongs to this tenant/actor."""

    chat_session_id = str(session_payload.get("chat_session_id") or "").strip()
    if not chat_session_id:
        return None
    query = ChatSessionContext.query.filter(
        ChatSessionContext.chat_session_id == chat_session_id,
        ChatSessionContext.tenant_id == tenant.id,
    )
    if viewer is not None:
        query = query.filter(ChatSessionContext.user_id == viewer.id)
    else:
        anon_id = str(session_payload.get("anon_id") or "").strip()
        if not anon_id:
            return None
        query = query.filter(
            ChatSessionContext.user_id.is_(None),
            ChatSessionContext.anon_id == anon_id,
        )
    return query.one_or_none()


def _history_viewer_for_tenant(
    tenant: TenantProfile,
    session_payload: dict,
) -> User | None:
    """Resolve an authenticated viewer or one unique opaque anon bearer."""

    authenticated = getattr(g, "viewer", None)
    if isinstance(authenticated, User):
        return authenticated

    anon_id = str(session_payload.get("anon_id") or "").strip()
    if not anon_id:
        return None
    follower_user_ids = TenantFollower.query.filter(
        TenantFollower.tenant_id == tenant.id
    ).with_entities(TenantFollower.user_id)
    matches = (
        User.query.filter(
            User.anon_id == anon_id,
            or_(
                User.tenant_id == tenant.id,
                User.id.in_(follower_user_ids),
            ),
        )
        .order_by(User.id.asc())
        .limit(2)
        .all()
    )
    viewer = matches[0] if len(matches) == 1 else None
    if viewer is not None and _anon_recovery_requires_strong_verification(viewer):
        return None
    return viewer


def _cart_counts_for_tenant(
    tenant: TenantProfile,
    session_payload: dict,
    *,
    viewer: User | None,
    verified_context: ChatSessionContext | None,
) -> dict:
    cart = None
    if viewer is not None:
        cart = (
            MarketCart.legacy_safe_query()
            .filter(
                MarketCart.tenant_id == tenant.id,
                MarketCart.status == "open",
                MarketCart.user_id == viewer.id,
            )
            .order_by(MarketCart.updated_at.desc())
            .first()
        )
    elif verified_context is not None:
        cart = (
            MarketCart.legacy_safe_query()
            .filter(
                MarketCart.tenant_id == tenant.id,
                MarketCart.status == "open",
                MarketCart.user_id.is_(None),
                MarketCart.session_id == verified_context.chat_session_id,
            )
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


def _widget_history_items(
    tenant: TenantProfile,
    session_payload: dict,
    *,
    viewer: User | None,
    verified_context: ChatSessionContext | None,
    limit: int = 30,
) -> list[dict]:
    items: list[dict] = []
    chat_session_id = str(session_payload.get("chat_session_id") or "")
    anon_id = str(session_payload.get("anon_id") or "")

    tenant_ticket_query = TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id)
    if viewer is None:
        tenant_ticket_query = tenant_ticket_query.filter(false())
    else:
        tenant_ticket_query = tenant_ticket_query.filter(TenantTicket.user_id == viewer.id)
    tenant_tickets = tenant_ticket_query.order_by(TenantTicket.created_at.desc()).limit(limit).all()
    for ticket in tenant_tickets:
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
        if viewer is not None:
            query = query.filter(model.user_id == viewer.id)
        elif anon_id:
            # Anonymous history is a bearer lookup: exact non-null ownership is
            # mandatory.  A NULL legacy owner is never a wildcard.
            query = query.filter(model.user_id.is_(None), model.anon_id == anon_id)
        else:
            query = query.filter(false())
        for ticket in query.order_by(model.fecha.desc()).limit(limit).all():
            code = getattr(ticket, "nro_ticket", None) or ticket.id
            detail = f"/api/public/tracking/experience?kind=claim&code={code_prefix}-{code}"
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

    pedido_query = PymePedido.query.filter(PymePedido.tenant_id == tenant.id)
    if viewer is None:
        # Legacy anonymous orders have no durable session/anon ownership
        # column.  Showing them by tenant alone would disclose every order.
        pedido_query = pedido_query.filter(false())
    else:
        pedido_query = pedido_query.filter(PymePedido.user_id == viewer.id)
    pedidos = pedido_query.order_by(PymePedido.fecha.desc()).limit(limit).all()
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

    if verified_context is not None:
        messages = (
            Conversacion.query.filter_by(session_id=verified_context.chat_session_id)
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


def _tenant_resolution_error_payload(contract_version: str) -> tuple[dict, int]:
    reserved_slug = _reserved_public_slug_from_request()
    if reserved_slug:
        return _reserved_slug_payload(reserved_slug), 404
    return (
        {
            "contract_version": contract_version,
            "ok": False,
            "reason_code": "tenant_resolution_failed",
            "message": "No se pudo resolver el tenant del widget.",
        },
        404,
    )


def _ensure_tenant_follower(user: User, tenant: TenantProfile) -> bool:
    existing = TenantFollower.query.filter_by(user_id=user.id, tenant_id=tenant.id).first()
    if existing:
        return False
    db.session.add(TenantFollower(user_id=user.id, tenant_id=tenant.id, notifications_enabled=True))
    return True


def _unique_user_for_anon(anon_id: object) -> tuple[User | None, bool]:
    """Return one exact anon principal and an ambiguity flag.

    ``User.anon_id`` is a legacy non-unique column.  Public registration/linking
    must never recover an arbitrary ``.first()`` when duplicate historical
    values exist.
    """

    normalized = str(anon_id or "").strip()
    if not normalized:
        return None, False
    matches = (
        User.query.filter(User.anon_id == normalized)
        .order_by(User.id.asc())
        .limit(2)
        .all()
    )
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


def _user_has_webauthn_credential(user: User | None) -> bool:
    if user is None:
        return False
    return (
        WebAuthnCredential.query.filter(WebAuthnCredential.user_id == user.id)
        .with_entities(WebAuthnCredential.id)
        .first()
        is not None
    )


def _anon_recovery_requires_strong_verification(user: User | None) -> bool:
    """Reject opaque ``anon_id`` recovery for accounts protected by a passkey.

    An ``anon_id`` is a convenience bearer, not an authentication factor.  A
    real authenticated viewer may continue managing their own account, but a
    public caller cannot use a copied/guessed anon value to mutate, link, or
    read the history of a WebAuthn-protected user.
    """

    if user is None:
        return False
    authenticated = getattr(g, "viewer", None)
    if isinstance(authenticated, User) and authenticated.id == user.id:
        return False
    return _user_has_webauthn_credential(user)


def _authenticated_viewer_matches_anon(
    authenticated: User | None,
    anon_user: User | None,
    anon_id: object,
) -> bool:
    """Require an authenticated principal to own the supplied anon bearer."""

    if authenticated is None:
        return False
    normalized = str(anon_id or "").strip()
    if not normalized:
        return False
    if anon_user is None:
        return True
    return bool(
        authenticated.id == anon_user.id
        and str(authenticated.anon_id or "").strip() == normalized
    )


def _is_provisional_passkey_user(user: User | None) -> bool:
    return bool(
        user is not None
        and str(user.email or "").lower().endswith("@passkey.chatboc")
        and _user_has_webauthn_credential(user)
    )


def _passkey_verification_required_payload(
    *,
    contract_version: str,
    tenant: TenantProfile,
    linked: bool | None = None,
) -> dict:
    payload = {
        "ok": False,
        "contract_version": contract_version,
        "tenant_slug": tenant.slug,
        "status": "verification_required",
        "reason_code": "passkey_verification_required",
        "next_action": "verify_passkey_or_login",
        "login_endpoint": "/auth/widget/bootstrap",
    }
    if linked is not None:
        payload["linked"] = linked
    return payload


def _stage_public_session_binding(
    tenant: TenantProfile,
    session_payload: dict,
    user: User | None,
) -> bool:
    """Stage one exact tenant/session binding or fail closed on collisions.

    Registration is the point where a new public session becomes attributable
    to the provisional account.  Existing session primary keys are accepted
    only when they already belong to that user, or are unclaimed records with
    the same opaque anon bearer in the same tenant.
    """

    chat_session_id = str(session_payload.get("chat_session_id") or "").strip()
    anon_id = str(session_payload.get("anon_id") or "").strip()
    if not chat_session_id or not anon_id:
        return False
    context = db.session.get(ChatSessionContext, chat_session_id)
    if context is None:
        db.session.add(
            ChatSessionContext(
                chat_session_id=chat_session_id,
                tenant_id=tenant.id,
                anon_id=anon_id,
            )
        )
        return True
    if context.tenant_id != tenant.id:
        return False
    if context.user_id is not None:
        return user is not None and context.user_id == user.id
    return context.anon_id == anon_id


def _session_identity_conflict_payload(
    *,
    contract_version: str,
    tenant: TenantProfile,
    linked: bool | None = None,
) -> dict:
    payload = {
        "ok": False,
        "contract_version": contract_version,
        "tenant_slug": tenant.slug,
        "status": "verification_required",
        "reason_code": "session_identity_conflict",
        "next_action": "start_new_session_or_login",
        "login_endpoint": "/auth/widget/bootstrap",
    }
    if linked is not None:
        payload["linked"] = linked
    return payload


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

    tenant = _get_tenant_from_request(slug)
    if not tenant and (
        _is_reserved_public_slug(slug)
        or _is_reserved_public_slug(request.args.get("tenant_slug"))
        or _is_reserved_public_slug(request.args.get("tenant"))
    ):
        return _public_json(_reserved_slug_payload(slug), 404)
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


@public_tenant_bp.route('/api/public/tenants/<slug>/catalog/download', methods=['GET', 'OPTIONS'])
@public_tenant_bp.route('/public/tenants/<slug>/catalog/download', methods=['GET', 'OPTIONS'])
def download_catalog(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True, "contract_version": "public.catalog_download.v1"}))

    fmt = str(request.args.get("format") or "pdf").strip().lower()
    if fmt not in {"pdf", "json"}:
        return _public_json(
            {
                "contract_version": "public.catalog_download.v1",
                "ok": False,
                "tenant_slug": _normalize_public_slug(slug),
                "reason_code": "unsupported_format",
                "supported_formats": ["pdf", "json"],
                "error": {"code": 400, "message": "Unsupported catalog download format"},
            },
            400,
        )

    if (
        _is_reserved_public_slug(slug)
        or _is_reserved_public_slug(request.args.get("tenant_slug"))
        or _is_reserved_public_slug(request.args.get("tenant"))
    ):
        return _catalog_download_error(slug, "reserved_public_slug")

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return _catalog_download_error(slug, "tenant_resolution_failed")

    owner = _resolve_catalog_owner(tenant)
    if not owner:
        current_app.logger.warning(
            "[public_tenant.catalog_download] tenant=%s has no owner binding (municipio_id=%s pyme_id=%s)",
            tenant.slug,
            tenant.municipio_id,
            tenant.pyme_id,
        )
        return _catalog_download_error(tenant.slug, "tenant_owner_missing")

    payload = _build_catalog_download_contract(tenant, owner)
    try:
        track_marketplace_event(
            tenant,
            "catalog_downloaded",
            {
                "source": "public_tenant_catalog_download",
                "format": fmt,
                "contract_version": payload.get("contract_version"),
                "total": payload.get("total"),
                "product_count": len(payload.get("products") or []),
            },
        )
    except Exception:
        current_app.logger.info("[public_tenant.catalog_download] analytics skipped", exc_info=True)

    filename = _catalog_download_filename(tenant, fmt)
    if fmt == "json":
        response = jsonify(
            {
                "contract_version": "public.catalog_download.v1",
                "ok": True,
                "format": "json",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "tenant": _tenant_public_summary(tenant),
                "catalog": payload,
            }
        )
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["X-Catalog-Contract-Version"] = str(payload.get("contract_version") or "")
        return _add_cors_headers(response)

    pdf_buffer = _write_catalog_pdf(payload, tenant)
    response = send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )
    response.headers["X-Catalog-Contract-Version"] = str(payload.get("contract_version") or "")
    response.headers["X-Request-Id"] = _request_id()
    return _add_cors_headers(response)


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

    contract_mode = str(request.args.get("contract") or request.args.get("view") or "").strip().lower()
    if contract_mode in {"marketplace", "market", "v2", "full"}:
        payload = build_public_market_catalog_contract(
            tenant,
            owner,
            base_web_url=current_app.config.get("APP_BASE_URL", "https://chatboc.ar"),
            categoria=request.args.get("categoria"),
            q=request.args.get("q"),
            precio_min=request.args.get("precio_min"),
            precio_max=request.args.get("precio_max"),
            en_promocion=request.args.get("en_promocion"),
            sort=request.args.get("sort"),
        )
        track_marketplace_event(
            tenant,
            "catalog_viewed",
            {
                "source": "public_tenant_catalog_contract",
                "contract_version": payload.get("contract_version"),
                "total": payload.get("total"),
                "product_count": len(payload.get("products") or []),
                "filters": payload.get("filters"),
                "sort": request.args.get("sort"),
                "assisted_intake_mode": (payload.get("assisted_intake") or {}).get("mode"),
            },
        )
        response = jsonify(payload)
        return _add_cors_headers(response)

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
    allowed, locked_payload, locked_status = _public_widget_integration_allowed(
        tenant,
        "public.widget_commerce_session.v1",
    )
    if not allowed:
        return _public_json(locked_payload, locked_status)

    session_payload = _session_context_payload()
    history_viewer = _history_viewer_for_tenant(tenant, session_payload)
    verified_history_context = _history_session_context(
        tenant,
        session_payload,
        history_viewer,
    )
    # A legacy ``User.anon_id`` is only a bearer hint, not an authenticated
    # principal by itself. Require a tenant-bound chat session before exposing
    # registered-user commerce state to an unauthenticated request.
    if history_viewer is not None and not isinstance(getattr(g, "viewer", None), User):
        if verified_history_context is None:
            history_viewer = None
            verified_history_context = _history_session_context(
                tenant,
                session_payload,
                None,
            )
    owner = _resolve_catalog_owner(tenant)
    catalog_products_count = 0
    if owner:
        catalog_products_count = (
            CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
            .filter_by(tenant_id=tenant.id, user_id=owner.id)
            .count()
        )
    has_catalog_items = catalog_products_count > 0
    catalog_enabled = bool(
        owner
        and (
            (tenant.tipo or "").lower() in {"pyme", "municipio", "gobierno", "government"}
            or tenant.pyme_id
            or tenant.municipio_id
            or has_catalog_items
        )
    )
    cart_enabled = catalog_enabled
    checkout_experience = build_checkout_experience_payload(tenant, channel="widget")
    assisted_intake = public_market_assisted_intake(tenant, total_products=catalog_products_count)
    public_api = public_market_api_contract(tenant, assisted_intake)
    assisted_primary_action = "assisted_upload" if assisted_intake.get("show_on_empty_catalog") else "catalog"

    payload = {
        "contract_version": "public.widget_commerce_session.v1",
        "tenant": _tenant_public_summary(tenant),
        "session": session_payload,
        "catalog": {
            "enabled": catalog_enabled,
            "endpoint": f"/api/public/tenants/{tenant.slug}/catalog",
            "marketplace_endpoint": f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace",
            "pwa_endpoint": f"/api/pwa/public/catalog?tenant={tenant.slug}",
            "quality_endpoint": f"/api/v2/tenants/{tenant.slug}/catalog/quality",
            "products_count": catalog_products_count,
            "has_products": has_catalog_items,
            "empty_catalog_mode": assisted_intake.get("mode"),
            "assisted_intake_enabled": True,
            "assisted_upload_endpoint": assisted_intake.get("submit", {}).get("endpoint"),
        },
        "assisted_intake": assisted_intake,
        "cart": {
            "enabled": cart_enabled,
            "summary_endpoint": "/api/pwa/public/cart/summary",
            "items_endpoint": "/api/pwa/public/cart/items",
            "legacy_endpoint": "/api/pwa/public/cart",
            "add_endpoint": "/api/pwa/public/cart/add",
            "update_endpoint": "/api/pwa/public/cart/update",
            "remove_endpoint": "/api/pwa/public/cart/remove",
            "checkout_preview_endpoint": "/api/pwa/public/cart/summary",
            "checkout_session_endpoint": "/api/checkout/crear-preferencia",
            "admin_checkout_preview_endpoint": f"/api/v2/tenants/{tenant.slug}/payments/checkout-preview",
            "admin_checkout_session_endpoint": f"/api/v2/tenants/{tenant.slug}/payments/checkout-session",
            "allow_guest_cart": True,
            "requires_contact_before_checkout": True,
            "public_api": public_api.get("cart"),
                **_cart_counts_for_tenant(
                    tenant,
                    session_payload,
                    viewer=history_viewer,
                    verified_context=verified_history_context,
                ),
        },
        "public_api": public_api,
        "payment": checkout_experience,
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
            "primary_actions": ["chat", assisted_primary_action, "catalog", "cart", "checkout", "portal"],
            "empty_state_behavior": (
                "assisted_intake_first"
                if assisted_intake.get("mode") == "assisted_first"
                else "chat_first_catalog_when_enabled"
            ),
            "assisted_intake_anchor_id": "market-assisted-upload",
            "supports_anonymous_assisted_upload": True,
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
    # Navigation must reflect the same citizen-visible publication predicate
    # as the public survey API. Draft, future, closed or unlinked instruments
    # must not advertise a route that is empty (or reveal their existence).
    has_surveys = bool(list_public_encuestas_for_tenant(tenant.id, limit=1))
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
    allowed, locked_payload, locked_status = _public_widget_integration_allowed(
        tenant,
        "public.widget_user_tenant_history.v1",
    )
    if not allowed:
        return _public_json(locked_payload, locked_status)

    session_payload = _session_context_payload()
    viewer = _history_viewer_for_tenant(tenant, session_payload)
    verified_context = _history_session_context(tenant, session_payload, viewer)
    if viewer is not None and not isinstance(getattr(g, "viewer", None), User):
        if verified_context is None:
            viewer = None
            verified_context = _history_session_context(tenant, session_payload, None)
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
        "items": _widget_history_items(
            tenant,
            session_payload,
            viewer=viewer,
            verified_context=verified_context,
        ),
        "cart": _cart_counts_for_tenant(
            tenant,
            session_payload,
            viewer=viewer,
            verified_context=verified_context,
        ),
    }
    return _public_json(payload)


@public_tenant_bp.route('/api/public/widget-user/register', methods=['POST', 'OPTIONS'])
def public_widget_user_register():
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "public.widget_user_register.v1"})

    tenant = _resolve_public_widget_tenant()
    if not tenant:
        payload, status = _tenant_resolution_error_payload("public.widget_user_register.v1")
        return _public_json(payload, status)
    allowed, locked_payload, locked_status = _public_widget_integration_allowed(
        tenant,
        "public.widget_user_register.v1",
    )
    if not allowed:
        return _public_json(locked_payload, locked_status)

    payload = request.get_json(silent=True) or {}
    session_payload = _session_context_payload()
    name = (payload.get("name") or payload.get("nombre") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    phone = (payload.get("phone") or payload.get("telefono") or "").strip()

    field_errors = {}
    if not name:
        field_errors["name"] = "required"
    if not email and not phone:
        field_errors["contact"] = "email_or_phone_required"
    if field_errors:
        return _public_json(
            {
                "ok": False,
                "contract_version": "public.widget_user_register.v1",
                "tenant_slug": tenant.slug,
                "reason_code": "validation_failed",
                "required_fields": ["name", "email_or_phone"],
                "field_errors": field_errors,
            },
            400,
        )

    anon_id = session_payload.get("anon_id")
    anon_user, anon_ambiguous = _unique_user_for_anon(anon_id)
    if anon_ambiguous:
        return _public_json(
            {
                "ok": False,
                "contract_version": "public.widget_user_register.v1",
                "tenant_slug": tenant.slug,
                "status": "verification_required",
                "reason_code": "anonymous_identity_ambiguous",
                "next_action": "verify_contact_or_login",
            },
            409,
        )
    authenticated = getattr(g, "viewer", None)
    if not isinstance(authenticated, User):
        authenticated = None
    if authenticated is not None and not _authenticated_viewer_matches_anon(
        authenticated,
        anon_user,
        anon_id,
    ):
        return _public_json(
            _session_identity_conflict_payload(
                contract_version="public.widget_user_register.v1",
                tenant=tenant,
            ),
            409,
        )
    if authenticated is None and _anon_recovery_requires_strong_verification(anon_user):
        return _public_json(
            _passkey_verification_required_payload(
                contract_version="public.widget_user_register.v1",
                tenant=tenant,
            ),
            409,
        )

    existing_user = None
    can_complete_authenticated_profile = bool(
        _is_provisional_passkey_user(authenticated)
        and anon_user is not None
        and authenticated is not None
        and anon_user.id == authenticated.id
        and str(authenticated.anon_id or "").strip() == str(anon_id or "").strip()
    )
    if authenticated is None:
        existing_user = (
            User.query.filter(func.lower(User.email) == email).one_or_none()
            if email
            else None
        )
    elif email and can_complete_authenticated_profile:
        contact_owner = User.query.filter(func.lower(User.email) == email).one_or_none()
        if contact_owner is not None and contact_owner.id != authenticated.id:
            return _public_json(
                {
                    "ok": False,
                    "contract_version": "public.widget_user_register.v1",
                    "tenant_slug": tenant.slug,
                    "status": "verification_required",
                    "reason_code": "contact_already_registered",
                    "next_action": "verify_contact_or_login",
                    "login_endpoint": "/auth/widget/bootstrap",
                },
                409,
            )
    if existing_user and existing_user.id != getattr(anon_user, "id", None):
        return _public_json(
            {
                "ok": True,
                "contract_version": "public.widget_user_register.v1",
                "tenant_slug": tenant.slug,
                "status": "verification_required",
                "reason_code": "contact_already_registered",
                "profile": {
                    "is_registered": False,
                    "contact": {"name": name, "email": email or None, "phone": phone or None},
                    "anon_id": session_payload["anon_id"],
                    "chat_session_id": session_payload["chat_session_id"],
                },
                "next_action": "verify_contact_or_login",
                "login_endpoint": "/auth/widget/bootstrap",
            }
        )

    candidate_user = authenticated or existing_user or anon_user
    if not _stage_public_session_binding(tenant, session_payload, candidate_user):
        db.session.rollback()
        return _public_json(
            _session_identity_conflict_payload(
                contract_version="public.widget_user_register.v1",
                tenant=tenant,
            ),
            409,
        )

    user = candidate_user or User.create_or_get_by_anon(anon_id, name)
    status = "linked_existing" if authenticated is not None or existing_user else "registered"
    can_mutate_profile = authenticated is None or can_complete_authenticated_profile
    if can_mutate_profile:
        if email and str(user.email or "").endswith("@passkey.chatboc"):
            user.email = email
        if name and (not user.name or str(user.name).startswith("Ciudadano")):
            user.name = name
        if phone and not getattr(user, "telefono", None):
            user.telefono = phone
    if not getattr(user, "tenant_id", None):
        user.tenant_id = tenant.id
    if not getattr(user, "tenant_slug", None):
        user.tenant_slug = tenant.slug

    db.session.add(user)
    db.session.flush()
    follower_created = _ensure_tenant_follower(user, tenant)
    merge_stats = merge_anon_into_user(
        anon_id,
        user,
        session_ids=[session_payload.get("chat_session_id")],
        tenant_id=tenant.id,
    )
    return _public_json(
        {
            "ok": True,
            "contract_version": "public.widget_user_register.v1",
            "tenant_slug": tenant.slug,
            "status": status,
            "profile": {
                "user_id": user.id,
                "is_registered": True,
                "contact": {
                    "name": user.name,
                    "email": (
                        None
                        if str(user.email or "").endswith("@passkey.chatboc")
                        else user.email
                    ),
                    "phone": getattr(user, "telefono", None),
                },
                "anon_id": session_payload["anon_id"],
                "chat_session_id": session_payload["chat_session_id"],
            },
            "tenant_follow": {
                "linked": True,
                "created": follower_created,
                "notifications_enabled": True,
            },
            "merge": merge_stats,
            "portal": {
                "view_url": f"/portal/{tenant.slug}",
                "history_endpoint": "/api/public/widget-user/tenant-history",
            },
            "next_action": "open_portal_or_continue_chat",
        }
    )


@public_tenant_bp.route('/api/public/widget-user/link-session', methods=['POST', 'OPTIONS'])
def public_widget_user_link_session():
    if request.method == 'OPTIONS':
        return _public_json({"ok": True, "contract_version": "public.widget_user_link_session.v1"})

    tenant = _resolve_public_widget_tenant()
    if not tenant:
        payload, status = _tenant_resolution_error_payload("public.widget_user_link_session.v1")
        return _public_json(payload, status)
    allowed, locked_payload, locked_status = _public_widget_integration_allowed(
        tenant,
        "public.widget_user_link_session.v1",
    )
    if not allowed:
        return _public_json(locked_payload, locked_status)

    session_payload = _session_context_payload()
    anon_id = session_payload.get("anon_id")
    anon_user, anon_ambiguous = _unique_user_for_anon(anon_id)
    authenticated = getattr(g, "viewer", None)
    if not isinstance(authenticated, User):
        authenticated = None
    if authenticated is not None and not _authenticated_viewer_matches_anon(
        authenticated,
        anon_user,
        anon_id,
    ):
        return _public_json(
            _session_identity_conflict_payload(
                contract_version="public.widget_user_link_session.v1",
                tenant=tenant,
                linked=False,
            ),
            409,
        )
    user = authenticated or anon_user

    if authenticated is None and _anon_recovery_requires_strong_verification(user):
        return _public_json(
            _passkey_verification_required_payload(
                contract_version="public.widget_user_link_session.v1",
                tenant=tenant,
                linked=False,
            ),
            409,
        )

    if user is None:
        return _public_json(
            {
                "ok": False,
                "contract_version": "public.widget_user_link_session.v1",
                "tenant_slug": tenant.slug,
                "linked": False,
                "reason_code": (
                    "anonymous_identity_ambiguous"
                    if anon_ambiguous
                    else "registration_required"
                ),
                "required_fields": ["name", "email_or_phone"],
                "register_endpoint": "/api/public/widget-user/register",
                "session": session_payload,
            }
        )

    if not _stage_public_session_binding(tenant, session_payload, user):
        db.session.rollback()
        return _public_json(
            _session_identity_conflict_payload(
                contract_version="public.widget_user_link_session.v1",
                tenant=tenant,
                linked=False,
            ),
            409,
        )

    follower_created = _ensure_tenant_follower(user, tenant)
    merge_stats = merge_anon_into_user(
        anon_id,
        user,
        session_ids=[session_payload.get("chat_session_id")],
        tenant_id=tenant.id,
    )
    return _public_json(
        {
            "ok": True,
            "contract_version": "public.widget_user_link_session.v1",
            "tenant_slug": tenant.slug,
            "linked": True,
            "profile": {
                "user_id": user.id,
                "name": user.name,
                "email": user.email if not str(user.email or "").endswith("@passkey.chatboc") else None,
                "phone": getattr(user, "telefono", None),
            },
            "tenant_follow": {
                "linked": True,
                "created": follower_created,
                "notifications_enabled": True,
            },
            "session": session_payload,
            "merge": merge_stats,
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
    if is_authorized_superadmin_user(current_user):
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

    from services.live_chat_schedule import build_live_chat_status, build_live_chat_transport
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
        transport = build_live_chat_transport(
            status,
            config_override=tenant.configuracion if tenant and isinstance(tenant.configuracion, dict) else None,
        )
        socket_enabled = bool(transport.get("socket_enabled"))
        status["transport"] = transport
        status["socket_transport_hint"] = "socket_io_enabled" if socket_enabled else "disabled"
        status["socket_transports"] = list(transport.get("transports") or [])
        status["socket_fallback_enabled"] = bool(socket_enabled and transport.get("http_fallback_enabled"))
        status["socket_enabled"] = socket_enabled
        status["socket_url"] = transport.get("socket_url")
        status["socket_path"] = transport.get("socket_path")
        status["realtime"] = socket_enabled
        status["fallback_mode"] = transport.get("fallback_mode") or "http_chat"
        return _add_cors_headers(jsonify(status))
    except Exception as e:
        current_app.logger.error(f"Error getting schedule: {e}")
        transport = build_live_chat_transport(None, config_override=None)
        response = jsonify({
            "contract_version": "live_chat.schedule.v1",
            "enabled": False,
            "available": False,
            "tenant_slug": str(requested_slug).strip().lower(),
            "source": "error_fallback",
            "fallback_reason": "schedule_error",
            "transport": transport,
            "socket_transport_hint": "disabled",
            "socket_transports": [],
            "socket_fallback_enabled": False,
            "socket_enabled": False,
            "socket_url": None,
            "socket_path": None,
            "realtime": False,
            "fallback_mode": "http_chat",
        })
        return _add_cors_headers(response)

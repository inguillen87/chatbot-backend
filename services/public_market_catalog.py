from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import func, or_

from models import CatalogoItem, Promocion, TenantProfile, User
from routes.catalogo import _formatear_producto
from services.common_utils import parse_precio_flexible
from utils.turnstile import (
    turnstile_enforce_public_intake,
    turnstile_public_intake_contract,
)


PUBLIC_MARKET_CATALOG_CONTRACT_VERSION = "public.market_catalog.v1"
PUBLIC_CATALOG_PROMOTIONS_CONTRACT_VERSION = "public.catalog_promotions.v1"
PUBLIC_MARKET_ASSISTED_INTAKE_CONTRACT_VERSION = "marketplace.assisted_intake_entry.v1"
PUBLIC_MARKET_API_CONTRACT_VERSION = "marketplace.public_api.v1"
PUBLIC_MARKET_ANALYTICS_CONTRACT_VERSION = "marketplace.public_analytics_loop.v1"


def tenant_public_summary(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "slug": tenant.slug,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical or ("gobierno" if tenant.tipo == "municipio" else "empresas"),
        "subvertical": tenant.subvertical,
        "display_name": tenant.nombre,
        "nombre": tenant.nombre,
        "logo_url": tenant.logo_url,
    }


def product_query_for_market_catalog(owner: User, tenant: TenantProfile, *, include_unavailable: bool = False):
    filters = [CatalogoItem.tenant_id == tenant.id, CatalogoItem.user_id == owner.id]
    if not include_unavailable:
        filters.append(or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None)))
    return (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter(*filters)
        .order_by(func.lower(CatalogoItem.nombre))
    )


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _product_price_value(item: CatalogoItem) -> float | None:
    _, price, _ = parse_precio_flexible(_safe_text(item.precio))
    return price


def _product_available(item: CatalogoItem) -> bool:
    return getattr(item, "disponible", True) is not False


def _format_catalog_product(item: CatalogoItem, tenant: TenantProfile) -> dict[str, Any]:
    price_value = _product_price_value(item)
    product = _formatear_producto(
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
            "precio_float": price_value,
            "extra_metadata": item.extra_metadata,
        }
    )
    product["catalogo_item_id"] = item.id
    product["catalog_item_id"] = item.id
    product["tenant_id"] = tenant.id
    product["tenant_slug"] = tenant.slug
    product["marca"] = product.get("marca") or item.marca
    product["precio_valor"] = price_value
    product["disponible"] = _product_available(item)
    product["en_promocion"] = bool(item.promocion_info)
    return product


def _search_matches(product: dict[str, Any], term: str) -> bool:
    text = " ".join(
        _safe_text(value)
        for value in (
            product.get("nombre"),
            product.get("descripcion"),
            product.get("descripcion_corta"),
            product.get("categoria"),
            product.get("marca"),
            product.get("promocion_info"),
        )
    ).lower()
    return term in text


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    normalized = _safe_text(value).lower()
    if normalized in {"1", "true", "yes", "si", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _public_market_analytics_contract(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "contract_version": PUBLIC_MARKET_ANALYTICS_CONTRACT_VERSION,
        "event_endpoint": "/api/analytics/event",
        "runtime_callback_endpoint_template": "/api/public/flows/{execution_id}/callback",
        "public_client_can_write_events_directly": False,
        "write_mode": "frontend_signal_plus_server_reconciliation",
        "client_signal_channel": "dataLayer",
        "tenant_slug": tenant.slug,
        "recommended_events": [
            "catalog_viewed",
            "whatsapp_cta_clicked",
            "assisted_upload_started",
            "assisted_upload_submitted",
            "product_viewed",
            "cart_started",
            "checkout_previewed",
            "checkout_session_created",
            "order_created",
            "order_tracking_opened",
        ],
        "funnel": [
            {"stage": "catalog", "event": "catalog_viewed", "label": "Catalogo visto"},
            {"stage": "assist", "event": "assisted_upload_submitted", "label": "Pedido asistido"},
            {"stage": "cart", "event": "cart_started", "label": "Carrito iniciado"},
            {"stage": "checkout", "event": "checkout_session_created", "label": "Checkout creado"},
            {"stage": "order", "event": "order_created", "label": "Pedido generado"},
            {"stage": "tracking", "event": "order_tracking_opened", "label": "Seguimiento abierto"},
        ],
        "privacy": {
            "raw_payment_data_allowed": False,
            "card_data_in_chat_allowed": False,
            "customer_pii_requires_consent": True,
        },
    }


def public_market_api_contract(tenant: TenantProfile, assisted_intake: dict[str, Any]) -> dict[str, Any]:
    tenant_param = quote_plus(str(tenant.slug or ""))
    pwa_cart_base = f"/api/pwa/public/cart?tenant={tenant_param}"
    pwa_cart_summary = f"/api/pwa/public/cart/summary?tenant={tenant_param}"
    turnstile_security = turnstile_public_intake_contract(
        surface="marketplace_assisted_upload",
        status="required" if turnstile_enforce_public_intake() else "not_required",
        reset_required=False,
    )
    return {
        "contract_version": PUBLIC_MARKET_API_CONTRACT_VERSION,
        "tenant_slug": tenant.slug,
        "anonymous": True,
        "guest_safe": True,
        "identity_headers": ["X-Anon-Id", "X-Chat-Session-Id", "X-Tenant"],
        "catalog": {
            "method": "GET",
            "endpoint": f"/api/market/{tenant.slug}/catalog?contract=marketplace",
            "alias_endpoint": f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace",
            "guest_safe": True,
        },
        "cart": {
            "summary": {"method": "GET", "endpoint": pwa_cart_summary, "guest_safe": True},
            "items": {"method": "GET", "endpoint": f"/api/pwa/public/cart/items?tenant={tenant_param}", "guest_safe": True},
            "add": {"method": "POST", "endpoint": f"/api/pwa/public/cart/add?tenant={tenant_param}", "guest_safe": True},
            "update": {"method": "POST", "endpoint": f"/api/pwa/public/cart/update?tenant={tenant_param}", "guest_safe": True},
            "remove": {"method": "POST", "endpoint": f"/api/pwa/public/cart/remove?tenant={tenant_param}", "guest_safe": True},
            "clear": {"method": "POST", "endpoint": f"/api/pwa/public/cart/clear?tenant={tenant_param}", "guest_safe": True},
            "legacy": {"method": "GET", "endpoint": pwa_cart_base, "guest_safe": True},
            "checkout": {
                "method": "GET",
                "endpoint": pwa_cart_summary,
                "guest_safe": True,
                "mode": "checkout_preview",
            },
        },
        "checkout": {
            "start": {"method": "POST", "endpoint": "/api/checkout/crear-preferencia"},
            "preview": {"method": "GET", "endpoint": pwa_cart_summary, "guest_safe": True},
            "requires_contact_before_checkout": True,
            "guest_safe": True,
            "fallback_behavior": "return_structured_plan_or_payment_error_never_tokenized_endpoint",
        },
        "assisted_upload": assisted_intake["submit"],
        "security": {
            "contract_version": "marketplace.public_security.v1",
            "turnstile": turnstile_security,
            "protected_surfaces": ["marketplace_assisted_upload"],
        },
        "flow_runtime": {
            "method": "GET",
            "endpoint": f"/api/public/flows/runtime?tenant={tenant_param}&channel=whatsapp",
            "actions_endpoint": f"/api/public/flows/actions?tenant={tenant_param}",
            "contract_version": "public.whatsapp.flow_runtime.v1",
            "guest_safe": True,
        },
        "tracking": {
            "order_path_template": f"/tracking/order/{{code}}?tenant_slug={tenant.slug}",
            "claim_path_template": f"/tracking/claim/{{code}}?tenant_slug={tenant.slug}",
            "source": "public_follow_up_from_assisted_upload",
        },
        "analytics": _public_market_analytics_contract(tenant),
    }


def _facets_from_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, int] = {}
    brands: dict[str, int] = {}
    prices: list[float] = []
    available = 0
    unavailable = 0
    promotion_count = 0

    for product in products:
        category = _safe_text(product.get("categoria")) or "Sin categoria"
        categories[category] = categories.get(category, 0) + 1
        brand = _safe_text(product.get("marca"))
        if brand:
            brands[brand] = brands.get(brand, 0) + 1
        price = product.get("precio_valor")
        if isinstance(price, (int, float)):
            prices.append(float(price))
        if product.get("disponible") is False:
            unavailable += 1
        else:
            available += 1
        if product.get("en_promocion"):
            promotion_count += 1

    def _facet_items(values: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"value": value, "label": value, "count": count}
            for value, count in sorted(values.items(), key=lambda entry: (-entry[1], entry[0].lower()))
        ]

    return {
        "contract_version": "public.market_catalog_facets.v1",
        "categories": _facet_items(categories),
        "brands": _facet_items(brands),
        "price_range": {
            "min": min(prices) if prices else None,
            "max": max(prices) if prices else None,
        },
        "availability": {
            "available": available,
            "unavailable": unavailable,
        },
        "promotion_count": promotion_count,
    }


def _promotion_status(promo: Promocion, now: datetime) -> str:
    if not promo.is_active:
        return "disabled"
    starts_at = promo.fecha_inicio
    ends_at = promo.fecha_fin
    if starts_at and starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    if ends_at and ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    if starts_at and starts_at > now:
        return "upcoming"
    if ends_at and ends_at < now:
        return "expired"
    return "active_now"


def _promotion_badge(promo: Promocion) -> str:
    kind = _safe_text(promo.tipo_promocion).upper()
    value = promo.valor_descuento
    if "PORCENTAJE" in kind and value:
        return f"{int(value) if float(value).is_integer() else value}% OFF"
    if "FIJO" in kind and value:
        return f"${value:g} OFF"
    if "COMPRA_X_LLEVA_Y" in kind and promo.cantidad_condicion_x and promo.cantidad_resultado_y:
        return f"{promo.cantidad_resultado_y}x{promo.cantidad_condicion_x}"
    if promo.codigo_promocion:
        return f"Codigo {promo.codigo_promocion}"
    return "Promo"


def _scope_product_ids(scopes: list[Any], catalog_items: list[CatalogoItem]) -> list[int]:
    if not scopes:
        return [item.id for item in catalog_items]

    product_ids: set[int] = set()
    by_category: dict[str, list[int]] = {}
    by_brand: dict[str, list[int]] = {}
    for item in catalog_items:
        category = _safe_text(item.categoria).lower()
        brand = _safe_text(item.marca).lower()
        if category:
            by_category.setdefault(category, []).append(item.id)
        if brand:
            by_brand.setdefault(brand, []).append(item.id)

    for scope in scopes:
        scope_type = _safe_text(getattr(scope, "tipo_alcance", "")).upper()
        if scope_type == "PRODUCTO" and getattr(scope, "catalogo_item_id", None):
            product_ids.add(scope.catalogo_item_id)
        elif scope_type == "CATEGORIA":
            for item_id in by_category.get(_safe_text(getattr(scope, "nombre_categoria", "")).lower(), []):
                product_ids.add(item_id)
        elif scope_type == "MARCA":
            for item_id in by_brand.get(_safe_text(getattr(scope, "nombre_marca", "")).lower(), []):
                product_ids.add(item_id)
    return sorted(product_ids)


def _promotion_terms_short(promo: Promocion) -> str:
    parts = []
    if promo.monto_minimo_carrito:
        parts.append(f"Compra minima ${promo.monto_minimo_carrito:g}")
    if promo.cantidad_minima_aplicable:
        parts.append(f"Desde {promo.cantidad_minima_aplicable} unidades")
    if promo.codigo_promocion:
        parts.append(f"Codigo {promo.codigo_promocion}")
    return ". ".join(parts) or "Sujeto a disponibilidad"


def public_catalog_promotions(
    tenant: TenantProfile,
    owner: User,
    catalog_items: list[CatalogoItem],
    *,
    limit: int = 8,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    structured = (
        Promocion.query.filter_by(pyme_user_id=owner.id, is_active=True)
        .order_by(Promocion.updated_at.desc())
        .limit(limit)
        .all()
    )
    catalog_badges = (
        CatalogoItem.query.filter(
            CatalogoItem.tenant_id == tenant.id,
            CatalogoItem.user_id == owner.id,
            CatalogoItem.promocion_info.isnot(None),
        )
        .order_by(CatalogoItem.timestamp.desc())
        .limit(limit)
        .all()
    )

    items: list[dict[str, Any]] = []
    for promo in structured:
        scopes = promo.alcances.all() if hasattr(promo.alcances, "all") else list(promo.alcances or [])
        ends_at = promo.fecha_fin
        seconds_remaining = None
        if ends_at:
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=timezone.utc)
            seconds_remaining = max(int((ends_at - now).total_seconds()), 0)
        items.append(
            {
                "id": promo.id,
                "title": promo.nombre_promocion,
                "description": promo.descripcion_publica or promo.tipo_promocion,
                "type": "structured",
                "status": _promotion_status(promo, now),
                "active_now": _promotion_status(promo, now) == "active_now",
                "display_badge": _promotion_badge(promo),
                "discount_type": promo.tipo_promocion,
                "discount_value": promo.valor_descuento,
                "min_cart_amount": promo.monto_minimo_carrito,
                "min_quantity": promo.cantidad_minima_aplicable,
                "code": promo.codigo_promocion,
                "terms_short": _promotion_terms_short(promo),
                "priority": 100 if _promotion_status(promo, now) == "active_now" else 40,
                "eligible_product_ids": _scope_product_ids(scopes, catalog_items),
                "countdown": {
                    "enabled": seconds_remaining is not None and _promotion_status(promo, now) == "active_now",
                    "seconds_remaining": seconds_remaining,
                    "ends_at": ends_at.isoformat() if ends_at else None,
                },
                "starts_at": promo.fecha_inicio.isoformat() if promo.fecha_inicio else None,
                "ends_at": promo.fecha_fin.isoformat() if promo.fecha_fin else None,
                "alcances": [
                    {
                        "id": scope.id,
                        "tipo_alcance": scope.tipo_alcance,
                        "catalogo_item_id": scope.catalogo_item_id,
                        "nombre_categoria": scope.nombre_categoria,
                        "nombre_marca": scope.nombre_marca,
                    }
                    for scope in scopes
                ],
            }
        )

    seen_ids = {str(item["id"]) for item in items}
    for product in catalog_badges:
        promo_id = f"catalog-item-{product.id}"
        if promo_id in seen_ids:
            continue
        items.append(
            {
                "id": promo_id,
                "title": product.promocion_info,
                "description": product.nombre,
                "type": "catalog_badge",
                "status": "active_now",
                "active_now": True,
                "display_badge": product.promocion_info,
                "catalogo_item_id": product.id,
                "eligible_product_ids": [product.id],
                "product_name": product.nombre,
                "category": product.categoria,
                "priority": 60,
                "terms_short": "Promo destacada del catalogo",
                "countdown": {"enabled": False, "seconds_remaining": None, "ends_at": None},
            }
        )
        if len(items) >= limit:
            break

    items.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("title") or "").lower()))
    return {
        "contract_version": PUBLIC_CATALOG_PROMOTIONS_CONTRACT_VERSION,
        "enabled": bool(items),
        "total": len(items),
        "active": sum(1 for item in items if item.get("active_now")),
        "structured_total": len(structured),
        "catalog_badge_total": len(catalog_badges),
        "catalog_items_with_promo_badge": len(catalog_badges),
        "items": items,
        "frontend_contract": {
            "render_as": "promotion_strip",
            "supports_badges": True,
            "supports_countdown": True,
            "supports_product_filters": True,
        },
    }


def public_market_assisted_intake(tenant: TenantProfile, *, total_products: int) -> dict[str, Any]:
    vertical_text = " ".join(
        _safe_text(value).lower()
        for value in (tenant.tipo, tenant.vertical, tenant.subvertical, tenant.nombre)
    )
    is_government = any(token in vertical_text for token in ("municipio", "gobierno", "ciudad"))
    is_education = any(token in vertical_text for token in ("colegio", "escuela", "educacion", "education"))

    if is_government:
        title = "Subi boletas, certificados, reclamos o notas y el municipio las responde con seguimiento"
        summary = (
            "Para vecinos que no quieren navegar formularios: Chatboc recibe una foto, PDF o texto, "
            "clasifica si es boleta, certificado, reclamo, pedido o consulta y deja seguimiento publico."
        )
        examples = [
            "Boleta de tasa municipal o comprobante",
            "Certificado, permiso o tramite",
            "Reclamo con direccion o foto",
            "Pedido escrito para cuadrilla, compras o mesa de entrada",
        ]
        text_examples = [
            {
                "id": "gov_tax_bill",
                "label": "Boleta municipal",
                "document_type": "tax_bill",
                "text": "Boleta de tasa municipal cuenta 9988 periodo 06/2026 vencimiento 15/07/2026",
            },
            {
                "id": "gov_certificate",
                "label": "Certificado",
                "document_type": "certificate",
                "text": "Necesito validar un certificado de libre deuda para el padron 12345",
            },
            {
                "id": "gov_order_note",
                "label": "Pedido escrito",
                "document_type": "handwritten_order",
                "text": "2 bolsas de cemento\n10 chapas para reparar techo de deposito",
            },
            {
                "id": "gov_service_request",
                "label": "Reclamo vecinal",
                "document_type": "service_request",
                "text": "Reclamo por luminaria quemada en Don Bosco 55 esquina Sarmiento. De noche queda muy oscuro.",
            },
        ]
    elif is_education:
        title = "Subi comprobantes, certificados o pedidos y el colegio los recibe ordenados"
        summary = (
            "El portal acepta documentos de familias, proveedores o administracion y crea un caso trazable "
            "para responder desde el panel sin exigir registro previo."
        )
        examples = [
            "Comprobante de cuota o transferencia",
            "Certificado medico o autorizacion",
            "Pedido de uniforme o materiales",
            "Nota escrita por la familia",
        ]
        text_examples = [
            {
                "id": "school_receipt",
                "label": "Comprobante",
                "document_type": "receipt",
                "text": "Adjunto comprobante de transferencia cuota junio alumno Juan Perez",
            },
            {
                "id": "school_certificate",
                "label": "Certificado",
                "document_type": "certificate",
                "text": "Certificado medico para justificar inasistencia del dia 15/06",
            },
            {
                "id": "school_order",
                "label": "Pedido",
                "document_type": "order_note",
                "text": "1 buzo talle 12\n2 remeras blancas talle 10",
            },
        ]
    else:
        title = "Subi una nota, foto o pedido y Chatboc la deja lista para responder"
        summary = (
            "Para clientes que no quieren cargar un carrito: pegan una lista, suben una foto "
            "de papel o adjuntan un documento y el equipo recibe items, faltantes, contacto y seguimiento publico."
        )
        examples = [
            "Foto de papel, mostrador o manuscrito",
            "Pedido de ferreteria, super o bebidas",
            "Pedido pegado desde WhatsApp",
            "Factura, recibo o comprobante",
        ]
        text_examples = [
            {
                "id": "hardware_order",
                "label": "Ferreteria",
                "document_type": "quote_request",
                "text": "2 chapas galvanizadas\n1 caja de clavos punta paris\n3 bolsas de cemento",
            },
            {
                "id": "grocery_order",
                "label": "Super/almacen",
                "document_type": "order_note",
                "text": "4 packs de agua sin gas\n2 arroz 1kg\n1 aceite 900ml",
            },
            {
                "id": "drinks_order",
                "label": "Bebidas",
                "document_type": "order_note",
                "text": "2 cajas de Malbec reserva\n1 caja degustacion\nConsultar envio",
            },
        ]

    return {
        "contract_version": PUBLIC_MARKET_ASSISTED_INTAKE_CONTRACT_VERSION,
        "render_as": "marketplace_assisted_intake",
        "mode": "assisted_first" if total_products <= 0 else "catalog_plus_assisted",
        "title": title,
        "summary": summary,
        "anonymous_intake": True,
        "catalog_matching": True,
        "show_on_empty_catalog": True,
        "submit": {
            "contract_version": "marketplace.assisted_intake_submit.v1",
            "method": "POST",
            "endpoint": "/api/pedidos/from-file?origen=marketplace",
            "content_type": "multipart/form-data",
            "tenant_fields": ["tenant", "tenant_slug"],
            "headers": ["X-Tenant", "X-Anon-Id", "X-Chat-Session-Id"],
            "file_field": "archivo",
            "text_field": "pedido_text",
            "document_type_field": "document_type",
            "contact_fields": ["contact_name", "contact_phone", "contact_email", "contact_notes"],
            "accepted_mime_types": [
                "application/pdf",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/csv",
                "text/plain",
                "image/png",
                "image/jpeg",
                "image/webp",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ],
            "accepted_extensions": ["pdf", "xls", "xlsx", "csv", "png", "jpg", "jpeg", "webp", "doc", "docx", "txt"],
            "max_file_mb": 8,
            "max_text_chars": 12000,
            "returns": {
                "contract_version": "marketplace.assisted_request.v1",
                "entity_aliases": ["pedido_id", "lead_id"],
                "operator_payloads": ["structured_extraction", "crm_handoff", "operator_pack"],
                "public_follow_up": True,
            },
        },
        "input_examples": examples,
        "text_examples": text_examples,
        "document_types": [
            {"id": "order_note", "label": "Nota de pedido", "supports_catalog_matching": True},
            {"id": "handwritten_order", "label": "Nota manuscrita", "supports_catalog_matching": True},
            {"id": "quote_request", "label": "Cotizacion", "supports_catalog_matching": True},
            {"id": "receipt", "label": "Factura / recibo", "supports_catalog_matching": False},
            {"id": "tax_bill", "label": "Boleta / impuesto", "supports_catalog_matching": False},
            {"id": "certificate", "label": "Certificado / tramite", "supports_catalog_matching": False},
            {"id": "service_request", "label": "Reclamo / solicitud vecinal", "supports_catalog_matching": False},
        ],
        "pipeline": [
            {
                "id": "capture",
                "label": "Foto, PDF o texto",
                "description": "El usuario sube papel manuscrito, boleta, certificado, comprobante o lista.",
            },
            {
                "id": "ai_parse",
                "label": "Datos ordenados",
                "description": "Lectura del documento, rubro probable, articulos/cantidades y candidatos de catalogo.",
            },
            {
                "id": "crm_handoff",
                "label": "Equipo responde",
                "description": "El operador ve archivo original, resumen, faltantes, canal sugerido y respuesta lista.",
            },
            {
                "id": "public_follow_up",
                "label": "Seguimiento publico",
                "description": "Link seguro para estado del pedido, reclamo o tramite y continuidad por WhatsApp.",
            },
        ],
        "crm_receives": [
            "Archivo o texto original",
            "Resumen con articulos, cantidades, rubro o tramite detectado",
            "Cruce con catalogo, candidatos y articulos faltantes",
            "Contacto y canal preferido",
            "Respuesta sugerida para WhatsApp, email o llamada",
            "Link publico de seguimiento",
        ],
        "empty_state": {
            "title": "Catalogo sin productos visibles, solicitud asistida activa",
            "description": (
                "Aunque todavia no haya productos publicados, el usuario puede subir una nota, manuscrito, "
                "boleta, certificado o reclamo para que quede ordenado y el equipo responda desde el panel."
            ),
            "primary_cta": "Subir pedido o documento",
            "secondary_cta": "Pegar pedido de ejemplo",
        },
        "frontend_contract": {
            "render_as": "marketplace_assisted_intake",
            "primary_cta": "Subir foto o papel",
            "secondary_cta": "Escribir pedido",
            "show_quick_examples": True,
            "show_on_empty_catalog": True,
            "supports_anonymous_follow_up": True,
            "submit_endpoint": "/api/pedidos/from-file?origen=marketplace",
            "submit_method": "POST",
            "accepted_extensions": ["pdf", "xls", "xlsx", "csv", "png", "jpg", "jpeg", "webp", "doc", "docx", "txt"],
            "max_file_mb": 8,
        },
    }


def build_public_market_catalog_contract(
    tenant: TenantProfile,
    owner: User,
    *,
    base_web_url: str,
    categoria: Any = None,
    q: Any = None,
    precio_min: Any = None,
    precio_max: Any = None,
    en_promocion: Any = None,
    sort: Any = None,
) -> dict[str, Any]:
    all_items = product_query_for_market_catalog(owner, tenant).all()
    all_products = [_format_catalog_product(item, tenant) for item in all_items]

    products = list(all_products)
    category = _safe_text(categoria).lower()
    if category:
        products = [product for product in products if _safe_text(product.get("categoria")).lower() == category]

    term = _safe_text(q).lower()
    if term:
        products = [product for product in products if _search_matches(product, term)]

    min_price = _parse_float(precio_min)
    max_price = _parse_float(precio_max)
    if min_price is not None:
        products = [product for product in products if product.get("precio_valor") is not None and product["precio_valor"] >= min_price]
    if max_price is not None:
        products = [product for product in products if product.get("precio_valor") is not None and product["precio_valor"] <= max_price]

    promo_filter = _parse_bool(en_promocion)
    if promo_filter is True:
        products = [product for product in products if product.get("en_promocion")]
    elif promo_filter is False:
        products = [product for product in products if not product.get("en_promocion")]

    sort_key = _safe_text(sort).lower() or "name_asc"
    if sort_key == "price_asc":
        products.sort(key=lambda product: (product.get("precio_valor") is None, product.get("precio_valor") or 0, _safe_text(product.get("nombre")).lower()))
    elif sort_key == "price_desc":
        products.sort(key=lambda product: (product.get("precio_valor") is None, -(product.get("precio_valor") or 0), _safe_text(product.get("nombre")).lower()))
    elif sort_key == "promo_first":
        products.sort(key=lambda product: (not product.get("en_promocion"), _safe_text(product.get("nombre")).lower()))
    else:
        products.sort(key=lambda product: _safe_text(product.get("nombre")).lower())

    base_web = base_web_url.rstrip("/")
    public_url = f"{base_web}/t/{tenant.slug}/market"
    share_text = f"Catalogo de {tenant.nombre or tenant.slug}: {public_url}"
    assisted_intake = public_market_assisted_intake(tenant, total_products=len(all_products))
    public_api = public_market_api_contract(tenant, assisted_intake)
    return {
        "contract_version": PUBLIC_MARKET_CATALOG_CONTRACT_VERSION,
        "tenant": tenant_public_summary(tenant),
        "tenant_slug": tenant.slug,
        "tenantName": tenant.nombre or tenant.slug,
        "tenantLogoUrl": tenant.logo_url,
        "products": products,
        "items": products,
        "total": len(products),
        "total_unfiltered": len(all_products),
        "facets": _facets_from_products(all_products),
        "filters": {
            "categoria": categoria,
            "q": q,
            "precio_min": min_price,
            "precio_max": max_price,
            "en_promocion": promo_filter,
            "sort": sort_key,
        },
        "sort_options": [
            {"id": "name_asc", "label": "Nombre"},
            {"id": "promo_first", "label": "Promociones primero"},
            {"id": "price_asc", "label": "Menor precio"},
            {"id": "price_desc", "label": "Mayor precio"},
        ],
        "promotions": public_catalog_promotions(tenant, owner, all_items),
        "assisted_intake": assisted_intake,
        "public_api": public_api,
        "publicCartUrl": f"{base_web}/t/{tenant.slug}/cart",
        "public_cart_url": f"{base_web}/t/{tenant.slug}/cart",
        "whatsappShareUrl": f"https://wa.me/?text={quote_plus(share_text)}",
        "heroSubtitle": (
            "Catalogo actualizado con promociones y disponibilidad operativa."
            if all_products
            else "Marketplace asistido para pedidos, boletas y documentos sin registro."
        ),
        "frontend_contract": {
            "render_as": "marketplace_catalog",
            "show_promotions_strip": True,
            "show_product_promo_badges": True,
            "show_cart_entrypoint": True,
            "show_faceted_filters": True,
            "show_sort_control": True,
            "show_price_range": True,
            "show_assisted_intake": True,
            "assisted_intake_anchor_id": "market-assisted-upload",
            "empty_catalog_mode": assisted_intake["mode"],
            "public_api_contract": public_api["contract_version"],
            "use_public_api_endpoints": True,
            "flow_runtime_endpoint": public_api["flow_runtime"]["endpoint"],
        },
    }

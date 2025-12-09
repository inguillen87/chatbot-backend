from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import List

from flask import Blueprint, abort, current_app, jsonify, g, request, session
from flask_cors import cross_origin
from sqlalchemy import func, or_

from models import (
    CatalogoItem,
    CategoriaTicket,
    MunicipioTicket,
    TenantProfile,
    User,
    WidgetConfig,
    db,
)
from config import ALLOWED_ORIGINS
from routes.auth import token_requerido
from routes.ticket import TICKET_ALLOWED_STATES
from utils.tenant import get_current_tenant, get_current_tenant_profile
from services.tenant_resolver import (
    TenantResolutionError,
    apply_tenant_alias,
    resolve_tenant_only,
)
from socket_service import emit_tenant_update

municipio_api_bp = Blueprint(
    "municipio_api",
    __name__,
    url_prefix="/api/municipio/<tenant_slug>",
)

public_market_bp = Blueprint(
    "public_market",
    __name__,
    url_prefix="/api/public/market/<tenant_slug>",
)

legacy_public_v2_bp = Blueprint(
    "legacy_public_api_v2",
    __name__,
    url_prefix="/api/municipio",
)

_PUBLIC_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "Origin",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "x-anon-id",
    "anon-id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Slug",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]

_PUBLIC_CORS_EXPOSE_HEADERS = ["Content-Type", "Authorization", "X-Anon-Id", "Anon-Id"]


def _public_cors_kwargs(methods: list[str]) -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _PUBLIC_CORS_ALLOWED_HEADERS,
        "expose_headers": _PUBLIC_CORS_EXPOSE_HEADERS,
        "methods": methods,
    }

widget_public_bp = Blueprint(
    "widget_public_config",
    __name__,
    url_prefix="/api/widget/config",
)


def _resolve_tenant_or_404(tenant_slug: str) -> TenantProfile:
    if tenant_slug:
        g.current_tenant = tenant_slug
        g.tenant_slug = tenant_slug

    normalized_slug = (tenant_slug or "").strip() or None
    mapped_slug = apply_tenant_alias(normalized_slug) or normalized_slug
    fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")

    tenant: TenantProfile | None = None
    for candidate in dict.fromkeys(
        [mapped_slug, normalized_slug, fallback_slug, get_current_tenant()]
    ):
        if not candidate:
            continue
        tenant = get_current_tenant_profile(candidate)
        if tenant:
            break
        try:
            tenant = resolve_tenant_only(
                tenant_slug=candidate,
                require_explicit_slug=False,
            )
        except TenantResolutionError:
            tenant = None
        if tenant:
            break

    if not tenant and fallback_slug:
        tenant = get_current_tenant_profile(fallback_slug)

    if not tenant:
        abort(404, "Tenant no encontrado")

    g.tenant = tenant
    return tenant


def _require_tenant_admin(current_user: User, tenant: TenantProfile) -> None:
    if not current_user or current_user.rol not in {"admin", "tenant_admin"}:
        abort(403, "Solo administradores pueden acceder")
    if current_user.tenant_id and current_user.tenant_id != tenant.id:
        abort(403, "No pertenece al tenant")


def _serialize_empleado(empleado: User) -> dict:
    return {
        "id": empleado.id,
        "nombre": empleado.name,
        "email": empleado.email,
        "rol": empleado.rol,
        "categorias": [c.to_dict() for c in getattr(empleado, "categorias_ticket", [])],
    }


@municipio_api_bp.url_value_preprocessor
def pull_tenant(endpoint, values):
    if not values:
        return
    slug = values.get("tenant_slug")
    if slug:
        _resolve_tenant_or_404(slug)


def _categorias_para_tenant(tenant: TenantProfile) -> list[dict]:
    categorias = (
        CategoriaTicket.query.filter_by(tenant_id=tenant.id)
        .order_by(CategoriaTicket.nombre.asc())
        .all()
    )
    return [c.to_dict() for c in categorias]


@municipio_api_bp.route("/tickets/categorias", methods=["GET", "OPTIONS"])
@token_requerido
def listar_categorias_ticket(current_user: User, tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    tenant = _resolve_tenant_or_404(tenant_slug)
    categorias = _categorias_para_tenant(tenant)
    return jsonify({"categorias": categorias, "categories": categorias})


@municipio_api_bp.route("/categorias", methods=["GET", "OPTIONS"])
@token_requerido
def listar_categorias_municipio(current_user: User, tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    tenant = _resolve_tenant_or_404(tenant_slug)
    categorias = _categorias_para_tenant(tenant)
    return jsonify({"categorias": categorias, "categories": categorias})


@municipio_api_bp.route("/estados", methods=["GET", "OPTIONS"])
@token_requerido
def listar_estados(current_user: User, tenant_slug: str):
    _resolve_tenant_or_404(tenant_slug)
    if request.method == "OPTIONS":
        return "", 204

    return jsonify({"estados": sorted(TICKET_ALLOWED_STATES)})


@municipio_api_bp.route("/empleados", methods=["POST"])
@token_requerido
def crear_empleado_multitenant(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)


@municipio_api_bp.route("/empleados", methods=["GET", "OPTIONS"])
@token_requerido
def listar_empleados_multitenant(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    if request.method == "OPTIONS":
        return "", 204

    _require_tenant_admin(current_user, tenant)

    empleados = (
        User.query.filter_by(tenant_id=tenant.id, es_empleado=True)
        .order_by(User.id.asc())
        .all()
    )
    payload = [_serialize_empleado(e) for e in empleados]
    return jsonify({"empleados": payload, "total": len(payload)})


@municipio_api_bp.route("/pedidos/categorias", methods=["GET", "OPTIONS"])
@token_requerido
def listar_categorias_pedidos(current_user: User, tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    tenant = _resolve_tenant_or_404(tenant_slug)
    categorias = _categorias_para_tenant(tenant)
    return jsonify({"categorias": categorias, "categories": categorias})

    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    rol = (data.get("rol") or "empleado").strip() or "empleado"
    categoria_ids: List[int] = data.get("categoria_ids") or []

    if not (nombre and email and password):
        return jsonify({"error": "Datos incompletos"}), 400
    if User.query.filter(func.lower(User.email) == email.lower()).first():
        return jsonify({"error": "Email ya registrado"}), 400

    categorias = (
        CategoriaTicket.query.filter(
            CategoriaTicket.tenant_id == tenant.id,
            CategoriaTicket.id.in_(categoria_ids),
        ).all()
        if categoria_ids
        else []
    )
    if categoria_ids and len(categorias) != len(set(categoria_ids)):
        return jsonify({"error": "Categorías inválidas"}), 400

    nuevo = User(
        name=nombre,
        email=email,
        rol=rol,
        es_empleado=True,
        tenant_id=tenant.id,
        tipo_chat="municipio",
    )
    nuevo.set_password(password)
    if categorias:
        nuevo.categorias_ticket = categorias

    db.session.add(nuevo)
    db.session.commit()

    return jsonify(
        {
            "id": nuevo.id,
            "nombre": nuevo.name,
            "email": nuevo.email,
            "rol": nuevo.rol,
            "categorias": [c.to_dict() for c in categorias],
        }
    ), 201


@municipio_api_bp.route("/empleados/<int:empleado_id>", methods=["PUT"])
@token_requerido
def actualizar_empleado_multitenant(current_user: User, tenant_slug: str, empleado_id: int):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)

    empleado = (
        User.query.filter_by(id=empleado_id, tenant_id=tenant.id, es_empleado=True)
        .order_by(User.id.asc())
        .first()
    )
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404

    data = request.get_json(silent=True) or {}
    if "nombre" in data or "name" in data:
        empleado.name = (data.get("nombre") or data.get("name") or empleado.name).strip()
    if "rol" in data:
        empleado.rol = (data.get("rol") or empleado.rol or "empleado").strip()
    if data.get("password"):
        empleado.set_password(data.get("password"))

    categoria_ids: List[int] = data.get("categoria_ids") or []
    if categoria_ids:
        categorias = (
            CategoriaTicket.query.filter(
                CategoriaTicket.tenant_id == tenant.id,
                CategoriaTicket.id.in_(categoria_ids),
            ).all()
        )
        if len(categorias) != len(set(categoria_ids)):
            return jsonify({"error": "Categorías inválidas"}), 400
        empleado.categorias_ticket = categorias
    elif "categoria_ids" in data:
        empleado.categorias_ticket = []

    db.session.commit()

    return jsonify(
        {
            "id": empleado.id,
            "nombre": empleado.name,
            "email": empleado.email,
            "rol": empleado.rol,
            "categorias": [c.to_dict() for c in empleado.categorias_ticket],
        }
    )


@municipio_api_bp.route("/widget-config", methods=["GET"])
@token_requerido
def obtener_widget_config(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)

    config = WidgetConfig.query.filter_by(tenant_id=tenant.id).first()
    if not config:
        config = WidgetConfig(tenant_id=tenant.id)
        db.session.add(config)
        db.session.commit()

    return jsonify(config.to_dict())


@municipio_api_bp.route("/widget-config", methods=["PUT"])
@token_requerido
def actualizar_widget_config(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)

    data = request.get_json(silent=True) or {}
    config = WidgetConfig.query.filter_by(tenant_id=tenant.id).first()
    if not config:
        config = WidgetConfig(tenant_id=tenant.id)
        db.session.add(config)

    for field in [
        "primary_color",
        "accent_color",
        "position",
        "logo_url",
        "welcome_message",
        "bubble_shape",
        "extra_css",
    ]:
        if field in data:
            setattr(config, field, data.get(field))

    db.session.commit()
    return jsonify(config.to_dict())


@widget_public_bp.route("/<tenant_slug>", methods=["GET"])
def obtener_config_publica(tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    config = WidgetConfig.query.filter_by(tenant_id=tenant.id).first()
    if not config:
        config = WidgetConfig(tenant_id=tenant.id)
        db.session.add(config)
        db.session.commit()
    return jsonify(config.to_dict())


@municipio_api_bp.route("/tickets/heatmap", methods=["GET"])
@token_requerido
def heatmap_tickets(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    start = request.args.get("from")
    end = request.args.get("to")
    categorias = request.args.getlist("categorias") or request.args.getlist("categorias[]")
    estados = request.args.getlist("estados") or request.args.getlist("estados[]")
    distrito = request.args.get("distrito")
    barrio = request.args.get("barrio")

    def _parse_date(raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    query = MunicipioTicket.query.filter(
        or_(
            MunicipioTicket.tenant_id == tenant.id,
            MunicipioTicket.municipio_id == tenant.municipio_id,
        )
    )

    if start_dt:
        query = query.filter(MunicipioTicket.fecha >= start_dt)
    if end_dt:
        query = query.filter(MunicipioTicket.fecha <= end_dt)
    if categorias:
        query = query.filter(
            or_(
                MunicipioTicket.categoria.in_(categorias),
                MunicipioTicket.categoria_id.in_([int(c) for c in categorias if str(c).isdigit()]),
            )
        )
    if estados:
        query = query.filter(MunicipioTicket.estado.in_(estados))
    if distrito:
        query = query.filter(MunicipioTicket.distrito == distrito)
    if barrio:
        query = query.filter(MunicipioTicket.barrio == barrio)

    tickets = query.all()
    points = []
    cells = defaultdict(lambda: {"intensidad": 0, "tickets_count": 0})
    summary_categoria = defaultdict(int)
    summary_estado = defaultdict(int)

    for t in tickets:
        if t.latitud is None or t.longitud is None:
            continue
        categoria_nombre = t.categoria or "Sin categoría"
        estado_val = t.estado or "desconocido"
        points.append(
            {
                "lat": t.latitud,
                "lng": t.longitud,
                "categoria": categoria_nombre,
                "estado": estado_val,
                "intensidad": 1,
                "ticket_id": t.id,
            }
        )
        cell_id = f"{round(t.latitud, 3)}:{round(t.longitud, 3)}"
        cell_data = cells[cell_id]
        cell_data["intensidad"] += 1
        cell_data["tickets_count"] += 1
        cell_data["lat"] = t.latitud
        cell_data["lng"] = t.longitud
        cell_data["cell_id"] = cell_id
        summary_categoria[categoria_nombre] += 1
        summary_estado[estado_val] += 1

    return jsonify(
        {
            "points": points,
            "cells": list(cells.values()),
            "summary": {
                "total_tickets": len(points),
                "por_categoria": summary_categoria,
                "por_estado": summary_estado,
            },
        }
    )


@public_market_bp.url_value_preprocessor
def pull_public_tenant(endpoint, values):
    if not values:
        return
    slug = values.get("tenant_slug")
    if slug:
        _resolve_tenant_or_404(slug)


@municipio_api_bp.route("/productos", methods=["GET"])
@token_requerido
def productos_admin(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)
    productos = CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id).all()
    return jsonify({"productos": [serialize_catalogo_item(p) for p in productos]})


@municipio_api_bp.route("/productos", methods=["POST"])
@token_requerido
def crear_producto_admin(current_user: User, tenant_slug: str):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)
    data = request.get_json(silent=True) or {}

    item = CatalogoItem(
        tenant_id=tenant.id,
        user_id=current_user.id,
        nombre=data.get("nombre"),
        descripcion=data.get("descripcion"),
        precio=data.get("precio"),
        moneda=data.get("moneda"),
        cantidad=data.get("stock"),
        imagen_url=data.get("foto_url"),
        disponible=data.get("estado_activo", True),
        categoria=data.get("categoria"),
        extra_metadata={"tags": data.get("tags") or []},
    )
    db.session.add(item)
    db.session.commit()
    return jsonify(serialize_catalogo_item(item)), 201


@municipio_api_bp.route("/productos/<int:producto_id>", methods=["PUT"])
@token_requerido
def actualizar_producto_admin(current_user: User, tenant_slug: str, producto_id: int):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)
    item = (
        CatalogoItem.query.filter_by(id=producto_id, tenant_id=tenant.id)
        .order_by(CatalogoItem.id.asc())
        .first()
    )
    if not item:
        return jsonify({"error": "Producto no encontrado"}), 404
    data = request.get_json(silent=True) or {}
    for field in [
        "nombre",
        "descripcion",
        "precio",
        "moneda",
        "cantidad",
        "categoria",
    ]:
        if field in data:
            setattr(item, field, data.get(field))
    if "stock" in data:
        item.cantidad = data.get("stock")
    if "foto_url" in data:
        item.imagen_url = data.get("foto_url")
    if "estado_activo" in data:
        item.disponible = bool(data.get("estado_activo"))
    if "tags" in data:
        meta = item.extra_metadata or {}
        meta["tags"] = data.get("tags") or []
        item.extra_metadata = meta

    db.session.commit()
    return jsonify(serialize_catalogo_item(item))


@municipio_api_bp.route("/productos/<int:producto_id>", methods=["DELETE"])
@token_requerido
def borrar_producto_admin(current_user: User, tenant_slug: str, producto_id: int):
    tenant = _resolve_tenant_or_404(tenant_slug)
    _require_tenant_admin(current_user, tenant)
    item = CatalogoItem.query.filter_by(id=producto_id, tenant_id=tenant.id).first()
    if not item:
        return jsonify({"error": "Producto no encontrado"}), 404
    db.session.delete(item)
    db.session.commit()
    return jsonify({"status": "ok"})


def serialize_catalogo_item(item: CatalogoItem) -> dict:
    return {
        "id": item.id,
        "nombre": item.nombre,
        "descripcion": item.descripcion,
        "precio": item.precio_monetario or item.precio,
        "moneda": item.moneda,
        "stock": item.cantidad,
        "foto_url": item.imagen_url,
        "estado_activo": bool(item.disponible),
        "tags": (item.extra_metadata or {}).get("tags", []),
    }


@public_market_bp.route("/productos", methods=["GET", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["GET", "OPTIONS"]))
def productos_publicos(tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    resolved_slug = get_current_tenant()
    tenant = _resolve_tenant_or_404(resolved_slug or tenant_slug)
    productos = (
        CatalogoItem.query.filter(
            CatalogoItem.tenant_id == tenant.id, CatalogoItem.disponible.is_(True)
        )
        .order_by(CatalogoItem.nombre.asc())
        .all()
    )
    return jsonify({"productos": [serialize_catalogo_item(p) for p in productos]})


@public_market_bp.route("/productos/<int:producto_id>", methods=["GET", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["GET", "OPTIONS"]))
def producto_publico(tenant_slug: str, producto_id: int):
    if request.method == "OPTIONS":
        return "", 204

    resolved_slug = get_current_tenant()
    tenant = _resolve_tenant_or_404(resolved_slug or tenant_slug)
    item = (
        CatalogoItem.query.filter_by(id=producto_id, tenant_id=tenant.id, disponible=True)
        .order_by(CatalogoItem.id.asc())
        .first()
    )
    if not item:
        return jsonify({"error": "Producto no encontrado"}), 404
    return jsonify(serialize_catalogo_item(item))


@public_market_bp.route("/carrito", methods=["GET", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["GET", "OPTIONS"]))
def obtener_carrito_publico(tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    resolved_slug = get_current_tenant()
    tenant = _resolve_tenant_or_404(resolved_slug or tenant_slug)
    cart = session.get(_cart_key(tenant), {"items": []})
    return jsonify(cart)


@public_market_bp.route("/carrito/items", methods=["POST", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["POST", "OPTIONS"]))
def agregar_item_carrito(tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    resolved_slug = get_current_tenant()
    tenant = _resolve_tenant_or_404(resolved_slug or tenant_slug)
    data = request.get_json(silent=True) or {}
    producto_id = data.get("producto_id")
    cantidad = int(data.get("cantidad") or 1)
    cart = session.get(_cart_key(tenant), {"items": []})
    cart.setdefault("items", [])
    cart["items"].append({"producto_id": producto_id, "cantidad": cantidad})
    session[_cart_key(tenant)] = cart
    session.modified = True
    return jsonify(cart), 201


@public_market_bp.route("/checkout", methods=["POST", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["POST", "OPTIONS"]))
def checkout_publico(tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    resolved_slug = get_current_tenant()
    tenant = _resolve_tenant_or_404(resolved_slug or tenant_slug)
    cart = session.get(_cart_key(tenant), {"items": []})
    session[_cart_key(tenant)] = {"items": []}
    session.modified = True
    return jsonify({"status": "ok", "tenant": tenant.slug, "carrito": cart})


@legacy_public_v2_bp.route("/carrito", methods=["GET", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["GET", "OPTIONS"]))
def legacy_carrito_publico():
    """Legacy endpoint for cart retrieval requiring tenant_slug querystring."""
    if request.method == "OPTIONS":
        return "", 204

    tenant_slug = request.args.get("tenant_slug")
    if not tenant_slug:
        abort(400, "tenant_slug query param required")

    tenant = _resolve_tenant_or_404(tenant_slug)
    cart = session.get(_cart_key(tenant), {"items": []})
    return jsonify(cart)


@legacy_public_v2_bp.route("/productos", methods=["GET", "OPTIONS"])
@cross_origin(**_public_cors_kwargs(["GET", "OPTIONS"]))
def legacy_productos_publicos():
    """Legacy endpoint for products retrieval requiring tenant_slug querystring."""
    if request.method == "OPTIONS":
        return "", 204

    tenant_slug = request.args.get("tenant_slug")
    if not tenant_slug:
        abort(400, "tenant_slug query param required")

    tenant = _resolve_tenant_or_404(tenant_slug)
    try:
        productos = (
            CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
            .filter(CatalogoItem.tenant_id == tenant.id, CatalogoItem.disponible.is_(True))
            .order_by(CatalogoItem.nombre.asc())
            .all()
        )
    except Exception as exc:  # pragma: no cover - fallback path exercised via tests
        # Legacy databases may miss newer catalog columns (e.g. ``disponible``).
        # If the main query fails with a DB-level error, retry with a minimal
        # column set and without filtering by the missing fields to keep the
        # storefront usable.
        from sqlalchemy.exc import ProgrammingError

        if not isinstance(exc, ProgrammingError):
            raise

        productos = (
            CatalogoItem.query.options(*CatalogoItem.legacy_safe_options(include_availability=False))
            .filter(CatalogoItem.tenant_id == tenant.id)
            .order_by(CatalogoItem.nombre.asc())
            .all()
        )

    return jsonify({"productos": [serialize_catalogo_item(p) for p in productos]})


def _cart_key(tenant: TenantProfile) -> str:
    return f"cart_{tenant.slug}"

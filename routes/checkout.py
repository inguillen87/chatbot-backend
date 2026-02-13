import logging
import os
from typing import List, Optional

import requests
from flask import Blueprint, jsonify, request, session, g
from flask_cors import cross_origin
from sqlalchemy import func

from config import ALLOWED_ORIGINS
from database import db
from models import CatalogoItem, CatalogoModalidad, PedidoConversacional, TenantProfile, User
from routes.catalogo import _formatear_producto
from services.rewards import recompensas_service
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user

logger = logging.getLogger(__name__)

_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]


def _cors_kwargs(methods: list[str]) -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _CORS_ALLOWED_HEADERS,
        "methods": methods,
    }


checkout_bp = Blueprint("checkout_bp", __name__, url_prefix="/api/checkout")
pedidos_checkout_bp = Blueprint("pedidos_checkout_bp", __name__, url_prefix="/api/pedidos")


def _session_cart(tenant_id: int):
    carts = session.get("carritos_pymes", {})
    return carts.get(str(tenant_id)) or carts.get(tenant_id) or []


def _lookup_owner(tenant: TenantProfile) -> Optional[User]:
    return getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)


def _normalize_quantity(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _build_items(cart_entries: List[dict], tenant: TenantProfile, owner: Optional[User]) -> List[dict]:
    items: List[dict] = []
    if not cart_entries:
        return items

    catalog_map: dict[int, CatalogoItem] = {}
    item_ids = [entry.get("catalogo_item_id") for entry in cart_entries if entry.get("catalogo_item_id")]
    if item_ids and owner:
        rows = (
            CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
            .filter(
                CatalogoItem.user_id == owner.id,
                func.coalesce(CatalogoItem.tenant_id, tenant.id) == tenant.id,
                CatalogoItem.id.in_(item_ids),
            )
            .all()
        )
        catalog_map = {row.id: row for row in rows}

    for entry in cart_entries:
        item_id = entry.get("catalogo_item_id")
        cantidad = _normalize_quantity(entry.get("cantidad") or entry.get("quantity"))
        item = catalog_map.get(item_id)
        if not item:
            continue

        formatted = _formatear_producto(
            {
                "nombre": item.nombre,
                "precio_str": item.precio,
                "precio_float": float(item.precio_monetario) if item.precio_monetario is not None else None,
                "sku": item.sku,
                "modalidad": item.modalidad_enum.value,
                "descripcion": item.descripcion,
                "imagen_url": item.imagen_url,
            }
        )
        items.append(
            {
                "title": formatted.get("nombre"),
                "quantity": cantidad,
                "unit_price": formatted.get("precio_unitario") or formatted.get("precio_pack") or 0,
                "currency_id": formatted.get("moneda") or item.moneda or "ARS",
                "modalidad": formatted.get("modalidad"),
                "catalogo_item_id": item.id,
                "tenant_id": tenant.id,
                "categoria": formatted.get("categoria"),
                "imagen_url": formatted.get("imagen_url"),
            }
        )
    return items


def _totales(items: List[dict]):
    total_money = 0.0
    total_points = 0
    has_donation = False
    for entry in items:
        moneda = entry.get("moneda") or entry.get("currency_id") or "ARS"
        modalidad = CatalogoModalidad.from_legacy(entry.get("modalidad"))
        subtotal = entry.get("subtotal") or entry.get("precio_unitario") or entry.get("unit_price")
        cantidad = entry.get("cantidad") or entry.get("quantity") or 1
        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            cantidad = 1
        if subtotal is None:
            continue
        subtotal = float(subtotal) * cantidad
        if modalidad is CatalogoModalidad.DONACION:
            has_donation = True
            continue
        if moneda == "PTS" and modalidad is CatalogoModalidad.CANJE:
            total_points += int(subtotal)
        elif moneda != "PTS":
            total_money += subtotal
    return total_money, total_points, has_donation


def _resolve_cart(tenant: TenantProfile, owner: Optional[User], payload: dict) -> List[dict]:
    if payload.get("items"):
        return _build_items(payload.get("items") or [], tenant, owner)
    return _build_items(_session_cart(tenant.id), tenant, owner)


def _ensure_contact(user: User, payload: dict) -> Optional[dict]:
    contacto = payload.get("contacto") or {}
    nombre_contacto = (contacto.get("nombre") or payload.get("nombre") or "").strip()
    email_contacto = (contacto.get("email") or payload.get("email") or "").strip()
    telefono_contacto = (contacto.get("telefono") or payload.get("telefono") or "").strip()

    if not nombre_contacto or not (email_contacto or telefono_contacto):
        return {
            "error": "Datos de contacto requeridos para finalizar la compra",
            "contacto_requerido": True,
        }

    if nombre_contacto:
        user.name = nombre_contacto
    if email_contacto:
        existing_email = User.query.filter(User.email == email_contacto, User.id != user.id).first()
        if not existing_email:
            user.email = email_contacto
    if telefono_contacto:
        user.telefono = telefono_contacto
    return None


def _pedido_tipo(total_money: float, total_points: int, has_donation: bool) -> str:
    if total_money == 0 and total_points == 0 and has_donation:
        return "donacion"
    if total_money > 0 and total_points > 0:
        return "mixto"
    if total_points > 0:
        return "canje"
    if has_donation and total_money > 0:
        return "mixto"
    return "compra"


def _resolve_tenant_user(payload: dict) -> tuple[TenantProfile, User]:
    tenant_arg = payload.get("tenant_slug") or request.args.get("tenant_slug")
    tenant_arg = tenant_arg or request.args.get("tenant") or request.headers.get("X-Tenant")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id") or payload.get("tenant_id")
    has_hint = bool(tenant_arg or tenant_id or widget_token or request.headers.get("X-Whatsapp-Dst"))
    if not has_hint:
        raise TenantResolutionError("Tenant requerido para checkout")
    try:
        tenant, user, _ = resolve_tenant_and_user(
            tenant_slug=tenant_arg,
            tenant_id=tenant_id,
            widget_token=widget_token,
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            current_user=(getattr(g, "user", None) or getattr(g, "viewer", None)),
        )
    except TenantResolutionError as exc:
        raise
    if not tenant:
        raise TenantResolutionError("Tenant no encontrado")
    return tenant, user


def _crear_pedido(payload: dict):
    try:
        tenant, user = _resolve_tenant_user(payload)
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc), "codigo": "tenant_no_encontrado"}), 404

    owner = _lookup_owner(tenant)
    if not owner:
        return jsonify({"error": "Catálogo no disponible para este tenant"}), 404

    cart_entries = _resolve_cart(tenant, owner, payload)
    if not cart_entries:
        return jsonify({"error": "Carrito vacío"}), 400

    total_money, total_points, has_donation = _totales(cart_entries)
    is_anonymous = bool(getattr(user, "anon_id", None))

    if total_points and is_anonymous:
        return (
            jsonify(
                {
                    "error": "Autenticación requerida para canjear puntos",
                    "codigo": "REQUIERE_LOGIN_PUNTOS",
                    "login_required": True,
                }
            ),
            401,
        )

    rewards = recompensas_service()
    if total_points:
        saldo_actual = rewards.obtener_saldo(user)
        if saldo_actual < total_points:
            faltantes = total_points - saldo_actual
            return (
                jsonify(
                    {
                        "error": "Saldo de puntos insuficiente",
                        "codigo": "SALDO_INSUFICIENTE",
                        "puntos_necesarios": faltantes,
                    }
                ),
                400,
            )
        rewards.canjear_puntos(user, tenant, total_points)

    if is_anonymous:
        contact_error = _ensure_contact(user, payload)
        if contact_error:
            return jsonify(contact_error), 400

    pedido_tipo = _pedido_tipo(total_money, total_points, has_donation)
    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=user.id,
        monto_monetario=total_money,
        monto_puntos=total_points,
        tipo=pedido_tipo,
        items=cart_entries,
        origen=payload.get("origen")
        or request.headers.get("X-Checkout-Origin")
        or request.args.get("origen")
        or "web",
        anon_id=getattr(user, "anon_id", None),
    )
    db.session.add(pedido)
    db.session.commit()

    tenant_cfg = tenant.configuracion or {}
    access_token = tenant_cfg.get("mercadopago_access_token") or os.getenv("MERCADOPAGO_ACCESS_TOKEN")
    init_point = None
    preference_id = None
    demo_mode = bool((getattr(g, "token_payload", {}) or {}).get("demo_mode") or payload.get("demo_mode"))

    if total_money > 0 and demo_mode:
        pedido.estado = "confirmado"
        pedido.mp_status = "demo_skipped"
        db.session.commit()
        return jsonify(
            {
                "pedido_id": pedido.id,
                "preference_id": None,
                "init_point": None,
                "total_monetario": total_money,
                "total_puntos": total_points,
                "estado": pedido.estado,
                "tipo": pedido.tipo,
                "demo_mode": True,
            }
        )

    if total_money > 0 and not access_token:
        pedido.estado = "pendiente_pago"
        db.session.commit()
        return (
            jsonify(
                {
                    "pedido_id": pedido.id,
                    "total_monetario": total_money,
                    "total_puntos": total_points,
                    "estado": pedido.estado,
                    "tipo": pedido.tipo,
                    "mercadopago_ready": False,
                    "error": "MercadoPago no configurado para este tenant",
                }
            ),
            503,
        )

    if total_money > 0 and access_token:
        preference_payload = {
            "items": [
                {
                    "title": it.get("title"),
                    "quantity": it.get("quantity"),
                    "unit_price": it.get("unit_price"),
                    "currency_id": it.get("currency_id"),
                }
                for it in cart_entries
            ],
            "external_reference": str(pedido.id),
            "metadata": {
                "tenant_id": tenant.id,
                "tenant_slug": tenant.slug,
                "pedido_id": pedido.id,
            },
        }
        resp = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=preference_payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            init_point = data.get("init_point")
            preference_id = data.get("id")
            pedido.mp_preference_id = preference_id
            db.session.commit()
        else:
            logger.error("MercadoPago error: %s", resp.text)
            pedido.estado = "pendiente_pago"
            db.session.commit()
    else:
        pedido.estado = "confirmado"
        db.session.commit()

        # Trigger PymePedido creation for persistence and notifications
        try:
            from services.pedido_service import servicio_pedidos
            servicio_pedidos.create_from_conversational(pedido)
        except Exception as e:
            logger.error(f"Error creating PymePedido from checkout: {e}")

    return jsonify(
        {
            "pedido_id": pedido.id,
            "preference_id": preference_id,
            "init_point": init_point,
            "total_monetario": total_money,
            "total_puntos": total_points,
            "estado": pedido.estado,
            "tipo": pedido.tipo,
        }
    )


@checkout_bp.route("/crear-preferencia", methods=["POST", "OPTIONS"])
@pedidos_checkout_bp.route("/checkout", methods=["POST", "OPTIONS"])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
def crear_preferencia():
    if request.method == "OPTIONS":
        return "", 204

    payload = request.get_json(silent=True) or {}
    return _crear_pedido(payload)


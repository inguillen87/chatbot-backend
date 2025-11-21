import logging
import os
from typing import List

import requests
from flask import Blueprint, jsonify, request, session, g

from database import db
from models import CatalogoItem, PedidoConversacional, User, CatalogoModalidad
from routes.catalogo import _formatear_producto
from services.rewards import recompensas_service
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user

logger = logging.getLogger(__name__)

checkout_bp = Blueprint("checkout_bp", __name__, url_prefix="/api/checkout")


def _session_cart(tenant_id: int):
    carts = session.get("carritos_pymes", {})
    return carts.get(str(tenant_id)) or carts.get(tenant_id) or []


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


@checkout_bp.route("/crear-preferencia", methods=["POST"])
def crear_preferencia():
    payload = request.get_json(silent=True) or {}

    try:
        tenant, user, _ = resolve_tenant_and_user(
            tenant_slug=request.headers.get("X-Tenant"), current_user=getattr(g, "user", None)
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    cart_entries = _session_cart(tenant.id)
    if not cart_entries:
        return jsonify({"error": "Carrito vacío"}), 400

    is_anonymous = bool(user.anon_id)

    items: List[dict] = []
    for entry in cart_entries:
        item = db.session.get(
            CatalogoItem,
            entry.get("catalogo_item_id"),
            options=CatalogoItem.legacy_safe_options(),
        )
        if not item:
            continue
        formatted = _formatear_producto(
            {
                "nombre": item.nombre,
                "precio_str": item.precio,
                "precio_float": float(item.precio_monetario) if item.precio_monetario is not None else None,
                "sku": item.sku,
                "modalidad": item.modalidad_enum.value,
            }
        )
        items.append({
            "title": formatted.get("nombre"),
            "quantity": entry.get("cantidad", 1),
            "unit_price": formatted.get("precio_unitario") or formatted.get("precio_pack") or 0,
            "currency_id": formatted.get("moneda") or item.moneda or "ARS",
            "modalidad": formatted.get("modalidad"),
        })

    total_money, total_points, has_donation = _totales(items)
    if total_points and is_anonymous:
        return (
            jsonify(
                {
                    "error": "Autenticación requerida para canjear puntos",
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
                        "puntos_faltantes": faltantes,
                    }
                ),
                400,
            )
        rewards.canjear_puntos(user, tenant, total_points)

    if is_anonymous:
        contacto = payload.get("contacto") or {}
        nombre_contacto = (contacto.get("nombre") or payload.get("nombre") or "").strip()
        email_contacto = (contacto.get("email") or payload.get("email") or "").strip()
        telefono_contacto = (contacto.get("telefono") or payload.get("telefono") or "").strip()

        if nombre_contacto:
            user.name = nombre_contacto
        if email_contacto:
            existing_email = User.query.filter(User.email == email_contacto, User.id != user.id).first()
            if not existing_email:
                user.email = email_contacto
        if telefono_contacto:
            user.telefono = telefono_contacto

    if total_money == 0 and total_points == 0 and has_donation:
        pedido_tipo = "donacion"
    elif total_money > 0 and total_points > 0:
        pedido_tipo = "mixto"
    elif total_points > 0:
        pedido_tipo = "canje"
    elif has_donation and total_money > 0:
        pedido_tipo = "mixto"
    else:
        pedido_tipo = "compra"

    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=user.id,
        monto_monetario=total_money,
        monto_puntos=total_points,
        tipo=pedido_tipo,
        items=items,
    )
    db.session.add(pedido)
    db.session.commit()

    access_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
    init_point = None
    preference_id = None
    if total_money > 0 and access_token:
        payload = {
            "items": [
                {
                    "title": it.get("title"),
                    "quantity": it.get("quantity"),
                    "unit_price": it.get("unit_price"),
                    "currency_id": it.get("currency_id"),
                }
                for it in items
            ],
            "external_reference": str(pedido.id),
        }
        resp = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=payload,
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
    else:
        pedido.estado = "confirmado"
        db.session.commit()

    return jsonify(
        {
            "pedido_id": pedido.id,
            "preference_id": preference_id,
            "init_point": init_point,
            "total_monetario": total_money,
            "total_puntos": total_points,
        }
    )


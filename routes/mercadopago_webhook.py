"""MercadoPago webhook handlers.

This endpoint now resolves the MercadoPago access token per tenant (when
possible) to support multi-tenant payment configurations and emits a
lightweight notification so chat/widget channels can reflect payment
status updates.
"""

import logging
import os
from typing import Optional

import requests
from flask import Blueprint, jsonify, request

from extensions import db
from models import PedidoConversacional, User, TenantProfile
from socket_service import socketio

from services.plan_config import (
    MERCADOPAGO_PLAN_LOOKUP,
    apply_plan_to_user,
    serialize_plan_for_response,
)

mp_bp = Blueprint("mp_bp", __name__)


def _resolve_access_token_for_pedido(pedido: PedidoConversacional) -> Optional[str]:
    tenant_cfg = getattr(getattr(pedido, "tenant", None), "configuracion", None) or {}
    return tenant_cfg.get("mercadopago_access_token") or os.getenv("MERCADOPAGO_ACCESS_TOKEN")




def _resolve_access_token_from_payload(payload: dict) -> Optional[str]:
    tenant_hint = (
        payload.get("tenant_id")
        or payload.get("tenantId")
        or ((payload.get("metadata") or {}).get("tenant_id"))
    )
    tenant_slug = (
        payload.get("tenant_slug")
        or payload.get("tenantSlug")
        or ((payload.get("metadata") or {}).get("tenant_slug"))
    )

    tenant = None
    if tenant_hint:
        try:
            tenant = TenantProfile.query.get(int(tenant_hint))
        except Exception:
            tenant = None
    if not tenant and tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=str(tenant_slug)).first()

    cfg = getattr(tenant, "configuracion", None) or {}
    return cfg.get("mercadopago_access_token")

def _emit_payment_notification(pedido: PedidoConversacional) -> None:
    payload = {
        "pedido_id": pedido.id,
        "estado": pedido.estado,
        "tipo": pedido.tipo,
        "tenant_id": pedido.tenant_id,
        "user_id": pedido.user_id,
    }
    try:
        socketio.emit("payment_update", payload, broadcast=True)
    except Exception:
        logging.exception("Error emitting payment_update notification for pedido %s", pedido.id)

@mp_bp.route("/mercadopago_webhook", methods=["POST"])
def mercadopago_webhook():
    data = request.json
    logging.info(f"🔵 Webhook Mercado Pago recibido: {data}")

    topic = data.get("type")
    action = data.get("action")
    preapproval_id = data.get("data", {}).get("id")  # El ID de la suscripción (preapproval_id)

    if topic == "payment":
        payment_id = data.get("data", {}).get("id")
        url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
        default_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
        payload_token = _resolve_access_token_from_payload(data or {})
        active_token = payload_token or default_token
        if not active_token:
            logging.warning("MercadoPago token faltante para webhook de pago %s", payment_id)
            return jsonify({"error": "MercadoPago no configurado"}), 503

        headers = {"Authorization": f"Bearer {active_token}"}
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            logging.warning("Pago no encontrado: %s", payment_id)
            return jsonify({"error": "Pago no encontrado"}), 400
        payment_info = resp.json()
        external_reference = payment_info.get("external_reference")
        status = payment_info.get("status")
        if not external_reference:
            return jsonify({"error": "Sin referencia externa"}), 400

        # Try MarketOrder first (new format MO-XXX)
        if str(external_reference).startswith("MO-"):
            from models import MarketOrder
            from services.notification_dispatcher import dispatch_order_update

            try:
                order_id = int(str(external_reference).replace("MO-", ""))
                order = MarketOrder.query.get(order_id)
            except ValueError:
                order = None

            if order:
                # Resolve tenant token
                tenant_cfg = getattr(order.tenant, "configuracion", None) or {}
                tenant_token = tenant_cfg.get("mercadopago_access_token")

                if tenant_token and tenant_token != active_token:
                    headers = {"Authorization": f"Bearer {tenant_token}"}
                    retry_resp = requests.get(url, headers=headers)
                    if retry_resp.ok:
                        payment_info = retry_resp.json()
                        status = payment_info.get("status")

                # Update Order
                meta = order.metadata_payload or {}
                meta["mp_payment_id"] = str(payment_id)
                meta["mp_status"] = status
                order.metadata_payload = meta

                if status == "approved":
                    order.status = "paid"
                    dispatch_order_update(order, "Tu pago ha sido aprobado. Procesando pedido.")
                elif status == "rejected":
                    order.status = "payment_failed"
                    dispatch_order_update(order, "Tu pago fue rechazado.")

                db.session.commit()
                # Emit update
                try:
                    socketio.emit(f"market_order_{order.tenant.slug}", {"event": "order_update", "order_id": order.id, "status": order.status})
                except Exception:
                    pass

                return jsonify({"ok": True, "estado": order.status, "model": "MarketOrder"})

        # Fallback to PedidoConversacional (Legacy)
        pedido = PedidoConversacional.query.get(external_reference)
        if not pedido:
            return jsonify({"error": "Pedido no encontrado"}), 404

        tenant_token = _resolve_access_token_for_pedido(pedido)
        if tenant_token and tenant_token != active_token:
            headers = {"Authorization": f"Bearer {tenant_token}"}
            retry_resp = requests.get(url, headers=headers)
            if retry_resp.ok:
                payment_info = retry_resp.json()
                status = payment_info.get("status")

        pedido.mp_payment_id = str(payment_id)
        pedido.mp_status = status
        if status == "approved":
            pedido.estado = "pagado"
        else:
            pedido.estado = status or "rechazado"
        db.session.commit()

        if status == "approved":
            # Persist to PymePedido for admin panel visibility
            try:
                from services.pedido_service import servicio_pedidos
                servicio_pedidos.create_from_conversational(pedido)
            except Exception as e:
                logging.error(f"Error creating PymePedido from webhook: {e}")

        _emit_payment_notification(pedido)
        return jsonify({"ok": True, "estado": pedido.estado})

    # Si el webhook es por suscripción (preapproval)
    if topic == "preapproval":
        # Consultamos detalles de la suscripción a la API de Mercado Pago
        url = f"https://api.mercadopago.com/preapproval/{preapproval_id}"
        access_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            logging.warning(f"No se pudo consultar preapproval: {preapproval_id}")
            return jsonify({"error": "No se pudo consultar preapproval"}), 400
        info = resp.json()

        # Extraer mail del usuario y plan_id
        email = info.get("payer_email")
        plan_id = info.get("preapproval_plan_id")
        status = info.get("status")  # authorized, cancelled, paused, etc.

        if not email or not plan_id:
            logging.warning(f"Faltan datos: {info}")
            return jsonify({"error": "Faltan datos"}), 400

        plan = MERCADOPAGO_PLAN_LOOKUP.get(plan_id)
        if not plan:
            logging.warning(f"Plan no reconocido: {plan_id}")
            return jsonify({"error": "Plan desconocido"}), 400

        user = User.query.filter_by(email=email).first()
        if not user:
            # Si no existe, podrías crearlo o loguear y abortar
            logging.warning(f"Usuario no encontrado para email: {email}")
            return jsonify({"error": "Usuario no encontrado"}), 404

        # Actualizá datos del usuario con la metadata centralizada
        plan_metadata = apply_plan_to_user(
            user,
            plan,
            status=status,
            preapproval_id=preapproval_id,
        )
        db.session.commit()
        logging.info(f"Usuario {email} actualizado a plan {plan}, status {status}")

        return jsonify(
            {
                "ok": True,
                "msg": f"Upgrade exitoso a {plan} ({status})",
                "plan": serialize_plan_for_response(plan_metadata),
            }
        )

    return jsonify({"ok": False, "msg": "Evento ignorado"}), 200

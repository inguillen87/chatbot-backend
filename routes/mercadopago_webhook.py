"""MercadoPago webhook handlers.

This endpoint now resolves the MercadoPago access token per tenant (when
possible) to support multi-tenant payment configurations and emits a
lightweight notification so chat/widget channels can reflect payment
status updates.
"""

import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests
from flask import Blueprint, jsonify, request

from extensions import db
from models import MarketOrder, OrderEvent, PedidoConversacional, TenantProfile, User
from socket_service import socketio

from services.plan_config import (
    MERCADOPAGO_PLAN_LOOKUP,
    apply_plan_to_user,
    serialize_plan_for_response,
)

mp_bp = Blueprint("mp_bp", __name__)


def _resolve_access_token_from_payload(payload: dict) -> Optional[str]:
    tenant_hint = (
        payload.get("tenant_id")
        or payload.get("tenantId")
        or ((payload.get("metadata") or {}).get("tenant_id"))
        or request.args.get("tenant_id")
    )
    tenant_slug = (
        payload.get("tenant_slug")
        or payload.get("tenantSlug")
        or ((payload.get("metadata") or {}).get("tenant_slug"))
        or request.args.get("tenant_slug")
    )

    tenant = None
    if tenant_hint:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_hint))
        except (TypeError, ValueError):
            tenant = None
    if not tenant and tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=str(tenant_slug)).first()

    cfg = getattr(tenant, "configuracion", None) or {}
    return cfg.get("mercadopago_access_token")


def _linked_market_order(pedido: PedidoConversacional) -> Optional[MarketOrder]:
    metadata = dict(pedido.metadata_payload or {})
    checkout = dict(metadata.get("checkout") or {})
    market_order_id = checkout.get("market_order_id")
    if market_order_id:
        order = MarketOrder.legacy_safe_query().filter_by(
            id=market_order_id,
            tenant_id=pedido.tenant_id,
        ).first()
        if order:
            return order
    return MarketOrder.legacy_safe_query().filter_by(
        tenant_id=pedido.tenant_id,
        external_provider="pedido_conversacional",
        external_order_id=str(pedido.id),
    ).first()


def _linked_pedido(order: MarketOrder) -> Optional[PedidoConversacional]:
    metadata = dict(order.metadata_payload or {})
    checkout = dict(metadata.get("checkout") or {})
    pedido_id = checkout.get("pedido_conversacional_id") or metadata.get("pedido_conversacional_id")
    if not pedido_id:
        return None
    return PedidoConversacional.query.filter_by(id=pedido_id, tenant_id=order.tenant_id).first()


def _tenant_access_token(pedido: Optional[PedidoConversacional], order: Optional[MarketOrder]) -> Optional[str]:
    tenant = getattr(pedido, "tenant", None) or getattr(order, "tenant", None)
    cfg = getattr(tenant, "configuracion", None) or {}
    return cfg.get("mercadopago_access_token") or os.getenv("MERCADOPAGO_ACCESS_TOKEN")


def _payment_amount(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    try:
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _validate_payment_info(
    payment_info: dict,
    *,
    external_reference: str,
    pedido: Optional[PedidoConversacional],
    order: Optional[MarketOrder],
) -> Optional[tuple[str, str]]:
    actual_reference = str(payment_info.get("external_reference") or "").strip()
    if actual_reference != external_reference:
        return "payment_reference_mismatch", "La referencia del pago no coincide con la orden"
    if not str(payment_info.get("status") or "").strip():
        return "payment_status_missing", "Mercado Pago no informo el estado del pago"

    tenant = getattr(pedido, "tenant", None) or getattr(order, "tenant", None)
    metadata = payment_info.get("metadata") if isinstance(payment_info.get("metadata"), dict) else {}
    tenant_id_hint = metadata.get("tenant_id") or payment_info.get("tenant_id")
    tenant_slug_hint = metadata.get("tenant_slug") or payment_info.get("tenant_slug")
    if tenant_id_hint is not None and str(tenant_id_hint) != str(getattr(tenant, "id", "")):
        return "payment_tenant_mismatch", "El pago pertenece a otro tenant"
    if tenant_slug_hint and str(tenant_slug_hint).strip().lower() != str(getattr(tenant, "slug", "")).strip().lower():
        return "payment_tenant_mismatch", "El pago pertenece a otro tenant"

    expected_amount = _payment_amount(
        getattr(pedido, "monto_monetario", None)
        if pedido is not None
        else getattr(order, "total_monetary", None)
    )
    actual_amount = _payment_amount(payment_info.get("transaction_amount"))
    if "transaction_amount" in payment_info and actual_amount is None:
        return "payment_amount_invalid", "Mercado Pago informo un monto invalido"
    if expected_amount is not None and actual_amount is not None and expected_amount != actual_amount:
        return "payment_amount_mismatch", "El monto acreditado no coincide con la orden"

    expected_currency = str(getattr(order, "currency", None) or "").strip().upper()
    if not expected_currency and pedido is not None:
        pedido_metadata = dict(pedido.metadata_payload or {})
        expected_currency = str(pedido_metadata.get("currency") or "").strip().upper()
    actual_currency = str(payment_info.get("currency_id") or "").strip().upper()
    if expected_currency and actual_currency and expected_currency != actual_currency:
        return "payment_currency_mismatch", "La moneda acreditada no coincide con la orden"
    return None


def _set_checkout_payment_metadata(entity, *, payment_id: str, status: Optional[str]) -> None:
    metadata = dict(getattr(entity, "metadata_payload", None) or {})
    checkout = dict(metadata.get("checkout") or {})
    payment = dict(checkout.get("payment") or {})
    payment.update({"mp_payment_id": payment_id, "mp_status": status})
    checkout["payment"] = payment
    metadata["checkout"] = checkout
    metadata["mp_payment_id"] = payment_id
    metadata["mp_status"] = status
    entity.metadata_payload = metadata


def _pedido_status_from_payment(status: Optional[str]) -> str:
    normalized = str(status or "").strip().lower()
    return {
        "approved": "pagado",
        "rejected": "rechazado",
        "cancelled": "cancelado",
        "canceled": "cancelado",
        "pending": "pendiente_pago",
        "in_process": "pendiente_pago",
    }.get(normalized, normalized or "rechazado")


def _market_status_from_payment(status: Optional[str], current: Optional[str]) -> str:
    normalized = str(status or "").strip().lower()
    return {
        "approved": "paid",
        "rejected": "payment_failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "pending": "pending_payment",
        "in_process": "pending_payment",
    }.get(normalized, current or "pending_payment")


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
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload invalido"}), 400
    logging.info("Webhook Mercado Pago recibido: %s", data)

    topic = data.get("type")
    preapproval_id = data.get("data", {}).get("id")  # El ID de la suscripción (preapproval_id)

    if topic == "payment":
        payment_id = data.get("data", {}).get("id")
        if not payment_id:
            return jsonify({"error": "Sin identificador de pago"}), 400
        url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
        default_token = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
        payload_token = _resolve_access_token_from_payload(data or {})
        active_token = payload_token or default_token
        if not active_token:
            logging.warning("MercadoPago token faltante para webhook de pago %s", payment_id)
            return jsonify({"error": "MercadoPago no configurado"}), 503

        headers = {"Authorization": f"Bearer {active_token}"}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException:
            logging.exception("No se pudo consultar el pago %s", payment_id)
            return jsonify({"error": "MercadoPago no disponible"}), 502
        if resp.status_code != 200:
            logging.warning("Pago no encontrado: %s", payment_id)
            return jsonify({"error": "Pago no encontrado"}), 400
        payment_info = resp.json()
        if not isinstance(payment_info, dict):
            return jsonify({"error": "Respuesta de pago invalida"}), 502
        external_reference = str(payment_info.get("external_reference") or "").strip()
        if not external_reference:
            return jsonify({"error": "Sin referencia externa"}), 400

        pedido = None
        order = None
        if external_reference.upper().startswith("MO-"):
            try:
                order_id = int(external_reference[3:])
                order = MarketOrder.legacy_safe_query().filter_by(id=order_id).first()
            except (TypeError, ValueError):
                order = None
            if order is not None:
                pedido = _linked_pedido(order)
        else:
            normalized_reference = external_reference[3:] if external_reference.upper().startswith("PC-") else external_reference
            try:
                pedido = db.session.get(PedidoConversacional, int(normalized_reference))
            except (TypeError, ValueError):
                pedido = None
            if pedido is not None:
                order = _linked_market_order(pedido)

        if pedido is None and order is None:
            return jsonify({"error": "Pedido no encontrado"}), 404

        tenant_token = _tenant_access_token(pedido, order)
        if tenant_token and tenant_token != active_token:
            headers = {"Authorization": f"Bearer {tenant_token}"}
            try:
                retry_resp = requests.get(url, headers=headers, timeout=10)
            except requests.RequestException:
                logging.exception("No se pudo revalidar el pago %s con el token del tenant", payment_id)
                return jsonify({"error": "MercadoPago no disponible"}), 502
            if retry_resp.ok:
                payment_info = retry_resp.json()
            else:
                return jsonify({"error": "Pago no encontrado para el tenant"}), 400
            if not isinstance(payment_info, dict):
                return jsonify({"error": "Respuesta de pago invalida"}), 502

        validation_error = _validate_payment_info(
            payment_info,
            external_reference=external_reference,
            pedido=pedido,
            order=order,
        )
        if validation_error:
            reason_code, message = validation_error
            logging.warning("Webhook rechazado para pago %s: %s", payment_id, reason_code)
            return jsonify({"error": message, "reason_code": reason_code}), 409

        status = payment_info.get("status")
        previous_pedido_status = getattr(pedido, "mp_status", None)
        previous_order_metadata = dict(getattr(order, "metadata_payload", None) or {})
        previous_order_status = previous_order_metadata.get("mp_status")

        if pedido is not None:
            pedido.mp_payment_id = str(payment_id)
            pedido.mp_status = status
            pedido.estado = _pedido_status_from_payment(status)
            _set_checkout_payment_metadata(pedido, payment_id=str(payment_id), status=status)
        if order is not None:
            order.status = _market_status_from_payment(status, order.status)
            _set_checkout_payment_metadata(order, payment_id=str(payment_id), status=status)
            if previous_order_status != status:
                db.session.add(
                    OrderEvent(
                        market_order_id=order.id,
                        type=f"payment.{str(status or 'unknown').lower()}",
                        payload={
                            "payment_id": str(payment_id),
                            "external_reference": external_reference,
                            "pedido_id": getattr(pedido, "id", None),
                        },
                    )
                )
        db.session.commit()

        if order is not None and previous_order_status != status:
            try:
                from services.notification_dispatcher import dispatch_order_update

                message = (
                    "Tu pago ha sido aprobado. Procesando pedido."
                    if status == "approved"
                    else "Tu pago fue rechazado."
                    if status == "rejected"
                    else "El estado de tu pago fue actualizado."
                )
                dispatch_order_update(order, message)
            except Exception:
                logging.exception("Error notificando actualizacion para MarketOrder %s", order.id)

        if pedido is not None and status == "approved" and previous_pedido_status != "approved":
            try:
                from services.pedido_service import servicio_pedidos

                servicio_pedidos.create_from_conversational(pedido)
            except Exception:
                logging.exception("Error creando PymePedido desde webhook para pedido %s", pedido.id)

        if pedido is not None:
            _emit_payment_notification(pedido)
        if order is not None:
            try:
                socketio.emit(
                    f"market_order_{order.tenant.slug}",
                    {"event": "order_update", "order_id": order.id, "status": order.status},
                )
            except Exception:
                logging.exception("Error emitiendo actualizacion para MarketOrder %s", order.id)

        return jsonify(
            {
                "ok": True,
                "estado": getattr(pedido, "estado", None) or getattr(order, "status", None),
                "pedido_id": getattr(pedido, "id", None),
                "market_order_id": getattr(order, "id", None),
                "model": "PedidoConversacional" if pedido is not None else "MarketOrder",
            }
        )

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

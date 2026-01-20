"""Utilidades centralizadas para despachar notificaciones multicanal.

Se encapsulan los envíos de email, SMS y WhatsApp para que las rutas solo
tengan que invocar una función y registrar el resultado. Esto evita que los
errores de configuración (SMTP/Twilio) pasen desapercibidos y deja un punto
único para instrumentar métricas o trazas futuras.
"""

from __future__ import annotations

import logging
import json
import os
from typing import Any, Dict

from services.email_service import (
    enviar_email_ticket_novedad,
    enviar_sms_ticket_novedad,
    enviar_whatsapp_ticket_novedad,
    enviar_whatsapp, # Generic sender
)
from services.telegram_service import send_telegram_message


logger = logging.getLogger(__name__)


def dispatch_ticket_update(
    ticket: Any,
    tipo: str,
    mensaje: str,
    *,
    comentario_reciente: Any = None,
    enable_whatsapp: bool = True,
) -> Dict[str, bool]:
    """Envía la notificación de novedad de ticket por los canales disponibles.

    Devuelve un diccionario con el estado de cada canal para facilitar el
    logging o la observabilidad desde las rutas.
    """

    resultados: Dict[str, bool] = {"email": False, "sms": False, "whatsapp": False}

    try:
        resultados["email"] = enviar_email_ticket_novedad(
            ticket,
            mensaje,
            comentario_reciente=comentario_reciente,
        )
    except Exception as exc:  # pragma: no cover - solo logging defensivo
        logger.error(
            "[NOTIFY] Error enviando email de novedad para ticket %s: %s",
            getattr(ticket, "id", "N/A"),
            exc,
            exc_info=True,
        )

    try:
        resultados["sms"] = enviar_sms_ticket_novedad(ticket, mensaje)
    except Exception as exc:  # pragma: no cover - solo logging defensivo
        logger.error(
            "[NOTIFY] Error enviando SMS de novedad para ticket %s: %s",
            getattr(ticket, "id", "N/A"),
            exc,
            exc_info=True,
        )

    if enable_whatsapp:
        try:
            resultados["whatsapp"] = enviar_whatsapp_ticket_novedad(ticket, mensaje)
        except Exception as exc:  # pragma: no cover - solo logging defensivo
            logger.error(
                "[NOTIFY] Error enviando WhatsApp de novedad para ticket %s: %s",
                getattr(ticket, "id", "N/A"),
                exc,
                exc_info=True,
            )

    # Notify Owner
    _notify_owner_generic(ticket, mensaje, resultados)

    return resultados


def dispatch_ticket_state_change(
    ticket: Any,
    tipo: str,
    nuevo_estado: str,
    *,
    comentario_estado: Any = None,
) -> Dict[str, bool]:
    """Envoltura especializada para cambios de estado.

    Construye el mensaje estándar y delega en :func:`dispatch_ticket_update`.
    """

    mensaje = f"El estado de tu ticket #{getattr(ticket, 'nro_ticket', '')} ha sido actualizado a: '{nuevo_estado}'."
    return dispatch_ticket_update(
        ticket,
        tipo,
        mensaje,
        comentario_reciente=comentario_estado,
        enable_whatsapp=True,
    )


def dispatch_order_update(
    order: Any,
    mensaje: str,
    enable_whatsapp: bool = True,
) -> Dict[str, bool]:
    """Envía notificación de novedad de pedido."""
    logger.info(f"[NOTIFY] Order {getattr(order, 'id', 'N/A')} update: {mensaje}")

    resultados = {"email": False, "sms": False, "whatsapp": False, "whatsapp_customer": False}

    # Notify Customer via WhatsApp
    customer_phone = getattr(order, 'contact_phone', None)
    logger.info(f"[NOTIFY_DEBUG] Checking conditions for customer WhatsApp. enable_whatsapp={enable_whatsapp}, customer_phone='{customer_phone}'")
    if enable_whatsapp and customer_phone:
        try:
            logger.info(f"[NOTIFY] Attempting to send WhatsApp to customer at {customer_phone}")
            resultados["whatsapp_customer"] = enviar_whatsapp(customer_phone, mensaje)
            if resultados["whatsapp_customer"]:
                logger.info(f"[NOTIFY] Successfully sent WhatsApp to customer.")
            else:
                logger.warning(f"[NOTIFY] Failed to send WhatsApp to customer.")
        except Exception as exc:
            logger.error(
                "[NOTIFY] Error sending WhatsApp to customer for order %s: %s",
                getattr(order, "id", "N/A"),
                exc,
                exc_info=True,
            )

    # Notify Owner
    _notify_owner_generic(order, mensaje, resultados, is_order=True)

    return resultados


def _notify_owner_generic(entity: Any, message: str, results: Dict[str, bool], is_order: bool = False):
    """Helper to send push notifications to the Tenant Owner."""
    try:
        tenant_obj = getattr(entity, "tenant", None)
        if not tenant_obj and hasattr(entity, "tenant_id") and entity.tenant_id:
            from models import TenantProfile
            tenant_obj = TenantProfile.query.get(entity.tenant_id)

        if not tenant_obj:
            return

        from flask import current_app
        config = getattr(tenant_obj, "configuracion", {}) or {}

        # 1. Telegram
        chat_id = config.get("owner_telegram_chat_id")
        bot_token = current_app.config.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

        if chat_id and bot_token:
            prefix = "📦 Pedido" if is_order else "🎫 Ticket"
            results["telegram_owner"] = send_telegram_message(chat_id, f"{prefix}: {message}", bot_token)

        # 2. WhatsApp (Twilio Template for Push)
        owner_phone = config.get("owner_notification_phone")

        # If it's an order, we might use a specific template if configured
        if is_order and owner_phone and config.get("twilio_order_template_sid"):
            sid = config.get("twilio_order_template_sid")
            # This requires advanced Twilio Client usage not fully wrapped in email_service yet.
            # We will use the generic message for now or implement template sending if library supports it.
            # Assuming simple message for MVP unless user provided specific SID logic
            results["whatsapp_owner"] = enviar_whatsapp(owner_phone, f"📦 {message}")
        elif owner_phone:
             results["whatsapp_owner"] = enviar_whatsapp(owner_phone, f"🔔 {message}")

    except Exception as e:
        logger.error(f"[NOTIFY] Error notifying owner: {e}")

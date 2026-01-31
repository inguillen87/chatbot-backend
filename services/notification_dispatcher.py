import logging
from typing import Optional

from models import PymePedido, TenantProfile
from services.email_service import (
    enviar_email_pedido_cliente,
    enviar_email_pedido_despacho,
    enviar_email_pedido_admin,
)
from services.notifications import (
    enviar_notificacion_sms,
    enviar_notificacion_whatsapp_con_plantilla,
)
from services.pedido_pdf import generar_pdf_nota_pedido

logger = logging.getLogger(__name__)

class NotificationDispatcher:
    """
    Centralized service to handle omnichannel notifications for orders.
    Designed to be robust: failures in one channel do not block others.
    """

    def dispatch_order_created(self, pedido: PymePedido, pdf_bytes: Optional[bytes] = None):
        """
        Dispatches all configured notifications for a new order.
        """
        logger.info(f"Dispatching notifications for order {pedido.nro_pedido} (Tenant: {pedido.tenant_id})")

        tenant = pedido.tenant
        if not tenant and pedido.tenant_id:
            from models import db
            tenant = db.session.get(TenantProfile, pedido.tenant_id)

        # 1. Generate PDF if not provided (and feasible)
        if not pdf_bytes:
            try:
                empresa_info = self._get_empresa_info(pedido)
                pdf_bytes = generar_pdf_nota_pedido(pedido, empresa_info=empresa_info)
            except Exception as e:
                logger.warning(f"Could not generate PDF for order {pedido.nro_pedido}: {e}")

        # 2. Customer Notifications
        self._notify_customer(pedido, pdf_bytes)

        # 3. Dispatch (Depósito) Notifications
        self._notify_dispatch(pedido, tenant, pdf_bytes)

        # 4. Admin Notifications (Owner)
        self._notify_admin(pedido, pdf_bytes)

    def dispatch_order_update(self, order, message: str):
        """
        Dispatches updates (e.g. status changes) to customer channels.
        Compatible with MarketOrder and PymePedido.
        """
        try:
            # WhatsApp/SMS Logic
            contact_phone = getattr(order, "contact_phone", None) or getattr(order, "telefono_cliente", None)
            order_id = getattr(order, "external_order_id", None) or getattr(order, "nro_pedido", None) or str(order.id)

            if contact_phone:
                try:
                    # Generic status update message
                    enviar_notificacion_whatsapp_con_plantilla(
                        contact_phone,
                        getattr(order, "contact_name", None) or getattr(order, "nombre_cliente", None) or "Cliente",
                        order_id,
                        message
                    )
                except Exception:
                    # Fallback or silent fail
                    pass
        except Exception as e:
            logger.error(f"Error dispatching order update for {order.id}: {e}")

    def _notify_customer(self, pedido: PymePedido, pdf_bytes: Optional[bytes]):
        # Email
        if pedido.email_cliente:
            try:
                enviar_email_pedido_cliente(pedido, pdf_bytes=pdf_bytes)
                logger.info(f"Customer email sent for order {pedido.nro_pedido}")
            except Exception as e:
                logger.error(f"Failed to send customer email for order {pedido.nro_pedido}: {e}")

        # WhatsApp / SMS
        if pedido.telefono_cliente:
            try:
                # Prioritize WhatsApp if available/configured, fallback to SMS logic handled inside or separately
                # Here we assume a template mechanism exists
                enviar_notificacion_whatsapp_con_plantilla(
                    pedido.telefono_cliente,
                    pedido.nombre_cliente or "Cliente",
                    pedido.nro_pedido,
                    pedido.rubro or "Pedido"
                )
                logger.info(f"Customer WhatsApp sent for order {pedido.nro_pedido}")
            except Exception as e:
                logger.error(f"Failed to send customer WhatsApp for order {pedido.nro_pedido}: {e}")
                # Fallback to SMS?
                try:
                    enviar_notificacion_sms(
                        pedido.telefono_cliente,
                        f"Hola {pedido.nombre_cliente or ''}! Tu pedido {pedido.nro_pedido} fue registrado."
                    )
                except Exception as sms_e:
                    logger.error(f"Failed to send customer SMS fallback for order {pedido.nro_pedido}: {sms_e}")

    def _notify_dispatch(self, pedido: PymePedido, tenant: Optional[TenantProfile], pdf_bytes: Optional[bytes]):
        if not tenant:
            return

        # Email to Warehouse
        if tenant.dispatch_email:
            try:
                enviar_email_pedido_despacho(pedido, tenant.dispatch_email, pdf_bytes=pdf_bytes)
                logger.info(f"Dispatch email sent to {tenant.dispatch_email} for order {pedido.nro_pedido}")
            except Exception as e:
                logger.error(f"Failed to send dispatch email for order {pedido.nro_pedido}: {e}")

        # WhatsApp to Warehouse
        if tenant.dispatch_phone:
            try:
                enviar_notificacion_whatsapp_con_plantilla(
                    tenant.dispatch_phone,
                    "Depósito", # Nombre genérico
                    pedido.nro_pedido,
                    "Nuevo Pedido a Preparar"
                )
                logger.info(f"Dispatch WhatsApp sent to {tenant.dispatch_phone} for order {pedido.nro_pedido}")
            except Exception as e:
                logger.error(f"Failed to send dispatch WhatsApp for order {pedido.nro_pedido}: {e}")

    def _notify_admin(self, pedido: PymePedido, pdf_bytes: Optional[bytes]):
        # This is the legacy "owner" notification
        try:
            enviar_email_pedido_admin(pedido, pdf_bytes=pdf_bytes)
        except Exception as e:
            logger.error(f"Failed to send admin email for order {pedido.nro_pedido}: {e}")

    def _get_empresa_info(self, pedido: PymePedido) -> dict:
        info = {}
        if pedido.pyme:
            info = {
                "nombre": getattr(pedido.pyme, "nombre_empresa", None) or getattr(pedido.pyme, "name", None),
                "direccion": getattr(pedido.pyme, "direccion", None),
                "telefono": getattr(pedido.pyme, "telefono", None),
                "email": getattr(pedido.pyme, "email", None),
            }
        return info

notification_dispatcher = NotificationDispatcher()
dispatch_order_update = notification_dispatcher.dispatch_order_update

import logging
from typing import Optional, Union

from models import PymePedido, TenantProfile, OrderEvent, MunicipioTicket, PymeTicket, db
from services.email_service import (
    enviar_email_pedido_cliente,
    enviar_email_pedido_despacho,
    enviar_email_pedido_admin,
    enviar_email_ticket_admin,
    enviar_email_ticket_cliente,
)
from services.notifications import (
    enviar_notificacion_sms,
    enviar_notificacion_whatsapp_con_plantilla,
)
from services.pedido_pdf import generar_pdf_nota_pedido

logger = logging.getLogger(__name__)

class NotificationDispatcher:
    """
    Centralized service to handle omnichannel notifications for orders and tickets.
    Designed to be robust: failures in one channel do not block others.
    """

    def dispatch_order_created(self, pedido, pdf_bytes: Optional[bytes] = None):
        """
        Dispatches all configured notifications for a new order.
        Supports both PymePedido (legacy) and Order (new).
        """
        # Determine ID and Tenant
        order_id = getattr(pedido, 'nro_pedido', getattr(pedido, 'id', 'unknown'))
        tenant_id = getattr(pedido, 'tenant_id', None)

        logger.info(f"Dispatching notifications for order {order_id} (Tenant: {tenant_id})")

        # Log initial event (if event system supports the model)
        # self._log_event(pedido, "order_created", {"status": getattr(pedido, 'status', getattr(pedido, 'estado', ''))})

        tenant = getattr(pedido, 'tenant', None)
        if not tenant and tenant_id:
            tenant = db.session.get(TenantProfile, tenant_id)

        # 1. Generate PDF if not provided (and feasible)
        # TODO: Adapt PDF generator for new Order model
        if not pdf_bytes and hasattr(pedido, 'nro_pedido'): # Only legacy for now for PDF
            try:
                empresa_info = self._get_empresa_info(pedido)
                pdf_bytes = generar_pdf_nota_pedido(pedido, empresa_info=empresa_info)
            except Exception as e:
                logger.warning(f"Could not generate PDF for order {order_id}: {e}")

        # 2. Customer Notifications
        if not tenant or getattr(tenant, 'send_buyer_email', True):
            self._notify_customer(pedido, pdf_bytes)
        else:
             logger.info(f"Buyer email disabled for tenant {tenant_id}")

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

    def _notify_customer(self, pedido, pdf_bytes: Optional[bytes]):
        # Adapt fields for Order vs PymePedido
        email = getattr(pedido, 'email_cliente', getattr(pedido, 'buyer_email', None))
        phone = getattr(pedido, 'telefono_cliente', getattr(pedido, 'buyer_phone', None))
        name = getattr(pedido, 'nombre_cliente', getattr(pedido, 'buyer_name', "Cliente"))
        order_ref = getattr(pedido, 'nro_pedido', getattr(pedido, 'id', ''))
        rubro_ref = getattr(pedido, 'rubro', "Pedido")

        # Create an adapter if it's the new Order model so legacy email service can read it
        pedido_adapter = pedido
        if not hasattr(pedido, 'nro_pedido'):
            class OrderAdapter:
                def __init__(self, order):
                    self.nro_pedido = order.id
                    self.nombre_cliente = order.buyer_name
                    self.email_cliente = order.buyer_email
                    self.telefono_cliente = order.buyer_phone
                    self.monto_total = float(order.total)
                    self.detalles = "items..." # Simplify or render items
                    # For PDF/HTML rendering, legacy service might expect 'detalles' string or items list
                    # We rely on email service handling the object if updated, or this adapter basic fields.
                    # Ideally, email service should be updated to handle Order object natively.
                    self.direccion = order.delivery_address if hasattr(order, 'delivery_address') else None

                def __getattr__(self, name):
                    return getattr(pedido, name, None)

            pedido_adapter = OrderAdapter(pedido)

        # Email
        if email:
            try:
                enviar_email_pedido_cliente(pedido_adapter, pdf_bytes=pdf_bytes)
                logger.info(f"Customer email sent for order {order_ref}")
            except Exception as e:
                logger.error(f"Failed to send customer email for order {order_ref}: {e}")

        # WhatsApp / SMS
        if phone:
            try:
                enviar_notificacion_whatsapp_con_plantilla(
                    phone,
                    name,
                    str(order_ref),
                    rubro_ref
                )
                logger.info(f"Customer WhatsApp sent for order {order_ref}")
            except Exception as e:
                logger.error(f"Failed to send customer WhatsApp for order {order_ref}: {e}")
                # Fallback to SMS
                try:
                    enviar_notificacion_sms(
                        phone,
                        f"Hola {name}! Tu pedido {order_ref} fue registrado."
                    )
                except Exception as sms_e:
                    logger.error(f"Failed to send customer SMS fallback for order {order_ref}: {sms_e}")

    def _notify_dispatch(self, pedido, tenant: Optional[TenantProfile], pdf_bytes: Optional[bytes]):
        if not tenant:
            return

        order_ref = getattr(pedido, 'nro_pedido', getattr(pedido, 'id', ''))

        # Email to Warehouse
        if getattr(tenant, 'dispatch_email', None) and getattr(tenant, 'send_dispatch_email', True):
            try:
                enviar_email_pedido_despacho(pedido, tenant.dispatch_email, pdf_bytes=pdf_bytes)
                logger.info(f"Dispatch email sent to {tenant.dispatch_email} for order {order_ref}")
            except Exception as e:
                logger.error(f"Failed to send dispatch email for order {order_ref}: {e}")

        # WhatsApp to Warehouse
        if getattr(tenant, 'dispatch_phone', None) and getattr(tenant, 'send_dispatch_whatsapp', True):
            try:
                enviar_notificacion_whatsapp_con_plantilla(
                    tenant.dispatch_phone,
                    "Depósito",
                    str(order_ref),
                    "Nuevo Pedido a Preparar"
                )
                logger.info(f"Dispatch WhatsApp sent to {tenant.dispatch_phone} for order {order_ref}")
            except Exception as e:
                logger.error(f"Failed to send dispatch WhatsApp for order {order_ref}: {e}")

    def _notify_admin(self, pedido: PymePedido, pdf_bytes: Optional[bytes]):
        # This is the legacy "owner" notification
        try:
            enviar_email_pedido_admin(pedido, pdf_bytes=pdf_bytes)
        except Exception as e:
            logger.error(f"Failed to send admin email for order {pedido.nro_pedido}: {e}")

    def _log_event(self, pedido, event_type, payload):
        try:
            event = OrderEvent(
                pyme_pedido_id=pedido.id,
                type=event_type,
                payload=payload
            )
            db.session.add(event)
            db.session.commit()
        except Exception as e:
             logger.error(f"Failed to log order event {event_type}: {e}")
             db.session.rollback()

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

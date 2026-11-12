import logging
from typing import Any, Optional

from models import PymePedido, TenantProfile, OrderEvent, db
from services.email_service import (
    enviar_email_pedido_cliente,
    enviar_email_pedido_despacho,
    enviar_email_pedido_admin,
)
from services.notifications import (
    enviar_notificacion_whatsapp_con_plantilla,
)
from services.pedido_pdf import generar_pdf_nota_pedido

logger = logging.getLogger(__name__)

class NotificationDispatcher:
    """
    Centralized service to handle omnichannel notifications for orders.
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
                    result = enviar_notificacion_whatsapp_con_plantilla(
                        contact_phone,
                        getattr(order, "contact_name", None) or getattr(order, "nombre_cliente", None) or "Cliente",
                        order_id,
                        message
                    )
                    if not result:
                        logger.info(
                            "Order update WhatsApp blocked tenant_id=%s reason=%s",
                            getattr(order, "tenant_id", None),
                            getattr(result, "reason_code", "provider_acceptance_unknown"),
                        )
                except Exception as exc:
                    logger.warning(
                        "Order update WhatsApp failed tenant_id=%s error_type=%s",
                        getattr(order, "tenant_id", None),
                        type(exc).__name__,
                    )
        except Exception as exc:
            logger.error(
                "Order update notification failed tenant_id=%s error_type=%s",
                getattr(order, "tenant_id", None),
                type(exc).__name__,
            )

    def _notify_customer(self, pedido, pdf_bytes: Optional[bytes]):
        # Adapt fields for Order vs PymePedido
        email = getattr(pedido, 'email_cliente', getattr(pedido, 'buyer_email', None))
        phone = getattr(pedido, 'telefono_cliente', getattr(pedido, 'buyer_phone', None))
        name = getattr(pedido, 'nombre_cliente', getattr(pedido, 'buyer_name', "Cliente"))
        order_ref = getattr(pedido, 'nro_pedido', getattr(pedido, 'id', ''))
        rubro_ref = getattr(pedido, 'rubro', "Pedido")
        tenant_id = getattr(pedido, 'tenant_id', None)

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
                result = enviar_notificacion_whatsapp_con_plantilla(
                    phone,
                    name,
                    str(order_ref),
                    rubro_ref
                )
                if result:
                    logger.info(
                        "Customer WhatsApp provider accepted tenant_id=%s",
                        tenant_id,
                    )
                else:
                    logger.info(
                        "Customer WhatsApp blocked tenant_id=%s reason=%s",
                        tenant_id,
                        getattr(result, "reason_code", "provider_acceptance_unknown"),
                    )
            except Exception as exc:
                logger.error(
                    "Customer WhatsApp failed tenant_id=%s error_type=%s",
                    tenant_id,
                    type(exc).__name__,
                )

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

        # WhatsApp to Warehouse (supports multiple comma-separated numbers)
        if getattr(tenant, 'dispatch_phone', None) and getattr(tenant, 'send_dispatch_whatsapp', True):
            phones = [p.strip() for p in tenant.dispatch_phone.split(',') if p.strip()]
            for phone in phones:
                try:
                    result = enviar_notificacion_whatsapp_con_plantilla(
                        phone,
                        "Depósito",
                        str(order_ref),
                        "Nuevo Pedido a Preparar"
                    )
                    if result:
                        logger.info(
                            "Dispatch WhatsApp provider accepted tenant_id=%s",
                            tenant.id,
                        )
                    else:
                        logger.info(
                            "Dispatch WhatsApp blocked tenant_id=%s reason=%s",
                            tenant.id,
                            getattr(result, "reason_code", "provider_acceptance_unknown"),
                        )
                except Exception as exc:
                    logger.error(
                        "Dispatch WhatsApp failed tenant_id=%s error_type=%s",
                        tenant.id,
                        type(exc).__name__,
                    )

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


def _dispatch_ticket_channels(
    ticket: Any,
    mensaje: str,
    *,
    tipo_ticket: str,
    comentario_reciente: Any = None,
    enable_whatsapp: bool = True,
    enabled_channels: Optional[list[str] | tuple[str, ...] | set[str]] = None,
    archivos_adjuntos: Optional[list[Any]] = None,
) -> dict[str, bool]:
    """Send ticket updates through configured customer channels.

    Ticket responses must not fail just because one notification channel is
    down. Keep imports local so tests can patch the email/WhatsApp functions and
    to avoid circular imports while routes are loading.
    """

    results = {"email": False, "sms": False, "whatsapp": False}
    normalized_channels = (
        {str(channel or "").strip().lower() for channel in enabled_channels}
        if enabled_channels is not None
        else {"email", "sms", "whatsapp"}
    )
    ticket_ref = getattr(ticket, "nro_ticket", None) or getattr(ticket, "id", "N/A")
    safe_message = (mensaje or "").strip() or f"Tu ticket {ticket_ref} tiene novedades."

    if "email" in normalized_channels:
        try:
            from services import email_service

            results["email"] = bool(
                email_service.enviar_email_ticket_novedad(
                    ticket,
                    safe_message,
                    comentario_reciente=comentario_reciente,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "Error sending ticket email notification ticket=%s tipo=%s: %s",
                ticket_ref,
                tipo_ticket,
                exc,
                exc_info=True,
            )

    if "sms" in normalized_channels:
        try:
            from services import email_service

            results["sms"] = bool(
                email_service.enviar_sms_ticket_novedad(ticket, safe_message)
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "Error sending ticket SMS notification ticket=%s tipo=%s: %s",
                ticket_ref,
                tipo_ticket,
                exc,
                exc_info=True,
            )

    if enable_whatsapp and "whatsapp" in normalized_channels:
        try:
            from services import email_service

            results["whatsapp"] = bool(
                email_service.enviar_whatsapp_ticket_novedad(
                    ticket,
                    safe_message,
                    archivos_adjuntos=archivos_adjuntos,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "Error sending ticket WhatsApp notification ticket=%s tipo=%s: %s",
                ticket_ref,
                tipo_ticket,
                exc,
                exc_info=True,
            )

    return results


def dispatch_ticket_update(
    ticket: Any,
    tipo_ticket: str,
    mensaje: str,
    *,
    comentario_reciente: Any = None,
    enable_whatsapp: bool = True,
    enabled_channels: Optional[list[str] | tuple[str, ...] | set[str]] = None,
    archivos_adjuntos: Optional[list[Any]] = None,
) -> dict[str, bool]:
    """Notify a citizen/customer that an agent replied or attached evidence."""

    return _dispatch_ticket_channels(
        ticket,
        mensaje,
        tipo_ticket=tipo_ticket,
        comentario_reciente=comentario_reciente,
        enable_whatsapp=enable_whatsapp,
        enabled_channels=enabled_channels,
        archivos_adjuntos=archivos_adjuntos,
    )


def dispatch_ticket_state_change(
    ticket: Any,
    tipo_ticket: str,
    nuevo_estado: str,
    *,
    comentario_estado: Any = None,
    enable_whatsapp: bool = True,
) -> dict[str, bool]:
    """Notify a citizen/customer when the visible ticket state changes."""

    ticket_ref = getattr(ticket, "nro_ticket", None) or getattr(ticket, "id", "N/A")
    estado_label = str(nuevo_estado or "").replace("_", " ").strip() or "actualizado"
    mensaje = f"Tu ticket {ticket_ref} fue actualizado a {estado_label}."
    return _dispatch_ticket_channels(
        ticket,
        mensaje,
        tipo_ticket=tipo_ticket,
        comentario_reciente=comentario_estado,
        enable_whatsapp=enable_whatsapp,
    )

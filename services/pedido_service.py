import json
import logging
from typing import Optional

from models import (
    db,
    CatalogoItem,
    MarketOrder,
    MarketOrderItem,
    PymePedido,
    TenantProfile,
    User,
)
from .email_service import (
    enviar_email_pedido_admin,
    enviar_email_pedido_cliente,
)
from .notifications import (
    enviar_notificacion_sms,
    enviar_notificacion_whatsapp_con_plantilla,
)
from utils.validators import (
    validate_name,
    validate_email_address,
    normalize_phone,
    validate_address,
)
from services.pedido_pdf import generar_pdf_nota_pedido

logger = logging.getLogger(__name__)


class PedidoService:
    def _crear_market_order_desde_pyme(
        self,
        pedido: PymePedido,
        channel: Optional[str] = None,
    ) -> Optional[MarketOrder]:
        tenant = TenantProfile.query.filter_by(pyme_id=pedido.pyme_id).first()
        if not tenant:
            return None

        existing = MarketOrder.query.filter_by(
            tenant_id=tenant.id,
            external_provider="pyme_pedido",
            external_order_id=pedido.nro_pedido,
        ).first()
        if existing:
            return existing

        status_map = {
            "pendiente": "pending",
            "confirmado": "confirmed",
            "en_proceso": "processing",
            "enviado": "shipped",
            "entregado": "delivered",
            "completado": "completed",
            "cancelado": "cancelled",
            "devuelto": "returned",
        }
        status = status_map.get((pedido.estado or "").lower(), "pending")

        order = MarketOrder(
            tenant_id=tenant.id,
            user_id=pedido.user_id,
            status=status,
            contact_name=pedido.nombre_cliente,
            contact_phone=pedido.telefono_cliente,
            contact_email=pedido.email_cliente,
            channel=channel or "chat",
            total_monetary=pedido.monto_total,
            currency="ARS",
            external_provider="pyme_pedido",
            external_order_id=pedido.nro_pedido,
            metadata_payload={
                "pyme_pedido_id": pedido.id,
                "pyme_id": pedido.pyme_id,
            },
        )

        try:
            detalles_items = json.loads(pedido.detalles or "[]")
        except (TypeError, ValueError):
            detalles_items = []

        for item in detalles_items if isinstance(detalles_items, list) else []:
            if not isinstance(item, dict):
                continue
            sku = item.get("sku")
            nombre = item.get("nombre") or item.get("nombre_producto")
            cantidad = item.get("cantidad") or 1
            precio = item.get("precio_unitario") or item.get("precio") or item.get("precio_unitario_original")

            producto = None
            if sku:
                producto = CatalogoItem.query.filter_by(user_id=pedido.pyme_id, sku=sku).first()
            if not producto and nombre:
                producto = CatalogoItem.query.filter_by(user_id=pedido.pyme_id, nombre=nombre).first()

            try:
                cantidad_normalizada = int(float(cantidad))
            except (TypeError, ValueError):
                cantidad_normalizada = 1

            order.items.append(
                MarketOrderItem(
                    product_id=producto.id if producto else None,
                    quantity=max(cantidad_normalizada, 1),
                    price_monetary=precio,
                    currency="ARS",
                    name_snapshot=nombre or (producto.nombre if producto else None),
                )
            )

        db.session.add(order)
        return order

    def sync_market_order_from_pyme(
        self,
        pedido: PymePedido,
        channel: Optional[str] = None,
    ) -> Optional[MarketOrder]:
        order = self._crear_market_order_desde_pyme(pedido, channel=channel)
        if order:
            db.session.commit()
        return order

    def crear_nuevo_pedido(self, pedido_data: dict) -> PymePedido | None:
        try:
            # Validar datos básicos
            if (
                not pedido_data.get("asunto")
                or not pedido_data.get("detalles")
                or not pedido_data.get("rubro")
            ):
                logger.error(
                    "Datos mínimos faltantes para crear pedido: asunto, detalles o rubro."
                )
                return None

            nombre = pedido_data.get("nombre_cliente")
            if nombre and not validate_name(nombre):
                logger.error("Nombre de cliente inválido")
                return None

            email = pedido_data.get("email_cliente")
            if email and not validate_email_address(email):
                logger.error("Email de cliente inválido")
                return None

            telefono = pedido_data.get("telefono_cliente")
            if telefono:
                telefono_normalizado = normalize_phone(telefono)
                if not telefono_normalizado:
                    logger.error("Teléfono de cliente inválido")
                    return None
                pedido_data["telefono_cliente"] = telefono_normalizado

            direccion = pedido_data.get("direccion")
            if direccion and not validate_address(direccion):
                logger.error("Dirección inválida")
                return None

            pyme_id = pedido_data.get("pyme_id")
            if not pyme_id:
                logger.error("pyme_id es requerido para registrar un pedido")
                return None

            nuevo_pedido = PymePedido(
                pyme_id=pyme_id,
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                monto_total=pedido_data.get("monto_total"),
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id"),  # Puede ser None si es anónimo
                direccion=pedido_data.get("direccion"),
                latitud=pedido_data.get("latitud"),
                longitud=pedido_data.get("longitud"),
            )
            if pedido_data.get("rubro"):
                nuevo_pedido.rubro = pedido_data.get("rubro")
            db.session.add(nuevo_pedido)
            db.session.commit()
            rubro_log = pedido_data.get("rubro") or getattr(nuevo_pedido, "rubro", None)
            logger.info(
                f"Nuevo pedido '{nuevo_pedido.nro_pedido}' creado para rubro '{rubro_log}' por cliente '{nuevo_pedido.nombre_cliente}'"
            )
            pyme_owner: Optional[User] = None
            empresa_info = None
            try:
                pyme_owner = db.session.get(User, pyme_id)
            except Exception:
                pyme_owner = User.query.get(pyme_id)
            if pyme_owner:
                empresa_info = {
                    "nombre": getattr(pyme_owner, "nombre_empresa", None) or getattr(pyme_owner, "name", None),
                    "direccion": getattr(pyme_owner, "direccion", None),
                    "telefono": getattr(pyme_owner, "telefono", None),
                    "email": getattr(pyme_owner, "email", None),
                }

            pdf_bytes = None
            try:
                pdf_bytes = generar_pdf_nota_pedido(nuevo_pedido, empresa_info=empresa_info)
            except RuntimeError as pdf_missing_dep:
                logger.warning(f"No se pudo generar el PDF del pedido: {pdf_missing_dep}")
            except Exception as e:
                logger.error(f"Error generando PDF de pedido {nuevo_pedido.nro_pedido}: {e}", exc_info=True)

            # Attach ephemeral attributes for downstream consumers (no commit)
            nuevo_pedido.nota_pedido_pdf_generado = bool(pdf_bytes)
            nuevo_pedido._nota_pedido_pdf_bytes = pdf_bytes  # type: ignore[attr-defined]
            nuevo_pedido._empresa_info_pdf = empresa_info  # type: ignore[attr-defined]

            try:
                enviar_email_pedido_admin(
                    nuevo_pedido,
                    pdf_bytes=pdf_bytes,
                    empresa_info=empresa_info,
                )
            except Exception as e:
                logger.error(f"Error enviando email de pedido: {e}")
            try:
                enviar_email_pedido_cliente(
                    nuevo_pedido,
                    pdf_bytes=pdf_bytes,
                    empresa_info=empresa_info,
                )
            except Exception as e:
                logger.error(f"Error enviando email al cliente: {e}")
            try:
                if nuevo_pedido.telefono_cliente:
                    telefono = nuevo_pedido.telefono_cliente
                    enviar_notificacion_sms(
                        telefono,
                        f"Hola {nuevo_pedido.nombre_cliente or ''}! Tu pedido {nuevo_pedido.nro_pedido} fue registrado.",
                    )
                    enviar_notificacion_whatsapp_con_plantilla(
                        telefono,
                        nuevo_pedido.nombre_cliente or "Cliente",
                        nuevo_pedido.nro_pedido,
                        nuevo_pedido.rubro or "Pedido",
                    )
            except Exception as e:
                logger.error(f"Error enviando SMS/WhatsApp de pedido: {e}")
            try:
                self.sync_market_order_from_pyme(
                    nuevo_pedido,
                    channel=pedido_data.get("channel"),
                )
            except Exception as e:
                db.session.rollback()
                logger.error(
                    "Error creando MarketOrder para pedido %s: %s",
                    nuevo_pedido.nro_pedido,
                    e,
                    exc_info=True,
                )
            return nuevo_pedido
        except Exception as e:
            db.session.rollback()
            logger.error(
                f"Error al crear nuevo pedido: {e}", exc_info=True
            )  # exc_info=True para ver el traceback
            return None

    def obtener_pedido_por_nro(self, nro_pedido: str) -> PymePedido | None:
        try:
            return PymePedido.query.filter_by(nro_pedido=nro_pedido).first()
        except Exception as e:
            logger.error(
                f"Error al obtener pedido por número '{nro_pedido}': {e}", exc_info=True
            )
            return None

    # Puedes añadir más funciones aquí, como actualizar estado, listar pedidos, etc.

    def crear_pedido_desde_carrito(self, pyme_id: int, cart_items: list, cliente_data: dict) -> PymePedido | None:
        """
        Crea un PymePedido a partir de los items del carrito.
        pyme_id: ID del usuario (Pyme) dueño del catálogo.
        cart_items: Lista de items del carrito (obtenida de services.cart.get_summary()).
        cliente_data: Diccionario con información del cliente y del pedido.
                      Debe incluir 'nombre_cliente', 'email_cliente', etc.
                      y opcionalmente 'cliente_user_id' si el cliente está registrado.
                      También 'rubro' para el pedido (de la pyme).
        """
        if not cart_items:
            logger.error("El carrito está vacío, no se puede crear el pedido.")
            return None

        detalles_pedido_items = []
        monto_total_calculado = 0.0
        cliente_user_id = cliente_data.get("cliente_user_id")

        from models import CatalogoItem  # Importación local para evitar circularidad
        from services.common_utils import parse_precio_flexible # Para parsear precios

        for item_carrito in cart_items:
            nombre_producto = item_carrito.get("nombre")
            cantidad_carrito = item_carrito.get("cantidad", 0)

            if not nombre_producto or cantidad_carrito <= 0:
                logger.warning(f"Item de carrito inválido omitido: {item_carrito}")
                continue

            # Buscar el producto en el catálogo de la Pyme especificada por pyme_id
            producto_catalogo = CatalogoItem.query.filter_by(user_id=pyme_id, nombre=nombre_producto).first()

            if not producto_catalogo:
                logger.error(f"Producto '{nombre_producto}' no encontrado en el catálogo de la Pyme {pyme_id}.")
                return None

            precio_str = producto_catalogo.precio
            _, precio_unitario_float, _ = parse_precio_flexible(precio_str)

            if precio_unitario_float is None:
                logger.error(f"No se pudo determinar el precio para el producto '{nombre_producto}'.")
                return None # O manejar error

            item_total = precio_unitario_float * cantidad_carrito

            detalles_pedido_items.append({
                "nombre": nombre_producto,
                "cantidad": cantidad_carrito,
                "precio_unitario": precio_unitario_float,
                "subtotal": item_total,
                "sku": producto_catalogo.sku,
                "categoria": producto_catalogo.categoria
            })
            monto_total_calculado += item_total

        if not detalles_pedido_items:
            logger.error("No se pudieron procesar items válidos del carrito.")
            return None

        import json # Para convertir la lista de detalles a JSON string
        asunto_base = cliente_data.get("asunto")
        if not asunto_base:
            asunto_base = f"Pedido desde carrito - Cliente {cliente_user_id or 'invitado'}"

        pedido_data = {
            "asunto": asunto_base,
            "detalles": json.dumps(detalles_pedido_items, ensure_ascii=False), # Guardar como JSON string
            "rubro": cliente_data.get("rubro", "general_pyme"), # Podría venir del perfil del usuario/pyme
            "nombre_cliente": cliente_data.get("nombre_cliente"),
            "email_cliente": cliente_data.get("email_cliente"),
            "telefono_cliente": cliente_data.get("telefono_cliente"),
            "direccion": cliente_data.get("direccion"),
            "latitud": cliente_data.get("latitud"),
            "longitud": cliente_data.get("longitud"),
            # user_id aquí es el del cliente que realiza el pedido, puede ser None para invitados
            "user_id": cliente_user_id,
            "pyme_id": pyme_id,
            # "monto_total" se asignará directamente al objeto PymePedido más abajo
        }

        # Validaciones de datos del cliente (nombre, email, telefono, direccion)
        # (reutilizando lógica de crear_nuevo_pedido si es aplicable o añadiendo aquí)
        nombre = pedido_data.get("nombre_cliente")
        if nombre and not validate_name(nombre):
            logger.error(f"Nombre de cliente inválido para pedido desde carrito: {nombre}")
            return None

        email = pedido_data.get("email_cliente")
        if email and not validate_email_address(email):
            logger.error(f"Email de cliente inválido para pedido desde carrito: {email}")
            return None

        telefono = pedido_data.get("telefono_cliente")
        if telefono:
            telefono_normalizado = normalize_phone(telefono)
            if not telefono_normalizado:
                logger.error(f"Teléfono de cliente inválido para pedido desde carrito: {telefono}")
                return None
            pedido_data["telefono_cliente"] = telefono_normalizado

        direccion = pedido_data.get("direccion")
        if direccion and not validate_address(direccion):
            logger.error(f"Dirección inválida para pedido desde carrito: {direccion}")
            return None

        try:
            # Crear el objeto PymePedido
            # El constructor de PymePedido ya maneja _generate_nro_pedido
            pyme_id = pedido_data.get("pyme_id") or pyme_id
            if not pyme_id:
                logger.error("pyme_id es requerido para crear el pedido desde carrito")
                return None

            nuevo_pedido_obj = PymePedido(
                pyme_id=pyme_id,
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                monto_total=monto_total_calculado,
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id"),
                direccion=pedido_data.get("direccion"),
                latitud=pedido_data.get("latitud"),
                longitud=pedido_data.get("longitud"),
            )
            if pedido_data.get("rubro"):
                nuevo_pedido_obj.rubro = pedido_data.get("rubro")

            db.session.add(nuevo_pedido_obj)
            db.session.commit()

            logger.info(
                "Nuevo pedido '%s' creado desde carrito para cliente '%s'. Monto: %s",
                nuevo_pedido_obj.nro_pedido,
                cliente_user_id or "anonimo",
                monto_total_calculado,
            )

            # Enviar notificaciones (reutilizando la lógica existente)
            try:
                enviar_email_pedido_admin(nuevo_pedido_obj)
            except Exception as e_admin_mail:
                logger.error(f"Error enviando email de pedido (carrito) al admin: {e_admin_mail}")
            try:
                enviar_email_pedido_cliente(nuevo_pedido_obj)
            except Exception as e_cliente_mail:
                logger.error(f"Error enviando email de pedido (carrito) al cliente: {e_cliente_mail}")
            try:
                if nuevo_pedido_obj.telefono_cliente:
                    telefono_notif = nuevo_pedido_obj.telefono_cliente
                    enviar_notificacion_sms(
                        telefono_notif,
                        f"Hola {nuevo_pedido_obj.nombre_cliente or ''}! Tu pedido {nuevo_pedido_obj.nro_pedido} desde el carrito fue registrado.",
                    )
                    # Asumiendo que enviar_notificacion_whatsapp_con_plantilla existe y es aplicable
                    enviar_notificacion_whatsapp_con_plantilla(
                        telefono_notif,
                        nuevo_pedido_obj.nombre_cliente or "Cliente",
                        nuevo_pedido_obj.nro_pedido,
                        nuevo_pedido_obj.rubro or "Pedido",
                    )
            except Exception as e_sms_wp:
                logger.error(f"Error enviando SMS/WhatsApp de pedido (carrito): {e_sms_wp}")

            return nuevo_pedido_obj

        except Exception as e:
            db.session.rollback()
            logger.error(
                f"Error al crear nuevo pedido desde carrito: {e}", exc_info=True
            )
            return None


# Instancia del servicio para usar en otros módulos
servicio_pedidos = PedidoService()

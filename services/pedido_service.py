import logging
from models import db, PymePedido  # Asegúrate que PymePedido esté importado desde models
from .email_service import (
    enviar_email_pedido_admin,
    enviar_email_pedido_cliente,
)
from .municipios import (
    enviar_notificacion_sms,
    enviar_notificacion_whatsapp_con_plantilla,
)
from utils.validators import (
    validate_name,
    validate_email_address,
    normalize_phone,
    validate_address,
)

logger = logging.getLogger(__name__)


class PedidoService:
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

            nuevo_pedido = PymePedido(
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                rubro=pedido_data["rubro"],
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id"),  # Puede ser None si es anónimo
                direccion=pedido_data.get("direccion"),
                latitud=pedido_data.get("latitud"),
                longitud=pedido_data.get("longitud"),
            )
            db.session.add(nuevo_pedido)
            db.session.commit()
            logger.info(
                f"Nuevo pedido '{nuevo_pedido.nro_pedido}' creado para rubro '{nuevo_pedido.rubro}' por cliente '{nuevo_pedido.nombre_cliente}'"
            )
            try:
                enviar_email_pedido_admin(nuevo_pedido)
            except Exception as e:
                logger.error(f"Error enviando email de pedido: {e}")
            try:
                enviar_email_pedido_cliente(nuevo_pedido)
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
        pedido_data = {
            "asunto": cliente_data.get("asunto", f"Pedido desde carrito - Usuario {user_id}"),
            "detalles": json.dumps(detalles_pedido_items, ensure_ascii=False), # Guardar como JSON string
            "rubro": cliente_data.get("rubro", "general_pyme"), # Podría venir del perfil del usuario/pyme
            "nombre_cliente": cliente_data.get("nombre_cliente"),
            "email_cliente": cliente_data.get("email_cliente"),
            "telefono_cliente": cliente_data.get("telefono_cliente"),
            "direccion": cliente_data.get("direccion"),
            "latitud": cliente_data.get("latitud"),
            "longitud": cliente_data.get("longitud"),
            # user_id aquí es el del cliente que realiza el pedido, puede ser None para invitados
            "user_id": cliente_data.get("cliente_user_id"),
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
            nuevo_pedido_obj = PymePedido(
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                rubro=pedido_data["rubro"],
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id"),
                direccion=pedido_data.get("direccion"),
                latitud=pedido_data.get("latitud"),
                longitud=pedido_data.get("longitud")
            )
            nuevo_pedido_obj.monto_total = monto_total_calculado # Asignar el monto calculado

            db.session.add(nuevo_pedido_obj)
            db.session.commit()

            logger.info(
                f"Nuevo pedido '{nuevo_pedido_obj.nro_pedido}' creado desde carrito para user '{user_id}'. Monto: {monto_total_calculado}"
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

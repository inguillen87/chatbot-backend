# services/actions/pyme_actions.py
import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any
from services.ticket_service import servicio_tickets

# from services import cart as cart_service # Example
# from models import PymePedido, db # Example

logger = logging.getLogger(__name__)

class CrearPedidoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearPedidoActionHandler with data: {action_data}")
        # Simplified logic: Acknowledge and simulate order creation
        # In real implementation, this would interact with cart_service and PymePedido model

        productos_pedido = action_data.get("productos_pedido") # Expects a list of product dicts
        if not productos_pedido or not isinstance(productos_pedido, list):
            return {
                "success": False,
                "message_to_user": "No entendí qué productos deseas pedir. ¿Podrías especificarlos?",
                "pedir_info": "productos_del_pedido"
            }

        try:
            # Simulate order creation
            # pedido = PymePedido(pyme_id=self.context.get("user_id"), cliente_id=self.context.get("cliente_id"), ...)
            # db.session.add(pedido)
            # db.session.commit()
            simulated_pedido_nro = "P-SIM" + str(action_data.get("id_simulacion", "67890"))

            items_desc = ", ".join([f"{p.get('cantidad', 1)} de {p.get('nombre', 'producto desconocido')}" for p in productos_pedido[:2]])
            if len(productos_pedido) > 2:
                items_desc += " y más"

            user_message = f"¡Entendido! Tu pedido de '{items_desc}' ha sido registrado con el número {simulated_pedido_nro}. Nos pondremos en contacto para confirmar."

            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"pedido_nro": simulated_pedido_nro, "status": "registrado", "items_count": len(productos_pedido)}
            }
        except Exception as e:
            logger.error(f"Error en CrearPedidoActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al registrar tu pedido. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }

class ConsultarProductoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarProductoActionHandler with data: {action_data}")
        nombre_producto = action_data.get("nombre_producto_mencionado")
        if not nombre_producto:
            return {
                "success": False,
                "message_to_user": "¿Sobre qué producto te gustaría saber más?",
                "pedir_info": "nombre_producto_consulta"
            }

        # Simulate fetching product info (e.g., from Qdrant or DB)
        # resultados_qdrant = buscar_catalogo_qdrant(...)
        simulated_info = f"Información sobre '{nombre_producto}': Es un excelente producto con [características]. Precio: $[precio_simulado]."
        if "zapatillas" in nombre_producto.lower():
            simulated_info += " Disponibles en talles 38 a 45."

        return {
            "success": True,
            "message_to_user": simulated_info,
            "data": {"producto_consultado": nombre_producto, "info_status": "simulada"}
        }

class AgregarAlCarritoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing AgregarAlCarritoActionHandler with data: {action_data}")
        producto_para_agregar = action_data.get("nombre_producto_mencionado")
        cantidad = action_data.get("cantidad_producto_mencionado", 1)

        if not producto_para_agregar:
            return {"success": False, "message_to_user": "¿Qué producto deseas agregar al carrito?", "pedir_info": "producto_para_agregar"}

        # Simulate adding to cart
        # success_cart = cart_service.add_item_to_cart(pyme_id=self.context.get("user_id"), ...)
        success_cart_simulated = True

        if success_cart_simulated:
            user_message = f"¡'{cantidad} de {producto_para_agregar}' agregado/s a tu carrito! ¿Deseas seguir comprando o finalizar el pedido?"
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"producto_agregado": producto_para_agregar, "cantidad": cantidad, "carrito_actualizado": True}
            }
        else:
            return {"success": False, "message_to_user": f"No pudimos agregar '{producto_para_agregar}' al carrito en este momento.", "data": {"producto_agregado": producto_para_agregar, "carrito_actualizado": False}}

class VerCarritoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing VerCarritoActionHandler with data: {action_data}")
        # Simulate fetching cart details
        # items_carrito = cart_service.get_cart_summary(...)
        simulated_cart_items = [
            {"nombre": "Producto Alfa", "cantidad": 2, "precio_unit": 100},
            {"nombre": "Producto Beta", "cantidad": 1, "precio_unit": 250}
        ]
        total_simulado = sum(item["cantidad"] * item["precio_unit"] for item in simulated_cart_items)

        if not simulated_cart_items:
            user_message = "Tu carrito está vacío."
        else:
            items_text_list = [f"- {item['cantidad']} x {item['nombre']} (${item['precio_unit']})" for item in simulated_cart_items]
            user_message = "En tu carrito tienes:\n" + "\n".join(items_text_list) + f"\n\nTotal: ${total_simulado}."

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"items_carrito": simulated_cart_items, "total": total_simulado}
        }

class ModificarCarritoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ModificarCarritoActionHandler with data: {action_data}")
        # Example: action_data = {"item_a_modificar": "Producto Alfa", "nueva_cantidad": 3}
        # O: action_data = {"item_a_eliminar": "Producto Beta"}

        # Simulate modification
        user_message = "Tu carrito ha sido modificado."
        if action_data.get("item_a_modificar") and "nueva_cantidad" in action_data:
            user_message = f"Cantidad de '{action_data['item_a_modificar']}' actualizada a {action_data['nueva_cantidad']}."
        elif action_data.get("item_a_eliminar"):
             user_message = f"'{action_data['item_a_eliminar']}' eliminado de tu carrito."
        else:
            return {"success": False, "message_to_user": "No especificaste qué modificar en el carrito."}

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"carrito_modificado": True}
        }

class FinalizarCompraActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing FinalizarCompraActionHandler with data: {action_data}")
        # Simulate finalizing purchase and creating PymePedido
        # This would involve getting all items from cart, contact details, etc.
        # pedido = PymePedido.crear_desde_carrito(...)
        simulated_pedido_final_nro = "FP-SIM" + str(action_data.get("id_simulacion", "XYZ"))
        user_message = f"¡Gracias por tu compra! Tu pedido {simulated_pedido_final_nro} ha sido confirmado. Recibirás un email con los detalles de pago y envío."
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"pedido_final_nro": simulated_pedido_final_nro, "status_pedido": "confirmado"}
        }

class ConsultarOfertasActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarOfertasActionHandler with data: {action_data}")
        # Simulate fetching offers
        # promos = promocion_service.get_promociones_for_pyme(...)
        simulated_offers = [
            {"nombre": "Descuento Primavera", "descripcion": "20% off en toda la tienda."},
            {"nombre": "2x1 en Bebidas", "descripcion": "Llevá 2 bebidas y pagá 1."}
        ]
        if simulated_offers:
            offers_text = "¡Tenemos estas ofertas especiales para vos!:\n" + "\n".join([f"- **{o['nombre']}**: {o['descripcion']}" for o in simulated_offers])
        else:
            offers_text = "Por el momento no tenemos ofertas especiales, ¡pero nuestro catálogo tiene excelentes precios!"

        return {
            "success": True,
            "message_to_user": offers_text,
            "data": {"ofertas_consultadas": len(simulated_offers)}
        }

class SolicitarUbicacionTiendaActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing SolicitarUbicacionTiendaActionHandler with data: {action_data}")
        # Simulate fetching store location(s)
        # locations = get_store_locations_from_db_or_config(...)
        simulated_location = "Nuestra tienda principal está en Av. Siempre Viva 742. ¡Te esperamos!"
        # Could also include map link or hours.
        return {
            "success": True,
            "message_to_user": simulated_location,
            "data": {"ubicacion_principal": "Av. Siempre Viva 742"}
        }

from services.pedido_service import servicio_pedidos


class ConsultarEstadoPedidoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoPedidoActionHandler with data: {action_data}")
        nro_pedido_llm = action_data.get("id_pedido_mencionado")
        if not nro_pedido_llm:
            return {
                "success": False,
                "message_to_user": "Para consultar el estado de tu pedido, necesito el número de referencia.",
                "pedir_info": "id_pedido_mencionado"
            }

        nro_pedido_str = str(nro_pedido_llm)
        pedido = servicio_pedidos.consultar_pedido_por_numero(nro_pedido_str, pyme_id=self.context.get("user_id"))

        if pedido:
            user_message = f"Tu pedido #{nro_pedido_str} se encuentra en estado: **{pedido.estado}**."
            if pedido.estado == "entregado":
                user_message += " ¡Gracias por tu compra!"
            elif pedido.estado == "en_camino":
                user_message += " Debería llegar pronto."
        else:
            user_message = f"No encontré ningún pedido con el número #{nro_pedido_str}. Por favor, verifica el número e intenta de nuevo."

        return {
            "success": True if pedido else False,
            "message_to_user": user_message,
            "data": {"nro_pedido": nro_pedido_str, "estado_actual": pedido.estado if pedido else None}
        }

class ConsultarInfoPymeActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarInfoPymeActionHandler with data: {action_data}")
        owner_user = self.context.get("user_obj")
        if not owner_user:
            return {"success": False, "message_to_user": "No se pudo identificar la empresa."}

        info_pyme = {
            "nombre": owner_user.nombre_empresa,
            "direccion": getattr(owner_user, 'direccion_fisica', 'No especificada'),
            "telefono": getattr(owner_user, 'telefono_contacto', 'No especificado'),
            "email": owner_user.email,
            "horarios": getattr(owner_user, 'horarios_atencion', 'No especificados')
        }

        respuesta = (
            f"Aquí tienes la información sobre **{info_pyme['nombre']}**:\n"
            f"- **Dirección**: {info_pyme['direccion']}\n"
            f"- **Teléfono**: {info_pyme['telefono']}\n"
            f"- **Email**: {info_pyme['email']}\n"
            f"- **Horarios**: {info_pyme['horarios']}"
        )

        return {
            "success": True,
            "message_to_user": respuesta,
            "data": info_pyme
        }

# This handler is now centralized in common_actions.py
# class DerivarHumanoActionHandlerPyme(BaseActionHandler): ...

class HacerSugerenciaActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing HacerSugerenciaActionHandler for PYME with data: {action_data}")
        descripcion_sugerencia = action_data.get("descripcion")
        if not descripcion_sugerencia:
            return {
                "success": False,
                "message_to_user": "Claro, ¿cuál es tu sugerencia o idea?",
                "pedir_info": "descripcion_sugerencia"
            }

        viewer_user = self.context.get("viewer_user_obj")
        owner_user = self.context.get("user_obj")
        user_id_db = getattr(viewer_user, "id", None)
        anon_id_db = self.context.get("anon_id") if not user_id_db else None
        pyme_id_db = getattr(owner_user, "id", None)
        nombre_cliente = getattr(viewer_user, "nombre", "Cliente Anónimo")

        ticket_data = {
            "asunto": f"Sugerencia de Cliente: {descripcion_sugerencia[:30]}...",
            "categoria": "Sugerencia",
            "detalles": descripcion_sugerencia,
            "estado": "nuevo",
            "user_id": user_id_db,
            "anon_id": anon_id_db,
            "pyme_id": pyme_id_db,
            "origen_reclamo": "LLM_CHATBOT_PYME"
        }

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            nro_ticket_str = f"S-{ticket_creado.nro_ticket}"
            logger.info(f"Ticket de sugerencia para PYME {nro_ticket_str} creado exitosamente.")

            user_message = f"¡Muchas gracias, {nombre_cliente}! Hemos recibido tu sugerencia con el número de referencia {nro_ticket_str}. Valoramos mucho tus ideas."
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en HacerSugerenciaActionHandler para PYME: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al registrar tu sugerencia. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }

class ProcesarAdjuntoPedidoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoPedidoActionHandler with data: {action_data}")
        archivo_url = action_data.get("archivo_url")
        analisis_archivo = action_data.get("analisis_archivo") # e.g., from DocumentAI for an Excel order

        if not archivo_url:
            return {"success": False, "message_to_user": "No se detectó ningún archivo adjunto para el pedido."}

        user_message = f"Recibí el archivo {archivo_url}. "
        if analisis_archivo and analisis_archivo.get("productos_extraidos"):
            num_prod = len(analisis_archivo["productos_extraidos"])
            user_message += f"He extraído {num_prod} productos del archivo. ¿Querés que los agregue a tu pedido?"
            # This data would then be used by CrearPedidoActionHandler or AgregarAlCarritoActionHandler
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"adjunto_procesado": True, "productos_para_agregar": analisis_archivo["productos_extraidos"]},
                "pedir_info": "confirmar_agregar_productos_de_archivo"
            }
        else:
            user_message += "Lo revisaremos manualmente. Mientras tanto, ¿cómo querés continuar con tu pedido?"
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"adjunto_procesado": True, "revision_manual_requerida": True}
            }

class CorregirDatosPedidoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CorregirDatosPedidoActionHandler with data: {action_data}")

        item_a_corregir = action_data.get("item_a_corregir") # e.g., "Vino Tinto" or an ID
        campo_a_corregir = action_data.get("campo_a_corregir") # e.g., "cantidad", "producto" (para cambiarlo por otro)
        nuevo_valor = action_data.get("nuevo_valor")

        if not item_a_corregir or not campo_a_corregir or nuevo_valor is None:
            return {
                "success": False,
                "message_to_user": "No especificaste qué producto o dato corregir, o cuál es el nuevo valor.",
                "pedir_info": "detalle_correccion_pedido"
            }

        user_message = f"Entendido. He actualizado '{campo_a_corregir}' para '{item_a_corregir}' a '{nuevo_valor}'. ¿Revisamos el carrito o finalizamos?"

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"item_corregido": item_a_corregir, "campo_corregido": campo_a_corregir, "valor_actualizado": nuevo_valor},
            "pedir_info": "confirmacion_tras_correccion_pedido"
        }

# services/actions/pyme_order_actions.py
import logging
import json
from typing import Dict, Any, List, Optional
from .base_action_handler import BaseActionHandler
from services.pedido_service import servicio_pedidos # For creating PymePedido
from services.conversation_summaries import build_order_confirmation_payload
from services.order_attachment_preview import build_order_attachment_preview
from services.cart import (
    add_item_to_cart, remove_item_from_cart,
    update_item_quantity_in_cart, clear_pyme_cart, get_cart_summary
)
from services.qdrant_search import buscar_catalogo_qdrant, CATALOGO_PYME
from models import CatalogoItem, db, User, PymePedido # Added PymePedido
from services.common_utils import parse_precio_flexible, validar_telefono, formatear_telefono_e164, validar_email

logger = logging.getLogger(__name__)

def _get_pyme_carts_data_from_context(context: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    chat_db_context_data = context.get("chat_db_context_data")
    if not isinstance(chat_db_context_data, dict):
        logger.error("chat_db_context_data is not a dict or not found. Initializing.")
        context["chat_db_context_data"] = {}
        chat_db_context_data = context["chat_db_context_data"]
    if 'carritos_pymes' not in chat_db_context_data or not isinstance(chat_db_context_data['carritos_pymes'], dict):
        chat_db_context_data['carritos_pymes'] = {}
    return chat_db_context_data['carritos_pymes']

class AgregarItemCarritoAction(BaseActionHandler):
    def _find_product_details(self, pyme_id: int, product_identifier: str) -> Optional[Dict[str, Any]]:
        # This function can be expanded with more sophisticated search logic,
        # including fuzzy matching, alias resolution, etc.
        # For now, it relies on a direct Qdrant search.
        qdrant_collection = CATALOGO_PYME
        logger.info(f"Searching Qdrant '{qdrant_collection}' for '{product_identifier}' (pyme_id: {pyme_id})")
        qdrant_results = buscar_catalogo_qdrant(user_id=pyme_id, pregunta=product_identifier, limite=1, coleccion=qdrant_collection)

        if not qdrant_results or not qdrant_results[0].payload:
            logger.warning(f"Product '{product_identifier}' not found for pyme_id {pyme_id}.")
            return None

        payload = qdrant_results[0].payload
        db_id = payload.get("db_id")
        item_db = db.session.get(CatalogoItem, db_id) if db_id else None

        if item_db:
            _, precio_float, moneda = parse_precio_flexible(item_db.precio)
            return {
                "catalogo_item_id": item_db.id, "nombre_producto": item_db.nombre,
                "precio_unitario": precio_float, "moneda": moneda or "ARS", "sku": item_db.sku,
                "presentacion": item_db.unidad, "imagen_url": item_db.imagen_url
            }

        logger.warning(f"Qdrant found '{product_identifier}', but no corresponding DB record via db_id={db_id}. Using Qdrant payload as fallback.")
        _, precio_float, moneda = parse_precio_flexible(payload.get("precio_str", "0"))
        return {
            "catalogo_item_id": payload.get("sku") or payload.get("nombre"),
            "nombre_producto": payload.get("nombre", product_identifier),
            "precio_unitario": precio_float, "moneda": moneda or "ARS",
            "sku": payload.get("sku"), "presentacion": payload.get("unidad_descripcion") or payload.get("unidad_original"),
            "imagen_url": payload.get("imagen_url")
        }

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing AgregarItemCarritoAction with data: {action_data}")
        pyme_id = self.context.get("user_id")
        if not pyme_id:
            return {"success": False, "message_to_user": "Error: Tienda no identificada."}

        product_identifier = action_data.get("nombre_producto_mencionado") or action_data.get("producto_sku")
        if not product_identifier:
            return {"success": False, "message_to_user": "Por favor, especifica el producto que deseas agregar.", "pedir_info": "nombre_producto_mencionado"}

        try:
            cantidad = int(action_data.get("cantidad_producto_mencionado", 1))
            if cantidad <= 0: raise ValueError("La cantidad debe ser un número positivo.")
        except (ValueError, TypeError):
            return {"success": False, "message_to_user": "La cantidad proporcionada no es válida. Por favor, indica un número."}

        producto_info = self._find_product_details(pyme_id, product_identifier)
        if not producto_info:
            return {"success": False, "message_to_user": f"No pude encontrar el producto '{product_identifier}'. ¿Quieres que busque otra cosa?"}
        if producto_info.get("precio_unitario") is None:
            return {"success": False, "message_to_user": f"El producto '{product_identifier}' no tiene un precio definido y no se puede agregar al carrito."}

        pyme_carts_data = _get_pyme_carts_data_from_context(self.context)
        add_item_to_cart(pyme_carts_data, pyme_id, producto_info, cantidad)

        cart_summary_obj = get_cart_summary(pyme_carts_data, pyme_id, self.context.get("cliente_id"))
        from services.pymes import formatear_carrito_desde_summary
        cart_display_text = formatear_carrito_desde_summary(cart_summary_obj, self.context)

        # Botones para acciones comunes después de agregar un item
        botones = [
            {"texto": "Finalizar Compra", "id_accion": "finalizar_compra"},
            {"texto": "Ver Catálogo", "id_accion": "ver_catalogo_pyme"},
            {"texto": "Modificar Carrito", "id_accion": "ver_carrito"},
        ]

        return {
            "success": True,
            "message_to_user": f"¡Listo! Agregué {cantidad} x '{producto_info['nombre_producto']}' a tu carrito.\n\n{cart_display_text}",
            "data": {"cart_summary": cart_summary_obj, "last_added": producto_info['nombre_producto']},
            "options_list": botones
        }

class CrearPedidoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearPedidoAction with data: {action_data}")
        pyme_id = self.context.get("user_id")
        if not pyme_id:
            return {"success": False, "message_to_user": "Error: Tienda no identificada."}

        pyme_carts_data = _get_pyme_carts_data_from_context(self.context)
        cliente_user_id = self.context.get("cliente_id")
        current_cart_summary = get_cart_summary(pyme_carts_data, pyme_id, cliente_user_id)

        if not current_cart_summary.get("items_detalle"):
            return {"success": False, "message_to_user": "Tu carrito está vacío. Por favor, agrega productos antes de crear un pedido."}

        # Data validation for customer info
        nombre_cliente = action_data.get("nombre_usuario_detectado") or self.context.get("nombre_usuario_contexto")
        telefono_cliente_raw = str(action_data.get("telefono_detectado") or self.context.get("telefono_usuario_contexto", ""))
        email_cliente_raw = str(action_data.get("email_detectado") or self.context.get("email_usuario_contexto", "")).lower()
        direccion_entrega = action_data.get("ubicacion") or self.context.get("direccion_usuario_contexto")

        telefono_cliente_validado = formatear_telefono_e164(telefono_cliente_raw) if validar_telefono(telefono_cliente_raw) else None
        email_cliente_validado = email_cliente_raw if validar_email(email_cliente_raw) else None

        missing_contact = []
        if not nombre_cliente:
            missing_contact.append("nombre")
        if self.context.get("channel") == "voice":
            if not telefono_cliente_validado:
                missing_contact.append("un teléfono de contacto")
        else:
            if not (telefono_cliente_validado or email_cliente_validado):
                missing_contact.append("un teléfono o email de contacto")
        if not direccion_entrega:
            missing_contact.append("una dirección de entrega")

        if missing_contact:
            campos_str = " y ".join(missing_contact)
            return {"success": False,
                    "message_to_user": f"Para finalizar tu pedido, necesito que me indiques {campos_str}.",
                    "pedir_info": missing_contact[0]}

        owner_user_obj = self.context.get("user_obj")
        rubro_pyme = getattr(owner_user_obj.rubro, "nombre", "General") if owner_user_obj and hasattr(owner_user_obj, "rubro") else "General"

        detalles_json_str = json.dumps([{
            "nombre": item.get("nombre_producto"), "cantidad": item.get("cantidad"),
            "precio_unitario": item.get("precio_unitario_original"), "subtotal": item.get("subtotal_con_descuento"),
            "sku": item.get("sku")
        } for item in current_cart_summary.get("items_detalle", [])])

        monto_total_estimado = current_cart_summary.get("total_final_con_descuento")
        if monto_total_estimado is None:
            monto_total_estimado = current_cart_summary.get("total_original_calculado", 0.0)

        pedido_payload_for_model = {
            "asunto": f"Pedido desde Chatbot para: {nombre_cliente}",
            "detalles": detalles_json_str,
            "rubro": rubro_pyme,
            "nombre_cliente": nombre_cliente,
            "email_cliente": email_cliente_validado,
            "telefono_cliente": telefono_cliente_validado,
            "user_id": cliente_user_id,
            "direccion": direccion_entrega,
            "monto_total": monto_total_estimado,
            "pyme_id": pyme_id,
            "channel": self.context.get("channel"),
        }

        try:
            nuevo_pedido = servicio_pedidos.crear_nuevo_pedido(pedido_payload_for_model)
            if not nuevo_pedido:
                 raise Exception("El servicio de pedidos no pudo crear el registro.")

            clear_pyme_cart(pyme_carts_data, pyme_id)
            logger.info(f"Pedido #{nuevo_pedido.nro_pedido} creado y carrito limpiado para pyme_id {pyme_id}.")

            from services.pymes import formatear_carrito_desde_summary  # Local import to avoid circular dependency

            resumen_carrito = formatear_carrito_desde_summary(current_cart_summary, self.context)
            cliente_payload = {
                "nombre": nombre_cliente,
                "telefono": telefono_cliente_validado,
                "email": email_cliente_validado,
                "direccion": direccion_entrega,
            }

            nota_pdf_generado = getattr(nuevo_pedido, "nota_pedido_pdf_generado", False)
            order_confirmation = build_order_confirmation_payload(
                cart_summary=current_cart_summary,
                customer=cliente_payload,
                delivery_address=direccion_entrega,
                channel=self.context.get("channel"),
            )
            data_payload = {
                "nro_pedido": nuevo_pedido.nro_pedido,
                "pedido_id": nuevo_pedido.id,
                "status_pedido": "registrado",
                "monto_total": monto_total_estimado,
                "cart_summary": current_cart_summary,
                "cliente": cliente_payload,
                "order_summary_text": resumen_carrito,
                "order_confirmation": order_confirmation,
                "confirmation_card": order_confirmation,
                "nota_pedido_pdf_generado": nota_pdf_generado,
            }

            mensaje_confirmacion = resumen_carrito
            if nota_pdf_generado and email_cliente_validado:
                mensaje_confirmacion += f"\n\nTe enviamos la nota de pedido en PDF a {email_cliente_validado}."
            elif nota_pdf_generado:
                mensaje_confirmacion += "\n\nLa nota de pedido en PDF está lista para compartir con tu equipo."

            return {
                "success": True,
                "message_body": mensaje_confirmacion,
                "data": data_payload,
                "fuente": "pyme_pedido_registrado",
            }
        except Exception as e:
            logger.error(f"Error crítico al crear PymePedido: {e}", exc_info=True)
            return {"success": False, "message_to_user": "Hubo un problema técnico al registrar tu pedido. Por favor, intenta de nuevo más tarde o contacta a soporte."}


class ConsultarProductoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarProductoAction with data: {action_data}")
        pyme_id = self.context.get("user_id")
        if not pyme_id:
             return {"success": False, "message_to_user": "Error: Tienda no identificada."}

        query = action_data.get("nombre_producto_mencionado") or action_data.get("consulta_producto")
        if not query:
            return {"success": False, "message_to_user": "Por favor, dime qué producto buscas.", "pedir_info": "nombre_producto_consulta"}

        pyme_user = self.context.get("user_obj")
        rubro_nombre = getattr(pyme_user.rubro, "nombre", "general") if pyme_user and hasattr(pyme_user, "rubro") else "general"
        qdrant_collection = CATALOGO_PYME

        resultados = buscar_catalogo_qdrant(user_id=pyme_id, pregunta=query, limite=3, coleccion=qdrant_collection)

        if not resultados:
            return {"success": True, "message_to_user": f"No encontré productos para '{query}'. ¿Intentar otra búsqueda?"}

        respuesta_str = f"Resultados para '{query}':\n"
        productos_info_list = []
        for hit in resultados:
            payload = hit.payload; nombre = payload.get("nombre", "N/A"); desc = payload.get("descripcion_corta", "")
            _, precio_f, moneda = parse_precio_flexible(payload.get("precio_str", "0"))

            prod_info = {"nombre": nombre, "descripcion": desc,
                         "precio_formateado": f"${precio_f:,.2f} {moneda or 'ARS'}" if precio_f is not None else "Consultar precio",
                         "sku": payload.get("sku")}
            productos_info_list.append(prod_info)
            respuesta_str += f"\n- **{nombre}**: {desc} (Precio: {prod_info['precio_formateado']})"

        respuesta_str += "\n\n¿Te interesa alguno o buscamos otra cosa?"

        sugerencias_botones_llm = [{"texto": f"Agregar {p['nombre'][:15]}", "action_id": f"agregar_item_carrito__{p.get('sku') or p['nombre']}"} for p in productos_info_list]
        sugerencias_botones_llm.append({"texto": "Buscar de nuevo", "action_id": "consultar_producto_pyme"})

        return {"success": True, "message_to_user": respuesta_str,
                "data": {"productos_encontrados": productos_info_list, "sugerencias_botones_llm": sugerencias_botones_llm}}

# --- Placeholder Handlers for PYME ---

class VerCarritoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing VerCarritoAction for PYME with data: {action_data}")
        pyme_id = self.context.get("user_id")
        if not pyme_id: return {"success": False, "message_to_user": "Error: Tienda no identificada."}

        pyme_carts_data = _get_pyme_carts_data_from_context(self.context)
        cliente_user_id = self.context.get("cliente_id")
        cart_summary = get_cart_summary(pyme_carts_data, pyme_id, cliente_user_id)
        from services.pymes import formatear_carrito_desde_summary

        user_message = formatear_carrito_desde_summary(cart_summary, self.context)
        if not cart_summary.get("items_detalle"):
            user_message = "Tu carrito está vacío."
        else:
            user_message += "\n\n¿Quieres finalizar la compra, seguir agregando productos o modificar algo?"

        return {"success": True, "message_to_user": user_message, "data": {"cart_summary": cart_summary}}

class ModificarCarritoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ModificarCarritoAction for PYME with data: {action_data}")
        pyme_id = self.context.get("user_id")
        if not pyme_id: return {"success": False, "message_to_user": "Error: Tienda no identificada."}

        item_identifier = action_data.get("item_a_modificar_nombre") or action_data.get("item_a_modificar_sku")
        nueva_cantidad_str = str(action_data.get("nueva_cantidad", "")).strip()
        accion_especifica = action_data.get("sub_accion_modificar") # "cambiar_cantidad" o "eliminar_item"

        if not item_identifier or not accion_especifica:
            return {"success": False, "message_to_user": "Por favor, dime qué producto modificar y cómo (ej: 'quitar manzanas' o 'cambiar peras a 3')."}

        pyme_carts_data = _get_pyme_carts_data_from_context(self.context)
        # Find the catalogo_item_id from the identifier (this is simplified, might need Qdrant/DB lookup)
        # For simplicity, assume item_identifier is the catalogo_item_id or can be resolved to it.
        # This part needs robust product identification from cart.

        # Conceptual: _resolve_item_id_in_cart(pyme_carts_data, pyme_id, item_identifier)
        # For now, let's assume item_identifier is the catalogo_item_id if it's an integer, or needs lookup.
        # This is a complex step if item_identifier is just a name.

        # Let's assume for now that LLM provides catalogo_item_id if it knows it from previous context.
        catalogo_item_id_to_mod = action_data.get("catalogo_item_id_para_modificar")

        if not catalogo_item_id_to_mod: # Fallback: try to find by name (very basic)
            current_cart_summary = get_cart_summary(pyme_carts_data, pyme_id, self.context.get("cliente_id"))
            for item_det in current_cart_summary.get("items_detalle", []):
                if item_identifier.lower() in item_det.get("nombre_producto","").lower():
                    catalogo_item_id_to_mod = item_det.get("catalogo_item_id")
                    break
            if not catalogo_item_id_to_mod:
                 return {"success": False, "message_to_user": f"No encontré '{item_identifier}' en tu carrito para modificar."}


        if accion_especifica == "eliminar_item":
            remove_item_from_cart(pyme_carts_data, pyme_id, catalogo_item_id_to_mod)
            message = f"'{item_identifier}' eliminado del carrito."
        elif accion_especifica == "cambiar_cantidad":
            if not nueva_cantidad_str:
                return {"success": False, "message_to_user": f"¿A qué cantidad quieres cambiar '{item_identifier}'?"}
            try:
                nueva_cant_int = int(nueva_cantidad_str)
                if nueva_cant_int < 0: return {"success": False, "message_to_user": "La cantidad no puede ser negativa."}
                if nueva_cant_int == 0:
                     remove_item_from_cart(pyme_carts_data, pyme_id, catalogo_item_id_to_mod)
                     message = f"'{item_identifier}' eliminado del carrito (cantidad 0)."
                else:
                     # update_item_quantity_in_cart needs the full product_info if item not in cart,
                     # or just id and new_qty if already in cart.
                     # We need to ensure it handles this correctly.
                     # For now, assume it finds item by id and updates qty.
                     update_item_quantity_in_cart(pyme_carts_data, pyme_id, catalogo_item_id_to_mod, nueva_cant_int)
                     message = f"Cantidad de '{item_identifier}' actualizada a {nueva_cant_int}."
            except ValueError:
                return {"success": False, "message_to_user": f"La cantidad '{nueva_cantidad_str}' no es válida."}
        else:
            return {"success": False, "message_to_user": "No entendí cómo quieres modificar el carrito."}

        updated_summary = get_cart_summary(pyme_carts_data, pyme_id, self.context.get("cliente_id"))
        from services.pymes import formatear_carrito_desde_summary
        cart_display = formatear_carrito_desde_summary(updated_summary, self.context)
        return {"success": True, "message_to_user": f"{message}\n\n{cart_display}", "data": {"cart_summary": updated_summary}}


class FinalizarCompraAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing FinalizarCompraAction for PYME with data: {action_data}")
        # This is essentially the same as CrearPedidoAction if all data is confirmed.
        # It might be triggered after user confirms the cart and contact details.
        return CrearPedidoAction(self.context).execute(action_data) # Reuse logic

class ConsultarOfertasAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarOfertasAction for PYME with data: {action_data}")
        # from services.promocion_service import promocion_service # Idealmente inyectado o accesible
        # pyme_id = self.context.get("user_id")
        # promos_activas = promocion_service.get_promociones_for_pyme(pyme_id, activas_unicamente=True)
        simulated_offers = [{"nombre": "20% OFF en Electrónica", "descripcion": "Válido este fin de semana."}]
        if simulated_offers:
            msg = "¡Ofertas actuales!:\n" + "\n".join([f"- {o['nombre']}: {o['descripcion']}" for o in simulated_offers])
        else:
            msg = "No hay ofertas especiales ahora, pero nuestro catálogo tiene excelentes productos."
        return {"success": True, "message_to_user": msg, "data": {"ofertas_listadas": len(simulated_offers)}}

class SolicitarUbicacionTiendaAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing SolicitarUbicacionTiendaAction for PYME with data: {action_data}")
        # pyme_user = self.context.get("user_obj")
        # direccion_tienda = getattr(pyme_user, "direccion_fisica", "Nuestra dirección principal es...")
        # horarios = getattr(pyme_user, "horarios_atencion", "")
        simulated_loc = "Nuestra tienda está en Av. Comercial 123. Horario: L-V 9-18hs."
        return {"success": True, "message_to_user": simulated_loc, "data": {"direccion": "Av. Comercial 123"}}

class ConsultarEstadoPedidoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoPedidoAction for PYME with data: {action_data}")
        nro_pedido_llm = action_data.get("id_pedido_mencionado")
        if not nro_pedido_llm:
            return {"success": False, "message_to_user": "Necesito el número de pedido para consultar.", "pedir_info": "id_pedido_mencionado"}

        nro_pedido_str = str(nro_pedido_llm)
        # pedido = servicio_pedidos.consultar_pedido_por_numero(nro_pedido_str, pyme_id=self.context.get("user_id"))
        sim_pedido_estado = "En preparación" # if pedido else None
        if sim_pedido_estado:
            msg = f"Tu pedido #{nro_pedido_str} está: **{sim_pedido_estado}**."
        else:
            msg = f"No encontré el pedido #{nro_pedido_str}."
        return {"success": True, "message_to_user": msg, "data": {"nro_pedido": nro_pedido_str, "estado_actual": sim_pedido_estado}}

class CorregirDatosPedidoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CorregirDatosPedidoAction for PYME with data: {action_data}")
        campo_a_corregir = action_data.get("campo_a_corregir") # e.g., "direccion_entrega", "telefono_cliente"
        nuevo_valor = action_data.get("nuevo_valor")
        # id_pedido_contexto = action_data.get("id_pedido_contexto")

        if not campo_a_corregir or nuevo_valor is None:
            return {"success": False, "message_to_user": "No indicaste qué dato del pedido corregir o el nuevo valor."}

        # Logic to update pending order data in session/context.
        # If order already submitted, this might need a different flow (e.g. contact support).
        return {"success": True, "message_to_user": f"Dato '{campo_a_corregir}' para el pedido actualizado a '{nuevo_valor}'.",
                "data": {"correccion_pedido_aplicada": True}}

class ProcesarAdjuntoPedidoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoPedidoAction for PYME with data: {action_data}")

        archivo_id = self.context.get("archivo_id_para_asociar")
        if not archivo_id:
            return {"success": False, "message_to_user": "No se encontró un archivo para procesar."}

        from services.document_processing_service import document_processing_service
        processing_result = document_processing_service.process_document_by_id(archivo_id)

        if not processing_result.get("success"):
            return {"success": False, "message_to_user": f"Hubo un error al procesar el archivo: {processing_result.get('error')}"}

        extracted_data = processing_result.get("extracted_data", {})
        texto_extraido = extracted_data.get("texto_ocr") or extracted_data.get("texto_extraido")

        if not texto_extraido:
            return {"success": False, "message_to_user": "No se pudo extraer texto del archivo para procesar el pedido."}

        preview = build_order_attachment_preview(
            texto_extraido=texto_extraido,
            pyme_id_context=self.context.get("user_id"),
            telefono=self.context.get("telefono_usuario_contexto"),
            email=self.context.get("email_usuario_contexto"),
            nombre=self.context.get("nombre_usuario_contexto"),
            direccion=self.context.get("direccion_usuario_contexto"),
            channel=self.context.get("channel"),
        )

        return {
            "success": True,
            "message_to_user": preview["message"],
            "data": {
                "adjunto_pedido_procesado": True,
                "texto_extraido": texto_extraido,
                "items_detectados": preview["items_detectados"],
                "order_confirmation": preview["order_confirmation"],
                "confirmation_card": preview["confirmation_card"],
            },
            "options_list": preview["options_list"],
        }

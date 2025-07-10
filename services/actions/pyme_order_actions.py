# services/actions/pyme_order_actions.py
import logging
import json
from typing import Dict, Any, List, Optional
from .base_action_handler import BaseActionHandler
from services.pedido_service import servicio_pedidos # For creating PymePedido
from services.cart import ( # Direct functions for cart operations, now taking pyme_carts_data
    add_item_to_cart,
    remove_item_from_cart,
    update_item_quantity_in_cart,
    clear_pyme_cart,
    get_cart_summary
)
from services.qdrant_search import buscar_catalogo_qdrant, CATALOGO_PYME # For product lookup
from models import CatalogoItem, db, User # For product details and Pyme user

logger = logging.getLogger(__name__)

# Helper to get the actual pyme_carts_data dictionary from the broader context
def _get_pyme_carts_data_from_context(context: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Retrieves the pyme_carts_data dictionary.
    It's expected to be under context['chat_db_context_data']['carritos_pymes'].
    Initializes it if not present.
    """
    chat_db_context_data = context.get("chat_db_context_data")
    if not isinstance(chat_db_context_data, dict):
        # This should ideally not happen if chat_db_context.context_data is always a dict
        logger.error("chat_db_context_data is not a dict or not found in context. Initializing.")
        context["chat_db_context_data"] = {} # Ensure it exists
        chat_db_context_data = context["chat_db_context_data"]

    if 'carritos_pymes' not in chat_db_context_data or not isinstance(chat_db_context_data['carritos_pymes'], dict):
        chat_db_context_data['carritos_pymes'] = {}
        logger.info("Initialized 'carritos_pymes' in chat_db_context_data.")

    return chat_db_context_data['carritos_pymes']


class AgregarItemCarritoAction(BaseActionHandler):
    def _find_product_details(self, pyme_id: int, product_name_or_sku: str) -> Optional[Dict[str, Any]]:
        """Helper to find product in Qdrant and then DB for full details."""
        # Try Qdrant first
        # Assuming owner_user (pyme) is in self.context['user_obj']
        pyme_user = self.context.get("user_obj")
        rubro_nombre = getattr(pyme_user.rubro, "nombre", "general") if pyme_user and hasattr(pyme_user, "rubro") else "general"

        # Determine Qdrant collection name (this logic might need to be centralized)
        # from services.qdrant_search import coleccion_catalogo_para_rubro
        # qdrant_collection = coleccion_catalogo_para_rubro(rubro_nombre)
        qdrant_collection = CATALOGO_PYME # Simplified for now

        logger.info(f"Searching Qdrant collection '{qdrant_collection}' for product '{product_name_or_sku}' for pyme_id {pyme_id}")

        qdrant_results = buscar_catalogo_qdrant(
            user_id=pyme_id,
            texto_busqueda=product_name_or_sku,
            limite=1,
            coleccion=qdrant_collection
        )

        if qdrant_results and qdrant_results[0].payload:
            payload = qdrant_results[0].payload
            db_id = payload.get("db_id")
            if db_id:
                item_db = db.session.get(CatalogoItem, db_id)
                if item_db:
                    from services.common_utils import parse_precio_flexible # Local import
                    _, precio_float, moneda = parse_precio_flexible(item_db.precio)
                    return {
                        "catalogo_item_id": item_db.id,
                        "nombre_producto": item_db.nombre,
                        "precio_unitario": precio_float,
                        "moneda": moneda or "ARS",
                        "sku": item_db.sku,
                        "presentacion": item_db.unidad, # Assuming 'unidad' is presentation
                        "imagen_url": None # Or fetch if stored separately
                    }
            # Fallback if db_id not found or no item_db, use Qdrant payload directly (less rich)
            logger.warning(f"Product found in Qdrant for '{product_name_or_sku}', but no DB record or ID. Using Qdrant payload.")
            from services.common_utils import parse_precio_flexible
            _, precio_float, moneda = parse_precio_flexible(payload.get("precio_str","0"))

            return {
                "catalogo_item_id": payload.get("sku") or payload.get("nombre"), # Use SKU or name as fallback ID
                "nombre_producto": payload.get("nombre", product_name_or_sku),
                "precio_unitario": precio_float,
                "moneda": moneda or "ARS",
                "sku": payload.get("sku"),
                "presentacion": payload.get("unidad_descripcion") or payload.get("unidad_original"),
                "imagen_url": payload.get("imagen_url")
            }
        logger.warning(f"Product '{product_name_or_sku}' not found in Qdrant/DB for pyme_id {pyme_id}.")
        return None

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing AgregarItemCarritoAction with data: {action_data}")

        pyme_id = self.context.get("user_id") # ID of the Pyme
        if not pyme_id:
            return {"success": False, "message_to_user": "Error: No se pudo identificar la tienda."}

        product_identifier = action_data.get("producto_nombre") or action_data.get("producto_sku")
        cantidad = int(action_data.get("cantidad", 1))

        if not product_identifier or cantidad <= 0:
            return {"success": False, "message_to_user": "Por favor, especifica el producto y la cantidad."}

        producto_info = self._find_product_details(pyme_id, product_identifier)

        if not producto_info or producto_info.get("precio_unitario") is None:
            return {
                "success": False,
                "message_to_user": f"No encontré el producto '{product_identifier}' o no tiene precio definido."
            }

        pyme_carts_data = _get_pyme_carts_data_from_context(self.context)
        add_item_to_cart(pyme_carts_data, pyme_id, producto_info, cantidad)

        # Update the main context's pyme_carts_data reference (if it was re-assigned by _get_pyme_carts_data_from_context)
        # This is implicitly handled if _get_pyme_carts_data_from_context modifies the dict in place.

        # Get updated cart summary for the response
        cliente_user_id_for_summary = self.context.get("cliente_id") # For promo calculations
        cart_summary_obj = get_cart_summary(pyme_carts_data, pyme_id, cliente_user_id_for_summary)

        from services.pymes import formatear_carrito_desde_summary # Local import to avoid circularity
        cart_display_text = formatear_carrito_desde_summary(cart_summary_obj, self.context)


        return {
            "success": True,
            "message_to_user": f"'{producto_info['nombre_producto']}' (x{cantidad}) agregado al carrito.\n\n{cart_display_text}",
            "data": {
                "cart_summary": cart_summary_obj, # Send full summary for potential UI update
                "last_added_item_name": producto_info['nombre_producto']
            }
        }

class CrearPedidoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearPedidoAction with data: {action_data}")

        pyme_id = self.context.get("user_id") # ID of the Pyme
        if not pyme_id:
            return {"success": False, "message_to_user": "Error: No se pudo identificar la tienda para el pedido."}

        pyme_carts_data = _get_pyme_carts_data_from_context(self.context)
        cliente_user_id_for_summary = self.context.get("cliente_id")
        current_cart_summary = get_cart_summary(pyme_carts_data, pyme_id, cliente_user_id_for_summary)

        cart_items_for_pedido = current_cart_summary.get("items_detalle", [])
        if not cart_items_for_pedido:
            return {"success": False, "message_to_user": "Tu carrito está vacío. Agrega productos antes de crear el pedido."}

        # Extract customer details from action_data or context
        nombre_cliente = action_data.get("nombre_cliente") or self.context.get("nombre_usuario_contexto")
        telefono_cliente = action_data.get("telefono_cliente") or self.context.get("telefono_usuario_contexto")
        email_cliente = action_data.get("email_cliente") or self.context.get("email_usuario_contexto")
        direccion_entrega = action_data.get("direccion_entrega") or self.context.get("direccion_usuario_contexto")

        # Basic validation of customer data (more can be added)
        if not nombre_cliente or not (telefono_cliente or email_cliente):
            return {"success": False, "message_to_user": "Necesitamos tu nombre y un teléfono o email para el pedido."}

        owner_user_obj = self.context.get("user_obj") # User object of the Pyme
        rubro_pyme = getattr(owner_user_obj.rubro, "nombre", "General") if owner_user_obj and hasattr(owner_user_obj, "rubro") else "General"

        # Prepare data for servicio_pedidos.crear_pedido_desde_carrito (or a similar new function)
        # The existing `crear_pedido_desde_carrito` in `pedido_service` expects `cart_items` from `CatalogoItem`
        # and `cliente_data`. We need to adapt or create a new method that takes the cart summary items.
        # For now, let's assume we adapt `servicio_pedidos` or pass enough info.

        # Re-structure items from cart_summary to what `servicio_pedidos.crear_pedido_desde_carrito` might expect
        # This is a temporary adaptation. Ideally, `crear_pedido_desde_carrito` would directly use
        # the structure from `get_cart_summary` or be more flexible.
        items_for_service = []
        for item_sum in cart_items_for_pedido:
            items_for_service.append({
                "nombre": item_sum.get("nombre_producto"), # Key name might differ
                "cantidad": item_sum.get("cantidad"),
                # `servicio_pedidos` would need to re-fetch price from DB based on pyme_id and product name/sku
                # or we pass price info directly if available and trusted from cart_summary
                "precio_unitario_registrado": item_sum.get("precio_unitario_original"), # Pass for reference
                "sku": item_sum.get("sku")
            })

        pedido_payload_for_service = {
            "asunto": f"Pedido desde Chatbot para {nombre_cliente}",
            # detalles will be constructed by servicio_pedidos from items_for_service + prices
            "rubro": rubro_pyme,
            "nombre_cliente": nombre_cliente,
            "email_cliente": email_cliente,
            "telefono_cliente": telefono_cliente,
            "direccion": direccion_entrega,
            # latitud, longitud can be added if collected
            "user_id": self.context.get("cliente_id"), # ID of the customer placing the order
            "pyme_id_owner": pyme_id # Pass the Pyme's ID to the service
        }

        # This is a conceptual call. `servicio_pedidos` would need a method like this:
        # nuevo_pedido = servicio_pedidos.crear_pedido_con_items_y_datos_cliente(
        #     pyme_id_owner=pyme_id,
        #     items_data=items_for_service, # List of {"nombre", "cantidad", "sku", "precio_unitario_registrado"}
        #     cliente_data=pedido_payload_for_service
        # )
        # For now, we'll use the existing structure of PymePedido which takes a JSON string for 'detalles'

        detalles_json_str = json.dumps([{
            "nombre": item.get("nombre_producto"), "cantidad": item.get("cantidad"),
            "precio_unitario": item.get("precio_unitario_original"), "subtotal": item.get("subtotal_con_descuento"),
            "sku": item.get("sku"), "categoria": "N/A" # Categoria del catalogo item no está en cart_summary
        } for item in cart_items_for_pedido])

        pedido_payload_for_model = {
            "asunto": f"Pedido Chatbot: {nombre_cliente}",
            "detalles": detalles_json_str,
            "rubro": rubro_pyme,
            "nombre_cliente": nombre_cliente,
            "email_cliente": email_cliente,
            "telefono_cliente": telefono_cliente,
            "user_id": self.context.get("cliente_id"), # ID of ChatUser
            "direccion": direccion_entrega,
            "monto_total": current_cart_summary.get("total_final_con_descuento", 0.0)
        }

        nuevo_pedido = servicio_pedidos.crear_nuevo_pedido(pedido_payload_for_model)

        if nuevo_pedido:
            clear_pyme_cart(pyme_carts_data, pyme_id) # Clear cart after successful order
            # Update main context's pyme_carts_data reference

            return {
                "success": True,
                "message_to_user": f"¡Gracias {nombre_cliente}! Tu pedido #{nuevo_pedido.nro_pedido} ha sido registrado. Nos pondremos en contacto pronto.",
                "data": {"nro_pedido": nuevo_pedido.nro_pedido, "pedido_id": nuevo_pedido.id}
            }
        else:
            return {
                "success": False,
                "message_to_user": "Hubo un problema al registrar tu pedido. Por favor, intenta de nuevo."
            }

class ConsultarProductoAction(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarProductoAction with data: {action_data}")
        pyme_id = self.context.get("user_id")
        if not pyme_id:
             return {"success": False, "message_to_user": "Error: No se pudo identificar la tienda."}

        query = action_data.get("consulta_producto")
        if not query:
            return {"success": False, "message_to_user": "Por favor, dime qué producto estás buscando."}

        # Utilize Qdrant for searching
        # Assuming pyme_user and rubro_nombre can be obtained from context
        pyme_user = self.context.get("user_obj")
        rubro_nombre = getattr(pyme_user.rubro, "nombre", "general") if pyme_user and hasattr(pyme_user, "rubro") else "general"

        # from services.qdrant_search import coleccion_catalogo_para_rubro
        # qdrant_collection = coleccion_catalogo_para_rubro(rubro_nombre)
        qdrant_collection = CATALOGO_PYME # Simplified

        resultados = buscar_catalogo_qdrant(
            user_id=pyme_id,
            texto_busqueda=query,
            limite=3, # Show a few results
            coleccion=qdrant_collection
        )

        if not resultados:
            return {"success": True, "message_to_user": f"No encontré productos que coincidan con '{query}'. ¿Quieres intentar con otras palabras?"}

        respuesta_str = f"Encontré estos productos relacionados con '{query}':\n"
        from services.common_utils import parse_precio_flexible # Local import

        productos_info_list = []
        for hit in resultados:
            payload = hit.payload
            if not payload: continue

            nombre = payload.get("nombre", "Producto Desconocido")
            desc_corta = payload.get("descripcion_corta", "")
            precio_str_q = payload.get("precio_str", "N/A")
            _, precio_float_q, moneda_q = parse_precio_flexible(precio_str_q)

            productos_info_list.append({
                "nombre": nombre,
                "descripcion": desc_corta,
                "precio_formateado": f"${precio_float_q:,.2f} {moneda_q}" if precio_float_q is not None else "Precio no disponible",
                "sku": payload.get("sku") # For potential "add to cart" action
            })
            respuesta_str += f"\n- **{nombre}**: {desc_corta} (Precio: {productos_info_list[-1]['precio_formateado']})"

        respuesta_str += "\n\n¿Te interesa alguno de estos o quieres que busque otra cosa?"

        # Prepare buttons for LLM to suggest or for direct use by formatter
        # These buttons should ideally be structured for the LLM to choose from or add to.
        # For now, this action handler itself is formatting the message.
        options_for_llm = []
        for prod_info_btn in productos_info_list:
            options_for_llm.append(
                {"texto": f"Agregar {prod_info_btn['nombre'][:15]}...", "action_id": f"add_to_cart_{prod_info_btn.get('sku', prod_info_btn['nombre'])}"}
            )
        options_for_llm.append({"texto": "Buscar de nuevo", "action_id": "search_again"})
        options_for_llm.append({"texto": "Ver catálogo completo", "action_id": "view_full_catalog"})


        return {
            "success": True,
            "message_to_user": respuesta_str, # This message is already user-facing
            "data": {
                "productos_encontrados": productos_info_list,
                "sugerencias_accion_llm": options_for_llm # For LLM to potentially use in its response_usuario
            }
        }


# More PYME actions:
# - ModificarPedidoAction
# - CancelarPedidoAction
# - ConsultarEstadoPedidoAction
# - ... etc.

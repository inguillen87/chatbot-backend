import logging
from typing import Any, Dict

from services.pyme_menu import get_pyme_menu_payload
from services.cart import get_cart_summary, add_item_to_cart, clear_pyme_cart, format_cart_for_display
from services.qdrant_search import buscar_catalogo_qdrant
from services.herramientas_pyme import add_preference

logger = logging.getLogger(__name__)

class BasePymeHandler:
    """Base class for all PYME action handlers."""
    def __init__(self, chat_context: Dict[str, Any]):
        self.chat_context = chat_context
        self.pyme_id = chat_context.get("user_id")
        self.cliente_id = chat_context.get("cliente_id")
        self.pyme_context = chat_context.get("contexto_pyme_v2", {})
        self.pyme_carts_data = chat_context.get("chat_db_context_data", {}).get("pyme_carts", {})

    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Subclasses must implement this method.")

    def _save_context(self):
        """Persists the updated context back into the main chat context."""
        self.chat_context["contexto_pyme_v2"] = self.pyme_context
        if "pyme_carts" not in self.chat_context.get("chat_db_context_data", {}):
            self.chat_context.setdefault("chat_db_context_data", {})["pyme_carts"] = {}
        self.chat_context["chat_db_context_data"]["pyme_carts"] = self.pyme_carts_data

class MenuPrincipalHandler(BasePymeHandler):
    """Handles displaying the main menu for a PYME."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        menu_context = {
            "rubro_slug": self.pyme_context.get("rubro_slug", "generico"),
            "nombre_pyme": self.pyme_context.get("nombre_pyme_cache", f"PYME {self.pyme_id}"),
        }
        menu_payload = get_pyme_menu_payload(menu_context)
        return {**menu_payload, "fuente": "pyme_menu_principal_handler"}

class CatalogoHandler(BasePymeHandler):
    """Handles searching and displaying the product catalog."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        query = action_data.get("message_body_original_llm", "productos destacados")
        self.chat_context = add_preference("busquedas", query, self.chat_context)
        try:
            search_results = buscar_catalogo_qdrant(user_id=self.pyme_id, pregunta=query, coleccion="catalogo_pyme")
            if search_results:
                product_list_str = "\n".join([f"- {hit.payload['nombre']} (${hit.payload.get('precio', 'N/A')})" for hit in search_results])
                message_body = f"Encontré estos productos para ti:\n{product_list_str}"
            else:
                message_body = "No encontré productos que coincidan con tu búsqueda."
            return {
                "message_body": message_body,
                "options_list": [
                    {"texto": "Agregar al Carrito", "action_id": "pyme_agregar_carrito"},
                    {"texto": "Ver mi Carrito", "action_id": "pyme_ver_carrito"},
                ],
            }
        except Exception as e:
            logger.error(f"Error in CatalogoHandler: {e}")
            return {"message_body": "Lo siento, tuve un problema al buscar en el catálogo."}

class VerCarritoHandler(BasePymeHandler):
    """Displays the current shopping cart."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        cart_summary = get_cart_summary(self.pyme_carts_data, self.pyme_id, self.cliente_id)
        if not cart_summary.get("items_detalle"):
            return {"message_body": "Tu carrito de compras está vacío.", "options_list": [{"texto": "Ver Catálogo", "action_id": "pyme_productos_stock"}]}
        cart_display = format_cart_for_display(cart_summary)
        return {
            "message_body": cart_display,
            "options_list": [
                {"texto": "Finalizar Compra", "action_id": "pyme_finalizar_compra"},
                {"texto": "Vaciar Carrito", "action_id": "pyme_vaciar_carrito"},
            ],
        }

class AgregarCarritoHandler(BasePymeHandler):
    """Adds an item to the shopping cart."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        sku = action_data.get("sku", "GEN-PROD-001")
        quantity = action_data.get("quantity", 1)
        # In a real scenario, we'd fetch product info from the DB here
        producto_info = {"catalogo_item_id": sku, "nombre": "Producto Genérico", "precio_unitario": 100.0, "moneda": "ARS"}
        self.pyme_carts_data = add_item_to_cart(self.pyme_carts_data, self.pyme_id, self.cliente_id, producto_info, quantity)
        self._save_context()
        cart_summary = get_cart_summary(self.pyme_carts_data, self.pyme_id, self.cliente_id)
        cart_display = format_cart_for_display(cart_summary)
        return {"message_body": f"Agregué el producto a tu carrito.\n\n{cart_display}"}

class VaciarCarritoHandler(BasePymeHandler):
    """Clears the shopping cart."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        self.pyme_carts_data = clear_pyme_cart(self.pyme_carts_data, self.pyme_id, self.cliente_id)
        self._save_context()
        return {"message_body": "Tu carrito ha sido vaciado."}

class FinalizarCompraHandler(BasePymeHandler):
    """Handles the checkout process."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        self.pyme_carts_data = clear_pyme_cart(self.pyme_carts_data, self.pyme_id, self.cliente_id)
        self._save_context()
        return {"message_body": "¡Gracias por tu compra! Tu pedido ha sido procesado."}

class PromocionesHandler(BasePymeHandler):
    """Displays current promotions."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        return {"message_body": "Actualmente no tenemos promociones especiales."}

class InfoEnvioHandler(BasePymeHandler):
    """Provides shipping information."""
    async def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        if action_data.get("location"):
            return {"message_body": "Costo de envío a tu ubicación: $500."}
        return {"message_body": "Realizamos envíos a todo el país."}
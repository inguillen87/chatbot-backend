import logging
# from flask import session # REMOVED
from typing import List, Dict, Any, Optional
# Suponiendo que promocion_service está disponible para ser importado
# from .promocion_service import promocion_service
# Por ahora, lo comentamos hasta que la integración sea el siguiente paso.

logger = logging.getLogger(__name__)

SESSION_CARTS_KEY = 'carritos_pymes' # Clave principal para el dict que solía estar en la sesión Flask

def _get_pyme_cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]], pyme_id: int) -> List[Dict[str, Any]]:
    """
    Devuelve el carrito específico de una PYME desde la estructura de datos proporcionada,
    creándolo si es necesario dentro de esa estructura.
    El carrito es una lista de diccionarios (ítems).
    pyme_carts_data es el diccionario que antes estaba en session[SESSION_CARTS_KEY].
    """
    if not isinstance(pyme_carts_data, dict):
        logger.warning(f"pyme_carts_data no es un diccionario. Recibido: {type(pyme_carts_data)}. Se reiniciará a {{}}.")
        pyme_carts_data = {}

    # Force pyme_id to string to avoid int/string key mismatches in JSON serialization
    key = str(pyme_id)
    if key not in pyme_carts_data:
        pyme_carts_data[key] = []
    return pyme_carts_data[key]

def add_item_to_cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]], pyme_id: int, producto_info: Dict[str, Any], cantidad: int = 1) -> None:
    """
    Añade un producto al carrito de la PYME o incrementa su cantidad si ya existe,
    operando sobre pyme_carts_data.
    producto_info debe contener al menos 'catalogo_item_id' y otros datos necesarios.
    """
    if not producto_info or not producto_info.get('catalogo_item_id') or cantidad <= 0:
        logger.warning(f"add_item_to_cart: Datos de producto/cantidad inválidos. producto_info={producto_info}, cantidad={cantidad}")
        return

    cart = _get_pyme_cart(pyme_carts_data, pyme_id) # Modifica pyme_carts_data si el cart de pyme_id no existe
    item_id_a_buscar = producto_info['catalogo_item_id']

    for item in cart:
        if item.get("catalogo_item_id") == item_id_a_buscar:
            item["cantidad"] = item.get("cantidad", 0) + cantidad
            logger.info(f"Cantidad actualizada para item {item_id_a_buscar} en carrito de PYME {pyme_id}. Nueva cantidad: {item['cantidad']}")
            # No session.modified = True
            return

    # Si el producto no está en el carrito, añadirlo
    nuevo_item_carrito = {
        "catalogo_item_id": producto_info['catalogo_item_id'],
        "nombre_producto": producto_info.get('nombre', 'Producto Desconocido'), # Tomar de producto_info
        "cantidad": cantidad,
        "precio_unitario_original": producto_info.get('precio_unitario'), # Asumiendo que esto viene de _formatear_producto
        "moneda": producto_info.get('moneda', 'ARS'), # Asumiendo que esto viene de _formatear_producto
        "sku": producto_info.get('sku'),
        "presentacion": producto_info.get('presentacion'),
        "imagen_url": producto_info.get('imagen_url')
        # No guardamos promocion_info aquí, se aplicará dinámicamente en get_summary_with_promotions
    }
    cart.append(nuevo_item_carrito)
    logger.info(f"Item {item_id_a_buscar} añadido al carrito de PYME {pyme_id} con cantidad {cantidad}.")
    # No session.modified = True

def remove_item_from_cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]], pyme_id: int, catalogo_item_id: Any) -> None:
    """Elimina un producto del carrito de la PYME, operando sobre pyme_carts_data."""
    cart = _get_pyme_cart(pyme_carts_data, pyme_id)
    item_encontrado = False
    for item in list(cart): # Iterar sobre una copia para poder modificar
        if item.get("catalogo_item_id") == catalogo_item_id:
            cart.remove(item)
            item_encontrado = True
            break
    if item_encontrado:
        logger.info(f"Item {catalogo_item_id} eliminado del carrito de PYME {pyme_id}.")
        # No session.modified = True
    else:
        logger.warning(f"Intento de eliminar item {catalogo_item_id} del carrito de PYME {pyme_id}, pero no se encontró.")


def update_item_quantity_in_cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]], pyme_id: int, catalogo_item_id: Any, nueva_cantidad: int) -> None:
    """Actualiza la cantidad para un producto en el carrito de la PYME, operando sobre pyme_carts_data."""
    if nueva_cantidad <= 0: # Si la nueva cantidad es 0 o menos, eliminar el ítem
        remove_item_from_cart(pyme_carts_data, pyme_id, catalogo_item_id)
        return

    cart = _get_pyme_cart(pyme_carts_data, pyme_id)
    item_actualizado = False
    for item in cart:
        if item.get("catalogo_item_id") == catalogo_item_id:
            item["cantidad"] = nueva_cantidad
            item_actualizado = True
            break

    if item_actualizado:
        logger.info(f"Cantidad actualizada para item {catalogo_item_id} en carrito de PYME {pyme_id} a {nueva_cantidad}.")
        # No session.modified = True
    else:
        logger.warning(f"Intento de actualizar cantidad para item {catalogo_item_id} en carrito de PYME {pyme_id}, pero no se encontró.")


def clear_pyme_cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]], pyme_id: int) -> None:
    """Vacía el carrito para una PYME específica, operando sobre pyme_carts_data."""
    if pyme_id in pyme_carts_data:
        pyme_carts_data[pyme_id] = []
        logger.info(f"Carrito para PYME {pyme_id} vaciado.")
        # No session.modified = True

def get_cart_summary(pyme_carts_data: Dict[int, List[Dict[str, Any]]], pyme_id: int, cliente_user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Devuelve un resumen del carrito para una PYME, incluyendo subtotales y
    potencialmente aplicando promociones en el futuro. Opera sobre pyme_carts_data.
    """
    cart_items_raw = _get_pyme_cart(pyme_carts_data, pyme_id)

    # Copia profunda para no modificar los ítems directamente aquí si las promociones lo hicieran.
    # (aunque la aplicación de promos podría devolver una nueva lista de ítems)
    items_para_calculo = [dict(item) for item in cart_items_raw]

    # Copia profunda para no modificar los ítems de la sesión directamente aquí
    items_para_calculo_promos = []
    for item_raw in cart_items_raw:
        items_para_calculo_promos.append({
            "catalogo_item_id": item_raw.get("catalogo_item_id"),
            "cantidad": item_raw.get("cantidad", 0),
            "precio_unitario_original": item_raw.get("precio_unitario_original", 0.0),
            # Añadir cualquier otro campo que aplicar_promociones_al_carrito pueda necesitar del item original
            "nombre_producto": item_raw.get("nombre_producto"), # Para logging o referencia
            "sku": item_raw.get("sku"),
            "presentacion": item_raw.get("presentacion"),
            "moneda": item_raw.get("moneda", "ARS"),
            "imagen_url": item_raw.get("imagen_url")
        })

    # --- Integración con PromocionService ---
    from .promocion_service import promocion_service # Importar aquí para evitar importación circular a nivel de módulo

    # El cliente_user_id se podría obtener si el usuario está autenticado y se pasa a esta función.
    # Si es anónimo, no se pueden aplicar promos con límite por cliente (a menos que el límite sea por sesión).
    # Por ahora, lo pasamos como None si no está disponible.

    summary_con_promos = promocion_service.aplicar_promociones_al_carrito(
        pyme_user_id=pyme_id,
        items_carrito=items_para_calculo_promos,
        cliente_user_id=cliente_user_id
    )

    logger.info(f"Resumen de carrito para PYME {pyme_id} con promociones aplicadas: {summary_con_promos}")

    # La estructura devuelta por aplicar_promociones_al_carrito ya debería ser bastante completa.
    # Solo añadimos el pyme_id para referencia.
    summary_con_promos["pyme_id"] = pyme_id
    return summary_con_promos

# --- Compatibility wrapper functions for legacy single-cart operations ---

def _cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Return cart list for legacy API using pyme_id 0, from pyme_carts_data."""
    return _get_pyme_cart(pyme_carts_data, 0) # pyme_id 0 for legacy


def add_item(pyme_carts_data: Dict[int, List[Dict[str, Any]]], nombre: str, cantidad: int = 1) -> None:
    """Add or increase quantity of a product in the legacy cart (pyme_id 0)."""
    if not nombre:
        return
    cart = _cart(pyme_carts_data)
    for item in cart:
        if item.get("nombre", "").lower() == nombre.lower():
            item["cantidad"] = item.get("cantidad", 0) + cantidad
            # No session.modified = True
            return
    cart.append({"nombre": nombre, "cantidad": cantidad})
    # No session.modified = True


def remove_item(pyme_carts_data: Dict[int, List[Dict[str, Any]]], nombre: str) -> None:
    """Remove a product from the legacy cart (pyme_id 0)."""
    cart = _cart(pyme_carts_data)
    for item in list(cart):
        if item.get("nombre", "").lower() == nombre.lower():
            cart.remove(item)
            # No session.modified = True
            break


def update_item(pyme_carts_data: Dict[int, List[Dict[str, Any]]], nombre: str, cantidad: int) -> None:
    """Update the quantity for a product in the legacy cart (pyme_id 0)."""
    cart = _cart(pyme_carts_data)
    for item in cart:
        if item.get("nombre", "").lower() == nombre.lower():
            item["cantidad"] = cantidad
            # No session.modified = True
            break


def clear_cart(pyme_carts_data: Dict[int, List[Dict[str, Any]]]) -> None:
    """Clear all items from the legacy cart (pyme_id 0)."""
    # Ensures the key 0 exists in pyme_carts_data then clears its list
    if 0 in pyme_carts_data:
        pyme_carts_data[0] = []
    else: # If pyme_id 0 cart never existed in this structure, effectively it's cleared.
        pyme_carts_data[0] = [] # Initialize it as empty.
    # No session.modified = True


def get_summary(pyme_carts_data: Dict[int, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Return a simplified summary of the legacy cart (pyme_id 0)."""
    current_cart = _cart(pyme_carts_data)
    return [
        {"nombre": it.get("nombre"), "cantidad": it.get("cantidad", 0)}
        for it in current_cart
    ]

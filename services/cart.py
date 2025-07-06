import logging
from flask import session
from typing import List, Dict, Any, Optional
# Suponiendo que promocion_service está disponible para ser importado
# from .promocion_service import promocion_service
# Por ahora, lo comentamos hasta que la integración sea el siguiente paso.

logger = logging.getLogger(__name__)

SESSION_CARTS_KEY = 'carritos_pymes' # Clave principal en la sesión Flask

def _get_pyme_cart(pyme_id: int) -> List[Dict[str, Any]]:
    """
    Devuelve el carrito específico de una PYME desde la sesión, creándolo si es necesario.
    El carrito es una lista de diccionarios (ítems).
    """
    if SESSION_CARTS_KEY not in session:
        session[SESSION_CARTS_KEY] = {}

    pyme_carts = session[SESSION_CARTS_KEY]

    if pyme_id not in pyme_carts:
        pyme_carts[pyme_id] = []
        session.modified = True # Marcar sesión como modificada

    return pyme_carts[pyme_id]

def add_item_to_cart(pyme_id: int, producto_info: Dict[str, Any], cantidad: int = 1) -> None:
    """
    Añade un producto al carrito de la PYME o incrementa su cantidad si ya existe.
    producto_info debe contener al menos 'catalogo_item_id' y otros datos necesarios.
    """
    if not pyme_id or not producto_info or not producto_info.get('catalogo_item_id') or cantidad <= 0:
        logger.warning(f"add_item_to_cart: Datos inválidos. pyme_id={pyme_id}, producto_info={producto_info}, cantidad={cantidad}")
        return

    cart = _get_pyme_cart(pyme_id)
    item_id_a_buscar = producto_info['catalogo_item_id']

    for item in cart:
        if item.get("catalogo_item_id") == item_id_a_buscar:
            item["cantidad"] = item.get("cantidad", 0) + cantidad
            logger.info(f"Cantidad actualizada para item {item_id_a_buscar} en carrito de PYME {pyme_id}. Nueva cantidad: {item['cantidad']}")
            session.modified = True
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
    session.modified = True

def remove_item_from_cart(pyme_id: int, catalogo_item_id: Any) -> None:
    """Elimina un producto del carrito de la PYME."""
    cart = _get_pyme_cart(pyme_id)
    item_encontrado = False
    for item in list(cart): # Iterar sobre una copia para poder modificar
        if item.get("catalogo_item_id") == catalogo_item_id:
            cart.remove(item)
            item_encontrado = True
            break
    if item_encontrado:
        logger.info(f"Item {catalogo_item_id} eliminado del carrito de PYME {pyme_id}.")
        session.modified = True
    else:
        logger.warning(f"Intento de eliminar item {catalogo_item_id} del carrito de PYME {pyme_id}, pero no se encontró.")


def update_item_quantity_in_cart(pyme_id: int, catalogo_item_id: Any, nueva_cantidad: int) -> None:
    """Actualiza la cantidad para un producto en el carrito de la PYME."""
    if nueva_cantidad <= 0: # Si la nueva cantidad es 0 o menos, eliminar el ítem
        remove_item_from_cart(pyme_id, catalogo_item_id)
        return

    cart = _get_pyme_cart(pyme_id)
    item_actualizado = False
    for item in cart:
        if item.get("catalogo_item_id") == catalogo_item_id:
            item["cantidad"] = nueva_cantidad
            item_actualizado = True
            break

    if item_actualizado:
        logger.info(f"Cantidad actualizada para item {catalogo_item_id} en carrito de PYME {pyme_id} a {nueva_cantidad}.")
        session.modified = True
    else:
        logger.warning(f"Intento de actualizar cantidad para item {catalogo_item_id} en carrito de PYME {pyme_id}, pero no se encontró.")


def clear_pyme_cart(pyme_id: int) -> None:
    """Vacía el carrito para una PYME específica."""
    if SESSION_CARTS_KEY in session and pyme_id in session[SESSION_CARTS_KEY]:
        session[SESSION_CARTS_KEY][pyme_id] = []
        logger.info(f"Carrito para PYME {pyme_id} vaciado.")
        session.modified = True

def get_cart_summary(pyme_id: int, cliente_user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Devuelve un resumen del carrito para una PYME, incluyendo subtotales y
    potencialmente aplicando promociones en el futuro.
    """
    cart_items_raw = _get_pyme_cart(pyme_id)

    # Copia profunda para no modificar los ítems de la sesión directamente aquí
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

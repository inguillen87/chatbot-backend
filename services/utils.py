import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

def clean_text(text: str) -> str:
    """Removes leading/trailing whitespace and converts to lowercase."""
    if not isinstance(text, str):
        return ""
    return text.strip().lower()

def get_session_id(normalized_phone: str, pyme_id: int) -> str:
    """Generates a unique session ID for a user and PYME."""
    return f"whatsapp_{pyme_id}_{normalized_phone}"

def format_cart_for_display(cart_summary: Dict[str, Any]) -> str:
    """
    Formats the cart summary into a human-readable string.
    This is a placeholder implementation.
    """
    if not cart_summary or not cart_summary.get("items_detalle"):
        return "Tu carrito está vacío."

    items = cart_summary.get("items_detalle", [])
    total = cart_summary.get("total_final_con_descuento", 0)

    lines = ["**Resumen de tu Carrito:**"]
    for item in items:
        lines.append(f"- {item.get('cantidad', 0)}x {item.get('nombre_producto', 'N/A')} - ${item.get('subtotal_con_descuento', 0):.2f}")

    lines.append(f"\n**Total: ${total:.2f}**")
    return "\n".join(lines)
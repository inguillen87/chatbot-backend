from flask import session


def _cart() -> list[dict]:
    """Return cart list from session, creating it if needed."""
    return session.setdefault("cart", [])


def add_item(nombre: str, cantidad: int = 1) -> None:
    """Add or increase quantity of a product in the cart."""
    if not nombre:
        return
    cart = _cart()
    for item in cart:
        if item.get("nombre", "").lower() == nombre.lower():
            item["cantidad"] = item.get("cantidad", 0) + cantidad
            session.modified = True
            return
    cart.append({"nombre": nombre, "cantidad": cantidad})
    session.modified = True


def remove_item(nombre: str) -> None:
    """Remove a product from the cart."""
    cart = _cart()
    for item in list(cart):
        if item.get("nombre", "").lower() == nombre.lower():
            cart.remove(item)
            session.modified = True
            break


def update_item(nombre: str, cantidad: int) -> None:
    """Update the quantity for a product."""
    cart = _cart()
    for item in cart:
        if item.get("nombre", "").lower() == nombre.lower():
            item["cantidad"] = cantidad
            session.modified = True
            break


def clear_cart() -> None:
    session["cart"] = []
    session.modified = True


def get_summary() -> list[dict]:
    """Return a copy of the cart contents."""
    return [
        {"nombre": it.get("nombre"), "cantidad": it.get("cantidad", 0)}
        for it in _cart()
    ]

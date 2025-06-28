"""Alias para exponer el catálogo como ``/productos``."""

from flask import Blueprint
from routes.auth import token_requerido
from routes.catalogo import listar_catalogo

productos_bp = Blueprint("productos", __name__, url_prefix="/productos")


@productos_bp.route("", methods=["GET"], strict_slashes=False)
@token_requerido
def obtener_productos(user):
    """Devuelve el catálogo de productos."""
    # ``listar_catalogo`` ya está protegido por ``token_requerido``.
    # Para evitar doble verificación, llamamos a la función subyacente
    # utilizando el atributo ``__wrapped__`` que conserva ``functools.wraps``.
    return listar_catalogo.__wrapped__(user)

from flask import Blueprint
from routes.auth import token_requerido
from routes.catalogo import listar_catalogo

productos_bp = Blueprint('productos_bp', __name__, url_prefix='/productos')

@productos_bp.route('/', methods=['GET'], strict_slashes=False)
@token_requerido
def obtener_productos(user):
    # Reutiliza la lógica de listar_catalogo para exponer un alias en /productos
    return listar_catalogo(user)

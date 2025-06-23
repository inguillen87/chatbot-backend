
from routes.crm import _obtener_historial_cliente

historial_bp = Blueprint('historial', __name__)


    from routes.chat import cors_options_response
    return cors_options_response()

@historial_bp.route('/historial', methods=['GET'])
@token_requerido
def historial_actual(current_user: User):
    """Devuelve el historial de interacciones del usuario autenticado."""
    datos = _obtener_historial_cliente(current_user.id)
    return jsonify(datos)

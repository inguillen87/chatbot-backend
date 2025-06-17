# routes/pedidos.py

from flask import Blueprint, request, jsonify
from services.pedido_service import servicio_pedidos
from models import User, PymePedido  # ¡Importante! Asegúrate de importar PymePedido
from routes.auth import token_requerido
import logging
import json

logger = logging.getLogger(__name__)

pedidos_bp = Blueprint('pedidos', __name__)

# --- Ruta para iniciar un nuevo pedido (Ejemplo) ---
@pedidos_bp.route('/pedidos/iniciar', methods=['POST'])
@token_requerido
def iniciar_pedido(user):
    data = request.get_json()
    if not data or not data.get('pregunta_usuario'):
        return jsonify({"error": "Pregunta del usuario es requerida para iniciar el pedido."}), 400

    pregunta_usuario = data.get('pregunta_usuario')
    rubro_nombre = data.get('rubro_nombre', 'general_pyme')

    try:
        # Lógica para crear un pedido. Esto es un ejemplo y puede ser más complejo.
        productos_solicitados = [{"item": "Producto de Ejemplo", "cantidad": 1, "unidad": "unidad"}]
        
        pedido_data = {
            "asunto": f"Pedido rápido: {pregunta_usuario[:50]}...",
            "detalles": json.dumps(productos_solicitados, ensure_ascii=False),
            "rubro": rubro_nombre,
            "nombre_cliente": data.get("nombre_cliente"),
            "email_cliente": data.get("email_cliente"),
            "telefono_cliente": data.get("telefono_cliente"),
            "direccion": data.get("direccion"),
            "latitud": data.get("latitud"),
            "longitud": data.get("longitud"),
            "user_id": user.id,
            "monto_total": 0.0
        }
        
        nuevo_pedido = servicio_pedidos.crear_nuevo_pedido(pedido_data)

        if nuevo_pedido:
            return jsonify({
                "mensaje": "Pedido iniciado correctamente.",
                "nro_pedido": nuevo_pedido.nro_pedido,
                "estado": nuevo_pedido.estado
            }), 201
        else:
            return jsonify({"error": "No se pudo iniciar el pedido."}), 500

    except Exception as e:
        logger.error(f"Error en la ruta /pedidos/iniciar: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar el pedido."}), 500


# --- Ruta para consultar el estado de un pedido específico ---
@pedidos_bp.route('/pedidos/<string:nro_pedido>', methods=['GET'])
@token_requerido
def consultar_estado_pedido(user, nro_pedido):
    # Usamos el servicio para obtener el pedido
    pedido = servicio_pedidos.obtener_pedido_por_nro(nro_pedido)

    if not pedido:
        return jsonify({"error": "Pedido no encontrado."}), 404
    
    # Asegurarse de que el usuario logueado es el dueño del pedido
    if pedido.user_id and pedido.user_id != user.id:
        return jsonify({"error": "No autorizado para ver este pedido."}), 403

    # Usamos el método to_dict() que agregamos al modelo para devolver una respuesta limpia
    return jsonify(pedido.to_dict()), 200

# --- RUTA AÑADIDA: Listar todos los pedidos del usuario ---
@pedidos_bp.route('/pedidos', methods=['GET'])
@token_requerido
def listar_pedidos_usuario(user):
    """
    Obtiene todos los pedidos realizados por el usuario autenticado.
    Esta es la ruta que tu frontend 'PedidosPage.tsx' necesita.
    """
    try:
        # Consulta la base de datos por todos los pedidos que coincidan con el user.id
        # y los ordena por fecha, del más nuevo al más viejo.
        pedidos_del_usuario = PymePedido.query.filter_by(user_id=user.id).order_by(PymePedido.fecha.desc()).all()
        
        # Convierte la lista de objetos de pedido a una lista de diccionarios
        # usando el método to_dict() que definimos en el modelo.
        pedidos_en_json = [p.to_dict() for p in pedidos_del_usuario]
        
        # Devuelve la lista en formato JSON, tal como lo espera el frontend
        return jsonify(pedidos_en_json), 200

    except Exception as e:
        logger.error(f"Error en la ruta /pedidos: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al obtener los pedidos."}), 500
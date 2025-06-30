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

# --- Ruta para Checkout del Carrito ---
@pedidos_bp.route('/pedidos/checkout', methods=['POST'])
@token_requerido
def checkout_carrito(user: User):
    """
    Procesa el carrito de compras actual y crea un PymePedido.
    """
    from services.cart import get_summary, clear_cart # Importaciones locales

    cart_items = get_summary()
    if not cart_items:
        return jsonify({"error": "El carrito está vacío."}), 400

    # Datos del cliente pueden venir en el request body o tomarse del perfil del usuario
    data = request.get_json() or {}

    # Priorizar datos del request, luego del perfil del usuario
    # El user.id aquí es el del cliente que está comprando.
    # El user_id para buscar en CatalogoItem dentro de crear_pedido_desde_carrito
    # debe ser el de la Pyme dueña del catálogo. Esto necesita ser manejado con cuidado.
    # Si el 'user' es el dueño de la pyme (admin/empleado) haciendo un pedido para un cliente,
    # el user.id es correcto para el catálogo. Si 'user' es el cliente final,
    # necesitamos el user_id de la pyme a la que está comprando.
    # Para este ejemplo, asumiremos que el 'user' autenticado (dueño del token)
    # es el que posee el catálogo y a quien se le hace el pedido (user.id es el pyme_id).
    # Si el sistema permite a clientes finales tener sus propios carritos para múltiples pymes,
    # el 'pyme_id' o 'empresa_id' a la que se compra debería ser parte del request o contexto.

    # Para el dueño del catálogo/pyme:
    # Asumimos que el 'user' autenticado (dueño del token) es la Pyme o un empleado de la Pyme.
    # El catálogo se busca usando user.id
    pyme_id_for_catalog = user.id

    # Para los datos del cliente que realiza el pedido:
    # Si el cliente es el mismo usuario autenticado (Pyme haciendo un pedido para sí misma o un empleado para la pyme),
    # entonces cliente_user_id es user.id.
    # Si es un pedido para un cliente final (potencialmente diferente o anónimo),
    # el `cliente_user_id` podría venir del request `data` si el cliente está logueado con otro sistema,
    # o ser None para clientes anónimos.
    # Por simplicidad y consistencia con el user_id de PymePedido,
    # si `data` no especifica un `cliente_user_id`, se asume que el pedido es para el `user` autenticado.

    cliente_user_id_from_request = data.get("cliente_user_id")
    # Si el que hace checkout es el mismo dueño de la pyme, su user.id es el cliente_user_id
    # Podría ser None si es un guest checkout y no se pasa cliente_user_id
    final_cliente_user_id = cliente_user_id_from_request if cliente_user_id_from_request is not None else user.id

    cliente_data = {
        "nombre_cliente": data.get("nombre_cliente") or user.name, # Nombre del cliente
        "email_cliente": data.get("email_cliente") or user.email,   # Email del cliente
        "telefono_cliente": data.get("telefono_cliente") or user.telefono, # Teléfono del cliente
        "direccion": data.get("direccion") or user.direccion, # Dirección de envío/cliente
        "latitud": data.get("latitud") or user.latitud,
        "longitud": data.get("longitud") or user.longitud,
        "asunto": data.get("asunto", f"Pedido desde carrito para {user.name or 'cliente'}"),
        "rubro": user.rubro.clave if user.rubro else "general_pyme", # Rubro de la Pyme (dueña del catálogo)
        "cliente_user_id": final_cliente_user_id # ID del cliente que crea el pedido
    }

    try:
        # El primer argumento es el ID de la Pyme dueña del catálogo.
        # El cliente_user_id dentro de cliente_data es para el campo user_id del PymePedido.
        nuevo_pedido = servicio_pedidos.crear_pedido_desde_carrito(
            pyme_id=pyme_id_for_catalog,
            cart_items=cart_items,
            cliente_data=cliente_data
        )

        if nuevo_pedido:
            clear_cart() # Vaciar el carrito después de un checkout exitoso
            return jsonify({
                "mensaje": "Pedido realizado con éxito desde el carrito.",
                "nro_pedido": nuevo_pedido.nro_pedido,
                "estado": nuevo_pedido.estado,
                "monto_total": nuevo_pedido.monto_total
            }), 201
        else:
            # Los logs dentro de crear_pedido_desde_carrito deberían indicar la causa del error
            return jsonify({"error": "No se pudo procesar el pedido desde el carrito."}), 500

    except Exception as e:
        logger.error(f"Error en la ruta /pedidos/checkout: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al procesar el checkout."}), 500
# routes/pedidos.py

from flask import Blueprint, request, jsonify, current_app
from services.pedido_service import servicio_pedidos # Importa tu servicio de pedidos
from services.pymes import PROMPT_CLASIFICACION_INTENCION_PYME, _extraer_cantidades_con_llm # Necesario para la lógica de pedidos
from services.logic import _clasificar_intencion_con_llm # Para clasificar intenciones si lo necesitas directamente aquí
from models import User # Si necesitas interactuar con usuarios, por ejemplo para obtener el user_id
from routes.auth import token_requerido # Si solo usuarios autenticados pueden crear pedidos
import logging
import json

logger = logging.getLogger(__name__)

pedidos_bp = Blueprint('pedidos', __name__)

# --- Ruta para iniciar un nuevo pedido (Ejemplo) ---
@pedidos_bp.route('/pedidos/iniciar', methods=['POST'])
@token_requerido # Si solo usuarios logueados pueden iniciar pedidos
def iniciar_pedido(user):
    data = request.get_json()
    if not data or not data.get('pregunta_usuario'):
        return jsonify({"error": "Pregunta del usuario es requerida para iniciar el pedido."}), 400

    pregunta_usuario = data.get('pregunta_usuario')
    rubro_nombre = data.get('rubro_nombre', 'general_pyme') # Puedes pasar el rubro desde el frontend si es relevante

    # Aquí podríamos clasificar la intención si no viene pre-clasificada
    # intencion = _clasificar_intencion_con_llm(pregunta_usuario)
    # if intencion != 'iniciar_pedido':
    #     return jsonify({"error": "La intención no es iniciar un pedido."}), 400

    # Lógica para extraer productos (ej. desde un contexto previo o directo de la pregunta)
    # Necesitas `productos_referencia` aquí si se espera que el usuario ya haya visto un catálogo.
    # Por ahora, simularemos que no hay productos de referencia para una primera aproximación.
    
    # Aquí es donde necesitarías la lógica de tu `PedidoHandler` que estaba en `pymes.py`
    # Esto se haría más complejo si necesitas estados de conversación como "esperando_detalles_pedido"
    
    # Para simplificar y darte un ejemplo básico, podemos intentar extraer algo directo
    # Normalmente, esta lógica es más compleja y se maneja en el servicio/handler de pymes
    
    # Si quisieras crear un pedido de ejemplo sin mucha lógica:
    try:
        # Aquí es donde integrarías la lógica de parsing de productos si la necesitas para esta ruta
        # Por ahora, un placeholder
        productos_solicitados = [{"item": "Producto de Ejemplo", "cantidad": 1, "unidad": "unidad"}]
        
        # Simular datos para el pedido
        pedido_data = {
            "asunto": f"Pedido rápido: {pregunta_usuario[:50]}...",
            "detalles": json.dumps(productos_solicitados, ensure_ascii=False),
            "rubro": rubro_nombre,
            "nombre_cliente": user.name,
            "email_cliente": user.email,
            "telefono_cliente": user.telefono if hasattr(user, 'telefono') else "N/A",
            "user_id": user.id,
            "monto_total": 0.0 # O calcula si tienes precios
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


# --- Ruta para consultar el estado de un pedido (Ejemplo) ---
@pedidos_bp.route('/pedidos/<string:nro_pedido>', methods=['GET'])
@token_requerido # Si solo usuarios logueados pueden consultar sus pedidos
def consultar_estado_pedido(user, nro_pedido):
    pedido = servicio_pedidos.obtener_pedido_por_nro(nro_pedido)

    if not pedido:
        return jsonify({"error": "Pedido no encontrado."}), 404
    
    # Opcional: Asegurarse de que el usuario logueado es el dueño del pedido (si no es anónimo)
    if pedido.user_id and pedido.user_id != user.id:
        return jsonify({"error": "No autorizado para ver este pedido."}), 403

    return jsonify({
        "nro_pedido": pedido.nro_pedido,
        "asunto": pedido.asunto,
        "estado": pedido.estado,
        "detalles": json.loads(pedido.detalles),
        "fecha_creacion": pedido.fecha.isoformat(),
        "nombre_cliente": pedido.nombre_cliente,
        "email_cliente": pedido.email_cliente,
        "telefono_cliente": pedido.telefono_cliente,
        "monto_total": str(pedido.monto_total) # Convertir a string para evitar problemas de serialización
    }), 200

# ... Puedes añadir más rutas para actualizar, cancelar, listar pedidos, etc.
from flask import Blueprint, request, jsonify, current_app
from models import User, PymePedido, db
from routes.auth import token_requerido, admin_o_empleado_requerido
from services.logic import es_rubro_publico
from datetime import datetime, timedelta
from sqlalchemy import func

pedidos_bp = Blueprint('pedidos_bp', __name__, url_prefix='/pedidos')

def _serialize_pedido(pedido: PymePedido):
    # PymePedido model has a to_dict() method from the model definition.
    return pedido.to_dict()

@pedidos_bp.route('', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def listar_pedidos_pyme(current_user: User):
    is_pyme_user = False
    if hasattr(current_user, 'tipo_chat') and current_user.tipo_chat == "pyme":
        is_pyme_user = True
    elif current_user.rubro and not es_rubro_publico(current_user.rubro):
        is_pyme_user = True

    if not is_pyme_user:
        return jsonify({"error": "Acceso denegado. Esta sección es solo para PYMEs."}), 403

    if not current_user.rubro or not current_user.rubro.nombre:
         current_app.logger.warning(f"Admin PYME {current_user.id} intentando listar pedidos sin rubro asignado.")
         return jsonify({"error": "Usuario PYME no tiene un rubro configurado para filtrar pedidos."}), 400
        
    # SECURITY NOTE: Filtering by PymePedido.rubro (string) == current_user.rubro.nombre (string)
    # is a temporary workaround. PymePedido should ideally have a direct empresa_id foreign key.
    query = PymePedido.query.filter(PymePedido.rubro == current_user.rubro.nombre)

    estado_filter = request.args.get('estado')
    fecha_inicio_str = request.args.get('fecha_inicio')
    fecha_fin_str = request.args.get('fecha_fin')
    cliente_email_filter = request.args.get('cliente_email')
    nro_pedido_filter = request.args.get('nro_pedido')

    if estado_filter:
        query = query.filter(PymePedido.estado == estado_filter)
    if cliente_email_filter:
        query = query.filter(PymePedido.email_cliente.ilike(f"%{cliente_email_filter}%"))
    if nro_pedido_filter:
        query = query.filter(PymePedido.nro_pedido.ilike(f"%{nro_pedido_filter}%"))

    try:
        if fecha_inicio_str:
            fecha_inicio = datetime.fromisoformat(fecha_inicio_str.split('T')[0])
            query = query.filter(PymePedido.fecha >= fecha_inicio)
        if fecha_fin_str:
            fecha_fin = datetime.fromisoformat(fecha_fin_str.split('T')[0])
            query = query.filter(PymePedido.fecha <= (fecha_fin + timedelta(days=1) - timedelta(seconds=1)))
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Usar YYYY-MM-DD."}), 400

    # Calculate total value per status for the filtered query
    # To do this correctly, we need to apply filters before aggregation.
    # One way is to get all filtered IDs, then query again, or use a subquery.

    # Simpler approach for now: calculate on the Python side after fetching filtered list.
    # More performant for DB would be a GROUP BY on the filtered query.
    pedidos_list = query.order_by(PymePedido.fecha.desc()).all()

    resumen_valor_por_estado = {}
    for p_obj in pedidos_list:
        estado_actual = p_obj.estado
        monto = p_obj.monto_total if p_obj.monto_total is not None else 0
        resumen_valor_por_estado[estado_actual] = resumen_valor_por_estado.get(estado_actual, 0) + monto

    return jsonify({
        "pedidos": [_serialize_pedido(p) for p in pedidos_list],
        "resumen_valor_por_estado": resumen_valor_por_estado,
        "total_pedidos_filtrados": len(pedidos_list)
    })

@pedidos_bp.route('/<int:pedido_id>', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def obtener_pedido_pyme(current_user: User, pedido_id: int):
    is_pyme_user = False
    if hasattr(current_user, 'tipo_chat') and current_user.tipo_chat == "pyme":
        is_pyme_user = True
    elif current_user.rubro and not es_rubro_publico(current_user.rubro):
        is_pyme_user = True
    if not is_pyme_user:
        return jsonify({"error": "Acceso denegado."}), 403

    pedido = PymePedido.query.get(pedido_id)
    if not pedido:
        return jsonify({"error": "Pedido no encontrado."}), 404

    if not current_user.rubro or pedido.rubro != current_user.rubro.nombre:
         return jsonify({"error": "Acceso denegado a este pedido (no coincide rubro)."}), 403

    return jsonify(_serialize_pedido(pedido))

@pedidos_bp.route('/<int:pedido_id>/estado', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_estado_pedido_pyme(current_user: User, pedido_id: int):
    is_pyme_user = False
    if hasattr(current_user, 'tipo_chat') and current_user.tipo_chat == "pyme":
        is_pyme_user = True
    elif current_user.rubro and not es_rubro_publico(current_user.rubro):
        is_pyme_user = True
    if not is_pyme_user:
        return jsonify({"error": "Acceso denegado."}), 403
        
    pedido = PymePedido.query.get(pedido_id)
    if not pedido:
        return jsonify({"error": "Pedido no encontrado."}), 404

    if not current_user.rubro or pedido.rubro != current_user.rubro.nombre:
         return jsonify({"error": "Acceso denegado a este pedido (no coincide rubro)."}), 403

    data = request.get_json()
    nuevo_estado = data.get('estado')

    if not nuevo_estado:
        return jsonify({"error": "Nuevo estado es obligatorio."}), 400

    allowed_statuses = ["pendiente", "confirmado", "en_proceso", "enviado", "entregado", "completado", "cancelado", "devuelto"]
    if nuevo_estado not in allowed_statuses:
        return jsonify({"error": f"Estado '{nuevo_estado}' no es válido. Permitidos: {', '.join(allowed_statuses)}"}), 400

    pedido.estado = nuevo_estado
    try:
        db.session.commit()
        return jsonify(_serialize_pedido(pedido))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar estado del pedido {pedido_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar estado."}), 500

# Nota: Registrar este blueprint en app.py o __init__.py de la aplicación principal.
# from routes.pedidos import pedidos_bp
# app.register_blueprint(pedidos_bp)

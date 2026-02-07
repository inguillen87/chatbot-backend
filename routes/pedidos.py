from flask import Blueprint, request, jsonify, current_app
from models import User, PymePedido, db, TenantProfile
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from services.logic import es_rubro_publico
from services.email_service import enviar_email_pedido_admin
from datetime import datetime, timedelta
from sqlalchemy import func
import json

pedidos_bp = Blueprint('pedidos_bp', __name__, url_prefix='/pedidos')

def _serialize_pedido(pedido: PymePedido):
    return pedido.to_dict()

@pedidos_bp.route('', methods=['GET'])
@token_requerido
def listar_pedidos_pyme(current_user: User):
    is_pyme_user = current_user.tipo_chat == "pyme"

    if not is_pyme_user:
        if current_user.tipo_chat == "municipio":
            from routes.ticket import get_tickets_del_usuario_logic
            return get_tickets_del_usuario_logic(current_user)
        return jsonify({"error": "Acceso denegado. Esta sección es solo para PYMEs."}), 403

    pyme_id_context = current_user.empresa_id or current_user.id
    query = PymePedido.query.filter(PymePedido.pyme_id == pyme_id_context)

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
    pedido = PymePedido.query.get(pedido_id)
    if not pedido:
        return jsonify({"error": "Pedido no encontrado."}), 404

    pyme_id_context = current_user.empresa_id or current_user.id
    if pedido.pyme_id != pyme_id_context:
        return jsonify({"error": "Acceso denegado a este pedido."}), 403

    return jsonify(_serialize_pedido(pedido))

@pedidos_bp.route('/<int:pedido_id>/estado', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_estado_pedido_pyme(current_user: User, pedido_id: int):
    pedido = PymePedido.query.get(pedido_id)
    if not pedido:
        return jsonify({"error": "Pedido no encontrado."}), 404

    pyme_id_context = current_user.empresa_id or current_user.id
    if pedido.pyme_id != pyme_id_context:
        return jsonify({"error": "Acceso denegado a este pedido."}), 403

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
        if nuevo_estado == "confirmado":
            enviar_email_pedido_admin(pedido)
        return jsonify(_serialize_pedido(pedido))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar estado del pedido {pedido_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar estado."}), 500

@pedidos_bp.route('', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def crear_pedido_pyme(current_user: User):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Datos no proporcionados."}), 400

    detalles = data.get('detalles')
    if not detalles or not isinstance(detalles, list):
        return jsonify({"error": "El campo 'detalles' es obligatorio y debe ser una lista de items."}), 400

    try:
        detalles_str = json.dumps(detalles)
    except (TypeError, ValueError):
        return jsonify({"error": "Formato de 'detalles' inválido. Debe ser un JSON serializable."}), 400

    pyme_id_context = current_user.empresa_id or current_user.id

    # Resolve tenant_id to ensure order visibility in Admin Panel
    tenant_id = current_user.tenant_id
    if not tenant_id:
        # Check if pyme owner has tenant_id
        if current_user.empresa_id:
             owner = db.session.get(User, current_user.empresa_id)
             if owner and owner.tenant_id:
                 tenant_id = owner.tenant_id

    if not tenant_id:
        # Find tenant linked to this pyme
        tenant = TenantProfile.query.filter_by(pyme_id=pyme_id_context).first()
        if tenant:
            tenant_id = tenant.id

    nuevo_pedido = PymePedido(
        pyme_id=pyme_id_context,
        tenant_id=tenant_id,
        asunto=data.get('asunto', f'Pedido de {data.get("nombre_cliente", "cliente")}'),
        detalles=detalles_str,
        monto_total=data.get('monto_total'),
        nombre_cliente=data.get('nombre_cliente'),
        email_cliente=data.get('email_cliente'),
        telefono_cliente=data.get('telefono_cliente'),
        direccion=data.get('direccion'),
        latitud=data.get('latitud'),
        longitud=data.get('longitud'),
        user_id=data.get('user_id')
    )

    if data.get('estado'):
        allowed_statuses = ["pendiente", "confirmado", "en_proceso", "enviado", "entregado", "completado", "cancelado", "devuelto"]
        if data['estado'] in allowed_statuses:
            nuevo_pedido.estado = data['estado']

    try:
        db.session.add(nuevo_pedido)
        db.session.commit()
        return jsonify(_serialize_pedido(nuevo_pedido)), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al crear pedido para pyme {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al crear el pedido."}), 500

# Nota: Registrar este blueprint en app.py o __init__.py de la aplicación principal.
# from routes.pedidos import pedidos_bp
# app.register_blueprint(pedidos_bp)

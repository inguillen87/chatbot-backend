from flask import Blueprint, request, jsonify, current_app
from models import User, Recordatorio, db
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido # Changed decorator
from datetime import datetime, date # Import date

recordatorios_bp = Blueprint('recordatorios_bp', __name__, url_prefix='/recordatorios') # Renamed blueprint for consistency

def _serialize_recordatorio(recordatorio: Recordatorio):
    cliente_email = "N/A"
    if recordatorio.cliente_id:
        cliente = User.query.get(recordatorio.cliente_id)
        if cliente:
            cliente_email = cliente.email

    return {
        "id": recordatorio.id,
        "empresa_id": recordatorio.empresa_id,
        "cliente_id": recordatorio.cliente_id,
        "cliente_email": cliente_email,
        "tipo": recordatorio.tipo,
        "descripcion": recordatorio.descripcion,
        "fecha_vencimiento": recordatorio.fecha_vencimiento.isoformat(),
        "enviado": recordatorio.enviado,
    }

@recordatorios_bp.route('', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido # Applied correct decorator
def crear_recordatorio_admin(current_user: User): # Renamed function for clarity
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    cliente_id = data.get('cliente_id')
    tipo = data.get('tipo')
    descripcion = data.get('descripcion')
    fecha_vencimiento_str = data.get('fecha_vencimiento')

    if not all([cliente_id, tipo, fecha_vencimiento_str]):
        return jsonify({"error": "Campos cliente_id, tipo y fecha_vencimiento son obligatorios."}), 400

    # Validate cliente_id belongs to the current_user's company (current_user is admin/emp of the company)
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente: # If current_user is an admin of an "empresa", their ID is the empresa_id for clients
        # If current_user is an employee, current_user.empresa_id is the ID of their employer.
        # The check should be against current_user.id if admin, or current_user.empresa_id if employee.
        # Simpler: an admin user has empresa_id=None. An employee has empresa_id set.
        # A client user also has empresa_id set to the ID of the admin User of their company.
        # So, for an admin (current_user.empresa_id is None), client.empresa_id should be current_user.id.
        # For an employee (current_user.empresa_id is not None), client.empresa_id should be current_user.empresa_id.
        expected_empresa_id = current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
        cliente_check = User.query.filter_by(id=cliente_id, empresa_id=expected_empresa_id).first()
        if not cliente_check:
             return jsonify({"error": "Cliente no encontrado o no asociado a esta empresa."}), 404


    try:
        # Handle both date and datetime strings for fecha_vencimiento
        if 'T' in fecha_vencimiento_str:
            fecha_vencimiento = datetime.fromisoformat(fecha_vencimiento_str)
        else:
            fecha_vencimiento = datetime.combine(date.fromisoformat(fecha_vencimiento_str), datetime.min.time())
    except ValueError:
        return jsonify({"error": "Formato de fecha_vencimiento inválido. Usar YYYY-MM-DD o YYYY-MM-DDTHH:MM:SS."}), 400

    nuevo_recordatorio = Recordatorio(
        empresa_id= current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id, # Store the main company admin ID
        cliente_id=cliente_id,
        tipo=tipo,
        descripcion=descripcion,
        fecha_vencimiento=fecha_vencimiento,
        enviado=data.get('enviado', False)
    )
    db.session.add(nuevo_recordatorio)
    try:
        db.session.commit()
        return jsonify(_serialize_recordatorio(nuevo_recordatorio)), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al crear recordatorio: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar el recordatorio."}), 500

@recordatorios_bp.route('', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido # Applied correct decorator
def listar_recordatorios_admin(current_user: User): # Renamed function
    # Determine the empresa_id to filter by
    empresa_id_to_filter = current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
    if not empresa_id_to_filter: # Should not happen if admin_o_empleado_requerido works
        return jsonify({"error": "No se pudo determinar la empresa para filtrar recordatorios."}), 403

    cliente_id_filter = request.args.get('cliente_id', type=int)
    tipo_filter = request.args.get('tipo')
    solo_pendientes = request.args.get('pendientes', type=lambda v: v.lower() == 'true')
    solo_vencidos = request.args.get('vencidos', type=lambda v: v.lower() == 'true')

    query = Recordatorio.query.filter_by(empresa_id=empresa_id_to_filter)

    if cliente_id_filter:
        query = query.filter_by(cliente_id=cliente_id_filter)
    if tipo_filter:
        query = query.filter(Recordatorio.tipo.ilike(f"%{tipo_filter}%"))

    hoy_dt = datetime.combine(date.today(), datetime.min.time())

    if solo_pendientes:
        query = query.filter(Recordatorio.enviado == False, Recordatorio.fecha_vencimiento >= hoy_dt)
    if solo_vencidos:
         query = query.filter(Recordatorio.enviado == False, Recordatorio.fecha_vencimiento < hoy_dt)

    recordatorios = query.order_by(Recordatorio.fecha_vencimiento.asc()).all()
    return jsonify([_serialize_recordatorio(r) for r in recordatorios])

@recordatorios_bp.route('/<int:recordatorio_id>', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido # Applied correct decorator
def obtener_recordatorio_admin(current_user: User, recordatorio_id: int): # Renamed
    empresa_id_to_filter = current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
    recordatorio = Recordatorio.query.filter_by(id=recordatorio_id, empresa_id=empresa_id_to_filter).first()
    if not recordatorio:
        return jsonify({"error": "Recordatorio no encontrado o no pertenece a esta empresa."}), 404
    return jsonify(_serialize_recordatorio(recordatorio))

@recordatorios_bp.route('/<int:recordatorio_id>', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido # Applied correct decorator
def actualizar_recordatorio_admin(current_user: User, recordatorio_id: int): # Renamed
    empresa_id_to_filter = current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
    recordatorio = Recordatorio.query.filter_by(id=recordatorio_id, empresa_id=empresa_id_to_filter).first()
    if not recordatorio:
        return jsonify({"error": "Recordatorio no encontrado o no pertenece a esta empresa."}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    if 'cliente_id' in data:
        cliente_check = User.query.filter_by(id=data['cliente_id'], empresa_id=empresa_id_to_filter).first()
        if not cliente_check:
            return jsonify({"error": "Cliente no encontrado o no asociado a esta empresa."}), 404
        recordatorio.cliente_id = data['cliente_id']

    if 'tipo' in data:
        recordatorio.tipo = data['tipo']
    if 'descripcion' in data:
        recordatorio.descripcion = data['descripcion']
    if 'fecha_vencimiento' in data:
        try:
            fv_str = data['fecha_vencimiento']
            if 'T' in fv_str:
                 recordatorio.fecha_vencimiento = datetime.fromisoformat(fv_str)
            else:
                 recordatorio.fecha_vencimiento = datetime.combine(date.fromisoformat(fv_str), datetime.min.time())
        except ValueError:
            return jsonify({"error": "Formato de fecha_vencimiento inválido."}), 400
    if 'enviado' in data:
        recordatorio.enviado = bool(data['enviado'])

    try:
        db.session.commit()
        return jsonify(_serialize_recordatorio(recordatorio))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar recordatorio {recordatorio_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar el recordatorio."}), 500

@recordatorios_bp.route('/<int:recordatorio_id>', methods=['DELETE'])
@token_requerido
@admin_o_empleado_requerido # Applied correct decorator
def eliminar_recordatorio_admin(current_user: User, recordatorio_id: int): # Renamed
    empresa_id_to_filter = current_user.id if current_user.rol == 'admin' and current_user.empresa_id is None else current_user.empresa_id
    recordatorio = Recordatorio.query.filter_by(id=recordatorio_id, empresa_id=empresa_id_to_filter).first()
    if not recordatorio:
        return jsonify({"error": "Recordatorio no encontrado o no pertenece a esta empresa."}), 404

    try:
        db.session.delete(recordatorio)
        db.session.commit()
        return jsonify({"mensaje": "Recordatorio eliminado correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al eliminar recordatorio {recordatorio_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al eliminar el recordatorio."}), 500

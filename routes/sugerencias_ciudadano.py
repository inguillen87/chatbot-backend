from flask import Blueprint, request, jsonify, current_app
from models import User, SugerenciaCiudadano, db
from routes.auth import token_requerido, admin_o_empleado_requerido # Ensures user is admin or employee
from services.rubro_classification import es_rubro_publico # To check if user is of 'municipio' type
from datetime import datetime, timedelta

sugerencias_ciudadano_bp = Blueprint('sugerencias_ciudadano_bp', __name__, url_prefix='/sugerencias-ciudadano')

def _serialize_sugerencia(sugerencia: SugerenciaCiudadano):
    # Fetch related user (suggester) info if available
    suggester_info = None
    if sugerencia.user_id:
        suggester_user = User.query.get(sugerencia.user_id)
        if suggester_user:
            suggester_info = {"id": suggester_user.id, "name": suggester_user.name, "email": suggester_user.email}

    return {
        "id": sugerencia.id,
        "user_id": sugerencia.user_id,
        "suggester_info": suggester_info, # Add more detailed suggester info
        "anon_id": sugerencia.anon_id,
        "municipio_id": sugerencia.municipio_id,
        "texto_sugerencia": sugerencia.texto_sugerencia,
        "fecha": sugerencia.fecha.isoformat(),
        "estado": sugerencia.estado,
        "categoria": sugerencia.categoria,
    }

@sugerencias_ciudadano_bp.route('', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido # User is admin or employee
def listar_sugerencias(current_user: User):
    # Ensure user is associated with a municipality
    # An admin of a 'municipio' type User should have their own User.id as the effective municipio_id reference,
    # or User.municipio_id should be set on the admin User model itself if it's a direct identifier.
    # Let's assume current_user.municipio_id is set for municipal admins/employees.

    # Check if user is of 'municipio' type
    if not ( (hasattr(current_user, 'tipo_chat') and current_user.tipo_chat == "municipio") or \
             (current_user.rubro and es_rubro_publico(current_user.rubro)) ):
        return jsonify({"error": "Acceso denegado. Esta sección es solo para usuarios de municipios."}), 403

    # The User model has `municipio_id` field. This should be the ID of the municipality this user (admin/emp) belongs to.
    # And SugerenciaCiudadano.municipio_id should match this.
    # If current_user.municipio_id is None, it means this admin/emp is not directly tied to ONE municipio via this field.
    # This logic depends on how municipio_id is set for admin users of a Municipio.
    # For now, we demand current_user.municipio_id to be set.

    target_municipio_id = current_user.municipio_id
    if not target_municipio_id:
         # If the admin user represents the municipality, their User.id might be the identifier used in SugerenciaCiudadano.municipio_id
         # This needs consistent definition. Assuming current_user.municipio_id is the direct link for staff.
        current_app.logger.warning(f"Usuario municipal {current_user.id} intentando listar sugerencias sin municipio_id asignado.")
        return jsonify({"error": "Usuario municipal no tiene un municipio_id asignado."}), 403

    query = SugerenciaCiudadano.query.filter_by(municipio_id=target_municipio_id)

    # Filters
    estado_filter = request.args.get('estado')
    categoria_filter = request.args.get('categoria')
    fecha_inicio_str = request.args.get('fecha_inicio')
    fecha_fin_str = request.args.get('fecha_fin')

    if estado_filter:
        query = query.filter(SugerenciaCiudadano.estado == estado_filter)
    if categoria_filter:
        query = query.filter(SugerenciaCiudadano.categoria.ilike(f"%{categoria_filter}%"))

    try:
        if fecha_inicio_str:
            fecha_inicio = datetime.fromisoformat(fecha_inicio_str.split('T')[0])
            query = query.filter(SugerenciaCiudadano.fecha >= fecha_inicio)
        if fecha_fin_str:
            fecha_fin = datetime.fromisoformat(fecha_fin_str.split('T')[0])
            query = query.filter(SugerenciaCiudadano.fecha <= (fecha_fin + timedelta(days=1) - timedelta(seconds=1)))
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Usar YYYY-MM-DD."}), 400

    sugerencias = query.order_by(SugerenciaCiudadano.fecha.desc()).all()
    return jsonify([_serialize_sugerencia(s) for s in sugerencias])

@sugerencias_ciudadano_bp.route('/<int:sugerencia_id>', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def obtener_sugerencia(current_user: User, sugerencia_id: int):
    target_municipio_id = current_user.municipio_id
    if not target_municipio_id:
        return jsonify({"error": "Usuario municipal no tiene un municipio_id asignado."}), 403

    sugerencia = SugerenciaCiudadano.query.filter_by(id=sugerencia_id, municipio_id=target_municipio_id).first()
    if not sugerencia:
        return jsonify({"error": "Sugerencia no encontrada o no pertenece a este municipio."}), 404
    return jsonify(_serialize_sugerencia(sugerencia))

@sugerencias_ciudadano_bp.route('/<int:sugerencia_id>', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_sugerencia(current_user: User, sugerencia_id: int):
    target_municipio_id = current_user.municipio_id
    if not target_municipio_id:
        return jsonify({"error": "Usuario municipal no tiene un municipio_id asignado."}), 403

    sugerencia = SugerenciaCiudadano.query.filter_by(id=sugerencia_id, municipio_id=target_municipio_id).first()
    if not sugerencia:
        return jsonify({"error": "Sugerencia no encontrada o no pertenece a este municipio."}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    allowed_statuses = ["nueva", "revisada", "en_evaluacion", "aprobada", "implementada", "descartada"]
    if 'estado' in data:
        if data['estado'] not in allowed_statuses:
            return jsonify({"error": f"Estado '{data['estado']}' no es válido. Permitidos: {', '.join(allowed_statuses)}"}), 400
        sugerencia.estado = data['estado']

    if 'categoria' in data: # Permitir actualizar categoría
        sugerencia.categoria = data['categoria']

    # Podrían añadirse campos como 'respuesta_municipio' a la sugerencia.

    try:
        db.session.commit()
        return jsonify(_serialize_sugerencia(sugerencia))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar sugerencia {sugerencia_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar la sugerencia."}), 500

# Nota: Registrar este blueprint en app.py o __init__.py.
# from routes.sugerencias_ciudadano import sugerencias_ciudadano_bp
# app.register_blueprint(sugerencias_ciudadano_bp)

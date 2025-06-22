from flask import Blueprint, request, jsonify
from models import User, TicketComentario, db
from routes.auth import token_requerido
import uuid

empleados_bp = Blueprint('empleados', __name__, url_prefix='/empleados')

@empleados_bp.route('', methods=['GET'])
@token_requerido
def listar_empleados(current_user: User):
    """Lista los empleados asociados al usuario actual."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
    empleados = (
        User.query.filter_by(empresa_id=current_user.id, rol='empleado')
        .order_by(User.name.asc())
        .all()
    )
    datos = [
        {"id": e.id, "name": e.name, "email": e.email, "rol": e.rol}
        for e in empleados
    ]
    return jsonify(datos)

@empleados_bp.route('', methods=['POST'])
@token_requerido
def crear_empleado(current_user: User):
    """Crea un nuevo empleado asociado al usuario actual."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    if not all([name, email, password]):
        return jsonify({"error": "Datos inválidos"}), 400
    if User.query.filter_by(email=email.strip().lower()).first():
        return jsonify({"error": "Email ya registrado"}), 400
    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        token=str(uuid.uuid4()),
        rol='empleado',
        empresa_id=current_user.id,
    )
    nuevo.set_password(password)
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"id": nuevo.id, "name": nuevo.name, "email": nuevo.email, "rol": nuevo.rol}), 201

@empleados_bp.route('/<int:emp_id>/historial', methods=['GET'])
@token_requerido
def historial_empleado(current_user: User, emp_id: int):
    """Devuelve el historial de atención del empleado."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404
    comentarios = (
        TicketComentario.query.filter_by(user_id=emp_id, es_admin=True)
        .order_by(TicketComentario.fecha.desc())
        .all()
    )
    historial = []
    for c in comentarios:
        tipo = 'pyme' if c.pyme_ticket_id else 'municipio'
        ticket_id = c.pyme_ticket_id or c.municipio_ticket_id
        historial.append({
            "ticket_id": ticket_id,
            "tipo": tipo,
            "comentario": c.comentario,
            "fecha": c.fecha.isoformat()
        })
    return jsonify(historial)


from flask import Blueprint, request, jsonify
from models import User, TicketComentario, db
from routes.auth import token_requerido, solo_admin_requerido
import uuid

empleados_bp = Blueprint('empleados', __name__, url_prefix='/empleados')

@empleados_bp.route('', methods=['GET'])
@token_requerido
@solo_admin_requerido
def listar_empleados(current_user: User):
    """Lista los empleados asociados al usuario actual."""
    empleados = (
        User.query.filter_by(empresa_id=current_user.id, rol='empleado')
        .order_by(User.name.asc())
        .all()
    )
    datos = [
        {
            "id": e.id,
            "name": e.name,
            "email": e.email,
            "rol": e.rol,
            "categorias": e.ticket_categorias or "",
        }
        for e in empleados
    ]
    return jsonify(datos)

@empleados_bp.route('', methods=['POST'])
@token_requerido
@solo_admin_requerido
def crear_empleado(current_user: User):
    """Crea un nuevo empleado asociado al usuario actual."""
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    categorias = data.get('categorias')
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
        ticket_categorias=(','.join(categorias) if isinstance(categorias, list) else categorias) if categorias else None,
    )
    nuevo.set_password(password)
    db.session.add(nuevo)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al crear"}), 500
    return jsonify({
        "id": nuevo.id,
        "name": nuevo.name,
        "email": nuevo.email,
        "rol": nuevo.rol,
        "categorias": nuevo.ticket_categorias or "",
    }), 201

@empleados_bp.route('/<int:emp_id>/historial', methods=['GET'])
@token_requerido
@solo_admin_requerido
def historial_empleado(current_user: User, emp_id: int):
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({'error': 'Empleado no encontrado o no pertenece a su empresa'}), 404
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


# --- Nuevas rutas para gestionar empleados ---

@empleados_bp.route('/<int:emp_id>', methods=['GET'])
@token_requerido
@solo_admin_requerido
def obtener_empleado(current_user: User, emp_id: int):
    """Devuelve los datos de un empleado específico."""
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404
    return jsonify({
        "id": empleado.id,
        "name": empleado.name,
        "email": empleado.email,
        "rol": empleado.rol,
        "categorias": empleado.ticket_categorias or "",
    })


@empleados_bp.route('/<int:emp_id>', methods=['PUT'])
@token_requerido
@solo_admin_requerido
def actualizar_empleado(current_user: User, emp_id: int):
    """Actualiza los datos básicos de un empleado."""
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404
    data = request.get_json(silent=True) or {}
    if 'name' in data:
        empleado.name = data['name'].strip()
    if 'email' in data:
        nuevo_email = data['email'].strip().lower()
        if nuevo_email != empleado.email and User.query.filter_by(email=nuevo_email).first():
            return jsonify({"error": "Email ya registrado"}), 400
        empleado.email = nuevo_email
    if 'password' in data and data['password']:
        empleado.set_password(data['password'])
    if 'categorias' in data:
        cats = data['categorias']
        empleado.ticket_categorias = (
            ','.join(cats) if isinstance(cats, list) else cats
        )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al actualizar"}), 500
    return jsonify({
        "id": empleado.id,
        "name": empleado.name,
        "email": empleado.email,
        "rol": empleado.rol,
        "categorias": empleado.ticket_categorias or "",
    })


@empleados_bp.route('/<int:emp_id>', methods=['DELETE'])
@token_requerido
@solo_admin_requerido
def eliminar_empleado(current_user: User, emp_id: int):
    """Elimina un empleado de la empresa."""
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404
    db.session.delete(empleado)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al eliminar"}), 500
    return jsonify({"mensaje": "Empleado eliminado"})


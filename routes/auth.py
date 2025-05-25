from flask import Blueprint, request, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from models import User, Rubro
from extensions import db
from functools import wraps
import uuid
import logging

auth_bp = Blueprint('auth', __name__)

# Decorador de autenticación robusto
def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token faltante"}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido"}), 403

        return f(user, *args, **kwargs)
    return decorated

# Endpoint para obtener datos del usuario actual
@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

    if not token:
        return jsonify({"error": "Token faltante"}), 401

    user = User.query.filter_by(token=token).first()

    if not user:
        return jsonify({"error": "Token inválido"}), 401

    rubro_nombre = "General"
    try:
        rubro = Rubro.query.get(user.rubro_id) if user.rubro_id else None
        if rubro:
            rubro_nombre = rubro.nombre
    except Exception as e:
        logging.error(f"❌ Error cargando rubro en /me: {str(e)}")

    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas,
        "nombre_empresa": user.nombre_empresa,
        "direccion": user.direccion,
        "telefono": user.telefono,
        "link_web": user.link_web,
        "horario": user.horario,
        "ubicacion": user.ubicacion,
        "logo_url": user.logo_url,
        "rubro": rubro_nombre
    })

# Registro de usuario
@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    nombre_empresa = data.get("nombre_empresa", "").strip()
    rubro_nombre = data.get("rubro", "").strip()

    if not all([name, email, password, nombre_empresa, rubro_nombre]):
        return jsonify({"error": "Todos los campos son obligatorios"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Ya existe un usuario con ese email"}), 400

    rubro = Rubro.query.filter_by(nombre=rubro_nombre).first()
    if not rubro:
        return jsonify({"error": "Rubro no válido"}), 400

    hashed_password = generate_password_hash(password)
    token = str(uuid.uuid4())

    user = User(
        name=name,
        email=email,
        password_hash=hashed_password,
        token=token,
        nombre_empresa=nombre_empresa,
        rubro_id=rubro.id,
        plan="gratis",
        preguntas_usadas=0,
        limite_preguntas=50
    )

    db.session.add(user)
    db.session.commit()
    logging.info(f"✅ Usuario registrado: {email}")

    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "nombre_empresa": user.nombre_empresa,
        "rubro_id": user.rubro_id,
        "rubro": rubro.nombre,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas
    })

# Login de usuario
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos"}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        logging.warning(f"❌ Intento fallido de login: {email}")
        return jsonify({"error": "Credenciales inválidas"}), 401

    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas
    })

# Obtener perfil completo
@auth_bp.route('/perfil', methods=['GET'])
@token_requerido
def get_profile(user):
    rubro = Rubro.query.get(user.rubro_id) if user.rubro_id else None
    return jsonify({
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "nombre_empresa": user.nombre_empresa,
        "telefono": user.telefono,
        "direccion": user.direccion,
        "ubicacion": user.ubicacion,
        "horario": user.horario,
        "link_web": user.link_web,
        "logo_url": user.logo_url,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas,
        "rubro": rubro.nombre if rubro else "General"
    })

# Actualizar perfil
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def update_profile(user):
    try:
        data = request.get_json()

        user.nombre_empresa = (data.get("nombre_empresa") or user.nombre_empresa or "").strip()
        user.telefono = (data.get("telefono") or user.telefono or "").strip()
        user.direccion = (data.get("direccion") or user.direccion or "").strip()
        user.ubicacion = (data.get("ubicacion") or user.ubicacion or "").strip()
        user.horario = (data.get("horario") or user.horario or "").strip()
        user.link_web = (data.get("link_web") or user.link_web or "").strip()
        user.logo_url = (data.get("logo_url") or user.logo_url or "").strip()

        db.session.commit()
        logging.info(f"✅ Perfil actualizado: {user.email}")
        return jsonify({"mensaje": "Perfil actualizado correctamente"})
    except Exception as e:
        logging.error(f"❌ Error al actualizar perfil: {str(e)}")
        return jsonify({"error": f"Error interno al guardar los datos: {str(e)}"}), 500

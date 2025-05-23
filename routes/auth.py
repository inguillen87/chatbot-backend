from flask import Blueprint, request, jsonify
from models import User, Rubro
import logging
from werkzeug.security import check_password_hash, generate_password_hash
import uuid
from extensions import db
from functools import wraps

auth_bp = Blueprint('auth', __name__)

# 📥 LOGIN
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos"}), 400

    user = User.query.filter_by(email=email).first()

    if not user or not check_password_hash(user.password_hash, password):
        logging.warning(f"❌ Intento fallido de login con usuario: {email}")
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

# 📋 INFO DEL USUARIO ACTUAL
@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

    if not token:
        return jsonify({"error": "Token faltante"}), 401

    user = User.query.filter_by(token=token).first()

    if not user:
        return jsonify({"error": "Token inválido"}), 401

    rubro = Rubro.query.get(user.rubro_id)

    return jsonify({
        "token": user.token,
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas,
        "nombre_empresa": user.nombre_empresa,
        "rubro": rubro.nombre if rubro else "General"
    })

# 📝 REGISTER
@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    nombre_empresa = data.get("nombre_empresa", "").strip()
    rubro_nombre = data.get("rubro", "").strip()

    if not name or not email or not password or not nombre_empresa or not rubro_nombre:
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
    print(f"✅ Usuario registrado: {email}")

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

# 🐞 DEBUG USERS (solo para desarrollo)
@auth_bp.route('/debug/users', methods=['GET'])
def list_users():
    try:
        users = User.query.all()
        return jsonify([
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "nombre_empresa": user.nombre_empresa,
                "plan": user.plan,
                "preguntas_usadas": user.preguntas_usadas,
                "limite_preguntas": user.limite_preguntas,
                "token": user.token,
                "rubro_id": user.rubro_id,
                "rubro_nombre": user.rubro.nombre if user.rubro else None
            } for user in users
        ])
    except Exception as e:
        logging.error(f"❌ Error al listar usuarios: {str(e)}")
        return jsonify({"error": f"Error al listar usuarios: {str(e)}"}), 500

# 🛠️ ACTUALIZACIÓN DE PERFIL DEL USUARIO
@auth_bp.route("/perfil", methods=["PUT"])
def actualizar_perfil():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    user = User.query.filter_by(token=token).first()

    if not user:
        return jsonify({"error": "Usuario no autenticado"}), 401

    data = request.get_json()

    user.nombre_empresa = data.get("nombre_empresa", user.nombre_empresa)
    user.direccion = data.get("direccion", user.direccion)
    user.telefono = data.get("telefono", user.telefono)
    user.link_web = data.get("link_web", user.link_web)
    user.horario = data.get("horario", user.horario)
    user.ubicacion = data.get("ubicacion", user.ubicacion)
    user.logo_url = data.get("logo_url", user.logo_url)

    try:
        db.session.commit()
        return jsonify({"mensaje": "Perfil actualizado correctamente"}), 200
    except Exception as e:
        return jsonify({"error": f"No se pudo actualizar el perfil: {str(e)}"}), 500

# 🔐 DECORADOR DE AUTENTICACIÓN REUTILIZABLE
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

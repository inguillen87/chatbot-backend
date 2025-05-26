from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import check_password_hash, generate_password_hash
from models import User, Rubro
from extensions import db
from functools import wraps
import uuid
import logging
import traceback

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

    try:
        rubro = Rubro.query.get(user.rubro_id) if user.rubro_id else None
        rubro_nombre = rubro.nombre if rubro else "General"

        return jsonify({
            "token": user.token,
            "id": user.id,
            "name": user.name or "",
            "email": user.email or "",
            "plan": user.plan or "gratis",
            "preguntas_usadas": user.preguntas_usadas or 0,
            "limite_preguntas": user.limite_preguntas or 0,
            "nombre_empresa": user.nombre_empresa or "",
            "direccion": user.direccion or "",
            "telefono": user.telefono or "",
            "link_web": user.link_web or "",
            "horario": user.horario or "",
            "ubicacion": user.ubicacion or "",
            "logo_url": user.logo_url or "",
            "rubro": rubro_nombre
        })
    except Exception as e:
        error_trace = traceback.format_exc()
        current_app.logger.error("❌ Error crítico en /me:\n" + error_trace)
        return jsonify({"error": "Error interno al obtener perfil"}), 500

# Registro de usuario
@auth_bp.route('/register', methods=['POST'])
def register():
    try:
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
    except Exception as e:
        error_trace = traceback.format_exc()
        logging.error("❌ Error en /register:\n" + error_trace)
        return jsonify({"error": "Error interno al registrar usuario"}), 500

# Login de usuario
@auth_bp.route('/login', methods=['POST'])
def login():
    try:
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
    except Exception as e:
        error_trace = traceback.format_exc()
        logging.error("❌ Error en /login:\n" + error_trace)
        return jsonify({"error": "Error interno al iniciar sesión"}), 500

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
        logging.error("❌ Error al actualizar perfil: %s", traceback.format_exc())
        return jsonify({"error": f"Error interno al guardar los datos"}), 500

# Crear base directo si no existen migraciones
@auth_bp.cli.command("crear_base")
def crear_base():
    db.create_all()
    print("Base creada directamente desde los modelos.")

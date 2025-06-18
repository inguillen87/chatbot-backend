# Contenido COMPLETO para: routes/auth.py

from flask import Blueprint, request, jsonify, current_app, g
from sqlalchemy import func
from werkzeug.security import check_password_hash # Importación que faltaba
from models import User, Rubro
from extensions import db
from functools import wraps
import uuid
import json
from datetime import datetime

auth_bp = Blueprint('auth', __name__)

def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        token = None
        
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
        
        if not token:
            return jsonify({"error": "Token faltante o malformado"}), 401

        user = User.query.filter_by(token=token).first()

        if not user:
            return jsonify({"error": "Token inválido o sesión expirada"}), 401

        return f(user, *args, **kwargs)
    return decorated

@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(email=data.get("email").strip().lower()).first()

    if not user or not user.check_password(data.get("password")):
        current_app.logger.warning(f"Intento de login fallido para el email: {data.get('email')}")
        return jsonify({"error": "Email o contraseña incorrectos."}), 401

    current_app.logger.info(f"Login exitoso para: {user.email}")
    return jsonify({
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": user.token,
        "email": user.email,
        "name": user.name,
    })

@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Solicitud JSON inválida."}), 400

    required_fields = ["name", "email", "password", "nombre_empresa", "rubro", "acepto_terminos"]
    if not all(field in data and data[field] for field in required_fields):
        return jsonify({"error": "Todos los campos son obligatorios."}), 400

    if not data["acepto_terminos"]:
        return jsonify({"error": "Es necesario aceptar los términos y condiciones."}), 400
        
    if User.query.filter_by(email=data['email'].strip().lower()).first():
        return jsonify({"error": "Ya existe un usuario con ese correo electrónico."}), 409

    rubro = Rubro.query.filter(func.lower(Rubro.nombre) == func.lower(data['rubro'].strip())).first()
    if not rubro:
        return jsonify({"error": f"El rubro '{data['rubro']}' no es válido."}), 400

    user = User(
        name=data['name'].strip(),
        email=data['email'].strip().lower(),
        token=str(uuid.uuid4()),
        nombre_empresa=data['nombre_empresa'].strip(),
        rubro_id=rubro.id,
        plan="gratis",
        acepto_terminos=True,
        fecha_aceptacion_terminos=datetime.utcnow()
    )
    user.set_password(data['password'])

    try:
        db.session.add(user)
        db.session.commit()
        current_app.logger.info(f"Usuario registrado: {user.email} con ID {user.id}")
        return jsonify({
            "mensaje": "Usuario registrado exitosamente.",
            "id": user.id,
            "token": user.token,
            "name": user.name,
            "email": user.email,
        }), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al registrar usuario: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar el usuario."}), 500

@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_current_user(user):
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    return jsonify({
        "id": user.id, "email": user.email, "name": user.name, "token": user.token,
        "rubro": rubro_nombre, "nombre_empresa": user.nombre_empresa, "telefono": user.telefono,
        "direccion": user.direccion, "ciudad": user.ciudad, "provincia": user.provincia,
        "pais": user.pais, "latitud": user.latitud, "longitud": user.longitud,
        "link_web": user.link_web, "plan": user.plan, "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas, "horario_json": user.horario_json,
        "logo_url": getattr(user, "logo_url", ""),
    })

@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def actualizar_perfil(user):
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    for key, value in data.items():
        if hasattr(user, key):
            if key == "horario_json":
                setattr(user, "horario", value)
            else:
                setattr(user, key, value)
    try:
        db.session.commit()
        return jsonify({"mensaje": "Perfil actualizado correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar perfil para {user.email}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar el perfil."}), 500
    
def anon_o_token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Permitir solicitudes OPTIONS (preflight CORS) sin autenticación
        if request.method == "OPTIONS":
            return "", 200
        # 1. Autenticación estándar primero
        auth_header = request.headers.get('Authorization', '')
        user = None
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            user = User.query.filter_by(token=token).first()
            if user:
                g.current_user = user
                return f(current_user=user, *args, **kwargs)

        # 2. Si no hay user, busca anon_id en el header
        anon_id = request.headers.get("Anon-Id") or request.args.get("anon_id")
        if not anon_id:
            anon_id = str(uuid.uuid4())
        g.anon_id = anon_id
        resp = f(current_user=None, anon_id=anon_id, *args, **kwargs)
        resp_obj = resp[0] if isinstance(resp, tuple) else resp
        try:
            resp_obj.headers["Anon-Id"] = anon_id
        except Exception:
            pass
        return resp
    return decorated

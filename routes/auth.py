# routes/auth.py (versión refactorizada y con detectives)

from flask import Blueprint, request, jsonify, current_app
from models import User, Rubro
from extensions import db
from functools import wraps
import uuid

auth_bp = Blueprint('auth', __name__)

# --- DECORADOR MEJORADO Y CON DETECTIVES ---
def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        token = None

        current_app.logger.info("\n--- DETECTIVE @token_requerido ---")
        
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            current_app.logger.info(f"1. Token recibido en el header: '{token[:15]}...'")
        else:
            current_app.logger.warning("1. ¡ALARMA! No se recibió el header 'Authorization' o no tiene el formato 'Bearer'.")
            return jsonify({"error": "Token faltante o malformado"}), 401

        # --- BÚSQUEDA REAL DEL USUARIO ---
        user = User.query.filter_by(token=token).first()
        current_app.logger.info(f"2. Resultado de buscar en la BD por ese token: {user}")

        if not user:
            current_app.logger.error("3. ¡ALARMA! Token no corresponde a ningún usuario. Acceso denegado.")
            current_app.logger.info("----------------------------------\n")
            return jsonify({"error": "Token inválido o sesión expirada"}), 401 # Usar 401 para "no autorizado"

        current_app.logger.info("3. ¡Éxito! Usuario encontrado. Permitiendo acceso a la ruta.")
        current_app.logger.info("----------------------------------\n")
        return f(user, *args, **kwargs) # Inyecta el usuario a la ruta
    return decorated

# --- RUTA DE LOGIN CON DETECTIVE ---
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    email = data.get("email").strip().lower()
    password = data.get("password").strip()

    user = User.query.filter_by(email=email).first()

    if not user or not user.check_password(password):
        current_app.logger.warning(f"Intento de login fallido para el email: {email}")
        return jsonify({"error": "Email o contraseña incorrectos."}), 401

    # --- DETECTIVE DE LOGIN ---
    current_app.logger.info("\n--- DETECTIVE LOGIN EXITOSO ---")
    current_app.logger.info(f"Usuario '{user.email}' autenticado. Devolviendo su token: '{user.token[:15]}...'")
    current_app.logger.info("-----------------------------\n")

    # En una API de token, solo devolvemos el token. No usamos login_user().
    return jsonify({
        "mensaje": "Login exitoso",
        "token": user.token,
        "email": user.email,
        "name": user.name,
        # ... puedes devolver más datos si el frontend los necesita al iniciar sesión
    })

# --- OTRAS RUTAS (ME, REGISTER, PERFIL) ---
# Tu ruta /me ya está bien, porque recibe el 'user' del decorador.
@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_current_user(user): # user es inyectado por @token_requerido
    # Tu código actual para serializar la respuesta del usuario es bueno.
    # Lo puedes mantener como está.
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    return jsonify({
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "token": user.token,
        "rubro": rubro_nombre,
        # ... todos los demás campos del perfil
        "nombre_empresa": user.nombre_empresa,
        "telefono": user.telefono,
        "direccion": user.direccion,
        # etc.
    })

# Tu código para /register y /perfil [PUT] también está bien,
# ya que usa el decorador @token_requerido de la misma forma.
# Puedes mantenerlos como están.

# ... (Pega aquí tus rutas de /register, /perfil, /debug/users etc. sin cambios) ...
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
    "id": user.id,            # <--- AGREGÁ ESTA LÍNEA
    "token": user.token,
    "email": user.email,
    "name": user.name,
    })

# --- OTRAS RUTAS (ME, REGISTER, PERFIL) ---
# Tu ruta /me ya está bien, porque recibe el 'user' del decorador.
@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_current_user(user):
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    return jsonify({
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "token": user.token,
        "rubro": rubro_nombre,
        "nombre_empresa": user.nombre_empresa,
        "telefono": user.telefono,
        "direccion": user.direccion,
        "ciudad": user.ciudad,
        "provincia": user.provincia,
        "pais": user.pais,
        "latitud": user.latitud,
        "longitud": user.longitud,
        "link_web": user.link_web,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas,
        "horario_json": user.horario_json,
        "logo_url": getattr(user, "logo_url", ""),
        # Agrega más si tenés nuevos campos
    })


@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def actualizar_perfil(user):
    data = request.get_json()
    # Actualizá TODOS los campos relevantes
    user.nombre_empresa = data.get("nombre_empresa", user.nombre_empresa)
    user.telefono = data.get("telefono", user.telefono)
    user.direccion = data.get("direccion", user.direccion)
    user.ciudad = data.get("ciudad", user.ciudad)
    user.provincia = data.get("provincia", user.provincia)
    user.pais = data.get("pais", user.pais)
    user.latitud = data.get("latitud", user.latitud)
    user.longitud = data.get("longitud", user.longitud)
    user.link_web = data.get("link_web", user.link_web)
    user.logo_url = data.get("logo_url", getattr(user, "logo_url", ""))
    if "horario_json" in data:
        user.horario = data["horario_json"]
    db.session.commit()
    return jsonify({"mensaje": "Perfil actualizado correctamente."})

@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password")
    name = data.get("name", "")
    # Agregá los campos que quieras capturar en el registro

    if not email or not password or not name:
        return jsonify({"error": "Faltan datos"}), 400

    # Chequea si ya existe el mail
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "El email ya está registrado"}), 409

    user = User(
        email=email,
        name=name,
        token=str(uuid.uuid4()),  # o tu método generate_token()
        # podés setear campos opcionales acá
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    return jsonify({
    "mensaje": "Usuario registrado con éxito.",
    "id": user.id,           # <--- AGREGÁ ESTA LÍNEA
    "token": user.token,
    "email": user.email,
    "name": user.name,
    # devolvé lo que quieras
}), 201

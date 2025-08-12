from flask import Blueprint, jsonify, request, current_app
from utils.auth_helpers import token_requerido
from services.logic import es_rubro_publico
from models import User

legacy_auth_bp = Blueprint('legacy_auth', __name__)

@legacy_auth_bp.route('/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    if not request.is_json:
        return jsonify({"error": "La solicitud debe ser de tipo JSON."}), 400
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(email=data.get("email").strip().lower()).first()

    if not user or not user.check_password(data.get("password")):
        current_app.logger.warning(f"Intento de login fallido para el email: {data.get('email')}")
        return jsonify({"error": "Email o contraseña incorrectos."}), 401

    current_app.logger.info(f"Login exitoso para: {user.email}")

    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    tipo_chat = getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme")

    # Integrar Flask-Login
    from flask_login import login_user
    login_user(user) # Establecer la sesión para el usuario
    current_app.logger.info(f"Usuario {user.email} logueado y sesión Flask-Login establecida.")

    return jsonify({
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": user.token,
        "email": user.email,
        "name": user.name,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": user.ticket_categorias or "",
    })

@legacy_auth_bp.route('/me', methods=['GET', 'OPTIONS'])
@legacy_auth_bp.route('/perfil', methods=['GET', 'OPTIONS'])
@legacy_auth_bp.route('/profile', methods=['GET', 'OPTIONS'])
@token_requerido
def get_current_user(user):
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    from utils.plan_limits import limite_para_usuario
    tipo_chat = getattr(user, "tipo_chat", None) or (
        "municipio" if es_rubro_publico(rubro_nombre) else "pyme"
    )
    catalogo_label = (
        "Cargar Catálogo de Trámites" if tipo_chat == "municipio" else "Cargar Catálogo de Productos"
    )
    return jsonify({
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "token": user.token,
        "rubro": rubro_nombre,
        "nombre_empresa": user.nombre_empresa,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
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
        "limite_preguntas": limite_para_usuario(user),
        "horario_json": user.horario_json,
        "logo_url": getattr(user, "logo_url", ""),
        "color_primario": getattr(user, "color_primario", None),
        "color_secundario": getattr(user, "color_secundario", None),
        "badge_tipo": getattr(user, "badge_tipo", None),
        "categorias": user.ticket_categorias or "",
        "tipo_chat": tipo_chat,
        "catalogo_label": catalogo_label,
    })

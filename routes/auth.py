# Contenido COMPLETO para: routes/auth.py

from flask import Blueprint, request, jsonify, current_app, g
from services.logic import es_rubro_publico, normalizar_rubro
import os
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from models import User, Rubro, MunicipioTicket, PymeTicket, TicketComentario, ChatSessionContext
from extensions import db
from functools import wraps
import uuid
import json
from datetime import datetime
from services.google_auth import login_o_crear_usuario
from services.pymes import get_or_create_pyme_user_by_token

auth_bp = Blueprint('auth', __name__)

def obtener_token():
    """Extrae el token desde header, query string o payload."""
    current_app.logger.debug(f"[obtener_token] Checking for token. Path: {request.path}")
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        current_app.logger.debug(f"[obtener_token] Found Authorization header: '{auth_header[:30]}...'")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1]
            current_app.logger.debug(f"[obtener_token] Extracted Bearer token: '{token[:10]}...'")
            return token
        current_app.logger.debug(f"[obtener_token] Returning raw Authorization header as token: '{auth_header[:10]}...'")
        return auth_header

    token_x_token = request.headers.get("X-Token")
    if token_x_token:
        token_x_token = token_x_token.strip()
        current_app.logger.debug(f"[obtener_token] Found X-Token header: '{token_x_token[:10]}...'")
        return token_x_token

    token_x_entity_token = request.headers.get("X-Entity-Token")
    if token_x_entity_token:
        token_x_entity_token = token_x_entity_token.strip()
        current_app.logger.debug(f"[obtener_token] Found X-Entity-Token header: '{token_x_entity_token[:10]}...'")
        return token_x_entity_token

    token_args = request.args.get("token")
    if token_args:
        token_args = token_args.strip()
        current_app.logger.debug(f"[obtener_token] Found token in query args: '{token_args[:10]}...'")
        return token_args

    if request.is_json:
        json_data = request.get_json(silent=True) or {}
        token_json = json_data.get("token")
        if token_json:
            token_json = token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found token in JSON payload: '{token_json[:10]}...'")
            return token_json
        # Also check for 'empresa_token' in JSON for /ask/municipio if it's being sent there for anonymous
        # This is specific for debugging the /ask/municipio anonymous case
        if request.path == '/ask/municipio' or request.path.endswith('/ask/municipio'): # Or other relevant /ask paths
            empresa_token_json = json_data.get("empresa_token")
            if empresa_token_json:
                empresa_token_json = empresa_token_json.strip()
                current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in JSON payload for {request.path}: '{empresa_token_json[:10]}...'")
                return empresa_token_json


    token_form = request.form.get("token")
    if token_form:
        token_form = token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found token in form data: '{token_form[:10]}...'")
        return token_form
    
    # For /ask/municipio anonymous, check form data for 'empresa_token' as well
    if request.path == '/ask/municipio' or request.path.endswith('/ask/municipio'):
        empresa_token_form = request.form.get("empresa_token")
        if empresa_token_form:
            empresa_token_form = empresa_token_form.strip()
            current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in form data for {request.path}: '{empresa_token_form[:10]}...'")
            return empresa_token_form

    current_app.logger.debug("[obtener_token] No token found in any common location.")
    return None

from flask_login import current_user

def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Permitir solicitudes OPTIONS (preflight CORS) sin autenticación
        if request.method == "OPTIONS":
            return "", 200

        # Primero, verificar si el usuario ya está autenticado vía Flask-Login (sesión de cookie)
        if current_user.is_authenticated:
            return f(current_user, *args, **kwargs)

        # Si no, buscar el token como se hacía antes
        token = obtener_token()

        if not token:
            return jsonify({"error": "Token faltante o malformado"}), 401

        user = User.query.filter_by(token=token).first()

        if not user:
            return jsonify({"error": "Token inválido o sesión expirada"}), 401

        return f(user, *args, **kwargs)
    return decorated

def admin_o_empleado_requerido(f):
    """Permite solo a admins (empresa_id None) o empleados."""
    @wraps(f)
    def decorated(user: User, *args, **kwargs):
        if user.empresa_id is not None and user.rol != "empleado":
            return jsonify({"error": "Permisos insuficientes"}), 403
        return f(user, *args, **kwargs)

    return decorated

def solo_admin_requerido(f):
    """Permite solo a usuarios administradores (empresa_id None)."""
    @wraps(f)
    def decorated(user: User, *args, **kwargs):
        if user.empresa_id is not None:
            return jsonify({"error": "Permisos insuficientes"}), 403
        return f(user, *args, **kwargs)

    return decorated

@auth_bp.route('/login', methods=['POST'])
def login():
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

@auth_bp.route('/google-client-id', methods=['GET'])
def get_google_client_id():
    """Devuelve el primer Google Client ID configurado."""
    ids = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    first = ids.split(',')[0].strip() if ids else ""
    return jsonify({"client_id": first})


@auth_bp.route('/google-maps-key', methods=['GET'])
def get_google_maps_key():
    """Retorna la API key de Google Maps si está configurada."""
    key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    return jsonify({"api_key": key})

@auth_bp.route('/google-login', methods=['POST'])
def google_login():
    """Inicia sesión utilizando un token de Google."""
    data = request.get_json(silent=True) or {}
    token_id = (
        data.get('id_token')
        or request.form.get('id_token')
    )
    tipo_chat = data.get('tipo_chat') or request.form.get('tipo_chat')
    rol = data.get('rol') or request.form.get('rol')
    if not token_id:
        return jsonify({"error": "id_token requerido"}), 400
    try:
        user = login_o_crear_usuario(token_id, rol=rol, tipo_chat=tipo_chat)
        current_app.logger.info(f"Login Google para: {user.email}")

        if not getattr(user, "rubro_id", None):
            return jsonify({
                "status": "falta_rubro",
                "token": user.token,
                "email": user.email,
            })

        rubro_nombre = user.rubro.nombre if user.rubro else "General"
        tipo_chat = getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme")

        # Integrar Flask-Login
        from flask_login import login_user
        login_user(user) # Establecer la sesión para el usuario
        current_app.logger.info(f"Usuario {user.email} logueado vía Google y sesión Flask-Login establecida.")

        return jsonify({
            "id": user.id,
            "token": user.token,
            "name": user.name,
            "email": user.email,
            "rol": user.rol,
            "empresa_id": user.empresa_id,
            "rubro": rubro_nombre,
            "tipo_chat": tipo_chat,
            "categorias": user.ticket_categorias or "",
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:  # pragma: no cover - unexpected errors
        current_app.logger.error(f"Error en google_login: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500

@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}

    if not data:
        return jsonify({
            "error": "Solicitud JSON inválida.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    # Permitir nombres alternativos para el rubro y términos
    rubro_raw = data.get("rubro") or data.get("rubro_id") or data.get("sector")
    terminos_flag = (
        data.get("acepto_terminos")
        if "acepto_terminos" in data
        else data.get("acepta_terminos")
        if "acepta_terminos" in data
        else data.get("terminos")
        if "terminos" in data
        else data.get("terms")
    )

    required_campos = {
        "name": data.get("name"),
        "email": data.get("email"),
        "password": data.get("password"),
        "nombre_empresa": data.get("nombre_empresa"),
        "rubro": rubro_raw,
        "tipo_chat": data.get("tipo_chat") or data.get("tipo") or data.get("tipo_empresa"),
    }

    missing = [k for k, v in required_campos.items() if not v]
    if missing or terminos_flag is None:
        return jsonify({
            "error": "Todos los campos son obligatorios.",
            "faltantes": missing + ([] if terminos_flag is not None else ["acepto_terminos"]),
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    if not bool(terminos_flag):
        return jsonify({
            "error": "Es necesario aceptar los términos y condiciones.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    if User.query.filter_by(email=required_campos["email"].strip().lower()).first():
        return jsonify({
            "error": "Ya existe un usuario con ese correo electrónico.",
            "botones": [{"texto": "Volver al chat"}],
        }), 409

    # Buscar el rubro por nombre o ID
    rubro = None
    if isinstance(rubro_raw, int) or (isinstance(rubro_raw, str) and rubro_raw.isdigit()):
        rubro = Rubro.query.filter_by(id=int(rubro_raw)).first()
    elif isinstance(rubro_raw, str):
        rubro = Rubro.query.filter(func.lower(Rubro.nombre) == func.lower(rubro_raw.strip())).first()

    if not rubro:
        return jsonify({
            "error": f"El rubro '{rubro_raw}' no es válido.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    acepta_marketing = bool(data.get('acepta_marketing'))
    tags = data.get('tags')
    if isinstance(tags, list):
        tags_value = ','.join(tags)
    elif isinstance(tags, str):
        tags_value = tags
    else:
        tags_value = ''

    tipo_chat_in = required_campos['tipo_chat']
    sinonimos = {
        'muni': 'municipio',
        'municipios': 'municipio',
        'municipio': 'municipio',
        'pymes': 'pyme',
        'pyme': 'pyme',
    }
    tipo_chat_normalizado = sinonimos.get(str(tipo_chat_in).strip().lower()) if tipo_chat_in else None
    if tipo_chat_normalizado not in ('pyme', 'municipio'):
        tipo_chat_normalizado = "municipio" if es_rubro_publico(rubro) else "pyme"

    empresa_existente = User.query.filter(
        func.lower(User.nombre_empresa) == func.lower(required_campos['nombre_empresa'])
    ).filter_by(empresa_id=None).first()
    if empresa_existente:
        rol_asignado = 'usuario'
        empresa_id = empresa_existente.id
    else:
        rol_asignado = 'admin'
        empresa_id = None
    user = User(
        name=data['name'].strip(),
        email=data['email'].strip().lower(),
        token=str(uuid.uuid4()),
        nombre_empresa=data['nombre_empresa'].strip(),
        rubro_id=rubro.id,
        plan="gratis",
        rol=rol_asignado,
        empresa_id=empresa_id,
        acepto_terminos=True,
        fecha_aceptacion_terminos=datetime.utcnow(),
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
        tipo_chat=tipo_chat_normalizado,
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
            "rol": user.rol,
            "tipo_chat": user.tipo_chat,
            "empresa_id": user.empresa_id,
        }), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al registrar usuario: {e}", exc_info=True)
        return jsonify({
            "error": "Error interno al guardar el usuario.",
            "botones": [{"texto": "Volver al chat"}],
        }), 500


@auth_bp.route('/widget/register', methods=['POST'])
@token_requerido
def register_from_widget(owner_user):
    """Registro rápido desde el widget asociado al token."""
    # Aceptar tanto JSON como formularios tradicionales
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    anon_id = request.headers.get("Anon-Id") or data.get("anon_id")
    if not name or not email or not password:
        return jsonify({
            "error": "Faltan datos obligatorios.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    if User.query.filter_by(email=email.strip().lower()).first():
        return jsonify({
            "error": "Email ya registrado.",
            "botones": [{"texto": "Volver al chat"}],
        }), 409

    acepta_marketing = bool(data.get('acepta_marketing'))
    tags = data.get('tags')
    if isinstance(tags, list):
        tags_value = ','.join(tags)
    elif isinstance(tags, str):
        tags_value = tags
    else:
        tags_value = ''
    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        token=str(uuid.uuid4()),
        rubro_id=owner_user.rubro_id,
        empresa_id=owner_user.id,
        plan="gratis",
        rol="usuario",
        tipo_chat=getattr(owner_user, "tipo_chat", None) or ("municipio" if es_rubro_publico(owner_user.rubro) else "pyme"),
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
    )
    nuevo.set_password(password)
    try:
        db.session.add(nuevo)
        db.session.commit()

        # --------- BLOQUE CRÍTICO --------------
        if anon_id:
            from services.ticket_service import servicio_tickets
            servicio_tickets.migrar_tickets_de_anonimo(anon_id, nuevo.id)
        # --------- FIN BLOQUE CRÍTICO ----------

        return jsonify({
            "id": nuevo.id,
            "token": nuevo.token,
            "name": nuevo.name,
            "email": nuevo.email,
            "rol": nuevo.rol,
            "tipo_chat": nuevo.tipo_chat,
            "empresa_id": nuevo.empresa_id,
        }), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error en register_from_widget: {e}", exc_info=True)
        return jsonify({
            "error": "Error interno al registrar usuario.",
            "botones": [{"texto": "Volver al chat"}],
        }), 500

@auth_bp.route('/widget/login', methods=['POST'])
@token_requerido
def login_from_widget(owner_user):
    """Login desde el widget asociado al token."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}
    email = data.get('email')
    password = data.get('password')
    anon_id = request.headers.get("Anon-Id") or data.get("anon_id")
    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(
        email=email.strip().lower(), empresa_id=owner_user.id
    ).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Credenciales inválidas."}), 401

    if anon_id:
        from services.ticket_service import servicio_tickets
        servicio_tickets.migrar_tickets_de_anonimo(anon_id, user.id)

    rubro_nombre = user.rubro.nombre if user.rubro else owner_user.rubro.nombre if owner_user else "General"
    tipo_chat = getattr(user, "tipo_chat", None) or getattr(owner_user, "tipo_chat", None) or ("municipio" if es_rubro_publico(rubro_nombre) else "pyme")

    return jsonify({
        "id": user.id,
        "token": user.token,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": user.ticket_categorias or "",
    })


# Nuevos endpoints para el panel de usuarios de chat

@auth_bp.route('/chatuserregisterpanel', methods=['POST'])
def chatuser_register_panel():
    """Permite registrar un usuario final indicando el token de la entidad."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    empresa_token = data.get('empresa_token') or obtener_token()
    # Log received data for debugging, excluding password
    logged_data = {k: v for k, v in data.items() if k != 'password'}
    current_app.logger.info(f"[chatuser_register_panel] Received data (password excluded): {logged_data}")
    current_app.logger.info(f"[chatuser_register_panel] Anon-Id header: {request.headers.get('Anon-Id')}")


    if not empresa_token:
        current_app.logger.warning("[chatuser_register_panel] Registration attempt failed: Falta empresa_token")
        return jsonify({"error": "Falta empresa_token"}), 400

    owner_user = get_or_create_pyme_user_by_token(empresa_token.strip())
    if not owner_user:
        current_app.logger.warning(
            f"[chatuser_register_panel] Registration attempt failed: Token de empresa inválido o no encontrado: {empresa_token}"
        )
        return (
            jsonify({"error": "Token de empresa inválido o no encontrado"}),
            404,
        )

    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    anon_id = request.headers.get("Anon-Id") or data.get("anon_id")

    if not name or not email or not password:
        return (
            jsonify({"error": "Faltan datos obligatorios.", "botones": [{"texto": "Volver al chat"}]}),
            400,
        )

    # Check if user with this email already exists
    existing_user = User.query.filter(func.lower(User.email) == func.lower(email.strip())).first()

    if existing_user:
        current_app.logger.info(f"[chatuser_register_panel] Email '{email}' ya existe. User ID: {existing_user.id}, Empresa ID: {existing_user.empresa_id}. Owner User ID: {owner_user.id}")
        # User exists. Check if they belong to the same 'empresa'
        if existing_user.empresa_id == owner_user.id:
            # Email exists and is associated with the same empresa_id. Simulate login.
            current_app.logger.info(f"[chatuser_register_panel] Usuario existente '{email}' pertenece a la misma entidad (Owner ID: {owner_user.id}). Devolviendo datos del usuario existente.")
            # Migrate tickets if anon_id is present
            if anon_id:
                from services.ticket_service import servicio_tickets
                servicio_tickets.migrar_tickets_de_anonimo(anon_id, existing_user.id)
            return jsonify({
                "id": existing_user.id,
                "token": existing_user.token,
                "name": existing_user.name,
                "email": existing_user.email,
                "rol": existing_user.rol,
                "tipo_chat": existing_user.tipo_chat or getattr(owner_user, "tipo_chat", None) or ("municipio" if es_rubro_publico(owner_user.rubro) else "pyme"),
                "empresa_id": existing_user.empresa_id,
                "already_registered": True,
                "message": "Usuario ya registrado con esta entidad."
            }), 200
        else:
            # Email exists but is associated with a different empresa_id.
            current_app.logger.warning(f"[chatuser_register_panel] Usuario existente '{email}' (Empresa ID: {existing_user.empresa_id}) intentó registrarse bajo una entidad diferente (Owner ID: {owner_user.id}).")
            return jsonify({
                "error": "El email ya está registrado en otra entidad.",
                "already_registered": True, # From the perspective of the email, it is registered.
                "conflicting_entity": True # More specific flag
            }), 409

    # If user does not exist, proceed with creation
    current_app.logger.info(f"[chatuser_register_panel] Email '{email}' no existe. Creando nuevo usuario para Owner ID: {owner_user.id}")
    acepta_marketing = bool(data.get('acepta_marketing'))
    tags = data.get('tags')
    if isinstance(tags, list):
        tags_value = ','.join(tags)
    elif isinstance(tags, str):
        tags_value = tags
    else:
        tags_value = ''

    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        token=str(uuid.uuid4()),
        rubro_id=owner_user.rubro_id,
        empresa_id=owner_user.id,
        plan="gratis",
        rol="usuario",
        tipo_chat=getattr(owner_user, "tipo_chat", None) or ("municipio" if es_rubro_publico(owner_user.rubro) else "pyme"),
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
    )
    nuevo.set_password(password)
    try:
        db.session.add(nuevo)
        db.session.commit()

        # Log successful registration and association
        current_app.logger.info(f"[chatuser_register_panel] Nuevo usuario '{nuevo.email}' (ID: {nuevo.id}) registrado y asociado con la empresa/owner ID: {owner_user.id} ({owner_user.nombre_empresa if owner_user.nombre_empresa else owner_user.email}).")

        if anon_id:
            from services.ticket_service import servicio_tickets
            servicio_tickets.migrar_tickets_de_anonimo(anon_id, nuevo.id)

        # Update ChatSessionContext
        chat_session_id = request.headers.get("X-Chat-Session-Id")
        if chat_session_id:
            chat_context = ChatSessionContext.query.get(chat_session_id)
            if chat_context:
                chat_context.user_id = nuevo.id
                chat_context.anon_id = None
                if chat_context.context_data is None:
                    chat_context.context_data = {}
                chat_context.context_data['just_logged_in_flag'] = True
                flag_modified(chat_context, "context_data")
                db.session.add(chat_context)
                db.session.commit()
                current_app.logger.info(f"Updated ChatSessionContext {chat_session_id} for new user {nuevo.id}")

        return (
            jsonify({
                "id": nuevo.id,
                "token": nuevo.token,
                "name": nuevo.name,
                "email": nuevo.email,
                "rol": nuevo.rol,
                "tipo_chat": nuevo.tipo_chat,
                "empresa_id": nuevo.empresa_id,
                "already_registered": False,
            }),
            201,
        )
    except Exception as e:  # pragma: no cover - por si falla la DB
        db.session.rollback()
        current_app.logger.error(f"Error en chatuser_register_panel: {e}", exc_info=True)
        return (
            jsonify({"error": "Error interno al registrar usuario.", "botones": [{"texto": "Volver al chat"}]}),
            500,
        )


@auth_bp.route('/chatuserloginpanel', methods=['POST'])
def chatuser_login_panel():
    """Login de usuarios finales indicando el token de la empresa."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    empresa_token = data.get('empresa_token')
    if not empresa_token:
        return jsonify({"error": "Falta empresa_token"}), 400

    owner_user = User.query.filter_by(token=empresa_token.strip()).first()
    if not owner_user:
        return jsonify({"error": "Token de empresa inválido"}), 404

    email = data.get('email')
    password = data.get('password')
    anon_id = request.headers.get("Anon-Id") or data.get("anon_id")

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(email=email.strip().lower(), empresa_id=owner_user.id).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Credenciales inválidas."}), 401

    if anon_id:
        from services.ticket_service import servicio_tickets
        servicio_tickets.migrar_tickets_de_anonimo(anon_id, user.id)

    rubro_nombre = user.rubro.nombre if user.rubro else owner_user.rubro.nombre if owner_user else "General"
    tipo_chat = getattr(user, "tipo_chat", None) or getattr(owner_user, "tipo_chat", None) or ("municipio" if es_rubro_publico(rubro_nombre) else "pyme")

    return jsonify({
        "id": user.id,
        "token": user.token,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
    })

@auth_bp.route('/me', methods=['GET', 'OPTIONS'])
@auth_bp.route('/perfil', methods=['GET', 'OPTIONS'])
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

# Nueva ruta para obtener información básica del token
@auth_bp.route('/token-info', methods=['GET', 'OPTIONS'])
@token_requerido
def token_info(user):
    """Devuelve el rubro y la empresa asociados al token."""
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    return jsonify({
        "id": user.id,
        "rubro": rubro_nombre,
        "nombre_empresa": user.nombre_empresa,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "tipo_chat": getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(rubro_nombre) else "pyme"),
    })


@auth_bp.route('/me/dashboard', methods=['GET', 'OPTIONS'])
@token_requerido
def dashboard_info(user: User):
    """Devuelve las secciones disponibles para el usuario actual."""
    rubro = user.rubro
    tipo_chat = user.tipo_chat or ("municipio" if es_rubro_publico(rubro) else "pyme")

    # Paneles base para todos los usuarios autenticados
    panels = ["perfil"]

    # Paneles para roles admin y empleado
    if user.rol in ["admin", "empleado"]:
        panels.extend([
            "tickets", # Gestión de tickets
            "usuarios_crm", # Gestión de clientes/ciudadanos
            "estadisticas", # Estadísticas generales de tickets/reclamos
            "analiticas_crm", # Analíticas específicas de CRM
            "mapa_tickets" # Mapa de tickets
        ])
        if tipo_chat == "pyme":
            panels.append("pedidos_pyme") # Gestión de pedidos para PYMEs
        elif tipo_chat == "municipio":
            panels.append("sugerencias_ciudadano") # Gestión de sugerencias para Municipios

    # Paneles exclusivos para admin
    if user.rol == "admin":
        panels.extend(["empleados"]) # Gestión de empleados

    # Eliminar duplicados por si acaso y ordenar alfabéticamente para consistencia
    final_panels = sorted(list(set(panels)))

    return jsonify({
        "id": user.id,
        "rol": user.rol,
        "tipo_chat": tipo_chat,
        "panels": final_panels, # Lista de identificadores de paneles
        # Se asume que el frontend mapeará estos identificadores a rutas y nombres visibles
        # Ejemplo de mapeo conceptual en frontend:
        # {
        #   "perfil": { "label": "Mi Perfil", "route": "/perfil" },
        #   "tickets": { "label": "Tickets", "route": "/tickets" },
        #   "usuarios_crm": { "label": "Usuarios CRM", "route": "/crm/usuarios" },
        #   "estadisticas": { "label": "Estadísticas", "route": "/estadisticas" },
        #   "analiticas_crm": { "label": "Analíticas CRM", "route": "/crm/analytics" },
        #   "mapa_tickets": { "label": "Mapa de Tickets", "route": "/tickets/mapa" }, # Asumiendo una ruta genérica o que el FE añade el tipo
        #   "pedidos_pyme": { "label": "Pedidos", "route": "/pedidos" },
        #   "sugerencias_ciudadano": { "label": "Sugerencias", "route": "/sugerencias/ciudadano" },
        #   "empleados": { "label": "Empleados", "route": "/empleados" }
        # }
    })

@auth_bp.route('/me', methods=['PUT', 'OPTIONS'])
@auth_bp.route('/perfil', methods=['PUT', 'OPTIONS'])
@token_requerido
def actualizar_me(user):
    """Permite que el usuario modifique sus datos personales."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    for key, value in data.items():
        if key in {"tags", "acepta_marketing", "rol", "role", "empresa_id"}:
            # Evitar cambios críticos que permitirían escalar privilegios
            continue
        elif hasattr(user, key):
            if key == "horario_json":
                setattr(user, "horario", value)
            else:
                if key == "plan" and isinstance(value, str):
                    value = value.lower()
                setattr(user, key, value)
                if key == "plan":
                    if value == "pro":
                        user.limite_preguntas = 200
                    elif value == "full":
                        user.limite_preguntas = None

    if "tags" in data:
        tags = data.get("tags")
        if isinstance(tags, list):
            user.tags = ",".join(tags)
        elif isinstance(tags, str):
            user.tags = tags

    if "acepta_marketing" in data:
        nueva = bool(data.get("acepta_marketing"))
        if nueva and not user.acepta_marketing:
            user.fecha_aceptacion_marketing = datetime.utcnow()
        user.acepta_marketing = nueva

    try:
        db.session.commit()
        return jsonify({"mensaje": "Perfil actualizado correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error al actualizar perfil para {user.email}: {e}", exc_info=True
        )
        return jsonify({"error": "Error interno al guardar el perfil."}), 500
    
def anon_o_token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Permitir solicitudes OPTIONS (preflight CORS) sin autenticación
        if request.method == "OPTIONS":
            return "", 200
        token = obtener_token()
        anon_id = request.headers.get("Anon-Id") or request.args.get("anon_id")
        current_app.logger.debug(
            f"Verificando autenticación | token_proporcionado={'sí' if token else 'no'} | anon_id={anon_id or 'no'}"
        )

        user = User.query.filter_by(token=token).first() if token else None
        if token and not user:
            current_app.logger.warning(
                f"Token proporcionado ('{token[:10]}...') pero inválido o usuario no encontrado. "
                "Consulta docs/token-invalid-troubleshooting.md para verificarlo."
            )

        if user and not anon_id: # Usuario autenticado por token, sin Anon-Id explícito en cabecera
            g.current_user = user
            current_app.logger.debug(f"Autenticado como user_id={user.id} (sin Anon-Id en cabecera). Token: '{token[:10]}...'")
            return f(current_user=user, *args, **kwargs)

        if anon_id: # Hay un Anon-Id, puede o no haber un owner_user (token de entidad)
            g.anon_id = anon_id
            if user: # Hay un owner_user (token de entidad) Y un Anon-Id (usuario final anónimo)
                g.owner_user = user # user aquí es el owner_user (entidad)
                current_app.logger.debug(
                    f"Acceso anónimo con Anon-Id: {anon_id} bajo entidad/owner_user id: {user.id}. Token entidad: '{token[:10]}...'"
                )
            else: # Hay Anon-Id pero no hay token de entidad (ej. chat público genérico sin token de entidad)
                current_app.logger.debug(
                    f"Acceso anónimo con Anon-Id: {anon_id} (sin entidad/owner_user específica por token)."
                )

            # Llamar a la función decorada, pasando owner_user (que es 'user' de la query por token, puede ser None)
            # y current_user=None porque el usuario final es anónimo (identificado por anon_id)
            response = f(current_user=None, anon_id=anon_id, owner_user=user, *args, **kwargs)

            # Intentar añadir Anon-Id a la respuesta si es un objeto Response
            resp_obj = response[0] if isinstance(response, tuple) else response
            if hasattr(resp_obj, 'headers'):
                try:
                    # Asegurarse de que no estamos intentando modificar un objeto inmutable si no es una instancia de Response
                    from flask import Response
                    if isinstance(resp_obj, Response):
                        resp_obj.headers["Anon-Id"] = anon_id
                    # Si no es un Response de Flask, no intentar añadir la cabecera (ej. si es un dict de error)
                except Exception as e_header: # pragma: no cover
                    current_app.logger.warning(f"No se pudo establecer header Anon-Id en respuesta: {e_header}")
            return response

        current_app.logger.warning(f"Token o Anon-Id requerido pero no proporcionado o inválido. Token recibido: {'presente' if token else 'ausente'}, Anon-Id recibido: {'presente' if anon_id else 'ausente'}")
        return jsonify({"error": "Token o anon_id requerido"}), 401
    return decorated


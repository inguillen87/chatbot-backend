# Contenido COMPLETO para: routes/auth.py

from flask import Blueprint, request, jsonify, current_app, g
from services.logic import es_rubro_publico, normalizar_rubro
import os
from sqlalchemy import func
from models import User, Rubro, MunicipioTicket, PymeTicket, TicketComentario
from extensions import db
from functools import wraps
import uuid
import json
from datetime import datetime
from services.google_auth import login_o_crear_usuario

auth_bp = Blueprint('auth', __name__)

def obtener_token():
    """Extrae el token desde header, query string o payload."""
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        if auth_header.lower().startswith("bearer "):
            return auth_header.split(" ", 1)[1]
        # Aceptar tokens enviados sin el prefijo "Bearer " para mayor compatibilidad
        return auth_header

    token = request.headers.get("X-Token")
    if token:
        return token.strip()

    token = request.headers.get("X-Entity-Token")
    if token:
        return token.strip()

    token = request.args.get("token")
    if token:
        return token.strip()

    if request.is_json:
        token = (request.get_json(silent=True) or {}).get("token")
        if token:
            return token.strip()

    token = request.form.get("token")
    if token:
        return token.strip()

    return None

def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Permitir solicitudes OPTIONS (preflight CORS) sin autenticación
        if request.method == "OPTIONS":
            return "", 200
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

@auth_bp.route('/google-login', methods=['POST'])
def google_login():
    """Inicia sesión utilizando un token de Google."""
    data = request.get_json(silent=True) or {}
    token_id = (
        data.get('id_token')
        or request.form.get('id_token')
    )
    if not token_id:
        return jsonify({"error": "id_token requerido"}), 400
    try:
        user = login_o_crear_usuario(token_id)
        current_app.logger.info(f"Login Google para: {user.email}")

        rubro_nombre = user.rubro.nombre if user.rubro else "General"
        tipo_chat = getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme")

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
        return jsonify({
            'error': "tipo_chat inválido",
            'botones': [{"texto": "Volver al chat"}],
        }), 400

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

    empresa_token = data.get('empresa_token')
    if not empresa_token:
        return jsonify({"error": "Falta empresa_token"}), 400

    owner_user = User.query.filter_by(token=empresa_token.strip()).first()
    if not owner_user:
        return jsonify({"error": "Token de empresa inválido"}), 404

    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    anon_id = request.headers.get("Anon-Id") or data.get("anon_id")

    if not name or not email or not password:
        return (
            jsonify({"error": "Faltan datos obligatorios.", "botones": [{"texto": "Volver al chat"}]}),
            400,
        )

    if User.query.filter_by(email=email.strip().lower()).first():
        return (
            jsonify({"error": "Email ya registrado.", "botones": [{"texto": "Volver al chat"}]}),
            409,
        )

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

        if anon_id:
            from services.ticket_service import servicio_tickets
            servicio_tickets.migrar_tickets_de_anonimo(anon_id, nuevo.id)

        return (
            jsonify({
                "id": nuevo.id,
                "token": nuevo.token,
                "name": nuevo.name,
                "email": nuevo.email,
                "rol": nuevo.rol,
                "tipo_chat": nuevo.tipo_chat,
                "empresa_id": nuevo.empresa_id,
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

@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_current_user(user):
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    from utils.plan_limits import limite_para_usuario
    tipo_chat = user.tipo_chat or ("municipio" if es_rubro_publico(rubro_nombre) else "pyme")
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
        "categorias": user.ticket_categorias or "",
        "tipo_chat": tipo_chat,
        "catalogo_label": catalogo_label,
    })

# Nueva ruta para obtener información básica del token
@auth_bp.route('/token-info', methods=['GET'])
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


@auth_bp.route('/me/dashboard', methods=['GET'])
@token_requerido
def dashboard_info(user):
    """Devuelve las secciones disponibles para el usuario actual."""
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    tipo_chat = user.tipo_chat or ("municipio" if es_rubro_publico(rubro_nombre) else "pyme")

    panels = ["perfil"]
    if user.rol == "admin":
        panels.extend(["tickets", "crm", "empleados"])
    elif user.rol == "empleado":
        panels.extend(["tickets", "crm"])
    if user.municipio_id:
        panels.append("municipio")

    return jsonify({
        "id": user.id,
        "rol": user.rol,
        "tipo_chat": tipo_chat,
        "panels": panels,
    })

@auth_bp.route('/me', methods=['PUT'])
@auth_bp.route('/perfil', methods=['PUT'])
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
        user = User.query.filter_by(token=token).first() if token else None

        if user and not anon_id:
            g.current_user = user
            return f(current_user=user, *args, **kwargs)

        if anon_id:
            if user:
                g.owner_user = user
            g.anon_id = anon_id
            response = f(current_user=None, anon_id=anon_id, owner_user=user, *args, **kwargs)
            resp_obj = response[0] if isinstance(response, tuple) else response
            try:
                resp_obj.headers["Anon-Id"] = anon_id
            except Exception:
                pass
            return response

        return jsonify({"error": "Token o anon_id requerido"}), 401
    return decorated


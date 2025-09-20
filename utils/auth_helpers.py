import uuid
from functools import wraps
from flask import request, jsonify, current_app, g, make_response
from flask_login import current_user
from models import User
import jwt
from datetime import datetime, timedelta, timezone
from services.demo_registry import demo_rubro_for_token

def generar_token(user_id, rol, tipo_chat, municipio_id, pyme_id):
    """Genera un token de autenticación para un usuario."""
    payload = {
        'exp': datetime.now(timezone.utc) + timedelta(days=1),
        'iat': datetime.now(timezone.utc),
        'user_id': user_id,
        'rol': rol,
        'tipo_chat': tipo_chat,
        'municipio_id': municipio_id,
        'pyme_id': pyme_id,
    }
    return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm="HS256")

def user_from_token(token: str) -> User | None:
    """
    Busca un usuario a partir de un token de autenticación JWT.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
        user_id = payload.get('user_id')
        if not user_id:
            return None
        return User.query.get(user_id)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as e:
        current_app.logger.warning(f"Error al decodificar token JWT: {e}")
        return None


def _resolve_owner_user(user: User | None) -> User | None:
    """Return the owner entity for a given authenticated user.

    Employees store the company owner ID in ``empresa_id``. Administrators (for
    both pymes and municipios) have ``empresa_id`` set to ``None`` so the owner
    is the user itself. This helper centralises the lookup so other modules can
    rely on ``g.owner_user`` being populated consistently.
    """

    if not user:
        return None

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id:
        owner = User.query.get(empresa_id)
        if owner:
            return owner

    return user

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

    # Fallback: intentar recuperar el token desde una cookie específica
    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    token_cookie = request.cookies.get(cookie_name)
    if token_cookie:
        token_cookie = token_cookie.strip()
        current_app.logger.debug(
            f"[obtener_token] Found token in cookie '{cookie_name}': '{token_cookie[:10]}...'"
        )
        return token_cookie

    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
    if widget_cookie_name:
        widget_cookie = request.cookies.get(widget_cookie_name)
        if widget_cookie:
            widget_cookie = widget_cookie.strip()
            current_app.logger.debug(
                f"[obtener_token] Found token in widget cookie '{widget_cookie_name}': '{widget_cookie[:10]}...'"
            )
            return widget_cookie

    token_args = request.args.get("token")
    if token_args:
        token_args = token_args.strip()
        current_app.logger.debug(f"[obtener_token] Found token in query args: '{token_args[:10]}...'")
        return token_args

    # Algunas integraciones envían el token como ``entityToken`` en la query
    entity_token_arg = request.args.get("entityToken") or request.args.get("entity_token")
    if entity_token_arg:
        entity_token_arg = entity_token_arg.strip()
        current_app.logger.debug(f"[obtener_token] Found entityToken in query args: '{entity_token_arg[:10]}...'")
        return entity_token_arg

    # Fallback to 'empresa_token' in query args
    empresa_token_arg = request.args.get("empresa_token")
    if empresa_token_arg:
        empresa_token_arg = empresa_token_arg.strip()
        current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in query args: '{empresa_token_arg[:10]}...'")
        return empresa_token_arg

    if request.is_json:
        json_data = request.get_json(silent=True) or {}
        token_json = json_data.get("token")
        if token_json:
            token_json = token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found token in JSON payload: '{token_json[:10]}...'")
            return token_json

        entity_token_json = json_data.get("entityToken") or json_data.get("entity_token")
        if entity_token_json:
            entity_token_json = entity_token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found entityToken in JSON payload: '{entity_token_json[:10]}...'")
            return entity_token_json

        # Fallback to 'empresa_token' in JSON payload (no longer path-restricted)
        empresa_token_json = json_data.get("empresa_token")
        if empresa_token_json:
            empresa_token_json = empresa_token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in JSON payload: '{empresa_token_json[:10]}...'")
            return empresa_token_json


    token_form = request.form.get("token")
    if token_form:
        token_form = token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found token in form data: '{token_form[:10]}...'")
        return token_form

    entity_token_form = request.form.get("entityToken") or request.form.get("entity_token")
    if entity_token_form:
        entity_token_form = entity_token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found entityToken in form data: '{entity_token_form[:10]}...'")
        return entity_token_form

    # Fallback to 'empresa_token' in form data (no longer path-restricted)
    empresa_token_form = request.form.get("empresa_token")
    if empresa_token_form:
        empresa_token_form = empresa_token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in form data: '{empresa_token_form[:10]}...'")
        return empresa_token_form

    current_app.logger.debug("[obtener_token] No token found in any common location.")
    return None


def get_or_create_anon_id() -> str:
    """Obtiene el ID anónimo de la request o genera uno nuevo."""
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.args.get("anon_id")
    )

    if not anon_id:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            anon_id = payload.get("anon_id")

    if not anon_id:
        cookie_name = current_app.config.get(
            "ANON_SESSION_COOKIE_NAME", "chatboc_anon_id"
        )
        cookie_value = request.cookies.get(cookie_name)
        if isinstance(cookie_value, str):
            cookie_value = cookie_value.strip()
        if cookie_value:
            anon_id = cookie_value

    if not anon_id:
        anon_id = str(uuid.uuid4())
        current_app.logger.info(
            f"Generado nuevo ID anónimo para la request: {anon_id}"
        )

    g.anon_id = anon_id
    return anon_id

def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        anon_id = get_or_create_anon_id()
        if request.method == 'OPTIONS':
            resp = make_response('', 204)
            origin = request.headers.get('Origin')
            if origin:
                resp.headers['Access-Control-Allow-Origin'] = origin
                resp.headers['Vary'] = 'Origin'
            else:
                resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Headers'] = (
                'Authorization, Content-Type, Origin, Accept, '
                'X-Entity-Token, X-Chat-Session-Id, X-Anon-Id, Anon-Id'
            )
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            resp.headers['Access-Control-Allow-Credentials'] = 'true'
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return _set_anon_cookie(resp, anon_id)

        # Siempre intentar recuperar el token para exponerlo a las vistas que lo necesiten.
        token = obtener_token()
        g.auth_token = token

        # Primero, verificar si el usuario ya está autenticado vía Flask-Login (sesión de cookie)
        if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
            g.current_user = current_user
            g.owner_user = _resolve_owner_user(current_user)
            return f(current_user, *args, **kwargs)

        # Si no, buscar el token como se hacía antes

        if not token:
            resp = jsonify({"error": "Token faltante o malformado"})
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            _set_anon_cookie(resp, anon_id)
            return resp, 401

        user = user_from_token(token)

        if not user:
            resp = jsonify({"error": "Token inválido o sesión expirada"})
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            _set_anon_cookie(resp, anon_id)
            return resp, 401

        g.current_user = user
        g.owner_user = _resolve_owner_user(user)

        response = f(user, *args, **kwargs)

        # Si el token vino por header/query y no hay cookie, establecerla para
        # futuras solicitudes (especialmente útil en iframes cross-domain).
        default_cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
        widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
        target_cookie = default_cookie_name

        token_payload = {}
        if token:
            try:
                token_payload = jwt.decode(
                    token,
                    current_app.config["SECRET_KEY"],
                    algorithms=["HS256"],
                    options={"verify_exp": False},
                )
            except Exception:
                token_payload = {}

        if token_payload.get("session_kind") == "widget" or token_payload.get("renew_until"):
            target_cookie = widget_cookie_name

        if token and not request.cookies.get(target_cookie):
            resp = make_response(response)
            cookie_args = {
                "key": target_cookie,
                "value": token,
                "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
                "httponly": True,
                "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
            }
            cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
            if cookie_domain:
                cookie_args["domain"] = cookie_domain

            resp.set_cookie(**cookie_args)
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return _set_anon_cookie(resp, anon_id)

        resp = make_response(response)
        resp.headers.setdefault("X-Anon-Id", anon_id)
        resp.headers.setdefault("Anon-Id", anon_id)
        return _set_anon_cookie(resp, anon_id)
    return decorated

def strict_token_requerido(f):
    """
    Decorador que requiere un token de autenticación, ignorando la sesión de Flask-Login.
    Usado para endpoints de API que deben ser estrictamente stateless.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return '', 200

        token = obtener_token()
        if not token:
            return jsonify({"error": "Token de autenticación es requerido."}), 401

        user = user_from_token(token)
        if not user:
            return jsonify({"error": "Token inválido o la sesión ha expirado."}), 401

        return f(user, *args, **kwargs)
    return decorated

def _set_anon_cookie(resp, anon_id: str | None):
    """Setea la cookie que identifica al visitante anónimo."""
    if resp is None or not anon_id:
        return resp

    cookie_name = current_app.config.get("ANON_SESSION_COOKIE_NAME", "chatboc_anon_id")
    max_age = current_app.config.get("ANON_SESSION_COOKIE_MAX_AGE", 60 * 60 * 24 * 30)

    cookie_args = {
        "key": cookie_name,
        "value": anon_id,
        "max_age": max_age,
        "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
        "httponly": False,
        "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        "path": "/",
    }

    cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
    if cookie_domain:
        cookie_args["domain"] = cookie_domain

    resp.set_cookie(**cookie_args)
    return resp


def admin_o_empleado_requerido(f):
    """Permite solo a admins (empresa_id None) o empleados."""
    @wraps(f)
    def decorated(user: User, *args, **kwargs):
        if user.empresa_id is not None and user.rol != "empleado":
            return jsonify({"error": "Permisos insuficientes"}), 403
        return f(user, *args, **kwargs)

    return decorated

def anon_o_token_requerido(f):
    """
    Decorador que maneja la autenticación para endpoints que aceptan
    tanto usuarios autenticados (JWT) como anónimos (con un token de entidad estático).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        anon_id = get_or_create_anon_id()
        if request.method == "OPTIONS":
            # Pre-flight request. Reply successfully.
            resp = make_response("", 204)
            origin = request.headers.get("Origin")
            if origin:
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
            else:
                resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Origin, Accept, "
                "X-Entity-Token, X-Chat-Session-Id, X-Anon-Id, Anon-Id"
            )
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return _set_anon_cookie(resp, anon_id)

        token = obtener_token()
        current_user = None  # El usuario final que chatea (el "viewer")
        owner_user = None    # El dueño del bot (la "entidad", ej: municipio)

        demo_token_detected = False

        if token:
            # Primero, intentar decodificar como JWT. Esto es para usuarios logueados.
            jwt_user = user_from_token(token)
            if jwt_user:
                current_app.logger.info(f"Request authenticated via JWT. User ID: {jwt_user.id}")
                current_user = jwt_user
                # Si un usuario logueado tiene un `empresa_id`, el owner es esa empresa.
                if jwt_user.empresa_id:
                    owner_user = User.query.get(jwt_user.empresa_id)
                else:
                    # Si no, el owner es el propio usuario (ej, el admin del municipio)
                    owner_user = jwt_user
            else:
                # Si falla el JWT, tratar el token como un token de entidad estático (API Key/UUID).
                # Esto es para el widget anónimo.
                entity_user = User.query.filter_by(token=token).first()
                if entity_user:
                    current_app.logger.info(f"Request authenticated via static entity token. Owner User ID: {entity_user.id}")
                    owner_user = entity_user
                    # El current_user sigue siendo None porque es una sesión anónima del widget.
                else:
                    current_app.logger.warning(f"Token '{token[:10]}...' provided but is not a valid JWT or a known entity token.")
                    try:
                        demo_token_detected = demo_rubro_for_token(token) is not None
                    except Exception:
                        demo_token_detected = False

        # Si después de todo no hay owner (ej. request anónima sin token),
        # cargar el owner por defecto para el municipio.
        if not owner_user and 'municipio' in request.path and not demo_token_detected:
            owner_user = User.query.filter_by(tipo_chat='municipio', rol='admin').first()
            if owner_user:
                current_app.logger.info(f"Anonymous request to '{request.path}', loaded DEFAULT municipality owner user ID: {owner_user.id}")
            else:
                current_app.logger.error(f"CRITICAL: Anonymous request to '{request.path}' but no default municipality user found.")

        g.auth_token = token
        g.current_user = current_user
        g.owner_user = owner_user

        # Llamar a la función de la ruta con los usuarios identificados
        response = f(
            current_user=current_user, owner_user=owner_user, anon_id=anon_id, *args, **kwargs
        )

        # Adjuntar el anon_id a la respuesta para que el cliente lo pueda usar
        resp = make_response(response)
        origin = request.headers.get("Origin")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
        else:
            resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers.setdefault("X-Anon-Id", anon_id)
        resp.headers.setdefault("Anon-Id", anon_id)
        return _set_anon_cookie(resp, anon_id)

    return decorated

import uuid
from functools import wraps
from flask import request, jsonify, current_app, g, make_response
from flask_login import current_user
from models import User

def user_from_token(token: str) -> User | None:
    """Busca un usuario a partir de un token de autenticación."""
    if not token:
        return None
    return User.query.filter_by(token=token).first()

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

    entity_token_form = request.form.get("entityToken") or request.form.get("entity_token")
    if entity_token_form:
        entity_token_form = entity_token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found entityToken in form data: '{entity_token_form[:10]}...'")
        return entity_token_form

    # For /ask/municipio anonymous, check form data for 'empresa_token' as well
    if request.path == '/ask/municipio' or request.path.endswith('/ask/municipio'):
        empresa_token_form = request.form.get("empresa_token")
        if empresa_token_form:
            empresa_token_form = empresa_token_form.strip()
            current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in form data for {request.path}: '{empresa_token_form[:10]}...'")
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
    if not anon_id and request.is_json:
        anon_id = (request.get_json(silent=True) or {}).get("anon_id")

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
            resp = make_response('', 200)
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
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
            resp.headers['Access-Control-Allow-Credentials'] = 'true'
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return resp

        # Primero, verificar si el usuario ya está autenticado vía Flask-Login (sesión de cookie)
        if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
            return f(current_user, *args, **kwargs)

        # Si no, buscar el token como se hacía antes
        token = obtener_token()

        if not token:
            resp = jsonify({"error": "Token faltante o malformado"})
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return resp, 401

        user = User.query.filter_by(token=token).first()

        if not user:
            resp = jsonify({"error": "Token inválido o sesión expirada"})
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return resp, 401

        response = f(user, *args, **kwargs)

        # Si el token vino por header/query y no hay cookie, establecerla para
        # futuras solicitudes (especialmente útil en iframes cross-domain).
        cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
        if not request.cookies.get(cookie_name) and token:
            resp = make_response(response)
            cookie_args = {
                "key": cookie_name,
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
            return resp

        resp = make_response(response)
        resp.headers.setdefault("X-Anon-Id", anon_id)
        resp.headers.setdefault("Anon-Id", anon_id)
        return resp
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

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido o la sesión ha expirado."}), 401

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

def anon_o_token_requerido(f):
    """
    Decorador que maneja la autenticación para endpoints que aceptan
    tanto usuarios autenticados con token como usuarios anónimos.
    Para anónimos en endpoints de municipio, carga un owner por defecto.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        anon_id = get_or_create_anon_id()
        if request.method == "OPTIONS":
            resp = make_response("", 200)
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
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return resp

        token = obtener_token()
        user = User.query.filter_by(token=token).first() if token else None
        owner_user = user # Por defecto, el owner es el mismo usuario

        if not user:
            # Lógica para usuarios anónimos
            current_user = None

            json_data = request.get_json(silent=True) or {}
            empresa_token = json_data.get("empresa_token")

            if empresa_token:
                owner_user = User.query.filter_by(token=empresa_token).first()
                if owner_user:
                    current_app.logger.info(f"Anonymous request to '{request.path}', loaded owner_user '{owner_user.id}' via 'empresa_token'.")
                else:
                     current_app.logger.warning(f"Anonymous request with an invalid 'empresa_token': {empresa_token}")

            # Como fallback para endpoints públicos de municipio, cargamos un owner por defecto.
            if 'municipio' in request.path and not owner_user:
                owner_user = User.query.filter_by(tipo_chat='municipio', rol='admin').first()
                if owner_user:
                    current_app.logger.info(f"Anonymous request to '{request.path}', loaded DEFAULT municipality owner user ID: {owner_user.id}")
                else:
                    current_app.logger.error(f"CRITICAL: Anonymous request to '{request.path}' but no default municipality user found.")
        else:
            # Lógica para usuarios autenticados
            current_user = user

        response = f(
            current_user=current_user, owner_user=owner_user, anon_id=anon_id, *args, **kwargs
        )

        resp = make_response(response)
        resp.headers.setdefault("X-Anon-Id", anon_id)
        resp.headers.setdefault("Anon-Id", anon_id)
        return resp

    return decorated

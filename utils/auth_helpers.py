import uuid
from functools import wraps
from flask import request, jsonify, current_app, g
from flask_login import current_user
from models import User

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

def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return '', 200

        # Primero, verificar si el usuario ya está autenticado vía Flask-Login (sesión de cookie)
        if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
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

def anon_o_token_requerido(f):
    """
    Decorador que maneja la autenticación para endpoints que aceptan
    tanto usuarios autenticados con token como usuarios anónimos.
    Para anónimos en endpoints de municipio, carga un owner por defecto.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "OPTIONS":
            return "", 200

        token = obtener_token()
        user = User.query.filter_by(token=token).first() if token else None
        owner_user = user # Por defecto, el owner es el mismo usuario

        g.anon_id = request.headers.get("X-Anon-Id") or request.args.get("anon_id")
        if not g.anon_id:
            g.anon_id = str(uuid.uuid4())
            current_app.logger.info(f"Generado nuevo ID anónimo para la request: {g.anon_id}")

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

        return f(current_user=current_user, owner_user=owner_user, anon_id=g.anon_id, *args, **kwargs)

    return decorated

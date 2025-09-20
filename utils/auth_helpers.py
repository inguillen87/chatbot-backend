import uuid
from functools import wraps
from flask import request, jsonify, current_app, g, make_response
from flask_login import current_user
from models import User
import jwt
from datetime import datetime, timedelta, timezone
from services.demo_registry import demo_rubro_for_token


_WIDGET_ALLOWED_PREFIXES: tuple[str, ...] = ("/auth/widget/",)
_WIDGET_ALLOWED_GET_PATHS: set[str] = {
    "/auth/me",
    "/auth/perfil",
    "/auth/profile",
    "/auth/token-info",
}


def _normalize_path(path: str | None) -> str:
    """Return a normalized absolute path used for widget access checks."""

    if not path:
        return "/"

    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    if "?" in normalized:
        normalized = normalized.split("?", 1)[0]

    if normalized != "/":
        normalized = normalized.rstrip("/") or "/"

    return normalized


def _is_jwt_token(token: str | None) -> bool:
    """Return True if the token string looks like a JWT."""

    if not token or not isinstance(token, str):
        return False
    return token.count(".") == 2


def _widget_session_allowed(path: str | None, method: str | None) -> bool:
    """Return True if a widget session token can access the given request."""

    normalized_path = _normalize_path(path)
    method = (method or "GET").upper()

    if method == "OPTIONS":
        return True

    for prefix in _WIDGET_ALLOWED_PREFIXES:
        if normalized_path.startswith(prefix.rstrip("/")):
            return True

    if method == "GET" and normalized_path in _WIDGET_ALLOWED_GET_PATHS:
        return True

    return False


def _lookup_owner_for_static_token(token: str | None) -> User | None:
    """Return the owner user associated with a static/demo token."""

    if not token:
        return None

    owner = User.query.filter_by(token=token).first()
    if owner:
        return owner

    try:
        demo_entry = demo_rubro_for_token(token)
    except Exception:
        demo_entry = None

    if demo_entry and demo_entry.owner_user_id:
        return User.query.get(demo_entry.owner_user_id)

    return None


def _generate_widget_session_token(owner_user: User) -> tuple[str, dict]:
    """Issue a short-lived widget session token for the given owner."""

    now = int(datetime.utcnow().timestamp())
    minutes = int(current_app.config.get("WIDGET_ACCESS_MINUTES", 45))
    renew_days = int(current_app.config.get("WIDGET_RENEW_DAYS", 7))

    payload = {
        "user_id": owner_user.id,
        "rol": owner_user.rol,
        "tipo_chat": getattr(owner_user, "tipo_chat", None),
        "municipio_id": getattr(owner_user, "municipio_id", None),
        "pyme_id": getattr(owner_user, "pyme_id", None),
        "session_kind": "widget",
        "iat": now,
        "exp": now + minutes * 60,
    }

    if renew_days:
        payload["renew_until"] = now + renew_days * 86400

    token = jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    return token, payload


def _decode_token_payload(token: str | None) -> dict:
    """Decode a JWT payload ignoring expiration errors."""

    if not _is_jwt_token(token):
        return {}

    try:
        return jwt.decode(
            token,
            current_app.config["SECRET_KEY"],
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
    except Exception:
        return {}


def _finalize_token_candidate(
    token_candidate: str | None, static_owner: User | None = None
) -> str | None:
    """Return the preferred token candidate for the current request.

    When a static entity token is provided alongside an existing widget cookie,
    prefer the cookie only if it belongs to the same owner and is still valid so
    stale cookies do not leak between entities or prevent renewals.
    """

    if not token_candidate:
        return None

    token_candidate = token_candidate.strip()
    if not token_candidate:
        return None

    if static_owner and not _is_jwt_token(token_candidate):
        widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
        if widget_cookie_name:
            cookie_value = request.cookies.get(widget_cookie_name)
            if _is_jwt_token(cookie_value):
                cookie_value = cookie_value.strip()
                payload = _decode_token_payload(cookie_value)
                cookie_user_id = payload.get("user_id")
                cookie_kind = payload.get("session_kind")
                try:
                    cookie_exp = int(payload.get("exp", 0))
                except (TypeError, ValueError):
                    cookie_exp = 0
                now_ts = int(datetime.utcnow().timestamp())

                if (
                    cookie_kind == "widget"
                    and cookie_user_id == static_owner.id
                    and cookie_exp > now_ts
                ):
                    return cookie_value

    return token_candidate

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
    if not token or not _is_jwt_token(token):
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

    checked_static_tokens: dict[str, User | None] = {}

    def _resolve_static_owner(token: str) -> User | None:
        if token not in checked_static_tokens:
            checked_static_tokens[token] = _lookup_owner_for_static_token(token)
        owner = checked_static_tokens[token]
        if owner and getattr(g, "_obtener_token_owner", None) is None:
            g._obtener_token_owner = owner
        return owner

    candidates: list[tuple[str, User | None, str]] = []

    def _register_candidate(raw_token: str | None, source: str):
        if not raw_token:
            return
        token_value = raw_token.strip()
        if not token_value:
            return
        owner: User | None = None
        if not _is_jwt_token(token_value):
            owner = _resolve_static_owner(token_value)

        candidate = _finalize_token_candidate(token_value, owner)
        if not candidate:
            return

        if not _is_jwt_token(candidate):
            owner = owner or _resolve_static_owner(candidate)
        candidates.append((candidate, owner, source))
        current_app.logger.debug(
            f"[obtener_token] Candidate from {source}: '{candidate[:10]}...'"
        )

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        current_app.logger.debug(
            f"[obtener_token] Found Authorization header: '{auth_header[:30]}...'"
        )
        if auth_header.lower().startswith("bearer "):
            _register_candidate(auth_header.split(" ", 1)[1], "Authorization/Bearer")
        else:
            _register_candidate(auth_header, "Authorization")

    token_x_token = request.headers.get("X-Token")
    if token_x_token:
        _register_candidate(token_x_token, "X-Token header")

    token_x_entity_token = request.headers.get("X-Entity-Token")
    if token_x_entity_token:
        _register_candidate(token_x_entity_token, "X-Entity-Token header")

    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    token_cookie = request.cookies.get(cookie_name)
    if token_cookie:
        _register_candidate(token_cookie, f"Cookie '{cookie_name}'")

    token_args = request.args.get("token")
    if token_args:
        _register_candidate(token_args, "query parameter 'token'")

    entity_token_arg = request.args.get("entityToken") or request.args.get("entity_token")
    if entity_token_arg:
        _register_candidate(entity_token_arg, "query parameter 'entityToken'")

    empresa_token_arg = request.args.get("empresa_token")
    if empresa_token_arg:
        _register_candidate(empresa_token_arg, "query parameter 'empresa_token'")

    if request.is_json:
        json_data = request.get_json(silent=True) or {}
        token_json = json_data.get("token")
        if token_json:
            _register_candidate(token_json, "JSON field 'token'")

        entity_token_json = json_data.get("entityToken") or json_data.get("entity_token")
        if entity_token_json:
            _register_candidate(entity_token_json, "JSON field 'entityToken'")

        empresa_token_json = json_data.get("empresa_token")
        if empresa_token_json:
            _register_candidate(empresa_token_json, "JSON field 'empresa_token'")

    token_form = request.form.get("token")
    if token_form:
        _register_candidate(token_form, "form field 'token'")

    entity_token_form = request.form.get("entityToken") or request.form.get("entity_token")
    if entity_token_form:
        _register_candidate(entity_token_form, "form field 'entityToken'")

    empresa_token_form = request.form.get("empresa_token")
    if empresa_token_form:
        _register_candidate(empresa_token_form, "form field 'empresa_token'")

    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
    if widget_cookie_name:
        widget_cookie = request.cookies.get(widget_cookie_name)
        if widget_cookie:
            _register_candidate(widget_cookie, f"Cookie '{widget_cookie_name}'")

    widget_cookie_candidate: tuple[str, str] | None = None
    fallback_token: str | None = None
    fallback_source: str | None = None

    for candidate, owner, source in candidates:
        if owner:
            current_app.logger.debug(
                f"[obtener_token] Using token from {source}: '{candidate[:10]}...'"
            )
            return candidate

        if _is_jwt_token(candidate):
            source_lower = source.lower()
            if "widget" in source_lower and "cookie" in source_lower:
                if widget_cookie_candidate is None:
                    widget_cookie_candidate = (candidate, source)
                continue

            current_app.logger.debug(
                f"[obtener_token] Using token from {source}: '{candidate[:10]}...'"
            )
            return candidate

        if fallback_token is None:
            fallback_token = candidate
            fallback_source = source

    if widget_cookie_candidate:
        candidate, source = widget_cookie_candidate
        current_app.logger.debug(
            f"[obtener_token] Using token from {source}: '{candidate[:10]}...'"
        )
        return candidate

    if fallback_token:
        current_app.logger.debug(
            f"[obtener_token] Returning fallback token from {fallback_source}: '{fallback_token[:10]}...'"
        )
        return fallback_token

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
        raw_token = obtener_token()
        g.widget_session = False
        g.widget_owner_user = None
        preloaded_owner = getattr(g, "_obtener_token_owner", None)

        # Primero, verificar si el usuario ya está autenticado vía Flask-Login (sesión de cookie)
        if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
            g.auth_token = raw_token
            g.current_user = current_user
            g.owner_user = _resolve_owner_user(current_user)
            return f(current_user, *args, **kwargs)

        if not raw_token:
            resp = jsonify({"error": "Token faltante o malformado"})
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            _set_anon_cookie(resp, anon_id)
            return resp, 401

        token = raw_token
        token_payload: dict = {}
        user = user_from_token(token)

        if user:
            token_payload = _decode_token_payload(token)
        else:
            owner_user = preloaded_owner
            if owner_user and raw_token and not _is_jwt_token(raw_token):
                if getattr(owner_user, "token", None) != raw_token:
                    owner_user = None

            if owner_user is None:
                owner_user = _lookup_owner_for_static_token(raw_token)

            if owner_user:
                if not _widget_session_allowed(request.path, request.method):
                    resp = jsonify({"error": "Token inválido o sesión expirada"})
                    resp.headers.setdefault("X-Anon-Id", anon_id)
                    resp.headers.setdefault("Anon-Id", anon_id)
                    _set_anon_cookie(resp, anon_id)
                    return resp, 403

                token, token_payload = _generate_widget_session_token(owner_user)
                g.widget_session = True
                g.widget_owner_user = owner_user
                user = owner_user
                current_app.logger.info(
                    "[token_requerido] Issued widget session token for owner %s via %s",
                    owner_user.id,
                    request.path,
                )
            else:
                resp = jsonify({"error": "Token inválido o sesión expirada"})
                resp.headers.setdefault("X-Anon-Id", anon_id)
                resp.headers.setdefault("Anon-Id", anon_id)
                _set_anon_cookie(resp, anon_id)
                return resp, 401

        if token_payload.get("session_kind") == "widget" and not _widget_session_allowed(request.path, request.method):
            resp = jsonify({"error": "Token inválido o sesión expirada"})
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            _set_anon_cookie(resp, anon_id)
            return resp, 403

        g.auth_token = token
        g.current_user = user
        g.owner_user = _resolve_owner_user(user)

        if token_payload.get("session_kind") == "widget":
            g.widget_session = True
            if g.widget_owner_user is None:
                g.widget_owner_user = g.owner_user

        response = f(user, *args, **kwargs)

        # Si el token vino por header/query y no hay cookie, establecerla para
        # futuras solicitudes (especialmente útil en iframes cross-domain).
        default_cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
        widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
        target_cookie = default_cookie_name

        if token_payload.get("session_kind") == "widget" or token_payload.get("renew_until"):
            target_cookie = widget_cookie_name

        existing_cookie_value = request.cookies.get(target_cookie)
        if existing_cookie_value and isinstance(existing_cookie_value, str):
            existing_cookie_value = existing_cookie_value.strip()

        if token and (not existing_cookie_value or existing_cookie_value != token):
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
        preloaded_owner = getattr(g, "_obtener_token_owner", None)
        owner_user = preloaded_owner    # El dueño del bot (la "entidad", ej: municipio)

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
                if owner_user and getattr(owner_user, "token", None) != token:
                    owner_user = None

                entity_user = owner_user or User.query.filter_by(token=token).first()
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

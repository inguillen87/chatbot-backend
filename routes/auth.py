# Contenido COMPLETO para: routes/auth.py

from flask import Blueprint, current_app, g, jsonify, make_response, request
from flask_cors import cross_origin
from services.logic import es_rubro_publico, normalizar_rubro
import os
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from models import ChatSessionContext, MunicipioTicket, PymeTicket, Rubro, TicketComentario, User, generate_token
from extensions import db
from functools import wraps
import uuid
import json
from datetime import datetime, timedelta, timezone
import jwt
from services.google_auth import login_o_crear_usuario
from services.pymes import get_or_create_pyme_user_by_token
from typing import Any, Callable, Dict, Optional
import secrets


def _looks_like_jwt(token: Optional[str]) -> bool:
    """Return True if the given token matches the typical JWT shape."""

    return bool(token and isinstance(token, str) and token.count(".") == 2)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

from utils.auth_helpers import (
    token_requerido,
    obtener_token,
    get_or_create_anon_id,
    generar_token,
    user_from_token,
    get_or_create_entity_token,
)
from flask_login import current_user
from utils.plan_limits import limite_para_usuario
from services.plan_config import (
    get_plan_metadata,
    serialize_plan_catalog,
    serialize_plan_for_response,
)
from services.rewards import recompensas_service
from services.user_service import (
    change_user_email,
    change_user_password,
    create_password_reset_request,
    reset_password_with_token,
    split_password_reset_token,
    update_user_profile,
)
from utils.map_config import get_map_config


_OWNER_TOKEN_RESOLVER: Optional[Callable[[User], Optional[str]]] = None


def _resolve_owner_token(user: User) -> Optional[str]:
    """Return the persistent entity token for the given user without crashing."""

    global _OWNER_TOKEN_RESOLVER

    resolver = _OWNER_TOKEN_RESOLVER
    if resolver is None:
        candidate = globals().get("get_or_create_owner_entity_token")
        if not callable(candidate):
            try:
                from utils.auth_helpers import get_or_create_owner_entity_token as helper
            except Exception:
                helper = None
            else:
                candidate = helper
        if not callable(candidate):
            try:
                from utils.response_utils import (
                    get_or_create_owner_entity_token as response_helper,
                )
            except Exception:
                response_helper = None
            else:
                candidate = response_helper

        if callable(candidate):
            _OWNER_TOKEN_RESOLVER = resolver = candidate  # type: ignore[assignment]

    owner_user = getattr(g, "owner_user", None) or user

    empresa_id = getattr(owner_user, "empresa_id", None)
    if empresa_id:
        try:
            looked_up = User.query.get(empresa_id)
        except Exception:
            looked_up = None
        else:
            if looked_up:
                owner_user = looked_up

    token_value = getattr(owner_user, "entity_token", None)
    if token_value and _looks_like_jwt(token_value):
        token_value = None
    if token_value:
        if not getattr(owner_user, "entity_token", None):
            try:
                owner_user.entity_token = token_value
                db.session.add(owner_user)
                db.session.commit()
            except Exception:
                current_app.logger.exception(
                    "[auth] Failed to persist legacy owner token for user %s",
                    getattr(owner_user, "id", None),
                )
                db.session.rollback()
        return getattr(owner_user, "entity_token", None) or token_value

    if callable(resolver):
        try:
            resolved = resolver(owner_user)
        except Exception:
            current_app.logger.exception(
                "[auth] Owner token helper failed for user %s", getattr(user, "id", None)
            )
        else:
            if resolved and not _looks_like_jwt(resolved):
                return resolved

    if not owner_user:
        return None

    legacy_value = getattr(owner_user, "token", None)
    if legacy_value and not _looks_like_jwt(legacy_value):
        try:
            owner_user.entity_token = legacy_value
            db.session.add(owner_user)
            db.session.commit()
            return legacy_value
        except Exception:
            current_app.logger.exception(
                "[auth] Failed to persist legacy owner token for user %s",
                getattr(owner_user, "id", None),
            )
            db.session.rollback()

    return get_or_create_entity_token(owner_user)


def _include_entity_token_fields(
    payload: Dict[str, Any], owner_token: Optional[str]
) -> Optional[str]:
    token_value = owner_token or payload.get("entity_token") or payload.get(
        "entityToken"
    ) or payload.get("owner_token")
    if _looks_like_jwt(token_value):
        token_value = None

    if token_value:
        payload.setdefault("entity_token", token_value)
        payload.setdefault("entityToken", token_value)
        payload.setdefault("owner_token", token_value)
        payload.setdefault("widget_embed_token", token_value)
        payload.setdefault("widget_embed_token_kind", "entity")

    return token_value


def _generate_email_verification_token() -> str:
    return secrets.token_urlsafe(48)


def _send_verification_email(user: User):
    """Placeholder sender that logs verification dispatches."""

    if not user.email_verification_token:
        return
    current_app.logger.info(
        "[verify_email] Enviando email de verificación a %s con token %s",
        user.email,
        user.email_verification_token,
    )


def _tenant_for_user(user: User):
    return getattr(user, "tenant_profile_municipio", None) or getattr(user, "tenant_profile_pyme", None)


def _apply_welcome_points_if_configured(user: User):
    try:
        recompensas_service().apply_welcome_points(user, _tenant_for_user(user))
    except Exception as exc:  # pragma: no cover - logging only
        current_app.logger.warning("[welcome_points] No se pudieron acreditar puntos: %s", exc)


def _timestamp_to_iso(value: Optional[object]) -> Optional[str]:
    """Convert a timestamp-like value to an ISO8601 string."""

    if value in (None, ""):
        return None

    try:
        ts_int = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    try:
        return datetime.utcfromtimestamp(ts_int).replace(microsecond=0).isoformat() + "Z"
    except (OverflowError, OSError, ValueError):
        return None


@auth_bp.route('/plans', methods=['GET'])
@cross_origin()
def public_plan_catalog():
    """Expose the available subscription plans for the frontend."""

    return jsonify({"planes": serialize_plan_catalog()})


def build_profile_payload(user: User) -> Dict[str, Any]:
    """Assemble the profile payload shared by the legacy and new endpoints."""

    rubro_obj = getattr(user, "rubro", None)
    rubro_nombre = getattr(rubro_obj, "nombre", None) or "General"

    try:
        rubro_es_publico = es_rubro_publico(rubro_obj or rubro_nombre)
    except Exception:
        rubro_es_publico = False

    tipo_chat = getattr(user, "tipo_chat", None) or ("municipio" if rubro_es_publico else "pyme")
    catalogo_label = (
        "Cargar Catálogo de Trámites" if tipo_chat == "municipio" else "Cargar Catálogo de Productos"
    )
    integration_guide_url = current_app.config.get(
        "INTEGRATION_GUIDE_URL",
        "https://docs.chatboc.ar/widget-integration",
    ) or "https://docs.chatboc.ar/widget-integration"

    profile_data: Dict[str, Any] = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": getattr(user, "categorias_lista", []),
        "nombre_empresa": getattr(user, "nombre_empresa", None),
        "badge_tipo": getattr(user, "badge_tipo", None),
        "catalogo_label": catalogo_label,
        "integration_guide_url": integration_guide_url,
        "ciudad": getattr(user, "ciudad", None),
        "color_primario": getattr(user, "color_primario", None),
        "color_secundario": getattr(user, "color_secundario", None),
        "plan": getattr(user, "plan", None),
        "limite_preguntas": limite_para_usuario(user),
        "acepta_marketing": getattr(user, "acepta_marketing", None),
        "tags": getattr(user, "tags", None),
        "horario": getattr(user, "horario", None),
        "horario_json": getattr(user, "horario_json", None),
        "telefono": getattr(user, "telefono", None),
        "direccion": getattr(user, "direccion", None),
        "provincia": getattr(user, "provincia", None),
        "pais": getattr(user, "pais", None),
        "latitud": getattr(user, "latitud", None),
        "longitud": getattr(user, "longitud", None),
        "link_web": getattr(user, "link_web", None),
        "logo_url": getattr(user, "logo_url", None),
        "preguntas_usadas": getattr(user, "preguntas_usadas", None),
    }

    profile_data["map_config"] = get_map_config()

    plan_metadata = get_plan_metadata(profile_data.get("plan"))
    profile_data["plan_detalle"] = serialize_plan_for_response(plan_metadata)
    profile_data["planes_disponibles"] = serialize_plan_catalog()
    tenant_slug_value = getattr(user, "tenant_slug", None)
    profile_data["tenant_slug"] = tenant_slug_value
    profile_data["tenantSlug"] = tenant_slug_value

    owner_token = _resolve_owner_token(user)
    widget_session_active = bool(getattr(g, "widget_session", False))
    widget_owner = getattr(g, "widget_owner_user", None)
    auth_token = getattr(g, "auth_token", None)
    token_payload = getattr(g, "token_payload", {}) or {}

    if auth_token:
        profile_data["token"] = auth_token
        profile_data["auth_token"] = auth_token
    else:
        fallback_token = owner_token or getattr(user, "token", None)
        if fallback_token and not _looks_like_jwt(fallback_token):
            profile_data["token"] = fallback_token

    profile_data["auth_token"] = auth_token

    session_kind = token_payload.get("session_kind")
    if not session_kind:
        session_kind = "widget" if widget_session_active else ("panel" if auth_token else "none")

    profile_data["session_token"] = auth_token
    profile_data["session_kind"] = session_kind
    profile_data["session_expires_at"] = _timestamp_to_iso(token_payload.get("exp"))
    profile_data["session_renew_until"] = _timestamp_to_iso(token_payload.get("renew_until"))
    profile_data["widget_session_active"] = widget_session_active
    profile_data["widget_session_owner_id"] = getattr(widget_owner, "id", None)

    if owner_token:
        profile_data["entity_token"] = owner_token
        profile_data["entityToken"] = owner_token
        profile_data["owner_token"] = owner_token
        profile_data["widget_embed_token"] = owner_token
        profile_data["widget_embed_token_kind"] = "entity"
    elif getattr(user, "token", None) and not _looks_like_jwt(user.token):
        profile_data["entity_token"] = user.token
        profile_data.setdefault("entityToken", user.token)
        profile_data.setdefault("widget_embed_token", user.token)
        profile_data.setdefault("widget_embed_token_kind", "legacy")

    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    profile_data["widget_token_cookie_name"] = widget_cookie_name
    profile_data["widget_access_minutes"] = current_app.config.get("WIDGET_ACCESS_MINUTES", 45)
    profile_data["widget_renew_days"] = current_app.config.get("WIDGET_RENEW_DAYS", 7)

    return profile_data


def _now():
    return int(datetime.utcnow().timestamp())


def _conf(k, d):
    try:
        return int(os.getenv(k, d))
    except Exception:
        return d


def _sign(payload, minutes, renew_days=None):
    payload = dict(payload)
    payload.setdefault("session_kind", "widget")
    payload.update({"iat": _now(), "exp": _now() + minutes * 60})
    if renew_days:
        payload["renew_until"] = _now() + renew_days * 86400
    tok = jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    return tok, payload["exp"]


def _refresh(tok, minutes):
    try:
        p = jwt.decode(
            tok,
            current_app.config["SECRET_KEY"],
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
    except Exception:
        return None
    if p.get("renew_until") and _now() > int(p["renew_until"]):
        return None
    p.pop("exp", None)
    ntok, _ = _sign(p, minutes, None)
    return ntok


def _add_cors(resp):
    origin = request.headers.get("Origin")
    allowed = os.getenv("CORS_ALLOWED_ORIGINS", "*").strip()
    if allowed == "*" and origin:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    else:
        allowed_set = {x.strip() for x in allowed.split(",") if x.strip()}
        if origin in allowed_set:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


@auth_bp.route("/widget-token", methods=["POST", "OPTIONS"], strict_slashes=False)
def widget_token():
    if request.method == "OPTIONS":
        return _add_cors(make_response("", 200))
    token = obtener_token()
    owner_user = None
    if token:
        jwt_user = user_from_token(token)
        if jwt_user:
            owner_user = (
                User.query.get(jwt_user.empresa_id)
                if jwt_user.empresa_id
                else jwt_user
            )
        else:
            owner_user = User.query.filter_by(token=token).first()
    if not owner_user:
        resp = _add_cors(jsonify({"error": "invalid_owner"}))
        return resp, 401

    # Reutilizar un token de widget ya emitido para este owner si sigue siendo válido.
    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    existing_widget_token = request.cookies.get(widget_cookie_name)
    if existing_widget_token:
        try:
            payload = jwt.decode(
                existing_widget_token,
                current_app.config["SECRET_KEY"],
                algorithms=["HS256"],
            )
        except Exception:
            payload = None

        if payload and payload.get("session_kind") == "widget":
            exp_ts = payload.get("exp")
            user_id = payload.get("user_id")
            if exp_ts and user_id == owner_user.id:
                remaining = int(exp_ts - datetime.utcnow().timestamp())
                if remaining > 0:
                    return _add_cors(
                        jsonify({"token": existing_widget_token, "expires_in": remaining})
                    )
    owner = {
        "user_id": owner_user.id,
        "rol": owner_user.rol,
        "tipo_chat": owner_user.tipo_chat,
        "municipio_id": owner_user.municipio_id,
        "pyme_id": owner_user.pyme_id,
    }
    minutes = _conf("WIDGET_ACCESS_MINUTES", 45)
    renew = _conf("WIDGET_RENEW_DAYS", 7)
    tok, _ = _sign(owner, minutes, renew)
    return _add_cors(jsonify({"token": tok, "expires_in": minutes * 60}))


@auth_bp.route("/widget-refresh", methods=["POST", "OPTIONS"], strict_slashes=False)
def widget_refresh():
    if request.method == "OPTIONS":
        return _add_cors(make_response("", 200))
    tok = (request.get_json(silent=True) or {}).get("token")
    if not tok:
        resp = _add_cors(jsonify({"error": "missing_token"}))
        return resp, 401
    minutes = _conf("WIDGET_ACCESS_MINUTES", 45)
    ntok = _refresh(tok, minutes)
    if not ntok:
        resp = _add_cors(jsonify({"error": "renew_window_expired"}))
        return resp, 401
    return _add_cors(jsonify({"token": ntok, "expires_in": minutes * 60}))


def solo_admin_requerido(f):
    """Permite solo a usuarios administradores (empresa_id None)."""
    @wraps(f)
    def decorated(user: User, *args, **kwargs):
        if user.empresa_id is not None:
            return jsonify({"error": "Permisos insuficientes"}), 403
        return f(user, *args, **kwargs)

    return decorated

@auth_bp.route('/login', methods=['POST', 'OPTIONS'])
@cross_origin(supports_credentials=True)
def login():
    anon_id = get_or_create_anon_id()
    if request.method == 'OPTIONS':
        resp = jsonify({'status': 'ok'})
        resp.headers.setdefault('X-Anon-Id', anon_id)
        resp.headers.setdefault('Anon-Id', anon_id)
        return resp
    if not request.is_json:
        resp = jsonify({"error": "La solicitud debe ser de tipo JSON."})
        resp.headers.setdefault('X-Anon-Id', anon_id)
        resp.headers.setdefault('Anon-Id', anon_id)
        return resp, 400
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        resp = jsonify({"error": "Email y contraseña requeridos."})
        resp.headers.setdefault('X-Anon-Id', anon_id)
        resp.headers.setdefault('Anon-Id', anon_id)
        return resp, 400

    user = User.query.filter_by(email=data.get("email").strip().lower()).first()

    if not user or not user.check_password(data.get("password")):
        current_app.logger.warning(f"Intento de login fallido para el email: {data.get('email')}")
        resp = jsonify({"error": "Email o contraseña incorrectos."})
        resp.headers.setdefault('X-Anon-Id', anon_id)
        resp.headers.setdefault('Anon-Id', anon_id)
        return resp, 401

    current_app.logger.info(f"Login exitoso para: {user.email}")

    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    tipo_chat = getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme")

    # Integrar Flask-Login
    from flask_login import login_user
    login_user(user) # Establecer la sesión para el usuario
    current_app.logger.info(f"Usuario {user.email} logueado y sesión Flask-Login establecida.")

    owner_token = _resolve_owner_token(user)

    # Generar el token JWT
    jwt_payload = {
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    response_payload = {
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": jwt_token,
        "email": user.email,
        "name": user.name,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": getattr(user, "categorias_lista", []),
        "tenant_slug": getattr(user, "tenant_slug", None),
        "tenantSlug": getattr(user, "tenant_slug", None),
    }

    entity_token_value = _include_entity_token_fields(response_payload, owner_token)

    response = jsonify(response_payload)

    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")

    if jwt_token:
        cookie_args = {
            "key": cookie_name,
            "value": jwt_token,
            "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
            "httponly": True,
            "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        }

        cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
        if cookie_domain:
            cookie_args["domain"] = cookie_domain

        response.set_cookie(**cookie_args)

    response.headers.setdefault('X-Anon-Id', anon_id)
    if entity_token_value:
        response.headers.setdefault("X-Entity-Token", entity_token_value)
    return response


@auth_bp.route("/integracion/regenerar-token", methods=["POST"])
@token_requerido
def regenerar_token_integracion(user):
    """Regenerates the persistent integration token for the current owner."""

    owner_user = getattr(g, "owner_user", None) or user
    empresa_id = getattr(owner_user, "empresa_id", None)
    if empresa_id:
        parent = User.query.get(empresa_id)
        if parent:
            owner_user = parent

    try:
        owner_user.entity_token = None
        db.session.add(owner_user)
        db.session.commit()
    except Exception:
        current_app.logger.exception(
            "[integracion] No se pudo limpiar el entity_token para regeneración"
        )
        db.session.rollback()
        return jsonify({"error": "No se pudo regenerar el token de integración."}), 500

    owner_token = _resolve_owner_token(owner_user)
    if not owner_token:
        return jsonify({"error": "No se pudo generar un token estable."}), 500

    response_payload = {"entity_token": owner_token, "entityToken": owner_token}
    resp = jsonify(response_payload)
    resp.headers.setdefault("X-Entity-Token", owner_token)
    return resp

@auth_bp.route('/google-client-id', methods=['GET'])
def get_google_client_id():
    """Devuelve el primer Google Client ID configurado."""
    ids = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    first = ids.split(',')[0].strip() if ids else ""
    return jsonify({"client_id": first})


@auth_bp.route('/google-maps-key', methods=['GET'])
def get_google_maps_key():
    """Expone la configuración de mapas para clientes autenticados."""
    return jsonify(get_map_config())

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

        owner_token = _resolve_owner_token(user)

        if not getattr(user, "rubro_id", None):
            # Aún si falta el rubro, generamos un token para que pueda continuar
            jwt_payload = {
                'user_id': user.id,
                'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
            }
            jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")
            response_payload = {
                "status": "falta_rubro",
                "token": jwt_token,
                "email": user.email,
            }
            entity_token_value = _include_entity_token_fields(response_payload, owner_token)
            resp = jsonify(response_payload)
            if entity_token_value:
                resp.headers.setdefault("X-Entity-Token", entity_token_value)
            return resp

        rubro_nombre = user.rubro.nombre if user.rubro else "General"
        tipo_chat = getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme")

        # Integrar Flask-Login
        from flask_login import login_user
        login_user(user) # Establecer la sesión para el usuario
        current_app.logger.info(f"Usuario {user.email} logueado vía Google y sesión Flask-Login establecida.")
        # Generar el token JWT
        jwt_payload = {
            'user_id': user.id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        response_payload = {
            "id": user.id,
            "token": jwt_token,
            "name": user.name,
            "email": user.email,
            "rol": user.rol,
            "empresa_id": user.empresa_id,
            "rubro": rubro_nombre,
            "tipo_chat": tipo_chat,
            "categorias": getattr(user, "categorias_lista", []),
            "tenant_slug": getattr(user, "tenant_slug", None),
            "tenantSlug": getattr(user, "tenant_slug", None),
        }

        entity_token_value = _include_entity_token_fields(response_payload, owner_token)

        response = jsonify(response_payload)

        cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")

        if jwt_token:
            cookie_args = {
                "key": cookie_name,
                "value": jwt_token,
                "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
                "httponly": True,
                "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
            }

            cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
            if cookie_domain:
                cookie_args["domain"] = cookie_domain

            response.set_cookie(**cookie_args)

        if entity_token_value:
            response.headers.setdefault("X-Entity-Token", entity_token_value)

        return response
    except ValueError as e:
        current_app.logger.error(f"Error de valor en google_login: {e}", exc_info=True)
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

    empresa_token = data.get("empresa_token") or obtener_token()

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

    # Si viene desde el widget/entidad y no se envía rubro, redirigir al flujo
    # de registro simplificado para usuarios finales.
    if empresa_token and not rubro_raw:
        current_app.logger.info(
            "[register] Delegando a chatuser_register_panel porque faltan datos de rubro/empresa"
        )
        return chatuser_register_panel()

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

    # BEGIN: MODIFIED LOGIC FOR tipo_chat
    rubro_nombre_normalizado = normalizar_rubro(rubro.nombre)
    
    # Determinar tipo_chat basado en el rubro, ignorando el input del usuario si el rubro es público.
    if es_rubro_publico(rubro):
        tipo_chat_final = "municipio"
        current_app.logger.info(f"Rubro '{rubro.nombre}' es público. Forzando tipo_chat a 'municipio'.")
    else:
        # Si no es un rubro público, respetar el `tipo_chat` del formulario, con 'pyme' como default.
        tipo_chat_in = required_campos.get('tipo_chat')
        sinonimos = {
            'muni': 'municipio',
            'municipios': 'municipio',
            'municipio': 'municipio',
            'pymes': 'pyme',
            'pyme': 'pyme',
        }
        tipo_chat_normalizado = sinonimos.get(str(tipo_chat_in).strip().lower()) if tipo_chat_in else 'pyme'
        
        # Asegurarse de que el tipo de chat sea válido, si no, usar 'pyme'.
        if tipo_chat_normalizado not in ('pyme', 'municipio'):
            tipo_chat_final = 'pyme'
            current_app.logger.warning(f"Valor de tipo_chat inválido: '{tipo_chat_in}'. Usando 'pyme' por defecto.")
        else:
            tipo_chat_final = tipo_chat_normalizado
    
    current_app.logger.info(f"Tipo de chat final determinado: '{tipo_chat_final}'")
    # END: MODIFIED LOGIC FOR tipo_chat

    rol_asignado = 'admin'
    empresa_id = None
    current_app.logger.info(f"[register] Attempting to register user with data: {data}")
    user = User(
        name=data['name'].strip(),
        email=data['email'].strip().lower(),
        token=generate_token(), # FIX: Generate and assign the legacy token on registration
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
        tipo_chat=tipo_chat_final,  # Usar la variable final determinada
        email_verified=False,
        email_verification_token=_generate_email_verification_token(),
        email_verification_sent_at=datetime.now(timezone.utc),
    )
    user.set_password(data['password'])

    try:
        db.session.add(user)
        db.session.commit()
        current_app.logger.info(f"Usuario registrado: {user.email} con ID {user.id}")

        _send_verification_email(user)
        _apply_welcome_points_if_configured(user)

        # Generar el token JWT
        jwt_payload = {
            'user_id': user.id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        owner_token = _resolve_owner_token(user)
        response_payload = {
            "mensaje": "Usuario registrado exitosamente.",
            "id": user.id,
            "token": jwt_token,
            "name": user.name,
            "email": user.email,
            "rol": user.rol,
            "tipo_chat": user.tipo_chat,
            "empresa_id": user.empresa_id,
            "tenant_slug": getattr(user, "tenant_slug", None),
            "tenantSlug": getattr(user, "tenant_slug", None),
        }
        entity_token_value = _include_entity_token_fields(response_payload, owner_token)

        resp = jsonify(response_payload)
        if entity_token_value:
            resp.headers.setdefault("X-Entity-Token", entity_token_value)
        return resp, 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al registrar usuario: {e}", exc_info=True)
        return jsonify({
            "error": "Error interno al guardar el usuario.",
            "botones": [{"texto": "Volver al chat"}],
        }), 500


@auth_bp.route('/verify-email', methods=['GET'])
def verify_email():
    token = request.args.get('token')
    if not token:
        return jsonify({"error": "Token requerido"}), 400

    user = User.query.filter_by(email_verification_token=token).first()
    if not user:
        return jsonify({"error": "Token inválido"}), 400

    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_sent_at = None
    db.session.add(user)
    db.session.commit()

    return jsonify({"mensaje": "Email verificado", "email_verified": True})


@auth_bp.route('/widget/register', methods=['POST'])
@token_requerido
def register_from_widget(user):
    """Registro rápido desde el widget asociado al token."""
    # Aceptar tanto JSON como formularios tradicionales
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}
    name = data.get('name') or "Sin nombre"
    email = data.get('email')
    password = data.get('password')
    telefono_raw = data.get('telefono') or data.get('phone')
    telefono = telefono_raw.strip() if isinstance(telefono_raw, str) and telefono_raw.strip() else None
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )

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
    current_app.logger.info(f"[register_from_widget] Attempting to register user with data: {data}")
    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        token=generate_token(), # FIX: Generate and assign the legacy token on registration
        rubro_id=user.rubro_id,
        empresa_id=user.id,
        plan="gratis",
        rol="usuario",
        tipo_chat=getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme"),
        telefono=telefono,
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
        email_verified=False,
        email_verification_token=_generate_email_verification_token(),
        email_verification_sent_at=datetime.now(timezone.utc),
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

        # Generar el token JWT
        jwt_payload = {
            'user_id': nuevo.id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        _send_verification_email(nuevo)
        _apply_welcome_points_if_configured(nuevo)

        owner_token = _resolve_owner_token(user)
        response_payload = {
            "id": nuevo.id,
            "token": jwt_token,
            "name": nuevo.name,
            "email": nuevo.email,
            "rol": nuevo.rol,
            "tipo_chat": nuevo.tipo_chat,
            "empresa_id": nuevo.empresa_id,
            "tenant_slug": getattr(nuevo, "tenant_slug", None),
            "tenantSlug": getattr(nuevo, "tenant_slug", None),
        }
        entity_token_value = _include_entity_token_fields(response_payload, owner_token)

        resp = jsonify(response_payload)
        if anon_id:
            resp.headers["X-Anon-Id"] = anon_id
            resp.headers["Anon-Id"] = anon_id
        if entity_token_value:
            resp.headers.setdefault("X-Entity-Token", entity_token_value)
        return resp, 201
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
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )
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

    # Generar el token JWT
    jwt_payload = {
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    owner_token = _resolve_owner_token(owner_user)
    response_payload = {
        "id": user.id,
        "token": jwt_token,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": getattr(user, "categorias_lista", []),
        "tenant_slug": getattr(user, "tenant_slug", None),
        "tenantSlug": getattr(user, "tenant_slug", None),
    }

    entity_token_value = _include_entity_token_fields(response_payload, owner_token)

    resp = jsonify(response_payload)
    if anon_id:
        resp.headers["X-Anon-Id"] = anon_id
        resp.headers["Anon-Id"] = anon_id
    if entity_token_value:
        resp.headers.setdefault("X-Entity-Token", entity_token_value)
    return resp


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
    current_app.logger.info(
        f"[chatuser_register_panel] X-Anon-Id header: {request.headers.get('X-Anon-Id') or request.headers.get('Anon-Id')}"
    )


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
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )

    owner_tipo_chat = getattr(owner_user, "tipo_chat", None) or (
        "municipio" if es_rubro_publico(owner_user.rubro) else "pyme"
    )
    owner_municipio_id = getattr(owner_user, "municipio_id", None)
    telefono_raw = data.get('telefono') or data.get('phone')
    telefono = telefono_raw.strip() if isinstance(telefono_raw, str) and telefono_raw.strip() else None

    # If the user is anonymous, we can assign a default password
    if not password:
        password = str(uuid.uuid4())

    if not name or not email:
        return (
            jsonify({"error": "Faltan datos obligatorios: nombre y email son requeridos.", "botones": [{"texto": "Volver al chat"}]}),
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
            if owner_municipio_id and existing_user.municipio_id != owner_municipio_id:
                existing_user.municipio_id = owner_municipio_id
            if not existing_user.tipo_chat:
                existing_user.tipo_chat = owner_tipo_chat
            if telefono and not existing_user.telefono:
                existing_user.telefono = telefono
            db.session.add(existing_user)
            db.session.commit()
            # Migrate tickets if anon_id is present
            if anon_id:
                from services.ticket_service import servicio_tickets
                servicio_tickets.migrar_tickets_de_anonimo(anon_id, existing_user.id)

            # Generar el token JWT
            jwt_payload = {
                'user_id': existing_user.id,
                'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
            }
            jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

            resp = jsonify({
                "id": existing_user.id,
                "token": jwt_token,
                "name": existing_user.name,
                "email": existing_user.email,
                "rol": existing_user.rol,
                "tipo_chat": existing_user.tipo_chat or owner_tipo_chat,
                "municipio_id": existing_user.municipio_id,
                "empresa_id": existing_user.empresa_id,
                "already_registered": True,
                "message": "Usuario ya registrado con esta entidad."
            })
            if anon_id:
                resp.headers["X-Anon-Id"] = anon_id
                resp.headers["Anon-Id"] = anon_id
            return resp, 200
        else:
            # Email exists but is associated with a different empresa_id.
            current_app.logger.warning(f"[chatuser_register_panel] Usuario existente '{email}' (Empresa ID: {existing_user.empresa_id}) intentó registrarse bajo una entidad diferente (Owner ID: {owner_user.id}).")
            return jsonify({
                "error": "El email ya está registrado en otra entidad.",
                "already_registered": True, # From the perspective of the email, it is registered.
                "conflicting_entity": True # More specific flag
            }), 409

    # If user does not exist, proceed with creation
    current_app.logger.info(f"[chatuser_register_panel] Email '{email}' no existe. Creando nuevo usuario para Owner ID: {owner_user.id} with data: {data}")
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
        # token=str(uuid.uuid4()), # El token ahora es JWT y se genera bajo demanda
        rubro_id=owner_user.rubro_id,
        empresa_id=owner_user.id,
        municipio_id=owner_municipio_id,
        plan="gratis",
        rol="lead" if not data.get('password') else "usuario",
        tipo_chat=owner_tipo_chat,
        telefono=telefono,
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
        email_verified=False,
        email_verification_token=_generate_email_verification_token(),
        email_verification_sent_at=datetime.now(timezone.utc),
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

        # Generar el token JWT
        jwt_payload = {
            'user_id': nuevo.id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        _send_verification_email(nuevo)
        _apply_welcome_points_if_configured(nuevo)

        resp = jsonify({
            "id": nuevo.id,
            "token": jwt_token,
            "name": nuevo.name,
            "email": nuevo.email,
            "rol": nuevo.rol,
            "tipo_chat": nuevo.tipo_chat,
            "municipio_id": nuevo.municipio_id,
            "empresa_id": nuevo.empresa_id,
            "already_registered": False,
        })
        if anon_id:
            resp.headers["X-Anon-Id"] = anon_id
            resp.headers["Anon-Id"] = anon_id
        return resp, 201
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
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )

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

    # Generar el token JWT
    jwt_payload = {
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    resp = jsonify({
        "id": user.id,
        "token": jwt_token,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
    })
    if anon_id:
        resp.headers["X-Anon-Id"] = anon_id
        resp.headers["Anon-Id"] = anon_id
    return resp


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


@auth_bp.route('/token-info', methods=['OPTIONS'])
def token_info_options():
    """Preflight CORS para /token-info."""
    anon_id = get_or_create_anon_id()
    resp = jsonify({'status': 'ok'})
    resp.headers.setdefault('X-Anon-Id', anon_id)
    resp.headers.setdefault('Anon-Id', anon_id)
    return resp, 204


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
            "tickets",
            "usuarios_crm",
            "estadisticas",
            "analiticas_crm",
            "mapa_tickets"
        ])
        if tipo_chat == "pyme":
            panels.append("pedidos")
        elif tipo_chat == "municipio":
            panels.append("sugerencias_ciudadano")

    # Paneles exclusivos para admin
    if user.rol == "admin":
        panels.append("empleados")

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

@auth_bp.route(
    '/me', methods=['GET', 'PUT', 'OPTIONS'], provide_automatic_options=False
)
@auth_bp.route(
    '/perfil', methods=['GET', 'PUT', 'OPTIONS'], provide_automatic_options=False
)
@auth_bp.route(
    '/profile', methods=['GET', 'PUT', 'OPTIONS'], provide_automatic_options=False
)
@cross_origin(supports_credentials=True)
@token_requerido
def me_perfil(user):
    """
    Obtiene (GET) o actualiza (PUT) el perfil del usuario.
    """
    if request.method == 'GET':
        profile_data = build_profile_payload(user)
        return jsonify({k: v for k, v in profile_data.items() if v is not None})

    elif request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({"error": "No se recibieron datos."}), 400

        # Usar el servicio centralizado para actualizar el perfil
        if update_user_profile(user, data):
            return jsonify({"mensaje": "Perfil actualizado correctamente."})
        else:
            message, status = getattr(g, "profile_update_error", ("Error interno al guardar el perfil.", 500))
            return jsonify({"error": message}), status

@auth_bp.route('/update_personal_data', methods=['POST'])
@token_requerido
def update_personal_data(current_user: User):
    """
    Actualiza los datos personales de un usuario.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    # Campos que se pueden actualizar
    email_value = data.get('email')
    if email_value is not None and email_value != current_user.email:
        success, message, status = change_user_email(
            current_user,
            email_value,
            data.get('current_password'),
            commit=False,
        )
        if not success:
            return jsonify({"error": message}), status

    allowed_fields = ['name', 'telefono', 'direccion']

    for field in allowed_fields:
        if field in data:
            setattr(current_user, field, data[field])

    try:
        db.session.commit()
        return jsonify({"mensaje": "Datos personales actualizados correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error al actualizar datos personales para {current_user.email}: {e}", exc_info=True
        )
        return jsonify({"error": "Error interno al guardar los datos."}), 500


@auth_bp.route('/password/reset/request', methods=['POST'])
def request_password_reset():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({"error": "El email es requerido."}), 400

    try:
        user = User.query.filter(func.lower(User.email) == email).first()
    except Exception:
        current_app.logger.exception("[auth] Error buscando usuario para reset de contraseña")
        return jsonify({"error": "No se pudo iniciar el reseteo de contraseña."}), 500

    generic_message = "Si el email está registrado, enviaremos las instrucciones para restablecer la contraseña."
    response_payload = {"mensaje": generic_message}

    if not user:
        return jsonify(response_payload)

    try:
        reset_token = create_password_reset_request(user)
    except Exception:
        current_app.logger.exception(
            "[auth] Error generando el token de reseteo para el usuario %s", getattr(user, 'id', None)
        )
        return jsonify({"error": "No se pudo iniciar el reseteo de contraseña."}), 500

    if current_app.config.get('TESTING'):
        response_payload['reset_token'] = reset_token
        response_payload['user_id'] = user.id
    else:
        current_app.logger.info(
            "[auth] Token de reseteo generado para %s. Pendiente de envío de email.", user.email
        )

    return jsonify(response_payload)


@auth_bp.route('/password/reset/confirm', methods=['POST'])
def confirm_password_reset():
    data = request.get_json(silent=True) or {}
    raw_token = data.get('token')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not raw_token or not new_password:
        return jsonify({"error": "Token y nueva contraseña son requeridos."}), 400

    if confirm_password and confirm_password != new_password:
        return jsonify({"error": "La confirmación de contraseña no coincide."}), 400

    token_parts = split_password_reset_token(raw_token)
    if not token_parts:
        return jsonify({"error": "Token inválido."}), 400

    selector, verifier = token_parts
    success, message, status = reset_password_with_token(selector, verifier, new_password)
    if not success:
        return jsonify({"error": message}), status

    return jsonify({"mensaje": message})


@auth_bp.route('/password/change', methods=['POST'])
@token_requerido
def change_password(current_user: User):
    data = request.get_json(silent=True) or {}
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not current_password or not new_password:
        return jsonify({"error": "Debes indicar la contraseña actual y la nueva contraseña."}), 400

    if confirm_password and confirm_password != new_password:
        return jsonify({"error": "La confirmación de contraseña no coincide."}), 400

    success, message, status = change_user_password(current_user, current_password, new_password)
    if not success:
        return jsonify({"error": message}), status

    return jsonify({"mensaje": message})


@auth_bp.route('/email/change', methods=['POST'])
@token_requerido
def change_email(current_user: User):
    data = request.get_json(silent=True) or {}
    new_email = data.get('new_email')
    current_password = data.get('current_password')

    if not new_email or not current_password:
        return jsonify({"error": "Debes indicar el nuevo email y tu contraseña actual."}), 400

    success, message, status = change_user_email(current_user, new_email, current_password)
    if not success:
        return jsonify({"error": message}), status

    return jsonify({"mensaje": message, "email": current_user.email})

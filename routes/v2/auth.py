from __future__ import annotations

from datetime import datetime, timedelta

import jwt
from flask import Blueprint, current_app, jsonify, request
from flask_login import logout_user

from models import User
from routes.auth import google_login as legacy_google_login, login as legacy_login, me_perfil as legacy_me
from utils.auth_helpers import is_user_auth_disabled, user_from_token

v2_auth_bp = Blueprint("v2_auth", __name__, url_prefix="/api/v2/auth")


def _token_from_auth_header() -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return None


def _decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except Exception:
        return None


@v2_auth_bp.route('/login', methods=['POST'])
def login_v2():
    # Reuse current production login flow to avoid diverging auth semantics.
    return legacy_login()


@v2_auth_bp.route('/google', methods=['POST', 'OPTIONS'], strict_slashes=False)
def google_login_v2():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    # Reuse current Google login flow so v2 clients do not pay a failing fallback request.
    return legacy_google_login()


@v2_auth_bp.route('/refresh', methods=['POST'])
def refresh_v2():
    data = request.get_json(silent=True) or {}
    raw_token = data.get("token") or data.get("refresh_token") or _token_from_auth_header()
    payload = _decode_token(raw_token) if raw_token else None
    if not payload:
        return jsonify({"error": "token inválido"}), 401

    if (
        payload.get("auth_provider") == "clerk"
        or payload.get("session_kind") in {"clerk", "demo"}
        or payload.get("demo_mode")
    ):
        return jsonify({
            "error": "Esta sesion debe renovarse con su proveedor de identidad.",
            "reason_code": "provider_resync_required",
        }), 401

    user = user_from_token(raw_token)
    if not user or is_user_auth_disabled(user):
        return jsonify({"error": "Usuario no encontrado"}), 401

    renewed_payload = {
        "user_id": user.id,
        "rol": user.rol,
        "tipo_chat": payload.get("tipo_chat") or user.tipo_chat,
        "empresa_id": payload.get("empresa_id") or user.empresa_id,
        "municipio_id": payload.get("municipio_id") or user.municipio_id,
        "tenant_slug": payload.get("tenant_slug") or user.tenant_slug,
        "demo_mode": bool(payload.get("demo_mode")),
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    token = jwt.encode(renewed_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    return jsonify({"token": token})


@v2_auth_bp.route('/logout', methods=['POST'])
def logout_v2():
    logout_user()
    response = jsonify({"ok": True, "message": "logout exitoso"})
    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
    cookie_options = {
        "path": "/",
        "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
        "httponly": True,
        "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
    }
    for name in {cookie_name, widget_cookie_name}:
        # Clear both the production domain cookie and older host-only variants.
        if cookie_domain:
            response.delete_cookie(name, domain=cookie_domain, **cookie_options)
        response.delete_cookie(name, **cookie_options)
    return response


@v2_auth_bp.route('/me', methods=['GET'])
def me_v2():
    # Reuse current production profile/me behavior for compatibility.
    return legacy_me()

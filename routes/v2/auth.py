from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from flask import Blueprint, current_app, jsonify, request

from models import User

v2_auth_bp = Blueprint("v2_auth", __name__, url_prefix="/api/v2/auth")


def _issue_token(user: User, *, token_type: str, minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user.id,
        "rol": user.rol,
        "tipo_chat": user.tipo_chat,
        "tenant_slug": getattr(user, "tenant_slug", None),
        "type": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


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
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")

    if not email or not password:
        return jsonify({"error": "email y password son obligatorios"}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Credenciales inválidas"}), 401

    access_token = _issue_token(user, token_type="access", minutes=30)
    refresh_token = _issue_token(user, token_type="refresh", minutes=60 * 24 * 7)

    return jsonify(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "rol": user.rol,
                "tipo_chat": user.tipo_chat,
                "tenant_slug": user.tenant_slug,
            },
        }
    )


@v2_auth_bp.route('/refresh', methods=['POST'])
def refresh_v2():
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token") or _token_from_auth_header()
    payload = _decode_token(refresh_token) if refresh_token else None
    if not payload or payload.get("type") != "refresh":
        return jsonify({"error": "refresh token inválido"}), 401

    user = User.query.get(int(payload.get("user_id"))) if payload.get("user_id") else None
    if not user:
        return jsonify({"error": "Usuario no encontrado"}), 401

    access_token = _issue_token(user, token_type="access", minutes=30)
    return jsonify({"access_token": access_token, "token_type": "Bearer"})


@v2_auth_bp.route('/logout', methods=['POST'])
def logout_v2():
    return jsonify({"ok": True, "message": "logout exitoso"})


@v2_auth_bp.route('/me', methods=['GET'])
def me_v2():
    token = _token_from_auth_header()
    payload = _decode_token(token) if token else None
    if not payload or payload.get("type") not in {"access", "refresh"}:
        return jsonify({"error": "No autenticado"}), 401

    user = User.query.get(int(payload.get("user_id"))) if payload.get("user_id") else None
    if not user:
        return jsonify({"error": "Usuario no encontrado"}), 401

    return jsonify(
        {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "rol": user.rol,
            "tipo_chat": user.tipo_chat,
            "tenant_slug": user.tenant_slug,
        }
    )

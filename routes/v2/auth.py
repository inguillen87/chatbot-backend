from __future__ import annotations

from datetime import datetime, timedelta

import jwt
import json
from flask import Blueprint, current_app, jsonify, request
from models import db
from services.auth_session_lifecycle import (
    retire_session, complete_provider_retirement, refresh_native_token, SessionLifecycleError,
)

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

    try:
        token, retirement = refresh_native_token(raw_token, expires_at=datetime.utcnow() + timedelta(hours=12))
        response = jsonify({"token": token, "session_retirement": retirement})
        response.headers['Cache-Control'] = 'no-store'
        return response
    except SessionLifecycleError as error:
        db.session.rollback()
        return jsonify({'reason_code': error.code}), error.status


@v2_auth_bp.route('/logout', methods=['POST'])
@v2_auth_bp.route('/sessions/retire', methods=['POST'])
def logout_v2():
    try:
        if request.content_length is not None and request.content_length > 2048:
            raise SessionLifecycleError('session_retirement_command_invalid', 400)
        def strict_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate_key')
                value[key] = item
            return value
        try:
            raw = request.stream.read(2049)
            if len(raw) > 2048 or not request.is_json:
                raise ValueError('invalid_command')
            data = json.loads(raw, object_pairs_hook=strict_object)
        except (ValueError, UnicodeError):
            raise SessionLifecycleError('session_retirement_command_invalid', 400)
        if not isinstance(data, dict) or set(data) != {'proof', 'request_id'} or request.args:
            raise SessionLifecycleError('session_retirement_command_invalid', 400)
        receipt = retire_session(data['proof'], data['request_id'])
        try:
            provider_status = complete_provider_retirement(receipt['lineage_id'], data['request_id'])
            receipt['provider_revocation'] = {'status': provider_status}
        except Exception:
            db.session.rollback()
            receipt['provider_revocation'] = {'status': 'pending'}
        from socket_service import disconnect_auth_session_sockets
        try:
            disconnect_auth_session_sockets(receipt['lineage_id'])
        except Exception:
            # Durable validators deny every further use even if transport is down.
            receipt['socket_disconnect_deferred'] = True
        response = jsonify(receipt)
    except SessionLifecycleError as error:
        db.session.rollback()
        response = jsonify({'reason_code': error.code})
        response.status_code = error.status
    except Exception:
        db.session.rollback()
        response = jsonify({'reason_code': 'session_retirement_unconfirmed'})
        response.status_code = 503
    response.headers['Cache-Control'] = 'no-store'
    return response


@v2_auth_bp.route('/me', methods=['GET'])
def me_v2():
    # Reuse current production profile/me behavior for compatibility.
    return legacy_me()

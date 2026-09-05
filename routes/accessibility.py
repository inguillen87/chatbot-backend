from typing import Any, Mapping

from flask import Blueprint, request, jsonify
from extensions import db
from models import User
from utils.auth_helpers import token_requerido

accessibility_bp = Blueprint(
    "accessibility_bp", __name__, url_prefix="/api/accessibility"
)

ACCESSIBILITY_CONTRACT_VERSION = "user.accessibility_preferences.v1"
DEFAULT_ACCESSIBILITY_PREFERENCES = {
    "dyslexia": False,
    "simplified": True,
    "highContrast": False,
    "largeControls": False,
    "captions": False,
    "reducedMotion": False,
}
_ACCESSIBILITY_ALIASES = {
    "dislexia": "dyslexia",
    "texto_simplificado": "simplified",
    "simplified_text": "simplified",
    "high_contrast": "highContrast",
    "large_controls": "largeControls",
    "subtitles": "captions",
    "reduced_motion": "reducedMotion",
}


def _serialize_preferences(raw: Any) -> tuple[dict[str, bool], bool]:
    source = raw if isinstance(raw, Mapping) else {}
    result = DEFAULT_ACCESSIBILITY_PREFERENCES.copy()
    initialized = False
    # Read legacy aliases first so an explicit canonical value always wins,
    # independently of the JSON object's insertion order.
    for stored_key, value in source.items():
        alias_key = _ACCESSIBILITY_ALIASES.get(str(stored_key))
        if alias_key in result and isinstance(value, bool):
            result[alias_key] = value
            initialized = True
    for canonical_key in result:
        value = source.get(canonical_key)
        if isinstance(value, bool):
            result[canonical_key] = value
            initialized = True
    return result, initialized


def _parse_preferences_update(
    payload: Any,
) -> tuple[dict[str, bool] | None, tuple[Any, int] | None]:
    if not isinstance(payload, dict):
        return None, (
            jsonify(
                {
                    "reason_code": "invalid_json",
                    "error": "El cuerpo JSON no es valido.",
                }
            ),
            400,
        )

    normalized: dict[str, bool] = {}
    unknown: list[str] = []
    invalid: list[str] = []
    for raw_key, value in payload.items():
        key = _ACCESSIBILITY_ALIASES.get(str(raw_key), str(raw_key))
        if key not in DEFAULT_ACCESSIBILITY_PREFERENCES:
            unknown.append(str(raw_key))
            continue
        if not isinstance(value, bool):
            invalid.append(str(raw_key))
            continue
        normalized[key] = value

    if unknown or invalid or not normalized:
        return None, (
            jsonify(
                {
                    "reason_code": "invalid_accessibility_preferences",
                    "error": "Las preferencias de accesibilidad no son validas.",
                    "details": {
                        "unknown_fields": sorted(unknown),
                        "boolean_fields_required": sorted(invalid),
                        "allowed_fields": sorted(DEFAULT_ACCESSIBILITY_PREFERENCES),
                    },
                }
            ),
            400,
        )
    return normalized, None


def _save_preferences(target_user: User):
    update, error = _parse_preferences_update(request.get_json(silent=True))
    if error is not None:
        return error
    stored = (
        dict(target_user.accesibilidad)
        if isinstance(target_user.accesibilidad, dict)
        else {}
    )
    # Preserve auth/identity/employee metadata in this legacy JSON column, but
    # remove preference aliases once their canonical values are written.
    for legacy_key in _ACCESSIBILITY_ALIASES:
        stored.pop(legacy_key, None)
    stored.update(update or {})
    target_user.accesibilidad = stored
    db.session.commit()
    return _serialize_preferences(stored)[0]


def _contract_response(target_user: User):
    preferences, initialized = _serialize_preferences(target_user.accesibilidad)
    return {
        "contract_version": ACCESSIBILITY_CONTRACT_VERSION,
        "user_id": target_user.id,
        "initialized": initialized,
        "preferences": preferences,
    }


@accessibility_bp.route("/me", methods=["GET", "PUT", "OPTIONS"])
@token_requerido
def accessibility_preferences_me(auth_user: User):
    if request.method == "OPTIONS":
        return "", 204
    if request.method == "PUT":
        saved = _save_preferences(auth_user)
        if isinstance(saved, tuple):
            return saved
    return jsonify(_contract_response(auth_user))


@accessibility_bp.route("/<int:user_id>", methods=["GET", "PUT", "OPTIONS"])
@token_requerido
def accessibility_preferences(auth_user: User, user_id: int):
    if request.method == "OPTIONS":
        return "", 204

    target_user = db.session.get(User, user_id)
    if not target_user:
        return jsonify({"error": "User not found"}), 404
    if auth_user.id != target_user.id:
        return jsonify({"error": "Permisos insuficientes"}), 403

    if request.method == "GET":
        return jsonify(_serialize_preferences(target_user.accesibilidad)[0])

    saved = _save_preferences(target_user)
    if isinstance(saved, tuple):
        return saved
    return jsonify(saved)


@accessibility_bp.after_request
def apply_cors(response):
    origin = request.headers.get("Origin")
    if origin:
        response.headers.setdefault("Access-Control-Allow-Origin", origin)
        response.headers.setdefault("Vary", "Origin")
    else:
        response.headers.setdefault("Access-Control-Allow-Origin", "*")

    def _merge(header_name: str, values: list[str]):
        existing = response.headers.get(header_name, "")
        items = [h.strip() for h in existing.split(",") if h.strip()]
        for v in values:
            if v not in items:
                items.append(v)
        response.headers[header_name] = ",".join(items)

    _merge(
        "Access-Control-Allow-Headers",
        [
            "Content-Type",
            "Authorization",
            "Origin",
            "Accept",
            "X-Entity-Token",
            "X-Chat-Session-Id",
            "X-Anon-Id",
            "Anon-Id",
        ],
    )
    _merge("Access-Control-Allow-Methods", ["GET", "PUT", "OPTIONS"])
    response.headers.setdefault("Access-Control-Allow-Credentials", "true")
    return response

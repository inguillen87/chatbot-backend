"""REST endpoints that implement the Passkey/WebAuthn flows for the PWA."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import jwt
from flask import Blueprint, abort, current_app, g, jsonify, request, session
from flask_login import login_user

from extensions import db
from models import User, WebAuthnCredential
from services.user_merge import merge_anon_into_user
from utils.auth_helpers import get_or_create_anon_id
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

webauthn_bp = Blueprint("webauthn", __name__, url_prefix="/api/webauthn")


def _rp_id() -> str:
    return current_app.config.get("WEBAUTHN_RP_ID", request.host.split(":")[0])


def _expected_origin() -> str:
    default_origin = f"https://www.{_rp_id()}"
    return current_app.config.get("WEBAUTHN_EXPECTED_ORIGIN", default_origin)


def _options_to_dict(options: Any) -> Dict[str, Any]:
    """Serialize library option objects into JSON-ready dictionaries."""

    serialized = options_to_json(options)
    if isinstance(serialized, str):
        return json.loads(serialized)
    # Some versions may already return a dict
    return serialized


def _existing_credentials(user: User) -> List[PublicKeyCredentialDescriptor]:
    descriptors: List[PublicKeyCredentialDescriptor] = []
    for credential in getattr(user, "webauthn_credentials", []) or []:
        try:
            descriptors.append(
                PublicKeyCredentialDescriptor(
                    id=base64url_to_bytes(credential.credential_id)
                )
            )
        except Exception:
            current_app.logger.exception(
                "[webauthn] Failed to decode credential %s for exclusion list",
                credential.id,
            )
    return descriptors


def _issue_login_response(user: User, anon_id: Optional[str]):
    """Return a JSON response identical to the password-based login flow."""

    login_user(user)
    current_app.logger.info("[webauthn] Usuario %s autenticado vía Passkey", user.id)

    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    try:
        from services.logic import es_rubro_publico
    except Exception:
        es_rubro_publico = lambda *_args, **_kwargs: False  # type: ignore

    tipo_chat = getattr(user, "tipo_chat", None)
    if not tipo_chat:
        try:
            tipo_chat = "municipio" if es_rubro_publico(user.rubro) else "pyme"
        except Exception:
            tipo_chat = "pyme"

    expiration_days = current_app.config.get("JWT_EXPIRATION_DAYS", 7)
    jwt_payload = {
        "user_id": user.id,
        "exp": datetime.utcnow() + timedelta(days=expiration_days),
    }
    token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")

    response = jsonify(
        {
            "mensaje": "Login exitoso",
            "id": user.id,
            "token": token,
            "email": user.email,
            "name": user.name,
            "rol": user.rol,
            "empresa_id": user.empresa_id,
            "rubro": rubro_nombre,
            "tipo_chat": tipo_chat,
            "categorias": getattr(user, "categorias_lista", []),
        }
    )

    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    if token:
        cookie_args: Dict[str, Any] = {
            "key": cookie_name,
            "value": token,
            "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
            "httponly": True,
            "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        }
        cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
        if cookie_domain:
            cookie_args["domain"] = cookie_domain
        response.set_cookie(**cookie_args)

    if anon_id:
        response.headers.setdefault("X-Anon-Id", anon_id)
        response.headers.setdefault("Anon-Id", anon_id)

    return response


@webauthn_bp.get("/register/options")
def register_options() -> Any:
    anon_id = get_or_create_anon_id()
    display_name = request.args.get("display_name") or "Ciudadano"

    user = getattr(g, "viewer", None)
    if not isinstance(user, User):
        user = User.create_or_get_by_anon(anon_id, display_name)
        db.session.commit()
        g.viewer = user

    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=current_app.config.get("WEBAUTHN_RP_NAME", "Chatboc"),
        user_id=str(user.id),
        user_name=f"user-{user.id}",
        user_display_name=display_name,
        attestation="none",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_existing_credentials(user),
    )

    session["webauthn_challenge"] = bytes_to_base64url(options.challenge)
    session["webauthn_user_id"] = user.id

    payload = _options_to_dict(options)
    response = jsonify(payload)
    response.headers.setdefault("X-Anon-Id", anon_id)
    response.headers.setdefault("Anon-Id", anon_id)
    return response


@webauthn_bp.post("/register/verify")
def register_verify() -> Any:
    anon_id = get_or_create_anon_id()
    body = request.get_json(silent=True) or {}
    attestation = body.get("attestationResponse")
    if not isinstance(attestation, dict):
        abort(400, "attestationResponse required")

    expected_challenge = session.pop("webauthn_challenge", None)
    expected_user_id = session.pop("webauthn_user_id", None)
    if not expected_challenge or not expected_user_id:
        abort(400, "challenge expired")

    user = User.query.get(expected_user_id)
    if not user:
        abort(404, "Usuario no encontrado para el challenge activo")

    try:
        credential = RegistrationCredential.parse_obj(attestation)
    except Exception as exc:
        current_app.logger.exception("[webauthn] Invalid attestation payload", exc_info=True)
        abort(400, "attestationResponse inválida")

    verification = verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=_rp_id(),
        expected_origin=_expected_origin(),
        require_user_verification=True,
    )

    credential_id = bytes_to_base64url(verification.credential_id)
    public_key = bytes_to_base64url(verification.credential_public_key)

    if WebAuthnCredential.query.filter_by(credential_id=credential_id).first():
        abort(409, "La credencial ya está registrada")

    new_credential = WebAuthnCredential(
        user_id=user.id,
        credential_id=credential_id,
        public_key=public_key,
        sign_count=verification.sign_count or 0,
        transports=attestation.get("transports"),
    )
    db.session.add(new_credential)
    db.session.commit()

    merge_anon_into_user(anon_id, user)
    g.viewer = user

    response = jsonify({"ok": True})
    response.headers.setdefault("X-Anon-Id", anon_id)
    response.headers.setdefault("Anon-Id", anon_id)
    return response


@webauthn_bp.get("/login/options")
def login_options() -> Any:
    anon_id = get_or_create_anon_id()
    options = generate_authentication_options(
        rp_id=_rp_id(),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session["webauthn_challenge"] = bytes_to_base64url(options.challenge)

    payload = _options_to_dict(options)
    response = jsonify(payload)
    response.headers.setdefault("X-Anon-Id", anon_id)
    response.headers.setdefault("Anon-Id", anon_id)
    return response


@webauthn_bp.post("/login/verify")
def login_verify() -> Any:
    anon_id = get_or_create_anon_id()
    body = request.get_json(silent=True) or {}
    authentication = body.get("authenticationResponse")
    if not isinstance(authentication, dict):
        abort(400, "authenticationResponse required")

    expected_challenge = session.pop("webauthn_challenge", None)
    if not expected_challenge:
        abort(400, "challenge expired")

    try:
        credential = AuthenticationCredential.parse_obj(authentication)
    except Exception:
        abort(400, "authenticationResponse inválida")

    stored_credential = WebAuthnCredential.query.filter_by(
        credential_id=credential.id
    ).first()
    if not stored_credential:
        abort(404, "Credencial no registrada")

    verification = verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=_rp_id(),
        expected_origin=_expected_origin(),
        credential_public_key=base64url_to_bytes(stored_credential.public_key),
        credential_current_sign_count=stored_credential.sign_count,
        require_user_verification=True,
    )

    if verification.new_sign_count is not None:
        stored_credential.sign_count = verification.new_sign_count
        db.session.add(stored_credential)
    db.session.commit()

    user = stored_credential.user
    merge_anon_into_user(anon_id, user)
    g.viewer = user

    response = _issue_login_response(user, anon_id)
    return response


__all__: Iterable[str] = ["webauthn_bp"]

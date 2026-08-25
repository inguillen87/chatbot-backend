"""REST endpoints that implement the Passkey/WebAuthn flows for the PWA."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import jwt
from flask import Blueprint, abort, current_app, g, jsonify, request, session
from flask_login import login_user
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models import User, WebAuthnCredential
from services.user_merge import merge_anon_into_user
from utils.auth_helpers import (
    auth_tenant_for_user,
    is_clerk_managed_user,
    is_demo_user_account,
    is_user_auth_disabled,
    user_from_token,
    user_tenant_auth_allowed,
)
from utils.lazy_module import LazyModule
from utils.roles import is_super_admin_role


_webauthn = LazyModule("webauthn")
_webauthn_helpers = LazyModule("webauthn.helpers")
_webauthn_structs = LazyModule("webauthn.helpers.structs")


# Keep the established module-level seams used by tests and integrations while
# deferring the WebAuthn/asn1 parsing stack until a passkey ceremony actually
# starts. Importing it for every unrelated API cold start is both expensive and
# unnecessary.
def generate_authentication_options(*args, **kwargs):
    return _webauthn.generate_authentication_options(*args, **kwargs)


def generate_registration_options(*args, **kwargs):
    return _webauthn.generate_registration_options(*args, **kwargs)


def verify_authentication_response(*args, **kwargs):
    return _webauthn.verify_authentication_response(*args, **kwargs)


def verify_registration_response(*args, **kwargs):
    return _webauthn.verify_registration_response(*args, **kwargs)


def base64url_to_bytes(value):
    return _webauthn_helpers.base64url_to_bytes(value)


def bytes_to_base64url(value):
    return _webauthn_helpers.bytes_to_base64url(value)


def options_to_json(value):
    return _webauthn_helpers.options_to_json(value)

webauthn_bp = Blueprint("webauthn", __name__, url_prefix="/api/webauthn")

_SESSION_ANON_KEY = "webauthn_anon_id_v1"
_REGISTRATION_CHALLENGE_KEY = "webauthn_registration_challenge_v1"
_REGISTRATION_ISSUED_AT_KEY = "webauthn_registration_issued_at_v1"
_REGISTRATION_MODE_KEY = "webauthn_registration_mode_v1"
_REGISTRATION_USER_ID_KEY = "webauthn_registration_user_id_v1"
_REGISTRATION_HANDLE_KEY = "webauthn_registration_handle_v1"
_REGISTRATION_DISPLAY_KEY = "webauthn_registration_display_v1"
_AUTHENTICATION_CHALLENGE_KEY = "webauthn_authentication_challenge_v1"
_AUTHENTICATION_ISSUED_AT_KEY = "webauthn_authentication_issued_at_v1"
_DEFAULT_CHALLENGE_TTL_SECONDS = 120
_MAX_CREDENTIAL_ID_LENGTH = 255


def _session_bound_anon_id() -> str:
    """Return a server-issued identity bound to the server-managed Flask session.

    Passkey enrollment must never select a User from X-Anon-Id, query params or
    a JavaScript-readable cookie. Those values are correlation hints elsewhere,
    but accepting them here enabled account/session fixation.
    """

    value = str(session.get(_SESSION_ANON_KEY) or "").strip().lower()
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        value = secrets.token_hex(16)
        session[_SESSION_ANON_KEY] = value
    return value


def _registration_handle(user: User | None) -> bytes:
    if user is None:
        return secrets.token_bytes(32)
    # Avoid exposing the sequential database ID as the RP user handle while
    # keeping a stable handle when an authenticated account adds a passkey.
    return hashlib.sha256(
        f"chatboc.webauthn.user.v1:{user.id}".encode("utf-8")
    ).digest()


def _clear_registration_state() -> None:
    for key in (
        _REGISTRATION_CHALLENGE_KEY,
        _REGISTRATION_ISSUED_AT_KEY,
        _REGISTRATION_MODE_KEY,
        _REGISTRATION_USER_ID_KEY,
        _REGISTRATION_HANDLE_KEY,
        _REGISTRATION_DISPLAY_KEY,
    ):
        session.pop(key, None)


def _challenge_ttl_seconds() -> int:
    try:
        configured = int(
            current_app.config.get(
                "WEBAUTHN_CHALLENGE_TTL_SECONDS",
                _DEFAULT_CHALLENGE_TTL_SECONDS,
            )
        )
    except (TypeError, ValueError):
        configured = _DEFAULT_CHALLENGE_TTL_SECONDS
    # A stale WebAuthn ceremony should not remain usable for the lifetime of
    # the Flask session. Keep the deployment override within a bounded window.
    return min(max(configured, 30), 600)


def _consume_challenge(challenge_key: str, issued_at_key: str) -> Optional[str]:
    """Consume request-local challenge state from the server-backed session."""

    challenge = session.pop(challenge_key, None)
    issued_at = session.pop(issued_at_key, None)
    if not isinstance(challenge, str) or not challenge or issued_at is None:
        return None
    try:
        age_seconds = time.time() - float(issued_at)
    except (TypeError, ValueError, OverflowError):
        return None
    if age_seconds < -5 or age_seconds > _challenge_ttl_seconds():
        return None
    return challenge


def _passkey_account_allowed(user: User | None) -> bool:
    """Mirror the password/Flask-Login provider and account-state gates."""

    return bool(
        isinstance(user, User)
        and not is_user_auth_disabled(user)
        and not is_demo_user_account(user)
        and not is_clerk_managed_user(user)
        and not is_super_admin_role(getattr(user, "rol", None))
        and user_tenant_auth_allowed(user)
    )


def _registration_viewer() -> User | None:
    """Resolve enrollment identity with explicit Bearer precedence.

    The application-wide viewer hook historically preferred an ambient
    Flask-Login cookie. For credential enrollment, an explicit Bearer must be
    authoritative and an invalid Bearer must never fall through to that cookie.
    """

    authorization = str(request.headers.get("Authorization") or "").strip()
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            abort(401, "Autenticación inválida")
        user = user_from_token(token.strip())
        if not _passkey_account_allowed(user):
            abort(401, "Autenticación inválida")
        return user

    viewer = getattr(g, "viewer", None)
    if isinstance(viewer, User):
        if not _passkey_account_allowed(viewer):
            abort(401, "Autenticación inválida")
        return viewer
    return None


def _new_provisional_user(anon_id: str, display_name: str) -> User:
    """Create a fresh passkey-first user only after attestation succeeds."""

    user = User(
        name=display_name,
        email=f"anon-{secrets.token_hex(16)}@passkey.chatboc",
        token=secrets.token_urlsafe(32),
        anon_id=anon_id,
        rol="usuario",
    )
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(user)
    db.session.flush()
    return user


def _rp_id() -> str:
    raw_rp_id = str(current_app.config.get("WEBAUTHN_RP_ID") or "").strip()
    try:
        rp_id = raw_rp_id.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        rp_id = ""
    labels = rp_id.split(".") if rp_id else []
    if (
        not rp_id
        or len(rp_id) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        )
    ):
        current_app.logger.error("[webauthn] Invalid or missing RP ID configuration")
        abort(503, "WebAuthn no está configurado")
    return rp_id


def _expected_origins() -> tuple[str, ...]:
    raw_origins = current_app.config.get("WEBAUTHN_EXPECTED_ORIGIN")
    if isinstance(raw_origins, str):
        candidates = raw_origins.split(",")
    elif isinstance(raw_origins, (list, tuple, set)):
        candidates = list(raw_origins)
    else:
        candidates = [raw_origins]
    rp_id = _rp_id()
    origins: list[str] = []
    for raw_origin in candidates:
        value = str(raw_origin or "").strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            host = str(parsed.hostname or "").rstrip(".").lower()
            parsed.port
        except ValueError:
            current_app.logger.error("[webauthn] Invalid expected-origin configuration")
            abort(503, "WebAuthn no está configurado")
        local_http = parsed.scheme == "http" and host in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.scheme != "https" and not local_http)
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not (host == rp_id or host.endswith(f".{rp_id}"))
        ):
            current_app.logger.error("[webauthn] Invalid expected-origin configuration")
            abort(503, "WebAuthn no está configurado")
        origins.append(value)
    unique_origins = tuple(dict.fromkeys(origins))
    if not unique_origins:
        current_app.logger.error("[webauthn] Missing expected-origin configuration")
        abort(503, "WebAuthn no está configurado")
    return unique_origins


def _expected_origin() -> str | List[str]:
    origins = _expected_origins()
    return origins[0] if len(origins) == 1 else list(origins)


def _require_browser_origin() -> None:
    """Reject browser cross-site requests outside the signed WebAuthn origin.

    Missing Origin remains valid for same-origin and non-browser clients. A
    cross-site Fetch-Metadata request without Origin is rejected so an image or
    form cannot silently rotate a victim's challenge.
    """

    origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
    if origin and origin not in _expected_origins():
        abort(403, "Origen no permitido")
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if not origin and fetch_site == "cross-site":
        abort(403, "Origen no permitido")


def _registration_transports(attestation: Dict[str, Any]) -> Optional[List[str]]:
    response = attestation.get("response")
    raw_transports = response.get("transports") if isinstance(response, dict) else None
    if raw_transports is None:
        raw_transports = attestation.get("transports")
    if not isinstance(raw_transports, list):
        return None
    transports: List[str] = []
    for raw_transport in raw_transports[:8]:
        transport = str(raw_transport or "").strip().lower()
        if transport and len(transport) <= 32 and transport not in transports:
            transports.append(transport)
    return transports or None


def _validated_sign_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        abort(400, "Respuesta WebAuthn inválida")
    return value


def _rotate_session_for_login() -> None:
    """Rotate the server-side SID before installing authenticated identity."""

    regenerate = getattr(current_app.session_interface, "regenerate", None)
    if not callable(regenerate):
        current_app.logger.error("[webauthn] Session backend cannot rotate session IDs")
        abort(503, "No se pudo completar la autenticación")
    try:
        regenerate(session)
    except Exception:
        current_app.logger.exception("[webauthn] Session ID rotation failed")
        abort(503, "No se pudo completar la autenticación")


def _options_to_dict(options: Any) -> Dict[str, Any]:
    """Serialize library option objects into JSON-ready dictionaries."""

    serialized = options_to_json(options)
    if isinstance(serialized, str):
        return json.loads(serialized)
    # Some versions may already return a dict
    return serialized


def _existing_credentials(user: User) -> List[Any]:
    descriptors: List[Any] = []
    for credential in getattr(user, "webauthn_credentials", []) or []:
        try:
            descriptors.append(
                _webauthn_structs.PublicKeyCredentialDescriptor(
                    id=base64url_to_bytes(credential.credential_id)
                )
            )
        except Exception:
            current_app.logger.exception(
                "[webauthn] Failed to decode credential %s for exclusion list",
                credential.id,
            )
    return descriptors


def _issue_login_response(
    user: User,
    anon_id: Optional[str],
    *,
    extra_payload: Optional[Dict[str, Any]] = None,
):
    """Return a JSON response identical to the password-based login flow."""

    if not _passkey_account_allowed(user):
        abort(401, "No se pudo autenticar la cuenta")

    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    tenant = None
    try:
        tenant = auth_tenant_for_user(user)
    except Exception:
        current_app.logger.exception(
            "[webauthn] Failed to resolve tenant for authenticated user_id=%s",
            user.id,
        )
    tenant_slug = getattr(tenant, "slug", None) if tenant is not None else None
    try:
        from services.rubro_classification import es_rubro_publico
    except Exception:
        es_rubro_publico = lambda *_args, **_kwargs: False  # type: ignore

    tipo_chat = getattr(tenant, "tipo", None) or getattr(user, "tipo_chat", None)
    if not tipo_chat:
        try:
            tipo_chat = "municipio" if es_rubro_publico(user.rubro) else "pyme"
        except Exception:
            tipo_chat = "pyme"

    expiration_days = current_app.config.get("JWT_EXPIRATION_DAYS", 7)
    jwt_payload = {
        "user_id": user.id,
        "exp": datetime.now(timezone.utc) + timedelta(days=expiration_days),
    }
    token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")

    payload = {
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": token,
        "email": user.email,
        "name": user.name,
        "rol": user.rol,
        "role": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": getattr(user, "categorias_lista", []),
        "tenant_slug": tenant_slug,
        "tenantSlug": tenant_slug,
        "tenant_id": getattr(tenant, "id", None),
    }
    # Frontend passkey consumers use the nested identity contract, while the
    # established flat fields above remain for backward compatibility. Never
    # include entity/widget integration credentials in this login payload.
    payload["user"] = {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "rol": user.rol,
        "role": user.rol,
        "empresa_id": user.empresa_id,
        "tipo_chat": tipo_chat,
        "tenant_slug": tenant_slug,
        "tenantSlug": tenant_slug,
        "tenant_id": getattr(tenant, "id", None),
    }
    if extra_payload:
        payload.update(extra_payload)
    response = jsonify(payload)

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

    # Establish the Flask-Login session only after all payload construction and
    # serialization succeeded, so a 500 while building the response cannot
    # leave a partially authenticated browser session behind.
    _rotate_session_for_login()
    if not login_user(user):
        abort(401, "No se pudo autenticar la cuenta")
    current_app.logger.info("[webauthn] Usuario %s autenticado vía Passkey", user.id)

    return response


@webauthn_bp.get("/register/options")
def register_options() -> Any:
    _require_browser_origin()
    anon_id = _session_bound_anon_id()
    display_name = str(request.args.get("display_name") or "Ciudadano").strip()[:100]
    display_name = display_name or "Ciudadano"

    user = _registration_viewer()
    authenticated = user is not None

    _clear_registration_state()
    user_handle = _registration_handle(user)
    user_handle_b64 = bytes_to_base64url(user_handle)

    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=current_app.config.get("WEBAUTHN_RP_NAME", "Chatboc"),
        user_id=user_handle,
        user_name=(
            f"user-{user.id}" if user is not None else f"passkey-{user_handle_b64[:12]}"
        ),
        user_display_name=display_name,
        attestation=_webauthn_structs.AttestationConveyancePreference.NONE,
        authenticator_selection=_webauthn_structs.AuthenticatorSelectionCriteria(
            resident_key=_webauthn_structs.ResidentKeyRequirement.PREFERRED,
            user_verification=_webauthn_structs.UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_existing_credentials(user) if user is not None else [],
    )

    session[_REGISTRATION_CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    session[_REGISTRATION_ISSUED_AT_KEY] = time.time()
    session[_REGISTRATION_MODE_KEY] = "authenticated" if authenticated else "provisional"
    session[_REGISTRATION_USER_ID_KEY] = user.id if user is not None else None
    session[_REGISTRATION_HANDLE_KEY] = user_handle_b64
    session[_REGISTRATION_DISPLAY_KEY] = display_name

    payload = _options_to_dict(options)
    response = jsonify(payload)
    response.headers.setdefault("X-Anon-Id", anon_id)
    response.headers.setdefault("Anon-Id", anon_id)
    return response


@webauthn_bp.post("/register/verify")
def register_verify() -> Any:
    _require_browser_origin()
    anon_id = _session_bound_anon_id()
    body = request.get_json(silent=True) or {}
    attestation = body.get("attestationResponse")
    if not isinstance(attestation, dict):
        abort(400, "attestationResponse required")

    expected_challenge = _consume_challenge(
        _REGISTRATION_CHALLENGE_KEY,
        _REGISTRATION_ISSUED_AT_KEY,
    )
    registration_mode = session.pop(_REGISTRATION_MODE_KEY, None)
    expected_user_id = session.pop(_REGISTRATION_USER_ID_KEY, None)
    expected_handle = session.pop(_REGISTRATION_HANDLE_KEY, None)
    display_name = str(session.pop(_REGISTRATION_DISPLAY_KEY, None) or "Ciudadano")
    if (
        not expected_challenge
        or registration_mode not in {"authenticated", "provisional"}
        or not expected_handle
    ):
        abort(400, "challenge expired")

    viewer = _registration_viewer()
    user: User | None = None
    if registration_mode == "authenticated":
        if not isinstance(viewer, User) or int(viewer.id) != int(expected_user_id or 0):
            abort(401, "La sesión autenticada cambió durante el registro")
        try:
            user = db.session.get(User, expected_user_id)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("[webauthn] Enrollment user lookup failed")
            abort(503, "No se pudo completar el registro")
        if user is None:
            abort(404, "Usuario no encontrado para el challenge activo")
    elif viewer is not None:
        # Do not let an anonymous ceremony started before login silently switch
        # an authenticated browser into a new provisional account.
        abort(401, "La sesión autenticada cambió durante el registro")

    try:
        # webauthn>=2 accepts the browser JSON object directly. Passing a
        # Pydantic-style ``parse_obj`` result broke against the real installed
        # dataclass-based library while the old test stub silently accepted it.
        verification = verify_registration_response(
            credential=attestation,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
            require_user_verification=True,
        )
    except Exception as exc:
        current_app.logger.info(
            "[webauthn] Registration attestation rejected error_type=%s",
            type(exc).__name__,
        )
        abort(400, "No se pudo verificar la credencial")

    credential_id = bytes_to_base64url(verification.credential_id)
    public_key = bytes_to_base64url(verification.credential_public_key)
    sign_count = _validated_sign_count(verification.sign_count)
    if (
        not credential_id
        or len(credential_id) > _MAX_CREDENTIAL_ID_LENGTH
        or not public_key
    ):
        abort(400, "Respuesta WebAuthn inválida")

    try:
        credential_exists = WebAuthnCredential.query.filter_by(
            credential_id=credential_id
        ).first()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("[webauthn] Credential lookup failed")
        abort(503, "No se pudo completar el registro")
    if credential_exists:
        abort(409, "La credencial ya está registrada")

    try:
        if registration_mode == "provisional":
            user = _new_provisional_user(anon_id, display_name)
        if user is None:  # pragma: no cover - guarded by registration_mode above
            abort(409, "No se pudo resolver la identidad de registro")

        new_credential = WebAuthnCredential(
            user_id=user.id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=_registration_transports(attestation),
        )
        db.session.add(new_credential)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "La credencial ya está registrada")
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("[webauthn] Registration persistence failed")
        abort(503, "No se pudo guardar la credencial")

    try:
        merge_anon_into_user(anon_id, user)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "[webauthn] Passkey created but anonymous merge failed user_id=%s",
            user.id,
        )
    g.viewer = user

    return _issue_login_response(
        user,
        anon_id,
        extra_payload={
            "ok": True,
            "registration": "completed",
            "account_state": (
                "provisional" if registration_mode == "provisional" else "authenticated"
            ),
        },
    )


@webauthn_bp.get("/login/options")
def login_options() -> Any:
    _require_browser_origin()
    anon_id = _session_bound_anon_id()
    options = generate_authentication_options(
        rp_id=_rp_id(),
        user_verification=_webauthn_structs.UserVerificationRequirement.REQUIRED,
    )
    session[_AUTHENTICATION_CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    session[_AUTHENTICATION_ISSUED_AT_KEY] = time.time()

    payload = _options_to_dict(options)
    response = jsonify(payload)
    response.headers.setdefault("X-Anon-Id", anon_id)
    response.headers.setdefault("Anon-Id", anon_id)
    return response


@webauthn_bp.post("/login/verify")
def login_verify() -> Any:
    _require_browser_origin()
    anon_id = _session_bound_anon_id()
    body = request.get_json(silent=True) or {}
    authentication = body.get("authenticationResponse")
    if not isinstance(authentication, dict):
        abort(400, "authenticationResponse required")

    expected_challenge = _consume_challenge(
        _AUTHENTICATION_CHALLENGE_KEY,
        _AUTHENTICATION_ISSUED_AT_KEY,
    )
    if not expected_challenge:
        abort(400, "challenge expired")

    credential_id = str(authentication.get("id") or "").strip()
    if not credential_id or len(credential_id) > _MAX_CREDENTIAL_ID_LENGTH:
        abort(400, "authenticationResponse inválida")

    try:
        stored_credential = WebAuthnCredential.query.filter_by(
            credential_id=credential_id
        ).first()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("[webauthn] Authentication credential lookup failed")
        abort(503, "No se pudo completar la autenticación")
    if not stored_credential:
        abort(401, "No se pudo verificar la credencial")

    try:
        verification = verify_authentication_response(
            credential=authentication,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
            credential_public_key=base64url_to_bytes(stored_credential.public_key),
            credential_current_sign_count=stored_credential.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        credential_ref = hashlib.sha256(credential_id.encode("utf-8")).hexdigest()[:12]
        current_app.logger.info(
            "[webauthn] Authentication assertion rejected credential_ref=%s error_type=%s",
            credential_ref,
            type(exc).__name__,
        )
        abort(401, "No se pudo verificar la credencial")

    try:
        user = stored_credential.user
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("[webauthn] Credential owner lookup failed")
        abort(503, "No se pudo completar la autenticación")
    if not _passkey_account_allowed(user):
        abort(401, "No se pudo verificar la credencial")
    new_sign_count = _validated_sign_count(verification.new_sign_count)
    if new_sign_count is not None and new_sign_count != stored_credential.sign_count:
        try:
            updated = WebAuthnCredential.query.filter(
                WebAuthnCredential.id == stored_credential.id,
                WebAuthnCredential.sign_count == stored_credential.sign_count,
            ).update(
                {"sign_count": new_sign_count},
                synchronize_session=False,
            )
            if updated != 1:
                db.session.rollback()
                abort(409, "La credencial cambió durante la autenticación")
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("[webauthn] Counter update failed")
            abort(503, "No se pudo completar la autenticación")

    try:
        merge_anon_into_user(anon_id, user)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "[webauthn] Login succeeded but anonymous merge failed user_id=%s",
            user.id,
        )
    g.viewer = user

    response = _issue_login_response(user, anon_id)
    return response


__all__: Iterable[str] = ["webauthn_bp"]

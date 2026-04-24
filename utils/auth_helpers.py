import uuid
from functools import wraps
from typing import Any, Dict, List, Optional, Set, Tuple

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from flask import current_app, g, jsonify, make_response, request
from flask_login import current_user
import jwt
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import Rubro, TenantProfile, User
import secrets
from services.demo_registry import demo_rubro_for_token
from utils.user_query import _safe_user_query


_WIDGET_ALLOWED_PREFIXES: Tuple[str, ...] = (
    "/auth/widget/",
    "/api/market/",
    "/market/",
    "/api/ask",
    "/api/profile-name",
    "/api/widget",
    "/api/live-chat",
    "/api/pwa/tenant-info",
    "/api/pwa/anon-id",
    "/api/public/tenants",
)

_WIDGET_ALLOWED_GET_PATHS: Set[str] = {
    "/auth/me",
    "/auth/perfil",
    "/auth/profile",
    "/auth/token-info",
    "/me",
    "/perfil",
    "/profile",
    "/api/me",
    "/api/perfil",
    "/api/profile",
    "/pwa/tenant-info",
    "/api/pwa/tenant-info",
    "/public/tenant",
    "/api/public/tenant",
    "/api/public/tenant-profile",
    "/notifications",
    "/api/notifications",
    "/widget/attention",
    "/widget/config",
    "/live-chat/schedule",
}

_WIDGET_ALLOWED_ANY_METHOD_PATHS: Set[str] = {
    "/ask",
    "/ask/pyme",
    "/ask/municipio",
    "/api/ask",
    "/api/ask/pyme",
    "/api/ask/municipio",
}

_DEMO_TOKEN_WARNED: Set[str] = set()


def _normalize_path(path: Optional[str]) -> str:
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


def _is_jwt_token(token: Optional[str]) -> bool:
    """Return True if the token string looks like a JWT."""

    if not token or not isinstance(token, str):
        return False
    return token.count(".") == 2


def _widget_session_allowed(path: Optional[str], method: Optional[str]) -> bool:
    """Return True if a widget session token can access the given request."""

    normalized_path = _normalize_path(path)
    method = (method or "GET").upper()

    if method == "OPTIONS":
        return True

    if normalized_path in _WIDGET_ALLOWED_ANY_METHOD_PATHS:
        return True

    for prefix in _WIDGET_ALLOWED_PREFIXES:
        if normalized_path.startswith(prefix.rstrip("/")):
            return True

    if method == "GET" and normalized_path in _WIDGET_ALLOWED_GET_PATHS:
        return True

    return False


def _normalize_alias_value(value: Optional[object]) -> Optional[str]:
    """Normalize a string value into a slug-ish token."""

    if value is None:
        return None

    text = str(value).strip().lower()
    if not text:
        return None

    decomposed = unicodedata.normalize("NFKD", text)
    sanitized = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    sanitized = re.sub(r"[^a-z0-9]+", "_", sanitized)
    sanitized = sanitized.strip("_")
    return sanitized or None


def _rubro_aliases(rubro: Rubro) -> Set[str]:
    """Return the normalized aliases associated with a Rubro."""

    aliases: Set[str] = set()

    for value in (getattr(rubro, "clave", None), getattr(rubro, "nombre", None)):
        normalized = _normalize_alias_value(value)
        if not normalized:
            continue
        aliases.add(normalized)
        collapsed = normalized.replace("_", "")
        if collapsed:
            aliases.add(collapsed)
        parts = [part for part in normalized.split("_") if part]
        aliases.update(parts)

    return {alias for alias in aliases if alias}


def _demo_token_fallback_owner(token: Optional[str]) -> Optional[User]:
    """Attempt to resolve demo tokens even if the registry is misconfigured."""

    if not token:
        return None

    user_query = _safe_user_query()
    normalized_token = _normalize_alias_value(token)
    if not normalized_token:
        # Fallback to simple strip/lower if normalization fails (shouldn't happen)
        normalized_token = token.strip().lower()

    slug = normalized_token
    for prefix in (
        "demo_token_",
        "demoanon_",
        "demo_anon_",
        "demo_anon",
        "demo_",
        "demo",
    ):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :].strip("_")
            break

    tokens = [part for part in slug.split("_") if part] if slug else []
    slug_variants: Set[str] = set(tokens)
    if slug:
        slug_variants.add(slug)
        collapsed = slug.replace("_", "")
        if collapsed:
            slug_variants.add(collapsed)

    tipo_hints: List[str] = []

    request_path = (getattr(request, "path", "") or "").lower()
    if "municipio" in request_path:
        tipo_hints.append("municipio")
    if any(keyword in request_path for keyword in ("pyme", "comercio", "empresa")):
        tipo_hints.append("pyme")

    if not tokens or tokens == ["anon"]:
        if "municipio" not in tipo_hints:
            tipo_hints.append("municipio")

    for part in tokens:
        if part in {"muni", "municipio", "ciudad", "vecino", "gobierno", "anon"}:
            if "municipio" not in tipo_hints:
                tipo_hints.append("municipio")
        if part in {"pyme", "comercio", "tienda", "negocio", "empresa"}:
            if "pyme" not in tipo_hints:
                tipo_hints.append("pyme")

    for tipo in tipo_hints:
        owner = (
            User.query.filter_by(tipo_chat=tipo, rol="admin").order_by(User.id.asc()).first()
            or User.query.filter_by(tipo_chat=tipo).order_by(User.id.asc()).first()
        )
        if owner:
            current_app.logger.info(
                "[auth] Resolved demo token '%s' to owner %s using tipo hint '%s'",
                token,
                owner.id,
                tipo,
            )
            return owner

    if slug_variants:
        try:
            rubros = Rubro.query.all()
        except Exception:
            rubros = []

        for rubro in rubros:
            aliases = _rubro_aliases(rubro)
            if not aliases:
                continue
            if slug_variants.intersection(aliases):
                owner = (
                    user_query.filter_by(rubro_id=rubro.id, rol="admin")
                    .order_by(User.id.asc())
                    .first()
                    or user_query.filter_by(rubro_id=rubro.id)
                    .order_by(User.id.asc())
                    .first()
                )
                if owner:
                    current_app.logger.info(
                        "[auth] Resolved demo token '%s' to owner %s via rubro %s",
                        token,
                        owner.id,
                        rubro.id,
                    )
                    return owner

    generic_owner = (
        user_query.filter_by(tipo_chat="municipio", rol="admin").order_by(User.id.asc()).first()
        or user_query.filter_by(tipo_chat="municipio").order_by(User.id.asc()).first()
        or user_query.filter_by(rol="admin").order_by(User.id.asc()).first()
    )

    if generic_owner and token not in _DEMO_TOKEN_WARNED:
        _DEMO_TOKEN_WARNED.add(token)
        current_app.logger.warning(
            "[auth] Using generic owner %s for demo token '%s' due to missing registry data.",
            generic_owner.id,
            token,
        )

    return generic_owner


def _lookup_owner_for_static_token(token: Optional[str]) -> Optional[User]:
    """Return the owner user associated with a static/demo token."""

    if not token:
        return None

    user_query = _safe_user_query()

    try:
        owner = user_query.filter_by(token=token).first()
    except SQLAlchemyError as exc:
        # Handle potential schema drift gracefully to avoid 500s in production.
        current_app.logger.error(
            "[auth] Failed to resolve user by token due to database schema mismatch.",
            exc_info=exc,
        )
        return None

    if owner:
        return owner

    tenant = (
        TenantProfile.query.filter(
            TenantProfile.configuracion["widget_tokens"].astext.contains(token)  # type: ignore[index]
        )
        .limit(1)
        .first()
    )

    if tenant:
        resolved_owner = tenant.municipio or tenant.pyme
        if resolved_owner:
            current_app.logger.info(
                "[auth] Resolved widget token to tenant %s owner %s", tenant.id, resolved_owner.id
            )
            return resolved_owner

    try:
        demo_entry = demo_rubro_for_token(token)
    except Exception:
        demo_entry = None

    if demo_entry:
        if demo_entry.owner_user_id:
            owner = user_query.get(demo_entry.owner_user_id)
            if owner:
                return owner

        owner_candidate: Optional[User] = None

        if demo_entry.rubro_id:
            owner_candidate = (
                user_query.filter_by(rubro_id=demo_entry.rubro_id, rol="admin")
                .order_by(User.id.asc())
                .first()
            )

        if not owner_candidate and demo_entry.rubro_clave:
            rubro = Rubro.query.filter_by(clave=demo_entry.rubro_clave).first()
            if rubro:
                owner_candidate = (
                    user_query.filter_by(rubro_id=rubro.id, rol="admin")
                    .order_by(User.id.asc())
                    .first()
                )

        tipo_chat = (demo_entry.tipo_chat or "").strip().lower()
        if not owner_candidate and tipo_chat:
            owner_candidate = (
                user_query.filter_by(tipo_chat=tipo_chat, rol="admin")
                .order_by(User.id.asc())
                .first()
            )
            if not owner_candidate:
                owner_candidate = (
                    user_query.filter_by(tipo_chat=tipo_chat)
                    .order_by(User.id.asc())
                    .first()
                )

        if owner_candidate:
            return owner_candidate

    fallback_owner = _demo_token_fallback_owner(token)
    if fallback_owner:
        return fallback_owner

    return None


def _ensure_entity_token(owner_user: Optional[User]) -> None:
    """Ensure the given owner has a persistent entity token assigned."""

    if not owner_user:
        return

    token_value = getattr(owner_user, "entity_token", None)
    if token_value and not _is_jwt_token(token_value):
        return

    try:
        owner_user.entity_token = secrets.token_urlsafe(32)
        db.session.add(owner_user)
        db.session.commit()
    except Exception:
        current_app.logger.exception(
            "[auth_helpers] Failed to ensure entity token for owner %s",
            getattr(owner_user, "id", None),
        )
        db.session.rollback()


def get_or_create_entity_token(user: Optional[User]) -> Optional[str]:
    """Return a stable entity token for the owning account."""

    owner_user = _resolve_owner_user(user)
    if not owner_user:
        return None

    token_value = getattr(owner_user, "entity_token", None)
    if token_value and not _is_jwt_token(token_value):
        return token_value

    try:
        owner_user.entity_token = secrets.token_urlsafe(32)
        db.session.add(owner_user)
        db.session.commit()
        db.session.refresh(owner_user)
        return owner_user.entity_token
    except Exception:
        current_app.logger.exception(
            "[auth_helpers] Failed to generate or persist entity token for owner %s",
            getattr(owner_user, "id", None),
        )
        db.session.rollback()
        return None


def get_or_create_owner_entity_token(user: Optional[User]) -> Optional[str]:
    """Return the persistent entity token for the owner's account."""

    if not user:
        return None

    owner_user = _resolve_owner_user(user)
    if not owner_user:
        return None

    token_value = getattr(owner_user, "entity_token", None)
    if token_value and not _is_jwt_token(token_value):
        return token_value

    legacy_value = getattr(owner_user, "token", None)
    if legacy_value and not _is_jwt_token(legacy_value):
        try:
            owner_user.entity_token = legacy_value
            db.session.add(owner_user)
            db.session.commit()
            return legacy_value
        except Exception:
            current_app.logger.exception(
                "[auth_helpers] Failed to persist legacy token as entity token for owner %s",
                getattr(owner_user, "id", None),
            )
            db.session.rollback()

    return get_or_create_entity_token(owner_user)


def _generate_widget_session_token(owner_user: User) -> Tuple[str, Dict[str, Any]]:
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


def _decode_token_payload(token: Optional[str]) -> dict:
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
    token_candidate: Optional[str], static_owner: Optional[User] = None
) -> Optional[str]:
    """Return the preferred token candidate for the current request.

    When a static entity token is provided alongside an existing widget cookie,
    prefer the cookie only if it belongs to the same owner and is still valid so
    stale cookies do not leak between entities or prevent renewals.
    """

    if not token_candidate:
        return None

    token_candidate = token_candidate.strip()
    if token_candidate and not _is_jwt_token(token_candidate):
        widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
        if widget_cookie_name:
            cookie_value = request.cookies.get(widget_cookie_name)
            if _is_jwt_token(cookie_value):
                return cookie_value.strip()

    return token_candidate or None


def obtener_entity_token() -> Optional[str]:
    """Extrae un token de entidad explícito sin interferir con JWT del usuario.

    Esto permite que el widget envíe simultáneamente el JWT del visitante
    (para autenticarlo) y el token de la entidad/tenant que define el contexto
    del bot. Si se encuentra, se devuelve el primer candidato válido respetando
    la misma lógica de _finalize_token_candidate.
    """

    def _extract_from_json(keys: tuple[str, ...]) -> list[str]:
        if not request.is_json:
            return []
        data = request.get_json(silent=True) or {}
        return [data.get(key) for key in keys if data.get(key)]

    def _extract_from_form(keys: tuple[str, ...]) -> list[str]:
        if not request.form:
            return []
        return [request.form.get(key) for key in keys if request.form.get(key)]

    candidate_sources: list[Optional[str]] = []

    # Headers take priority because the widget can set token context explicitly.
    candidate_sources.append(request.headers.get("X-Entity-Token"))
    candidate_sources.append(request.headers.get("X-Owner-Token"))
    candidate_sources.append(request.headers.get("X-Widget-Token"))

    # Cookies (ej. WIDGET_TOKEN_COOKIE_NAME) are next, useful after an iframe load.
    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
    if widget_cookie_name:
        candidate_sources.append(request.cookies.get(widget_cookie_name))

    # Query params and body fallbacks used by legacy + new widget contracts.
    entity_keys = (
        "entityToken",
        "entity_token",
        "empresa_token",
        "ownerToken",
        "owner_token",
        "widgetToken",
        "widget_token",
        "token",
    )
    for key in entity_keys:
        candidate_sources.append(request.args.get(key))
    candidate_sources.extend(_extract_from_json(entity_keys))
    candidate_sources.extend(_extract_from_form(entity_keys))

    for raw_candidate in candidate_sources:
        finalized = _finalize_token_candidate(raw_candidate)
        if finalized:
            return finalized

    return None

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

def user_from_token(token: str) -> Optional[User]:
    """
    Busca un usuario a partir de un token de autenticación JWT.
    """
    if not token or not _is_jwt_token(token):
        current_app.logger.debug("[user_from_token] Token no JWT recibido, se ignora.")
        return None
    try:
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
        user_id = payload.get('user_id')
        current_app.logger.debug("[user_from_token] Decoded payload, user_id: %s", user_id)
        if not user_id:
            current_app.logger.warning(f"[user_from_token] No user_id in payload: {payload}")
            return None
        user = User.query.get(user_id)
        current_app.logger.debug("[user_from_token] Found user: %s", user.email if user else "None")
        return user
    except jwt.ExpiredSignatureError as e:
        current_app.logger.warning(f"[user_from_token] Expired JWT token: {e}")
        return None
    except jwt.InvalidTokenError as e:
        current_app.logger.warning(f"[user_from_token] Invalid JWT token: {e}")
        return None
    except Exception as e:
        current_app.logger.error(f"[user_from_token] Unexpected error decoding token: {e}", exc_info=True)
        return None


def _resolve_owner_user(user: Optional[User]) -> Optional[User]:
    """Return the owner entity for a given authenticated user.

    Employees store the company owner ID in empresa_id. Administrators (for
    both pymes and municipios) have empresa_id set to None so the owner
    is the user itself. This helper centralises the lookup so other modules can
    rely on g.owner_user being populated consistently.
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

    # Security Fix: Prevent public widget from reading auth_token cookie
    # If Origin is public landing (chatboc.ar) OR request has entityToken (widget mode),
    # strictly ignore auth_token cookie.
    origin = request.headers.get("Origin", "").lower()
    is_public_landing = "chatboc.ar" in origin and "app.chatboc.ar" not in origin

    # Check if request has explicit entityToken (widget context)
    has_entity_token = (
        request.args.get("entityToken")
        or request.headers.get("X-Entity-Token")
        or (request.is_json and (request.get_json(silent=True) or {}).get("entityToken"))
    )

    allow_cookie_auth = True
    if is_public_landing or has_entity_token:
        # Check if we are in a protected app/admin route where cookies might still be needed
        # But if it's the public widget endpoints, force disable cookie auth
        path_lower = request.path.lower()
        if (
            path_lower.startswith("/api/ask")
            or path_lower.startswith("/api/public")
            or path_lower.startswith("/api/widget")
            or path_lower.startswith("/api/pwa")
        ):
            allow_cookie_auth = False
            current_app.logger.info(f"[obtener_token] Cookie auth DISABLED for public/widget request. Origin: {origin}, Path: {request.path}")


    checked_static_tokens: Dict[str, Optional[User]] = {}

    def _resolve_static_owner(token: str) -> Optional[User]:
        if token not in checked_static_tokens:
            checked_static_tokens[token] = _lookup_owner_for_static_token(token)
        owner = checked_static_tokens[token]
        if owner and getattr(g, "_obtener_token_owner", None) is None:
            g._obtener_token_owner = owner
        return owner

    candidates: List[Tuple[str, Optional[User], str]] = []

    def _register_candidate(raw_token: Optional[str], source: str):
        if not raw_token:
            return
        token_value = raw_token.strip()
        if not token_value:
            return
        owner: Optional[User] = None
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
        current_app.logger.debug(f"[obtener_token] Found Authorization header: '{auth_header[:30]}...'")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1]
            current_app.logger.debug(f"[obtener_token] Extracted Bearer token: '{token[:10]}...'")
            return _finalize_token_candidate(token)
        current_app.logger.debug(f"[obtener_token] Returning raw Authorization header as token: '{auth_header[:10]}...'")
        return _finalize_token_candidate(auth_header)

    token_x_token = request.headers.get("X-Token")
    if token_x_token:
        token_x_token = token_x_token.strip()
        current_app.logger.debug(f"[obtener_token] Found X-Token header: '{token_x_token[:10]}...'")
        return _finalize_token_candidate(token_x_token)

    token_x_entity_token = request.headers.get("X-Entity-Token")
    if token_x_entity_token:
        token_x_entity_token = token_x_entity_token.strip()
        current_app.logger.debug(f"[obtener_token] Found X-Entity-Token header: '{token_x_entity_token[:10]}...'")
        return _finalize_token_candidate(token_x_entity_token)

    if allow_cookie_auth:
        # Fallback: intentar recuperar el token desde una cookie específica
        cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
        token_cookie = request.cookies.get(cookie_name)
        if token_cookie:
            token_cookie = token_cookie.strip()
            current_app.logger.debug(
                f"[obtener_token] Found token in cookie '{cookie_name}': '{token_cookie[:10]}...'"
            )
            return _finalize_token_candidate(token_cookie)

    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
    if widget_cookie_name:
        widget_cookie = request.cookies.get(widget_cookie_name)
        if widget_cookie:
            widget_cookie = widget_cookie.strip()
            current_app.logger.debug(
                f"[obtener_token] Found token in widget cookie '{widget_cookie_name}': '{widget_cookie[:10]}...'"
            )
            return _finalize_token_candidate(widget_cookie)

    token_args = request.args.get("token")
    if token_args:
        token_args = token_args.strip()
        current_app.logger.debug(f"[obtener_token] Found token in query args: '{token_args[:10]}...'")
        return _finalize_token_candidate(token_args)

    # Algunas integraciones envían el token como entityToken en la query
    entity_token_arg = request.args.get("entityToken") or request.args.get("entity_token")
    if entity_token_arg:
        entity_token_arg = entity_token_arg.strip()
        current_app.logger.debug(f"[obtener_token] Found entityToken in query args: '{entity_token_arg[:10]}...'")
        return _finalize_token_candidate(entity_token_arg)

    # Fallback to 'empresa_token' in query args
    empresa_token_arg = request.args.get("empresa_token")
    if empresa_token_arg:
        empresa_token_arg = empresa_token_arg.strip()
        current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in query args: '{empresa_token_arg[:10]}...'")
        return _finalize_token_candidate(empresa_token_arg)

    if request.is_json:
        json_data = request.get_json(silent=True) or {}
        token_json = json_data.get("token")
        if token_json:
            token_json = token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found token in JSON payload: '{token_json[:10]}...'")
            return _finalize_token_candidate(token_json)

        entity_token_json = json_data.get("entityToken") or json_data.get("entity_token")
        if entity_token_json:
            entity_token_json = entity_token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found entityToken in JSON payload: '{entity_token_json[:10]}...'")
            return _finalize_token_candidate(entity_token_json)

        # Fallback to 'empresa_token' in JSON payload (no longer path-restricted)
        empresa_token_json = json_data.get("empresa_token")
        if empresa_token_json:
            empresa_token_json = empresa_token_json.strip()
            current_app.logger.debug(f"[obtener_token] Found 'empresa_token' in JSON payload: '{empresa_token_json[:10]}...'")
            return _finalize_token_candidate(empresa_token_json)


    token_form = request.form.get("token")
    if token_form:
        token_form = token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found token in form data: '{token_form[:10]}...'")
        return _finalize_token_candidate(token_form)

    entity_token_form = request.form.get("entityToken") or request.form.get("entity_token")
    if entity_token_form:
        entity_token_form = entity_token_form.strip()
        current_app.logger.debug(f"[obtener_token] Found entityToken in form data: '{entity_token_form[:10]}...'")
        return _finalize_token_candidate(entity_token_form)

    # Fallback to 'empresa_token' in form data (no longer path-restricted)
    empresa_token_form = request.form.get("empresa_token")
    if empresa_token_form:
        _register_candidate(empresa_token_form, "form field 'empresa_token'")

    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME")
    if widget_cookie_name:
        widget_cookie = request.cookies.get(widget_cookie_name)
        if widget_cookie:
            _register_candidate(widget_cookie, f"Cookie '{widget_cookie_name}'")

    widget_cookie_candidate: Optional[Tuple[str, str]] = None
    auth_header_widget_candidate: Optional[Tuple[str, str]] = None
    preferred_jwt_candidate: Optional[Tuple[str, str]] = None
    static_candidate: Optional[Tuple[str, User, str]] = None
    fallback_token: Optional[str] = None
    fallback_source: Optional[str] = None

    for candidate, owner, source in candidates:
        source_lower = source.lower()

        if owner:
            if static_candidate is None:
                static_candidate = (candidate, owner, source)
            continue

        if _is_jwt_token(candidate):
            payload = _decode_token_payload(candidate)
            if not payload:
                if fallback_token is None:
                    fallback_token = candidate
                    fallback_source = source
                continue

            if payload.get("session_kind") != "widget":
                if preferred_jwt_candidate is None:
                    preferred_jwt_candidate = (candidate, source)
                continue

            if "widget" in source_lower and "cookie" in source_lower:
                if widget_cookie_candidate is None:
                    widget_cookie_candidate = (candidate, source)
                continue

            if "authorization" in source_lower:
                if auth_header_widget_candidate is None:
                    auth_header_widget_candidate = (candidate, source)
                continue

            if fallback_token is None:
                fallback_token = candidate
                fallback_source = source
            continue

        if fallback_token is None:
            fallback_token = candidate
            fallback_source = source

    if preferred_jwt_candidate:
        candidate, source = preferred_jwt_candidate
        current_app.logger.debug(
            f"[obtener_token] Using token from {source}: '{candidate[:10]}...'"
        )
        return candidate

    if static_candidate:
        candidate, owner, source = static_candidate
        current_app.logger.debug(
            f"[obtener_token] Using token from {source}: '{candidate[:10]}...'"
        )
        return candidate

    if widget_cookie_candidate:
        candidate, source = widget_cookie_candidate
        current_app.logger.debug(
            f"[obtener_token] Using token from {source}: '{candidate[:10]}...'"
        )
        return candidate

    if auth_header_widget_candidate:
        candidate, source = auth_header_widget_candidate
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
    """Return the anonymous visitor identifier associated with the request."""

    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.args.get("anon_id")
        or getattr(g, "anon_id", None)
    )

    if not anon_id:
        cookie_name = current_app.config.get("ANON_SESSION_COOKIE_NAME", "chatboc_anon_id")
        anon_id = request.cookies.get(cookie_name)

    if not anon_id:
        anon_id = uuid.uuid4().hex

    g.anon_id = anon_id
    return anon_id
def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        anon_id = get_or_create_anon_id()

        def _finalize_response(resp_obj, status: int | None = None):
            resp = make_response(resp_obj, status) if status is not None else make_response(resp_obj)
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            from routes.auth import (
                _DEFAULT_CORS_HEADERS,
                _add_cors as _cors_helper,
            )

            requested_method = request.headers.get("Access-Control-Request-Method")
            allow_methods = set()
            if request.url_rule and request.url_rule.methods:
                allow_methods.update(request.url_rule.methods)
            allow_methods.add(request.method)
            if requested_method:
                allow_methods.add(requested_method)
            allow_methods.add("OPTIONS")

            requested_headers = request.headers.get("Access-Control-Request-Headers")
            allow_headers = _DEFAULT_CORS_HEADERS
            if requested_headers:
                allow_headers = f"{_DEFAULT_CORS_HEADERS}, {requested_headers}"

            return _cors_helper(
                _set_anon_cookie(resp, anon_id),
                allow_credentials=True,
                allow_methods=sorted(allow_methods),
                allow_headers=allow_headers,
            )

        if request.method == 'OPTIONS':
            status_code = 200 if request.blueprint == "legacy_auth" else 204
            return _finalize_response('', status_code)

        def _auth_error(message: str, status_code: int = 401, code: str = "token_expired"):
            payload = {"error": {"code": status_code, "message": message}}
            return _finalize_response(jsonify(payload), status_code)

        # Siempre intentar recuperar el token para exponerlo a las vistas que lo necesiten.
        raw_token = obtener_token()
        g.token_payload = _decode_token_payload(raw_token) if raw_token else {}
        g.widget_session = False
        g.widget_owner_user = None

        # Primero, verificar si el usuario ya está autenticado vía Flask-Login (sesión de cookie)
        if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
            g.auth_token = raw_token
            g.current_user = current_user
            g.owner_user = _resolve_owner_user(current_user)
            _ensure_entity_token(g.owner_user)
            if raw_token:
                g.token_payload = _decode_token_payload(raw_token) or {}
            return f(current_user, *args, **kwargs)

        if not raw_token:
            return _auth_error("Token faltante o malformado", 401, "token_missing")

        token = raw_token
        token_payload: Dict[str, Any] = {}
        user = user_from_token(token)

        if user:
            token_payload = _decode_token_payload(token)
        else:
            owner_user = _lookup_owner_for_static_token(raw_token)
            if owner_user:
                if not _widget_session_allowed(request.path, request.method):
                    return _auth_error(
                        "Token inválido o sesión expirada",
                        403,
                        "token_expired",
                    )

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
                return _auth_error("Token inválido o sesión expirada", 401)

        if token_payload.get("session_kind") == "widget" and not _widget_session_allowed(request.path, request.method):
            return _auth_error("Token inválido o sesión expirada", 403)

        g.token_payload = dict(token_payload) if token_payload else {}
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

        if token and not request.cookies.get(target_cookie):
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
            return _finalize_response(resp)

        return _finalize_response(response)

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

def _set_anon_cookie(resp, anon_id: Optional[str]):
    """Ensure the anonymous visitor cookie is persisted in the response."""

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
            resp.headers.setdefault("X-Anon-Id", anon_id)
            resp.headers.setdefault("Anon-Id", anon_id)
            return _set_anon_cookie(resp, anon_id)

        # Enforce Origin validation for widget calls
        # If origin is public landing (chatboc.ar), deny auth_token cookies/headers
        origin = request.headers.get("Origin", "").lower()
        is_public_landing = "chatboc.ar" in origin and "app.chatboc.ar" not in origin

        # Check explicit entity token presence to confirm "widget mode"
        has_entity_token = (
            request.args.get("entityToken")
            or request.headers.get("X-Entity-Token")
            or (request.is_json and (request.get_json(silent=True) or {}).get("entityToken"))
        )

        # Force anonymous/public logic if strictly on public landing and no valid user intent
        # (Though we still call obtener_token to see if there's a bearer token for widget login)

        token = obtener_token()
        current_user = None  # El usuario final que chatea (el "viewer")
        owner_user = None    # El dueño del bot (la "entidad", ej: municipio)
        owner_resolution_source = "anonymous"

        demo_token_detected = False
        token_payload: Dict[str, Any] = {}
        is_widget_token = False

        if token:
            # Primero, intentar decodificar como JWT. Esto es para usuarios logueados.
            jwt_user = user_from_token(token)
            if jwt_user:
                current_app.logger.info(f"Request authenticated via JWT. User ID: {jwt_user.id}")
                current_user = jwt_user
                # Si un usuario logueado tiene un empresa_id, el owner es esa empresa.
                if jwt_user.empresa_id:
                    owner_user = User.query.get(jwt_user.empresa_id)
                    owner_resolution_source = "jwt_parent_owner"
                else:
                    # Si no, el owner es el propio usuario (ej, el admin del municipio)
                    owner_user = jwt_user
                    owner_resolution_source = "jwt_self_owner"
            else:
                # Si falla el JWT, tratar el token como un token de entidad estático (API Key/UUID).
                # Esto es para el widget anónimo.
                if owner_user and getattr(owner_user, "token", None) != token:
                    owner_user = None

                entity_user = owner_user or User.query.filter_by(token=token).first()
                if not entity_user:
                    entity_user = _lookup_owner_for_static_token(token)

                if entity_user:
                    current_app.logger.info(f"Request authenticated via static entity token. Owner User ID: {entity_user.id}")
                    owner_user = entity_user
                    owner_resolution_source = "static_entity_token"
                    # El current_user sigue siendo None porque es una sesión anónima del widget.
                else:
                    current_app.logger.warning(f"Token '{token[:10]}...' provided but is not a valid JWT or a known entity token.")
                    try:
                        demo_token_detected = demo_rubro_for_token(token) is not None
                    except Exception:
                        demo_token_detected = False

        # Permitir que un token de entidad explícito tenga prioridad como owner,
        # incluso si el usuario está autenticado con JWT (ej. login desde el widget).
        explicit_entity_token = obtener_entity_token()
        if explicit_entity_token:
            entity_owner = _lookup_owner_for_static_token(explicit_entity_token) or User.query.filter_by(token=explicit_entity_token).first()
            if entity_owner:
                if owner_user and owner_user.id != entity_owner.id:
                    current_app.logger.info(
                        "[auth] Overriding owner_user %s with explicit entity owner %s from token %s",
                        owner_user.id,
                        entity_owner.id,
                        explicit_entity_token[:10],
                    )
                owner_user = entity_owner
                g.widget_owner_user = entity_owner
                owner_resolution_source = "explicit_entity_token"

        # Double check: If on public landing, and token was a cookie JWT (not bearer),
        # ensure current_user is WIPED to enforce anonymous mode.
        # This handles the case where obtener_token() logic might have been bypassed or race conditions.
        if is_public_landing and current_user and not request.headers.get("Authorization"):
             # It likely came from a first-party auth cookie. Force anonymous/demo
             # mode unless an explicit entity token re-established tenant context.
             current_app.logger.warning(
                 f"[auth] Public origin detected with Cookie JWT. Forcing Anonymous. User was: {current_user.id}"
             )
             current_user = None
             if owner_resolution_source in {"jwt_parent_owner", "jwt_self_owner"}:
                 owner_user = None
                 owner_resolution_source = "anonymous"

        # Si después de todo no hay owner (ej. request anónima sin token),
        # cargar el owner por defecto para el municipio.
        if not owner_user and 'municipio' in request.path and not demo_token_detected:
            owner_user = User.query.filter_by(tipo_chat='municipio', rol='admin').first()
            if owner_user:
                current_app.logger.info(f"Anonymous request to '{request.path}', loaded DEFAULT municipality owner user ID: {owner_user.id}")
                owner_resolution_source = "default_municipio_owner"
            else:
                current_app.logger.error(f"CRITICAL: Anonymous request to '{request.path}' but no default municipality user found.")

        g.token_payload = dict(token_payload) if token_payload else {}
        g.auth_token = token
        g.current_user = current_user
        g.owner_user = owner_user
        g.owner_resolution_source = owner_resolution_source

        # Llamar a la función de la ruta con los usuarios identificados
        response = f(
            current_user=current_user, owner_user=owner_user, anon_id=anon_id, *args, **kwargs
        )

        default_cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
        widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
        target_cookie = (
            widget_cookie_name
            if token_payload.get("session_kind") == "widget" or token_payload.get("renew_until")
            else default_cookie_name
        )

        existing_cookie_value = request.cookies.get(target_cookie)

        should_set_cookie = (
            _is_jwt_token(token)
            and token
            and (not existing_cookie_value or existing_cookie_value != token)
        )

        # Security Fix: Only set auth_token cookie if NOT on public landing
        if should_set_cookie and is_public_landing and target_cookie == default_cookie_name:
             should_set_cookie = False

        if should_set_cookie:
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

def _get_user_from_token() -> Optional[User]:
    """
    Extracts user from JWT token found in request headers.
    """
    token = obtener_token()
    if not token:
        return None
    return user_from_token(token)

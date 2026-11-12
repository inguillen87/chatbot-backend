"""Authenticated, fixed-target Meta credential administration; disabled by default."""
from __future__ import annotations

import json
import re

import jwt
from flask import Blueprint, current_app, request
from limits import parse
from limits.storage import RedisClusterStorage, RedisSentinelStorage, RedisStorage

from cutover_writer_fence import cutover_read_only_view
from extensions import limiter
from services import meta_credential_admin_runtime as runtime
from services import meta_credential_operations as ops
from utils.auth_helpers import user_from_token
from utils.meta_credential_http import MAX_BODY_BYTES, PANEL_ORIGINS, error, response
from utils.roles import is_authorized_superadmin_user

meta_credential_admin_bp = Blueprint("meta_credential_admin", __name__, url_prefix="/api/internal/meta-credentials")


class RequestDenied(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


def _authenticate():
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", authorization, re.IGNORECASE)
        if match is None or len(authorization) > 16_384:
            raise RequestDenied("meta_operation_authentication_required", 401)
        token = match.group(1)
    else:
        # HttpOnly Chatboc JWT issued by Clerk session exchange. No Flask-login,
        # widget, query-string or JSON token is a credential for this endpoint.
        token = request.cookies.get(current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token"))
        if not token:
            raise RequestDenied("meta_operation_authentication_required", 401)
        if request.headers.get("Origin") not in PANEL_ORIGINS:
            raise RequestDenied("meta_operation_csrf_origin_required", 403)
        if not isinstance(token, str) or len(token) > 16_384:
            raise RequestDenied("meta_operation_authentication_required", 401)
    user = user_from_token(token)
    if user is None:
        raise RequestDenied("meta_operation_authentication_required", 401)
    if not is_authorized_superadmin_user(user):
        raise RequestDenied("meta_operation_platform_admin_required", 403)
    try:
        claims = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
        if (claims.get("auth_provider") != "clerk" or claims.get("session_kind") != "clerk"
                or claims.get("auth_audience") not in {"tenant_owner", "panel"}
                or claims.get("auth_intent", "tenant_owner") != "tenant_owner"
                or claims.get("impersonated_by")
                or type(claims.get("exp")) is not int or type(claims.get("sv")) is not int
                or not isinstance(claims.get("asid"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", claims["asid"])
                or not isinstance(claims.get("clerk_sid") or claims.get("sid"), str)):
            raise ValueError
        actor = runtime.ActorSession(int(user.id), claims["asid"],
            claims.get("clerk_sid") or claims["sid"], claims["sv"], claims["exp"], claims["auth_audience"])
    except (ValueError, TypeError, KeyError, jwt.InvalidTokenError):
        raise RequestDenied("meta_operation_clerk_session_required", 401) from None
    runtime.release_auth_reads()
    return actor


def _body(action):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    try:
        raw = request.get_data(cache=False)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        required = {"target"} if action == "status" else {
            "target", "access_token", "expected_revision", "expected_writer_epoch"}
        allowed = required | ({"expires_at"} if action != "status" else set())
        if action == "apply":
            required |= {"expires_at", "approved_plan_sha256", "expected_database_identity_sha256"}
            allowed |= required
        if not isinstance(value, dict) or set(value) - allowed or required - set(value):
            raise ValueError
        if not isinstance(value["target"], str) or value["target"] not in ops.TARGETS:
            raise ValueError
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise RequestDenied("meta_operation_request_invalid", 400) from None


def _rate_limit(actor, action):
    try:
        strategy = limiter.limiter
        if (not current_app.config.get("TESTING") and not isinstance(
                strategy.storage, (RedisStorage, RedisClusterStorage, RedisSentinelStorage))):
            raise RequestDenied("meta_operation_rate_storage_required", 503)
        limits = ("30 per minute",) if action == "status" else ("12 per minute", "60 per hour")
        scope = "status" if action == "status" else "changes"
        for limit in limits:
            if not strategy.hit(parse(limit), "meta-credential-admin-v1", scope, str(actor.actor_id)):
                raise RequestDenied("meta_operation_rate_limited", 429)
    except RequestDenied:
        raise
    except Exception:
        raise RequestDenied("meta_operation_rate_storage_unavailable", 503) from None


def _handle(action):
    body = None
    try:
        actor = _authenticate()
        _rate_limit(actor, action)
        body = _body(action)
        result = (runtime.status(body, actor, current_app.config) if action == "status"
                  else runtime.run_operation(action, body, actor, current_app.config))
        return response(result)
    except RequestDenied as exc:
        return error(exc.code, exc.status, writes_performed=False)
    except Exception as exc:
        code = "meta_operation_unavailable"
        status_code = 503
        # Only finite application-owned codes cross this boundary. Never
        # serialize provider, SQL, URL, exception text or request values.
        if type(exc).__module__ in {"services.meta_credential_operations", "services.meta_whatsapp_runtime",
                                   "services.tenant_provider_credentials", "services.global_writer_authority"}:
            candidate = str(exc)
            if re.fullmatch(r"(?:meta|provider_credential|tenant_provider|global_writer)_[a-z0-9_]{1,100}", candidate):
                code, status_code = candidate, 409
        extra = ({"database_outcome": "unverified", "requires_status_check": True}
                 if action == "apply" else {"writes_performed": False})
        return error(code, status_code, **extra)
    finally:
        if isinstance(body, dict):
            body.pop("access_token", None)
        request.__dict__.pop("_cached_data", None)
        request.__dict__.pop("_cached_json", None)


@meta_credential_admin_bp.post("/plan")
@cutover_read_only_view
def plan():
    return _handle("plan")


@meta_credential_admin_bp.post("/apply")
def apply():
    return _handle("apply")


@meta_credential_admin_bp.post("/status")
@cutover_read_only_view
def status():
    return _handle("status")

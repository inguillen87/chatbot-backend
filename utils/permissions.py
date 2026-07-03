from functools import wraps
import uuid

from flask import g, jsonify, request

from utils.roles import ROLE_SUPERADMIN, canonical_role, is_authorized_superadmin_user


def _permission_error(reason_code: str):
    request_id = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or getattr(g, "request_id", None)
        or uuid.uuid4().hex
    )
    g.request_id = request_id
    response = jsonify(
        {
            "contract_version": "shared.error.v1",
            "status_code": 403,
            "reason_code": reason_code,
            "retryable": False,
            "request_id": request_id,
            "error": {"code": 403, "message": "Permisos insuficientes"},
        }
    )
    response.headers["X-Request-Id"] = request_id
    return response, 403


def require_role(*roles):
    """Abort 403 unless the current user's role matches one of the allowed roles."""
    allowed_roles = {canonical_role(role) for role in roles}

    def decorator(f):
        @wraps(f)
        def wrapper(current_user, *args, **kwargs):
            user_role = getattr(current_user, "rol", None)
            canonical = canonical_role(user_role)
            if canonical not in allowed_roles:
                return _permission_error("insufficient_permissions")
            if canonical == ROLE_SUPERADMIN and not is_authorized_superadmin_user(current_user):
                return _permission_error("superadmin_email_not_authorized")
            return f(current_user, *args, **kwargs)
        return wrapper
    return decorator


def require_municipio_access(f):
    """Abort 403 if the user does not have a municipio_id attribute."""
    @wraps(f)
    def wrapper(current_user, *args, **kwargs):
        if not getattr(current_user, "municipio_id", None):
            return _permission_error("municipio_access_required")
        return f(current_user, *args, **kwargs)
    return wrapper

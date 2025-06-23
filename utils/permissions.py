from functools import wraps
from flask import abort


def require_role(*roles):
    """Abort 403 unless the current user's role matches one of the allowed roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(current_user, *args, **kwargs):
            if getattr(current_user, "rol", None) not in roles:
                abort(403)
            return f(current_user, *args, **kwargs)
        return wrapper
    return decorator


def require_municipio_access(f):
    """Abort 403 if the user does not have a municipio_id attribute."""
    @wraps(f)
    def wrapper(current_user, *args, **kwargs):
        if not getattr(current_user, "municipio_id", None):
            abort(403)
        return f(current_user, *args, **kwargs)
    return wrapper

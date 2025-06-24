from functools import wraps
from flask import abort

# Roles adicionales que se mapearán a su forma canónica para simplificar
# las verificaciones de acceso.
ROLE_ALIASES = {
    "admin_municipio": "admin",
    "admin_pyme": "admin",
    "empleado_municipio": "empleado",
    "empleado_pyme": "empleado",
}


def require_role(*roles):
    """Abort 403 unless the current user's role matches one of the allowed roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(current_user, *args, **kwargs):
            user_role = getattr(current_user, "rol", None)
            canonical = ROLE_ALIASES.get(user_role, user_role)
            if canonical not in roles:
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

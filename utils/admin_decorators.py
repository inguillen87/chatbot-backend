from functools import wraps
from flask import abort, g, jsonify
from models import User
from utils.roles import ROLE_SUPERADMIN

def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Assumes token_requerido has already run and populated g.user or current_user
        # We need to ensure token_requerido is called before this decorator

        # Check if 'current_user' is in kwargs (passed by token_requerido)
        # or in g.viewer (set by attach_current_user in app.py)
        user = None

        # token_requerido passes user as first positional argument
        if args and isinstance(args[0], User):
            user = args[0]
        elif 'current_user' in kwargs:
            user = kwargs['current_user']
        elif hasattr(g, 'current_user'):
            user = g.current_user
        elif hasattr(g, 'viewer'):
            user = g.viewer

        if not user:
             return jsonify({"error": "Authentication required"}), 401

        # Check role against constant
        if user.rol != ROLE_SUPERADMIN:
            return jsonify({"error": f"Requires Super Admin privileges. User is {user.rol}"}), 403

        return f(*args, **kwargs)
    return decorated_function

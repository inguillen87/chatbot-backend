"""Compat alias routes that mirror legacy endpoints under the /api prefix.

These aliases prevent 404s when the widget or frontend calls newer /api/*
paths while the canonical blueprints live under non-/api prefixes (e.g.,
/productos, /carrito, /app). The functions are reused directly so CORS and
behavior remain consistent with the original endpoints.
"""

from flask import Blueprint, request

from routes.auth import login as login_view, me_perfil as perfil_view
from routes.carrito import agregar, carrito_root, eliminar, vaciar, actualizar
from routes.productos import obtener_productos
from routes.pwa_app import follow_tenant, list_followed_tenants, unfollow_tenant
from routes.pwa_misc import provide_anon_id


api_aliases_bp = Blueprint("api_aliases", __name__, url_prefix="/api")


@api_aliases_bp.route("/productos", methods=["GET", "OPTIONS"], strict_slashes=False)
def productos_alias():
    return obtener_productos()


@api_aliases_bp.route("/carrito", methods=["GET", "POST", "OPTIONS"], strict_slashes=False)
def carrito_alias_root():
    return carrito_root()


@api_aliases_bp.route("/carrito/agregar", methods=["POST", "OPTIONS"], strict_slashes=False)
def carrito_alias_agregar():
    return agregar()


@api_aliases_bp.route("/carrito/actualizar", methods=["POST", "OPTIONS"], strict_slashes=False)
def carrito_alias_actualizar():
    return actualizar()


@api_aliases_bp.route("/carrito/eliminar", methods=["POST", "OPTIONS"], strict_slashes=False)
def carrito_alias_eliminar():
    return eliminar()


@api_aliases_bp.route("/carrito/vaciar", methods=["POST", "OPTIONS"], strict_slashes=False)
def carrito_alias_vaciar():
    return vaciar()


@api_aliases_bp.route("/auth/login", methods=["POST", "OPTIONS"], strict_slashes=False)
def auth_login_alias():
    return login_view()


@api_aliases_bp.route("/perfil", methods=["GET", "PUT", "OPTIONS"], strict_slashes=False)
def perfil_alias():
    return perfil_view()


@api_aliases_bp.route("/app/me/tenants", methods=["GET", "OPTIONS"], strict_slashes=False)
def tenants_alias_list():
    return list_followed_tenants()


@api_aliases_bp.route("/app/me/tenants/follow", methods=["POST", "DELETE", "OPTIONS"], strict_slashes=False)
def tenants_alias_follow():
    if request.method == "DELETE":
        return unfollow_tenant()
    return follow_tenant()


@api_aliases_bp.route("/pwa/anon-id", methods=["GET", "OPTIONS"], strict_slashes=False)
@api_aliases_bp.route("/api/pwa/anon-id", methods=["GET", "OPTIONS"], strict_slashes=False)
def anon_id_alias():
    return provide_anon_id()

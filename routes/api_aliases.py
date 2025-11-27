"""Compat alias routes that mirror legacy endpoints under the /api prefix.

These aliases prevent 404s when the widget or frontend calls newer /api/*
paths while the canonical blueprints live under non-/api prefixes (e.g.,
/productos, /carrito, /app). The functions are reused directly so CORS and
behavior remain consistent with the original endpoints.
"""

from flask import Blueprint, request

from routes.auth import login as login_view, me_perfil as perfil_view
from routes.chat import ask, ask_municipio, ask_pyme
from routes.carrito import agregar, carrito_root, eliminar, vaciar, actualizar
from routes.estadisticas import (
    estadisticas_tickets,
    mapa_calor_datos,
    tickets_options,
)
from routes.municipal_legacy import list_municipal_posts, municipal_categorias
from routes.notifications import get_notifications, notifications_options
from routes.ticket import (
    get_chat_mensajes,
    get_ticket_details,
    get_tickets_del_usuario,
)
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


@api_aliases_bp.route("/me", methods=["GET", "PUT", "OPTIONS"], strict_slashes=False)
def me_alias():
    return perfil_view()


@api_aliases_bp.route("/ask", methods=["POST", "OPTIONS"], strict_slashes=False)
def ask_alias():
    return ask()


@api_aliases_bp.route("/ask/pyme", methods=["POST", "OPTIONS"], strict_slashes=False)
def ask_pyme_alias():
    return ask_pyme()


@api_aliases_bp.route("/ask/municipio", methods=["POST", "OPTIONS"], strict_slashes=False)
def ask_municipio_alias():
    return ask_municipio()


@api_aliases_bp.route("/notifications", methods=["GET"], strict_slashes=False)
def notifications_alias():
    return get_notifications()  # token_requerido inside original view


@api_aliases_bp.route("/notifications", methods=["OPTIONS"], strict_slashes=False)
def notifications_options_alias():
    return notifications_options()


@api_aliases_bp.route("/tickets", methods=["GET"], strict_slashes=False)
@api_aliases_bp.route("/tickets/", methods=["GET"], strict_slashes=False)
def tickets_alias():
    return get_tickets_del_usuario()


@api_aliases_bp.route(
    "/tickets/municipio/<int:ticket_id>", methods=["GET"], strict_slashes=False
)
def tickets_municipio_alias(ticket_id: int):
    return get_ticket_details(ticket_id=ticket_id)


@api_aliases_bp.route(
    "/tickets/chat/<int:ticket_id>/mensajes",
    methods=["GET"],
    strict_slashes=False,
)
def tickets_chat_alias(ticket_id: int):
    return get_chat_mensajes(ticket_id=ticket_id)


@api_aliases_bp.route(
    "/municipal/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_categorias_alias():
    return municipal_categorias()


@api_aliases_bp.route(
    "/municipal/posts", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_posts_alias():
    return list_municipal_posts()


@api_aliases_bp.route(
    "/estadisticas/mapa_calor/datos", methods=["GET"], strict_slashes=False
)
def estadisticas_heatmap_alias():
    return mapa_calor_datos()


@api_aliases_bp.route(
    "/estadisticas/tickets", methods=["GET"], strict_slashes=False
)
def estadisticas_tickets_alias():
    return estadisticas_tickets()


@api_aliases_bp.route(
    "/estadisticas/tickets", methods=["OPTIONS"], strict_slashes=False
)
def estadisticas_tickets_options_alias():
    return tickets_options()


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

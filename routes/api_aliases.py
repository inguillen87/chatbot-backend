"""Compat alias routes that mirror legacy endpoints under the /api prefix.

These aliases prevent 404s when the widget or frontend calls newer /api/*
paths while the canonical blueprints live under non-/api prefixes (e.g.,
/productos, /carrito, /app). The functions are reused directly so CORS and
behavior remain consistent with the original endpoints.
"""

from flask import Blueprint, jsonify, request
from flask_cors import cross_origin

from routes.auth import (
    chatuser_login_panel,
    chatuser_register_panel,
    get_google_client_id,
    regenerar_token_integracion,
    login as login_view,
    me_perfil as perfil_view,
    google_login,
)
from routes.chat import ask, ask_municipio, ask_pyme
from routes.carrito import agregar, carrito_root, eliminar, vaciar, actualizar
from routes.estadisticas import (
    estadisticas_tickets,
    mapa_calor_datos,
    tickets_options,
)
from routes.municipal_legacy import (
    list_municipal_posts,
    municipal_categorias,
    municipal_estados,
)
from routes.notifications import get_notifications, notifications_options
from routes.ticket import (
    get_chat_mensajes,
    get_ticket_by_number_public,
    get_ticket_details,
    get_tickets_del_usuario,
)
from routes.municipio_api import (
    listar_categorias_municipio,
    listar_categorias_pedidos,
    listar_categorias_ticket,
    listar_empleados_multitenant,
)
from routes.productos import obtener_productos
from routes.pedidos import (
    listar_pedidos_pyme,
    obtener_pedido_pyme,
    actualizar_estado_pedido_pyme,
    crear_pedido_pyme,
)
from routes.pwa_app import follow_tenant, list_followed_tenants, unfollow_tenant
from routes.pwa_misc import provide_anon_id
from routes.public_resolver import tenant_profile


api_aliases_bp = Blueprint("api_aliases", __name__, url_prefix="/api")
public_aliases_bp = Blueprint("public_aliases", __name__)


@api_aliases_bp.route("/productos", methods=["GET", "OPTIONS"], strict_slashes=False)
def productos_alias():
    return obtener_productos()


@api_aliases_bp.route(
    "/<tenant_slug>/productos", methods=["GET", "OPTIONS"], strict_slashes=False
)
def productos_alias_with_slug(tenant_slug: str):
    """Alias that allows /api/<slug>/productos to hit the catalog endpoint."""

    return obtener_productos()


@api_aliases_bp.route("/carrito", methods=["GET", "POST", "OPTIONS"], strict_slashes=False)
def carrito_alias_root():
    return carrito_root()


@api_aliases_bp.route(
    "/<tenant_slug>/carrito", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def carrito_alias_with_slug(tenant_slug: str):
    """Alias that allows /api/<slug>/carrito to reach the cart endpoint."""

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


@api_aliases_bp.route("/auth/login", methods=["GET", "POST", "OPTIONS"], strict_slashes=False)
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


def _options_ok():
    return jsonify({"ok": True})


@api_aliases_bp.route(
    "/chatuserregisterpanel", methods=["POST", "OPTIONS"], strict_slashes=False
)
def chatuser_register_panel_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return chatuser_register_panel()


@api_aliases_bp.route(
    "/chatuserloginpanel", methods=["POST", "OPTIONS"], strict_slashes=False
)
def chatuser_login_panel_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return chatuser_login_panel()


@api_aliases_bp.route(
    "/google-login", methods=["POST", "OPTIONS"], strict_slashes=False
)
def google_login_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return google_login()


@api_aliases_bp.route(
    "/google-client-id", methods=["GET", "OPTIONS"], strict_slashes=False
)
def google_client_id_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return get_google_client_id()


@api_aliases_bp.route(
    "/integracion/regenerar-token", methods=["POST", "OPTIONS"], strict_slashes=False
)
def regenerar_token_integracion_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return regenerar_token_integracion()  # token_requerido en la vista original


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
    "/tickets/municipio/por_numero/<string:nro_ticket>",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def tickets_municipio_por_numero_alias(nro_ticket: str):
    """Expose the public ticket lookup under the /api namespace."""

    if request.method == "OPTIONS":
        return _options_ok()
    return get_ticket_by_number_public(nro_ticket=nro_ticket)


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


@api_aliases_bp.route("/pedidos", methods=["GET", "POST", "OPTIONS"], strict_slashes=False)
def pedidos_alias():
    """Expose pedidos endpoints under /api for the widget."""

    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_pedido_pyme()
    return listar_pedidos_pyme()


@api_aliases_bp.route(
    "/pedidos/<int:pedido_id>", methods=["GET", "OPTIONS"], strict_slashes=False
)
def pedidos_detalle_alias(pedido_id: int):
    if request.method == "OPTIONS":
        return _options_ok()
    return obtener_pedido_pyme(pedido_id=pedido_id)


@api_aliases_bp.route(
    "/pedidos/<int:pedido_id>/estado",
    methods=["PUT", "OPTIONS"],
    strict_slashes=False,
)
def pedidos_estado_alias(pedido_id: int):
    if request.method == "OPTIONS":
        return _options_ok()
    return actualizar_estado_pedido_pyme(pedido_id=pedido_id)


@api_aliases_bp.route(
    "/municipal/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_categorias_alias():
    return municipal_categorias()


@api_aliases_bp.route(
    "/municipal/estados", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_estados_alias():
    return municipal_estados()


@api_aliases_bp.route(
    "/municipal/posts", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_posts_alias():
    return list_municipal_posts()


@public_aliases_bp.route(
    "/chatuserregisterpanel", methods=["POST", "OPTIONS"], strict_slashes=False
)
def root_chatuser_register_panel_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return chatuser_register_panel()


@public_aliases_bp.route(
    "/chatuserloginpanel", methods=["POST", "OPTIONS"], strict_slashes=False
)
def root_chatuser_login_panel_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return chatuser_login_panel()


@public_aliases_bp.route("/google-login", methods=["POST", "OPTIONS"], strict_slashes=False)
def root_google_login_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return google_login()


@public_aliases_bp.route(
    "/google-client-id", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_google_client_id_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return get_google_client_id()


@api_aliases_bp.route(
    "/estadisticas/mapa_calor/datos", methods=["GET"], strict_slashes=False
)
def estadisticas_heatmap_alias():
    return mapa_calor_datos()


@api_aliases_bp.route(
    "/estadisticas/mapa_calor/datos",
    methods=["OPTIONS"],
    strict_slashes=False,
)
def estadisticas_heatmap_options_alias():
    return _options_ok()


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


# --- Alias de compatibilidad para prefijo /api/municipio/municipio ---


@api_aliases_bp.route(
    "/municipio/municipio/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipio_alias_categorias():
    return listar_categorias_municipio(tenant_slug="municipio")


@api_aliases_bp.route(
    "/municipio/municipio/tickets/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def municipio_alias_tickets_categorias():
    return listar_categorias_ticket(tenant_slug="municipio")


@api_aliases_bp.route(
    "/municipio/municipio/pedidos/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def municipio_alias_pedidos_categorias():
    return listar_categorias_pedidos(tenant_slug="municipio")


@api_aliases_bp.route(
    "/municipio/municipio/empleados", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipio_alias_empleados():
    return listar_empleados_multitenant(tenant_slug="municipio")


# Public aliases for callers that omit the /api prefix (e.g., service workers)
# These respond to the same underlying endpoints but avoid 404s on preflight
# requests when the origin issues OPTIONS without the /api/ prefix.
@public_aliases_bp.route(
    "/municipio/municipio/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_municipio_alias_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_municipio(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipio/municipio/tickets/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_municipio_alias_tickets_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_ticket(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipio/municipio/pedidos/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_municipio_alias_pedidos_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_pedidos(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipio/municipio/empleados", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_municipio_alias_empleados():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_empleados_multitenant(tenant_slug="municipio")


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


@api_aliases_bp.route(
    "/pwa/tenant-info",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
    provide_automatic_options=False,
)
@cross_origin(origins="*", supports_credentials=True)
def pwa_tenant_info_alias():
    """Alias so widgets hitting /api/pwa/tenant-info receive tenant details."""

    return tenant_profile()


@api_aliases_bp.route("/public/tenant", methods=["GET", "OPTIONS"], strict_slashes=False)
def public_tenant_alias():
    """Expose public tenant info under /api/public/tenant for legacy callers."""

    return tenant_profile()


@public_aliases_bp.route("/public/tenant", methods=["GET", "OPTIONS"], strict_slashes=False)
def root_public_tenant_alias():
    """Expose public tenant info for callers that omit the /api prefix."""

    return tenant_profile()


@public_aliases_bp.route(
    "/pwa/tenant-info",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
    provide_automatic_options=False,
)
@cross_origin(origins="*", supports_credentials=True)
def root_pwa_tenant_info_alias():
    """Alias without /api prefix for PWA tenant info requests."""

    return tenant_profile()


@public_aliases_bp.route(
    "/<tenant_slug>/productos", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_productos_alias_with_slug(tenant_slug: str):
    """Public alias to serve /<slug>/productos via the catalog endpoint."""

    return obtener_productos()


@public_aliases_bp.route(
    "/<tenant_slug>/carrito", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def root_carrito_alias_with_slug(tenant_slug: str):
    """Public alias to serve /<slug>/carrito via the cart endpoint."""

    return carrito_root()

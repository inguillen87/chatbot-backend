"""Compat alias routes that mirror legacy endpoints under the /api prefix.

These aliases prevent 404s when the widget or frontend calls newer /api/*
paths while the canonical blueprints live under non-/api prefixes (e.g.,
/productos, /carrito, /app). The functions are reused directly so CORS and
behavior remain consistent with the original endpoints.
"""

import uuid

from flask import Blueprint, abort, current_app, jsonify, make_response, request
from flask_cors import cross_origin

from routes.admin_ai import get_bot_settings, update_bot_settings
from routes.analytics import analytics_event_ingest
from services.analytics.config import get_config
from routes.admin_analytics import (
    admin_analytics_export_csv,
    admin_analytics_export_pdf,
    admin_analytics_heatmap,
    admin_analytics_overview,
    admin_analytics_dashboard,
    admin_analytics_hub,
)
from routes.auth import (
    chatuser_login_panel,
    chatuser_register_panel,
    get_google_client_id,
    regenerar_token_integracion,
    login as login_view,
    me_perfil as perfil_view,
    google_login,
    admin_login,
    demo_catalog,
    login_demo,
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
    municipal_estados,
)
from routes.notifications import get_notifications, notifications_options
from routes.ticket import (
    get_chat_mensajes,
    get_public_ticket_status,
    get_ticket_workflow_metadata,
    get_ticket_by_number_public,
    get_ticket_details,
    get_tickets_del_usuario,
)
from routes.municipio_api import (
    agregar_item_carrito,
    checkout_publico,
    listar_categorias_municipio,
    listar_categorias_pedidos,
    listar_categorias_ticket,
    listar_empleados_multitenant,
    crear_empleado_multitenant,
    obtener_carrito_publico,
    producto_publico,
    productos_publicos,
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
from routes.public_resolver import tenant_profile, widget_config as public_widget_config
from routes.public_tenant import (
    get_catalog,
    get_contacts,
    get_links,
    get_menu,
    get_widget_config,
)
from routes.pwa_public import public_events, public_news


api_aliases_bp = Blueprint("api_aliases", __name__, url_prefix="/api")
public_aliases_bp = Blueprint("public_aliases", __name__)


@api_aliases_bp.route("/auth/admin/login", methods=["POST", "OPTIONS"], strict_slashes=False)
def admin_login_alias():
    try:
        return admin_login()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] /auth/admin/login alias failed: %s", exc)
        return jsonify({"error": "Servicio de autenticación temporalmente no disponible", "reason_code": "auth_service_unavailable"}), 503


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

    # Explicitly set context from slug if middleware missed it
    if tenant_slug:
        from services.tenant_resolver import resolve_tenant_only
        from flask import g
        try:
            # This triggers lazy creation if needed
            g.tenant_profile = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=False)
        except Exception:
            pass

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




@api_aliases_bp.route("/auth/demo/catalog", methods=["GET", "OPTIONS"], strict_slashes=False)
def auth_demo_catalog_alias():
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    response = demo_catalog()

    flask_response = make_response(response)
    flask_response.headers.setdefault("X-Request-Id", request_id)
    return flask_response


@api_aliases_bp.route("/auth/demo", methods=["POST", "OPTIONS"], strict_slashes=False)
def auth_demo_login_alias():
    return login_demo()

@api_aliases_bp.route("/perfil", methods=["GET", "PUT", "OPTIONS"], strict_slashes=False)
def perfil_alias():
    return perfil_view()


@api_aliases_bp.route("/me", methods=["GET", "PUT", "OPTIONS"], strict_slashes=False)
def me_alias():
    try:
        return perfil_view()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] /me alias degraded: %s", exc)
        return jsonify({"error": "No se pudo obtener el perfil", "reason_code": "profile_unavailable"}), 503


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


@api_aliases_bp.route("/analytics/event", methods=["POST", "OPTIONS"], strict_slashes=False)
def analytics_event_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    if not get_config().feature_enabled:
        abort(404)
    return analytics_event_ingest()


@api_aliases_bp.route("/admin/analytics/overview", methods=["GET"], strict_slashes=False)
def admin_analytics_overview_alias():
    return admin_analytics_overview()


@api_aliases_bp.route("/admin/analytics/heatmap", methods=["GET"], strict_slashes=False)
def admin_analytics_heatmap_alias():
    return admin_analytics_heatmap()




@api_aliases_bp.route("/admin/analytics/dashboard", methods=["GET"], strict_slashes=False)
def admin_analytics_dashboard_alias():
    return admin_analytics_dashboard()


@api_aliases_bp.route("/admin/analytics/hub", methods=["GET"], strict_slashes=False)
def admin_analytics_hub_alias():
    return admin_analytics_hub()

@api_aliases_bp.route("/admin/analytics/export.csv", methods=["GET"], strict_slashes=False)
def admin_analytics_export_csv_alias():
    return admin_analytics_export_csv()


@api_aliases_bp.route("/admin/analytics/export.pdf", methods=["GET"], strict_slashes=False)
def admin_analytics_export_pdf_alias():
    return admin_analytics_export_pdf()


@api_aliases_bp.route("/admin/bot/settings", methods=["GET"], strict_slashes=False)
def admin_bot_settings_get_alias():
    return get_bot_settings()


@api_aliases_bp.route("/admin/bot/settings", methods=["PUT"], strict_slashes=False)
def admin_bot_settings_put_alias():
    return update_bot_settings()


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
    "/tickets/public/status",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def tickets_public_status_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return get_public_ticket_status()


@api_aliases_bp.route(
    "/tickets/workflow/metadata",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def tickets_workflow_metadata_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return get_ticket_workflow_metadata()


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
    "/municipal/estados", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_estados_alias():
    return municipal_estados()


@api_aliases_bp.route(
    "/municipal/posts", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_posts_alias():
    return list_municipal_posts()


def _alias_tenant_slug() -> str | None:
    """Extract tenant slug hints from the current query string.

    These aliases need to respect the caller's tenant selection instead of
    forcing "municipio" so that multitenant deployments continue to work.
    """

    return request.args.get("tenant_slug") or request.args.get("tenant")


@api_aliases_bp.route(
    "/municipal/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_categorias_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_municipio(tenant_slug=_alias_tenant_slug())


@api_aliases_bp.route(
    "/municipal/tickets/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_tickets_categorias_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_ticket(tenant_slug=_alias_tenant_slug())


@api_aliases_bp.route(
    "/municipal/pedidos/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_pedidos_categorias_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_pedidos(tenant_slug=_alias_tenant_slug())


@api_aliases_bp.route(
    "/municipal/empleados", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def municipal_empleados_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_empleado_multitenant(tenant_slug=_alias_tenant_slug())
    return listar_empleados_multitenant(tenant_slug=_alias_tenant_slug())


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
    "/municipio/municipio/empleados", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def municipio_alias_empleados():
    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_empleado_multitenant(tenant_slug="municipio")
    return listar_empleados_multitenant(tenant_slug="municipio")


# --- Alias de compatibilidad para prefijo /api/municipal/municipio ---


@api_aliases_bp.route(
    "/municipal/municipio/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def municipal_alias_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_municipio(tenant_slug="municipio")


@api_aliases_bp.route(
    "/municipal/municipio/tickets/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def municipal_alias_tickets_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_ticket(tenant_slug="municipio")


@api_aliases_bp.route(
    "/municipal/municipio/pedidos/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def municipal_alias_pedidos_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_pedidos(tenant_slug="municipio")


@api_aliases_bp.route(
    "/municipal/municipio/empleados", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def municipal_alias_empleados():
    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_empleado_multitenant(tenant_slug="municipio")
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
    "/municipio/municipio/empleados", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def root_municipio_alias_empleados():
    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_empleado_multitenant(tenant_slug="municipio")
    return listar_empleados_multitenant(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipal/municipio/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_municipal_alias_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_municipio(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipal/municipio/tickets/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_municipal_alias_tickets_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_ticket(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipal/municipio/pedidos/categorias",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_municipal_alias_pedidos_categorias():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_pedidos(tenant_slug="municipio")


@public_aliases_bp.route(
    "/municipal/municipio/empleados", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def root_municipal_alias_empleados():
    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_empleado_multitenant(tenant_slug="municipio")
    return listar_empleados_multitenant(tenant_slug="municipio")

@public_aliases_bp.route(
    "/municipal/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_municipal_categorias_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_municipio(tenant_slug=_alias_tenant_slug())


@public_aliases_bp.route(
    "/municipal/tickets/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_municipal_tickets_categorias_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_ticket(tenant_slug=_alias_tenant_slug())


@public_aliases_bp.route(
    "/municipal/pedidos/categorias", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_municipal_pedidos_categorias_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    return listar_categorias_pedidos(tenant_slug=_alias_tenant_slug())


@public_aliases_bp.route(
    "/municipal/empleados", methods=["GET", "POST", "OPTIONS"], strict_slashes=False
)
def root_municipal_empleados_alias_v2():
    if request.method == "OPTIONS":
        return _options_ok()
    if request.method == "POST":
        return crear_empleado_multitenant(tenant_slug=_alias_tenant_slug())
    return listar_empleados_multitenant(tenant_slug=_alias_tenant_slug())


# --- Alias sin prefijo /api para endpoints de estadísticas ---


@public_aliases_bp.route(
    "/estadisticas/tickets", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_estadisticas_tickets_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return estadisticas_tickets()


@public_aliases_bp.route(
    "/estadisticas/mapa_calor/datos",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_estadisticas_heatmap_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return mapa_calor_datos()


# --- Alias sin prefijo /api para el marketplace público ---


@public_aliases_bp.route(
    "/public/market/<tenant_slug>/productos",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_public_market_productos_alias(tenant_slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return productos_publicos(tenant_slug=tenant_slug)


@public_aliases_bp.route(
    "/public/market/<tenant_slug>/productos/<int:producto_id>",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_public_market_producto_alias(tenant_slug: str, producto_id: int):
    if request.method == "OPTIONS":
        return _options_ok()
    return producto_publico(tenant_slug=tenant_slug, producto_id=producto_id)


@public_aliases_bp.route(
    "/public/market/<tenant_slug>/carrito",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
)
def root_public_market_carrito_alias(tenant_slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return obtener_carrito_publico(tenant_slug=tenant_slug)


@public_aliases_bp.route(
    "/public/market/<tenant_slug>/carrito/items",
    methods=["POST", "OPTIONS"],
    strict_slashes=False,
)
def root_public_market_carrito_items_alias(tenant_slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return agregar_item_carrito(tenant_slug=tenant_slug)


@public_aliases_bp.route(
    "/public/market/<tenant_slug>/checkout",
    methods=["POST", "OPTIONS"],
    strict_slashes=False,
)
def root_public_market_checkout_alias(tenant_slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return checkout_publico(tenant_slug=tenant_slug)


# --- Alias sin prefijo /api para configuraciones de tenant público ---




@api_aliases_bp.route("/public/widget-config", methods=["GET", "OPTIONS"], strict_slashes=False)
def public_widget_config_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return public_widget_config()

@public_aliases_bp.route(
    "/public/tenants/<slug>/menu", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_tenant_menu(slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return get_menu(slug)


@public_aliases_bp.route(
    "/public/tenants/<slug>/contacts", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_tenant_contacts(slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return get_contacts(slug)


@public_aliases_bp.route(
    "/public/tenants/<slug>/links", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_tenant_links(slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return get_links(slug)


@public_aliases_bp.route(
    "/public/tenants/<slug>/widget-config", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_tenant_widget_config(slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return get_widget_config(slug)


@public_aliases_bp.route(
    "/public/tenants/<slug>/catalog", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_tenant_catalog(slug: str):
    if request.method == "OPTIONS":
        return _options_ok()
    return get_catalog(slug)


# --- Alias sin prefijo /api para news/events públicos ---


@public_aliases_bp.route(
    "/public/news", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_news_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return public_news()


@public_aliases_bp.route(
    "/public/events", methods=["GET", "OPTIONS"], strict_slashes=False
)
def root_public_events_alias():
    if request.method == "OPTIONS":
        return _options_ok()
    return public_events()


@api_aliases_bp.route("/app/me/tenants", methods=["GET", "OPTIONS"], strict_slashes=False)
def tenants_alias_list():
    try:
        return list_followed_tenants()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] /app/me/tenants alias degraded: %s", exc)
        return jsonify([]), 200


@api_aliases_bp.route("/app/me/tenants/follow", methods=["POST", "DELETE", "OPTIONS"], strict_slashes=False)
def tenants_alias_follow():
    try:
        if request.method == "DELETE":
            return unfollow_tenant()
        return follow_tenant()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] /app/me/tenants/follow alias failed: %s", exc)
        return jsonify({"error": "No se pudo actualizar el seguimiento", "reason_code": "tenant_follow_unavailable"}), 503


@api_aliases_bp.route("/pwa/anon-id", methods=["GET", "OPTIONS"], strict_slashes=False)
@api_aliases_bp.route("/api/pwa/anon-id", methods=["GET", "OPTIONS"], strict_slashes=False)
def anon_id_alias():
    try:
        return provide_anon_id()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] /pwa/anon-id alias degraded: %s", exc)
        return jsonify({"anon_id": None, "reason_code": "anon_id_unavailable"}), 200


@public_aliases_bp.route("/pwa/anon-id", methods=["GET", "OPTIONS"], strict_slashes=False)
def root_anon_id_alias():
    """Alias without /api prefix for PWA anon-id requests."""
    try:
        return provide_anon_id()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] root /pwa/anon-id alias degraded: %s", exc)
        return jsonify({"anon_id": None, "reason_code": "anon_id_unavailable"}), 200


@api_aliases_bp.route(
    "/pwa/tenant-info",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
    provide_automatic_options=False,
)
@cross_origin(origins="*", supports_credentials=True)
def pwa_tenant_info_alias():
    """Alias so widgets hitting /api/pwa/tenant-info receive tenant details."""
    try:
        return tenant_profile()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] /pwa/tenant-info alias degraded: %s", exc)
        return jsonify({"tenant": None, "reason_code": "tenant_info_unavailable"}), 200


# @api_aliases_bp.route("/public/tenant", methods=["GET", "OPTIONS"], strict_slashes=False)
# def public_tenant_alias():
#     """Expose public tenant info under /api/public/tenant for legacy callers."""
#
#     return tenant_profile()


@public_aliases_bp.route("/public/tenant", methods=["GET", "OPTIONS"], strict_slashes=False)
def root_public_tenant_alias():
    """Expose public tenant info for callers that omit the /api prefix."""
    try:
        return tenant_profile()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] root /public/tenant alias degraded: %s", exc)
        return jsonify({"tenant": None, "reason_code": "tenant_info_unavailable"}), 200


@public_aliases_bp.route(
    "/pwa/tenant-info",
    methods=["GET", "OPTIONS"],
    strict_slashes=False,
    provide_automatic_options=False,
)
@cross_origin(origins="*", supports_credentials=True)
def root_pwa_tenant_info_alias():
    """Alias without /api prefix for PWA tenant info requests."""
    try:
        return tenant_profile()
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.warning("[api_aliases] root /pwa/tenant-info alias degraded: %s", exc)
        return jsonify({"tenant": None, "reason_code": "tenant_info_unavailable"}), 200


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

from datetime import datetime, timedelta
import logging
import os
import sys
import uuid

# --- Monkey patch BEFORE imports that might use threading ---
# This is crucial for Gunicorn/eventlet compatibility to avoid timeouts
# and ensure proper async handling.
try:
    import eventlet
    # Check if we are running under gunicorn with eventlet worker
    # Or explicitly asked to patch via env var
    if 'gunicorn' in sys.modules or os.environ.get("FLASK_USE_EVENTLET") == "1":
        eventlet.monkey_patch()
except ImportError:
    pass
# ------------------------------------------------------------

from flask import Flask, jsonify, request, g
from werkzeug.exceptions import HTTPException
from extensions import db, migrate, login_manager, sock, limiter
from config import Config
from services.google_maps_service import servicio_google_maps
from flask_cors import CORS
from socket_service import socketio
from logging.config import dictConfig
import logging.config

# --- Logging Configuration ---
# Configure structured JSON logging if requested, otherwise standard logging.
# This must happen before app creation to capture early logs.
try:
    from config import LOGGING_CONFIG
    logging.config.dictConfig(LOGGING_CONFIG)
except Exception:
    # Fallback basic config if dictConfig fails or is missing
    logging.basicConfig(level=logging.INFO)

# Flag to prevent multiple initializations in some setups
MIGRATIONS_ONLY = os.environ.get("FLASK_MIGRATIONS_ONLY") == "1"

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    # Limiter requires  or similar key function
    # We use a lambda for key func to avoid circular imports if defined elsewhere
    limiter.init_app(app)

    # CORS configuration
    # More restrictive CORS can be applied per blueprint if needed.
    CORS(app,
         resources={r"/*": {"origins": "*"}},
         supports_credentials=True,
         allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-Chat-Session-Id", "X-Anon-Id", "X-Tenant-ID", "X-Entity-Token", "X-Owner-Token", "X-Widget-Token", "Sentry-Trace", "Baggage"],
         expose_headers=["Content-Range", "X-Content-Range", "X-Chat-Session-Id", "X-Anon-Id"]
    )

    # Register Global Error Handlers with Unified JSON Format
    @app.errorhandler(400)
    def handle_bad_request(error):
        detail = getattr(error, "description", None) or str(error)
        return jsonify({"error": {"code": 400, "message": detail}}), 400

    @app.errorhandler(404)
    def handle_not_found(error):
        detail = getattr(error, "description", None) or "Recurso no encontrado"
        return jsonify({"error": {"code": 404, "message": detail}}), 404

    @app.errorhandler(500)
    def handle_server_error(error):
        app.logger.exception("Unhandled server error: %s", error)
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": {"code": 500, "message": "Internal server error"}}), 500

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException):
        # Generic handler for other HTTP exceptions (401, 403, 429, etc.)
        payload = {
            "error": {"code": error.code, "message": error.description},
        }
        return jsonify(payload), error.code

    # --- Blueprint Registration ---
    # Import routes here to avoid circular dependencies at module level
    from routes.auth import auth_bp, login_view_func
    from routes.legacy_auth import legacy_auth_bp
    from routes.config import config_bp
    from routes.catalogo import catalogo_bp
    from routes.productos import productos_bp
    from routes.pedidos import pedidos_bp
    from routes.carrito import carrito_bp
    from routes.chat import chat_bp
    from routes.ticket import ticket_bp
    from routes.crm.routes import crm_bp
    from routes.analytics import analytics_bp
    from routes.analytics_routes import analytics_v2_bp
    from routes.gov_analytics import gov_analytics_bp
    from routes.archivos import archivos_bp
    from routes.metricas import metricas_bp
    from routes.rubros import rubros_bp
    from routes.public_resolver import public_resolver_bp, public_municipios_bp
    from routes.municipio_api import municipio_api_bp
    from routes.subastas import subastas_bp
    from routes.puntos import puntos_public_bp, puntos_bp
    from routes.rewards_rules import rewards_rules_bp
    from routes.catalog_import import catalog_import_bp
    from routes.pedidos_from_file import pedidos_from_file_bp
    from routes.kits import kits_bp
    from routes.checkout import checkout_bp, pedidos_checkout_bp
    from routes.estadisticas import estadisticas_bp
    from routes.empleados import empleados_bp
    from routes.categorias import categorias_bp
    from routes.municipio_api import legacy_public_v2_bp, widget_public_bp, public_market_bp
    from routes.municipal_legacy import municipal_bp
    from routes.admin_market import admin_market_bp
    from routes.market import market_bp, market_admin_bp
    from routes.pwa_app import pwa_app_bp, pwa_app_legacy_bp
    from routes.pwa_misc import pwa_misc_bp
    from routes.pwa_public import pwa_public_bp, pwa_tenant_info_bp, public_api_bp
    from routes.webauthn import webauthn_bp
    from routes.admin_tenant import admin_tenant_bp
    from routes.public_tenant import public_tenant_bp
    from routes.integrations import integrations_bp
    from routes.recordatorios import recordatorios_bp
    from routes.historial import historial_bp
    from routes.notifications import notifications_bp
    from routes.reacciones import reacciones_bp
    from routes.ai_templates import ai_templates_bp
    from routes.ai import ai_bp
    from routes.promociones import promociones_bp
    from routes.catalog_mappings import catalog_mappings_bp, catalog_mappings_public_bp
    from routes.document_intelligence import document_intelligence_bp, document_intelligence_public_bp
    from routes.catalog_vector_sync import catalog_vector_sync_bp
    from routes.widget_settings import integracion_widget_bp, widget_settings_bp
    from routes.whatsapp_webhook import webhook_bp as whatsapp_webhook_bp
    from routes.whatsapp_promocionar import whatsapp_promocionar_bp
    from routes.omnichannel import omnichannel_bp
    from routes.mercadopago_webhook import mp_bp
    from routes.media import media_bp
    from routes.accessibility import accessibility_bp
    from routes.api_aliases import api_aliases_bp, public_aliases_bp
    from routes.portal_api import portal_api_bp
    from routes.pyme_catalog_fixes import pyme_catalog_fix_bp
    from routes.pyme_api import pyme_api_bp
    from routes.health import health_bp
    from routes.voice_routes import voice_bp
    from routes.catalog_routes import catalog_bp as catalog_v2_bp
    from routes.orders import orders_bp
    from routes.admin_fulfillment import admin_fulfillment_bp
    from routes.widget_config_routes import widget_config_bp
    from cli_commands import register_commands

    # Import survey blueprints conditionally or always, keeping imports consistent
    # (Assuming imports are available)
    try:
        from routes.encuestas_admin import (
            encuestas_admin_api_bp,
            encuestas_admin_bp,
            encuestas_admin_legacy_bp,
            encuestas_municipal_api_bp,
            encuestas_admin_publicas_bp, # Check if this exists in module
        )
        from routes.encuestas_public import (
            encuestas_public_bp,
            encuestas_public_legacy_bp,
            encuestas_public_share_bp,
            encuestas_public_publicas_bp, # Check if this exists in module
        )
    except ImportError:
        # Fallback if specific blueprint names differ in older versions
        encuestas_admin_publicas_bp = None
        encuestas_public_publicas_bp = None
        pass

    from routes.encuestas_analytics import (
        encuestas_analytics_bp,
        encuestas_analytics_legacy_bp,
        encuestas_analytics_admin_bp,
        encuestas_analytics_municipal_bp,
    )
    from routes.encuestas_anchor import (
        encuestas_anchor_bp,
        encuestas_anchor_legacy_bp,
        encuestas_anchor_admin_bp,
        encuestas_anchor_municipal_bp,
    )

    app.register_blueprint(config_bp)
    app.register_blueprint(auth_bp)

    # Alias de login para clientes que aún llaman a  en lugar de
    @app.route('/login', methods=['GET', 'POST', 'OPTIONS'])
    def login_alias():
        if request.method == "OPTIONS":
            return "", 204
        if request.method == "GET":
            return jsonify({"status": "ok"})
        return login_view_func()

    # Aliases /perfil
    from routes.auth import me_perfil_view_func
    @app.route('/perfil', methods=['GET', 'PUT', 'OPTIONS'])
    def perfil_alias():
        if request.method == "OPTIONS":
            return "", 204
        return me_perfil_view_func()

    # Más blueprints
    app.register_blueprint(legacy_auth_bp)
    app.register_blueprint(chat_bp)
    # Core APIs
    app.register_blueprint(ticket_bp)
    app.register_blueprint(crm_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(analytics_v2_bp)
    app.register_blueprint(gov_analytics_bp)
    # app.register_blueprint(upload_bp)
    app.register_blueprint(archivos_bp)

    # Mount Rubros BP flexibly
    # 1. At /rubros (legacy root)
    app.register_blueprint(rubros_bp, url_prefix="/rubros")
    # 2. At /api/rubros (new standard)
    app.register_blueprint(rubros_bp, url_prefix="/api/rubros", name="rubros_bp_api")

    app.register_blueprint(metricas_bp)
    app.register_blueprint(catalogo_bp)
    app.register_blueprint(productos_bp)
    app.register_blueprint(pedidos_bp)
    app.register_blueprint(carrito_bp)
    app.register_blueprint(public_resolver_bp)
    app.register_blueprint(public_municipios_bp)
    app.register_blueprint(subastas_bp)
    app.register_blueprint(puntos_public_bp)
    app.register_blueprint(puntos_bp)
    app.register_blueprint(rewards_rules_bp)
    app.register_blueprint(catalog_import_bp)
    app.register_blueprint(pedidos_from_file_bp)
    app.register_blueprint(kits_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(pedidos_checkout_bp)
    app.register_blueprint(estadisticas_bp)
    app.register_blueprint(empleados_bp)
    app.register_blueprint(categorias_bp)
    # Register legacy blueprint first to ensure /api/municipio/carrito isn't shadowed by /api/municipio/<slug>
    app.register_blueprint(legacy_public_v2_bp)
    app.register_blueprint(municipio_api_bp)
    app.register_blueprint(public_market_bp)
    app.register_blueprint(widget_public_bp)
    app.register_blueprint(recordatorios_bp)
    app.register_blueprint(historial_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(municipal_bp)
    app.register_blueprint(reacciones_bp)
    app.register_blueprint(ai_templates_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(promociones_bp)
    app.register_blueprint(catalog_mappings_bp)
    app.register_blueprint(catalog_mappings_public_bp)
    app.register_blueprint(document_intelligence_bp)
    app.register_blueprint(document_intelligence_public_bp)
    app.register_blueprint(catalog_vector_sync_bp)
    app.register_blueprint(integracion_widget_bp)
    app.register_blueprint(widget_settings_bp)
    app.register_blueprint(whatsapp_webhook_bp)
    app.register_blueprint(whatsapp_promocionar_bp)
    app.register_blueprint(omnichannel_bp)
    app.register_blueprint(mp_bp)
    # app.register_blueprint(bp_est)
    app.register_blueprint(media_bp)
    app.register_blueprint(accessibility_bp)
    app.register_blueprint(api_aliases_bp)
    app.register_blueprint(public_aliases_bp)
    app.register_blueprint(pwa_tenant_info_bp)
    app.register_blueprint(pwa_public_bp)
    app.register_blueprint(public_api_bp)

    # API aliases with "/api" prefix for frontends that hardcode that base path.
    # Flask allows registering the same blueprint multiple times as long as the
    # registration name is unique. This mirrors the existing routes under a
    # prefixed namespace without duplicating the view logic.
    app.register_blueprint(ticket_bp, url_prefix="/api", name="ticket_bp_api")
    app.register_blueprint(
        municipal_bp, url_prefix="/api", name="municipal_bp_api"
    )
    app.register_blueprint(market_bp)
    app.register_blueprint(market_admin_bp)
    # Register portal API with v1 prefix (primary)
    app.register_blueprint(portal_api_bp, url_prefix='/api/v1/portal/<tenant_slug>')
    # Register portal API with legacy/compat prefix (for existing frontend snippets)
    app.register_blueprint(portal_api_bp, name='portal_api_legacy', url_prefix='/api/portal/<tenant_slug>')
    app.register_blueprint(pwa_misc_bp)
    app.register_blueprint(pwa_app_bp)
    app.register_blueprint(pwa_app_legacy_bp)
    app.register_blueprint(webauthn_bp)
    app.register_blueprint(admin_tenant_bp)
    app.register_blueprint(public_tenant_bp)

    FEATURE_ENCUESTAS = True # Or load from config

    if encuestas_admin_publicas_bp:
        app.register_blueprint(encuestas_admin_publicas_bp)
    elif FEATURE_ENCUESTAS:
        try:
            app.register_blueprint(encuestas_admin_bp)
        except Exception:
            pass

    app.register_blueprint(pyme_catalog_fix_bp)
    app.register_blueprint(pyme_api_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(voice_bp)
    app.register_blueprint(catalog_v2_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(admin_fulfillment_bp)
    app.register_blueprint(widget_config_bp)

    from routes.super_admin import super_admin_bp
    app.register_blueprint(super_admin_bp)
    app.register_blueprint(integrations_bp, url_prefix='/api/integrations')

    from routes.admin_market import admin_market_bp
    app.register_blueprint(admin_market_bp, url_prefix='/api/admin/tenants/<slug>')

    if encuestas_public_publicas_bp:
        app.register_blueprint(encuestas_public_publicas_bp)
    elif FEATURE_ENCUESTAS:
        try:
            app.register_blueprint(encuestas_public_bp)
        except Exception:
            pass

    if FEATURE_ENCUESTAS:
        try:
            app.register_blueprint(encuestas_admin_api_bp)
            app.register_blueprint(encuestas_admin_legacy_bp)
            app.register_blueprint(encuestas_municipal_api_bp)
            app.register_blueprint(encuestas_public_legacy_bp)
            app.register_blueprint(encuestas_public_share_bp)
            app.register_blueprint(encuestas_analytics_bp)
            app.register_blueprint(encuestas_analytics_legacy_bp)
            app.register_blueprint(encuestas_analytics_admin_bp)
            app.register_blueprint(encuestas_analytics_municipal_bp)
            app.register_blueprint(encuestas_anchor_bp)
            app.register_blueprint(encuestas_anchor_legacy_bp)
            app.register_blueprint(encuestas_anchor_admin_bp)
            app.register_blueprint(encuestas_anchor_municipal_bp)
        except Exception:
            pass

    # Comandos CLI
    register_commands(app)

    # --- Robustness: Ensure DB tables exist if they are missing in production ---
    if not MIGRATIONS_ONLY:
        # Check if we should attempt to create tables.
        # We assume if the user is running the app, they expect it to work.
        # Catching specific errors is hard without making a query.
        # But create_all is idempotent if tables exist.
        # We wrap in try/except to avoid crashing if connection fails (let gunicorn retry or fail later).
        with app.app_context():
            try:
                # Force import of models to ensure all tables are registered
                import models  # noqa: F401
                # This will create tables if they don't exist.
                # It does NOT handle migrations (schema updates), but it fixes "UndefinedTable" for new deployments.
                db.create_all()

                # Auto-initialize tenants if missing (ensure demo tenants exist)
                if not app.config.get("SKIP_INIT_TENANTS"):
                    from init_tenants import init_tenants
                    init_tenants()

                db.session.remove()
                app.logger.info("Startup: db.create_all() executed successfully (tables ensured).")
            except Exception as e:
                # Log warning but proceed; maybe DB is readonly or connection transiently failed.
                app.logger.warning(f"Startup db.create_all() failed (ignoring): {e}")
                try:
                    db.session.remove()
                except Exception:
                    pass

    # Inicializar SocketIO solo en runtime normal
    if socketio is not None:
        socketio.init_app(app)

    # Inicializar Flask-Sock
    sock.init_app(app)

    return app

# Objeto global de app para Gunicorn (se puede omitir en tests configurando
# FLASK_SKIP_GLOBAL_APP=1)
if os.getenv("FLASK_SKIP_GLOBAL_APP") != "1":
    app = create_app(Config)
else:
    app = None

if __name__ == '__main__':
    if socketio is not None:
        socketio.run(app, debug=True, host='0.0.0.0', port=8080, allow_unsafe_werkzeug=True)
    else:
        # Fallback simple si alguien ejecuta con FLASK_MIGRATIONS_ONLY=1 localmente
        app.run(debug=True, host='0.0.0.0', port=8080)

# Fix Proxy headers for Render/Gunicorn
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# app.py
import ssl
import os
import sys
import logging
from typing import Pattern

# Cargar variables de entorno desde .env lo más temprano posible para que Config
# y el resto de la app vean las credenciales (e.g., SMTP) incluso cuando el
# proceso se inicia fuera del CLI de Flask.
from dotenv import load_dotenv

load_dotenv()  # override=False por defecto para respetar variables ya definidas

# --- Modo "solo migraciones" para que Alembic no cargue nada pesado ---
MIGRATIONS_ONLY = os.getenv("FLASK_MIGRATIONS_ONLY") == "1"

# Desactivar greendns SIEMPRE antes de importar eventlet (evita getaddrinfo 'type')
os.environ.setdefault("EVENTLET_NO_GREENDNS", "YES")

# Solo en runtime normal (no durante migraciones) parcheamos con eventlet
if not MIGRATIONS_ONLY:
    import eventlet
    eventlet.monkey_patch()

from flask import Flask, request, current_app, g, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

# Logging básico del proyecto
from services.logging_config import setup_logging
setup_logging()

# Ruta del proyecto al sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from flask_cors import CORS
from flask_session import Session
from sqlalchemy import event as sa_event

from config import Config, ALLOWED_ORIGINS
from config.feature_flags import FEATURE_ENCUESTAS
from extensions import db, migrate, login_manager  # livianos
from middleware import tenant_middleware
from utils.errors import ApiError


def _mask_token(value: str | None) -> str | None:
    """Return a partially masked token for diagnostic logging."""

    if not value:
        return value

    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)

    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _describe_database_uri(database_uri: str | None) -> str:
    """Return a sanitised representation of the configured database URI."""

    if not database_uri:
        return "<unset>"

    try:
        from sqlalchemy.engine.url import make_url

        url = make_url(database_uri)
        user_segment = ""
        if url.username:
            user_segment = _mask_token(url.username) or "*"
            password_segment = ":***" if url.password else ""
            user_segment = f"{user_segment}{password_segment}@"

        host = url.host or "localhost"
        port = f":{url.port}" if url.port else ""
        database = f"/{url.database}" if url.database else ""
        query = f"?{url.query}" if url.query else ""

        return f"{url.drivername}://{user_segment}{host}{port}{database}{query}"
    except Exception:
        return "<configured>"

# En migraciones NO importamos socket_service ni blueprints
if not MIGRATIONS_ONLY:
    # SocketIO real
    from socket_service import socketio  # usa eventlet
else:
    socketio = None  # marcador para evitar usarlo

# Solo en desarrollo local seteamos credenciales de Google (y no en migraciones)
if os.environ.get("FLASK_ENV") != "production" and not MIGRATIONS_ONLY:
    local_cred_path = os.path.join("data", "google_service_key.json")
    if os.path.exists(local_cred_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = local_cred_path
        print(f"✅ LOCAL DEV: Set GOOGLE_APPLICATION_CREDENTIALS to '{local_cred_path}'")
    else:
        print(f"⚠️ LOCAL DEV: Credential file not found at '{local_cred_path}'. Google services may fail.")

# Listener para SQLite (no afecta Postgres; se envuelve en try/except)
def my_on_connect_listener(dbapi_connection, connection_record):
    try:
        enable_fk = True
        try:
            from flask import current_app

            enable_fk = not current_app.config.get("DISABLE_SQLITE_FOREIGN_KEYS", False)
        except Exception:
            enable_fk = True

        cur = dbapi_connection.cursor()
        cur.execute(f"PRAGMA foreign_keys={'ON' if enable_fk else 'OFF'}")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
    except Exception:
        pass

def create_app(config_class=Config):
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    print("Creating app...")

    # Detrás de proxy/reverse-proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Cargar configuración
    app.config.from_object(config_class)
    print(f"Loaded config: {config_class}")
    print(
        "Database URI: "
        f"{_describe_database_uri(app.config.get('SQLALCHEMY_DATABASE_URI'))}"
    )
    print(f"DB object: {db}")

    if str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite"):
        engine_opts = app.config.setdefault("SQLALCHEMY_ENGINE_OPTIONS", {})
        engine_opts.setdefault("execution_options", {}).setdefault(
            "sqlite_foreign_keys", False
        )

    if app.config.get("TESTING"):
        app.config.setdefault("DISABLE_SQLITE_FOREIGN_KEYS", True)

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        status = getattr(error, "status_code", 400) or 400
        payload = {"error": error.message, "message": error.message}
        response = jsonify(payload)
        response.status_code = status
        return response

    # --- Diagnóstico de sesión (solo en runtime normal) ---
    if not MIGRATIONS_ONLY:
        print("--- DIAGNÓSTICO DE SESIÓN (desde app.py) ---")
        session_ext = Session()
        secret_key = app.config.get("SECRET_KEY")
        print(
            "SECRET_KEY configurada: "
            f"{'sí' if secret_key else 'no'}"
        )
        print(f"SESSION_COOKIE_SECURE: {app.config.get('SESSION_COOKIE_SECURE')}")
        print(f"SESSION_COOKIE_SAMESITE: {app.config.get('SESSION_COOKIE_SAMESITE')}")
        print(f"SESSION_TYPE: {app.config.get('SESSION_TYPE')}")
        print(f"SESSION_COOKIE_DOMAIN: {app.config.get('SESSION_COOKIE_DOMAIN')}")
        print("-----------------------------")

        # Rutas de prueba de sesión
        @app.route('/poner-memoria')
        def poner_memoria():
            from flask import session
            session['clave_de_prueba'] = 'funciona!'
            return "<h1>Memoria establecida. Ahora andá a /leer-memoria</h1>"

        @app.route('/leer-memoria')
        def leer_memoria():
            from flask import session
            valor = session.get('clave_de_prueba', '¡LA MEMORIA ESTÁ VACÍA!')
            return f"<h1>El valor guardado en la memoria es: {valor}</h1>"

        # Log de headers/cookies
        @app.before_request
        def log_headers():
            current_app.logger.info(f"--- RAW FLASK REQUEST.COOKIES: {request.cookies} ---")
            current_app.logger.debug(f"Request Headers (complete): {dict(request.headers)}")

        @app.before_request
        def attach_current_user():
            """
            Adjunta usuario a g.viewer. Evita tocar DB en migraciones.
            """
            from utils.auth_helpers import obtener_token, user_from_token
            from flask_login import current_user

            g.viewer = None
            if current_user and current_user.is_authenticated:
                g.viewer = current_user
                current_app.logger.debug(f"User {current_user.id} via Flask-Login.")
                return

            token = obtener_token()
            if token:
                user = user_from_token(token)
                if user:
                    g.viewer = user
                    current_app.logger.debug(f"User {user.id} via token.")

        tenant_middleware(app)

    # --- Inicialización de extensiones base (seguras para migraciones) ---
    with app.app_context():
        db.init_app(app)
        migrate.init_app(app, db)

        # Registrar PRAGMA listener si el engine es SQLite
        try:
            if db.engine.url.drivername.startswith("sqlite"):
                sa_event.listen(db.engine, "connect", my_on_connect_listener)
        except Exception:
            # En algunas fases de inicialización aún no hay engine
            pass

        if FEATURE_ENCUESTAS:
            from models import EncEncuesta  # noqa: F401  # asegura registro de tablas

        # En tests, crear tablas mínimo
        if app.config.get("TESTING") and not MIGRATIONS_ONLY:
            db.create_all()

    # Flask-Login: solo runtime normal (no necesario para migraciones)
    if not MIGRATIONS_ONLY:
        from models import User  # liviano, NO dispara spaCy/Google
        login_manager.init_app(app)
        login_manager.session_protection = "strong"
        login_manager.login_view = "auth.login"

        @login_manager.user_loader
        def load_user(user_id):
            try:
                return User.query.get(int(user_id))
            except Exception:
                return None

    # Sesiones en servidor (solo runtime normal)
    if not MIGRATIONS_ONLY:
        app.config['SESSION_SQLALCHEMY'] = db
        session_ext = Session()
        if app.config.get("TESTING"):
            app.config['SESSION_TYPE'] = 'filesystem'
        session_ext.init_app(app)

    # Logging de app
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    app.logger.propagate = False
    app.logger.info(f"Aplicación creada. Nivel de logging: {log_level}")
    app.logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # Manejadores de errores JSON consistentes
    @app.errorhandler(400)
    def handle_bad_request(error):
        detail = getattr(error, "description", None) or str(error)
        return jsonify({"error": "bad_request", "detail": detail}), 400

    @app.errorhandler(404)
    def handle_not_found(error):
        detail = getattr(error, "description", None) or "Recurso no encontrado"
        return jsonify({"error": "not_found", "detail": detail}), 404

    @app.errorhandler(500)
    def handle_server_error(error):
        app.logger.exception("Unhandled server error: %s", error)
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "server_error"}), 500

    @app.errorhandler(Exception)
    def handle_generic_exception(error):
        """Handle non-HTTP exceptions."""
        if isinstance(error, HTTPException):
            return error

        app.logger.exception("Unhandled Exception: %s", error)
        try:
            db.session.rollback()
            db.session.remove()
        except Exception:
            pass
        return jsonify({"error": "server_error", "detail": str(error)}), 500

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException):
        """Return JSON for any uncaught HTTP exception instead of HTML."""

        payload = {
            "error": error.name.lower().replace(" ", "_"),
            "detail": error.description,
        }
        return jsonify(payload), error.code

    # CORS y headers (solo runtime normal)
    if not MIGRATIONS_ONLY:
        cors_resources = {
            r"/public/*": {"origins": "*"},
            r"/pwa/*": {"origins": "*"},
            r"/api/pwa/*": {"origins": "*"},
            r"/api/public/*": {"origins": "*"},
            r"/api/rubros": {"origins": "*"},
            r"/api/rubros/*": {"origins": "*"},
            r"/admin/*": {
                "origins": ["https://www.chatboc.ar", "https://chatboc.ar"],
            },
            r"/integracion/*": {
                "origins": [
                    "https://www.chatboc.ar",
                    "https://chatboc-demo-widget-oigs.vercel.app",
                ],
                "supports_credentials": True,
            },
            r"/api/*": {"origins": ALLOWED_ORIGINS},
            r"/*": {"origins": ALLOWED_ORIGINS},
        }

        allow_headers = [
            "Content-Type",
            "Authorization",
            "Origin",
            "X-Chatboc-Token",
            "X-Entity-Token",
            "X-Chat-Session-Id",
            "X-Anon-Id",
            "Anon-Id",
            "x-anon-id",  # browsers sometimes compare case-sensitively
            "anon-id",
            "Cache-Control",
            "token",
            "X-Tenant",
            "X-Tenant-Slug",
            "X-Tenant-Id",
            "X-Widget-Token",
            "X-Whatsapp-Dst",
        ]

        allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

        CORS(
            app,
            resources=cors_resources,
            supports_credentials=True,
            methods=allow_methods,
            allow_headers=allow_headers,
            expose_headers=[
                "Content-Type",
                "Authorization",
                "X-Anon-Id",
                "Anon-Id",
            ],
        )

        @app.after_request
        def add_permissions_policy(resp):
            policy = current_app.config.get("PERMISSIONS_POLICY_HEADER", "geolocation=(self)")
            resp.headers.setdefault("Permissions-Policy", policy)
            return resp

        def _origin_is_allowed(origin: str | None) -> bool:
            if not origin:
                return False

            for allowed in ALLOWED_ORIGINS:
                if isinstance(allowed, Pattern):
                    if allowed.match(origin):
                        return True
                elif origin.rstrip("/") == str(allowed).rstrip("/"):
                    return True

            return False

        @app.after_request
        def ensure_cors_headers(resp):
            origin = request.headers.get("Origin")
            if not _origin_is_allowed(origin):
                return resp

            # Override any duplicate CORS headers emitted upstream so browsers
            # don't reject responses with repeated origins.
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"

            # Echo CORS allowances for preflight responses to ensure custom headers like
            # "x-anon-id" are accepted by browsers.
            resp.headers["Access-Control-Allow-Headers"] = ", ".join(allow_headers)
            resp.headers["Access-Control-Allow-Methods"] = ", ".join(allow_methods)

            vary_header = resp.headers.get("Vary")
            if vary_header:
                if "Origin" not in vary_header:
                    resp.headers["Vary"] = f"{vary_header}, Origin"
            else:
                resp.headers["Vary"] = "Origin"

            return resp

    # --- Blueprints (solo runtime normal) ---
    from routes.config import config_bp
    from routes.auth import auth_bp, login as login_view_func, me_perfil as me_perfil_view_func
    from routes.legacy_auth import legacy_auth_bp
    from routes.chat import chat_bp
    from routes.ticket import ticket_bp
    from routes.crm import crm_bp
    from routes.analytics import analytics_bp
    from routes.gov_analytics import gov_analytics_bp
    from services.upload_processor import upload_bp
    from routes.archivos import archivos_bp
    from routes.rubros import rubros_bp
    from routes.metricas import metricas_bp
    from routes.municipio_api import municipio_api_bp, public_market_bp, widget_public_bp, legacy_public_v2_bp
    from routes.catalogo import catalogo_bp
    from routes.productos import productos_bp
    from routes.pedidos import pedidos_bp
    from routes.carrito import carrito_bp
    from routes.catalog_import import catalog_import_bp
    from routes.checkout import checkout_bp, pedidos_checkout_bp
    from routes.estadisticas import estadisticas_bp
    from routes.empleados import empleados_bp
    from routes.categorias import categorias_bp
    from routes.recordatorios import recordatorios_bp
    from routes.historial import historial_bp
    from routes.notifications import notifications_bp
    from routes.puntos import puntos_bp, puntos_public_bp
    from routes.municipal_legacy import municipal_bp
    from routes.reacciones import reacciones_bp
    from routes.ai_templates import ai_templates_bp
    from routes.ai import ai_bp as ai_suggest_bp
    from routes.promociones import promociones_bp
    from routes.catalog_mappings import catalog_mappings_bp, catalog_mappings_public_bp
    from routes.catalog_vector_sync import catalog_vector_sync_bp
    from routes.document_intelligence import (
        document_intelligence_bp,
        document_intelligence_public_bp,
    )
    from routes.rewards_rules import rewards_rules_bp
    from routes.api_aliases import api_aliases_bp, public_aliases_bp
    from routes.whatsapp_webhook import webhook_bp as whatsapp_webhook_bp
    from routes.whatsapp_promocionar import whatsapp_promocionar_bp
    from routes.omnichannel import omnichannel_bp
    from routes.mercadopago_webhook import mp_bp
    from routes.estacionamiento import bp_est
    from routes.media import media_bp
    from routes.accessibility import accessibility_bp
    from routes.encuestas_publicas import (
        encuestas_admin_bp,
        encuestas_public_bp,
    )
    from routes.pwa_public import pwa_public_bp, pwa_tenant_info_bp, public_api_bp
    from routes.market import market_admin_bp, market_bp
    from routes.portal_api import portal_api_bp
    from routes.public_resolver import public_resolver_bp, public_municipios_bp
    from routes.widget_settings import integracion_widget_bp, widget_settings_bp
    from routes.subastas import subastas_bp
    from routes.pedidos_from_file import pedidos_from_file_bp
    from routes.kits import kits_bp
    from routes.pwa_app import pwa_app_bp, pwa_app_legacy_bp
    from routes.pwa_misc import pwa_misc_bp
    from routes.webauthn import webauthn_bp
    from routes.admin_tenant import admin_tenant_bp
    from routes.public_tenant import public_tenant_bp
    from routes.integrations import integrations_bp
    from routes.pyme_catalog_fixes import pyme_catalog_fix_bp
    from routes.pyme_api import pyme_api_bp
    from routes.health import health_bp
    from cli_commands import register_commands

    if FEATURE_ENCUESTAS:
        from routes.encuestas_admin import (
            encuestas_admin_api_bp,
            encuestas_admin_bp,
            encuestas_admin_legacy_bp,
        )
        from routes.encuestas_public import (
            encuestas_public_bp,
            encuestas_public_legacy_bp,
            encuestas_public_share_bp,
        )
    from routes.encuestas_analytics import (
        encuestas_analytics_bp,
        encuestas_analytics_legacy_bp,
    )
    from routes.encuestas_anchor import (
        encuestas_anchor_bp,
        encuestas_anchor_legacy_bp,
    )

    app.register_blueprint(config_bp)
    app.register_blueprint(auth_bp)

    # Alias de login para clientes que aún llaman a `/login` en lugar de `/auth/login`
    @app.route('/login', methods=['GET', 'POST', 'OPTIONS'])
    def login_alias():
        if request.method == "OPTIONS":
            return "", 204
        if request.method == "GET":
            return jsonify({"status": "ok"})
        return login_view_func()

    # Aliases /perfil
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
    app.register_blueprint(gov_analytics_bp)
    app.register_blueprint(upload_bp)
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
    app.register_blueprint(ai_suggest_bp)
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
    app.register_blueprint(bp_est)
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
    app.register_blueprint(encuestas_admin_bp)
    app.register_blueprint(pyme_catalog_fix_bp)
    app.register_blueprint(pyme_api_bp)
    app.register_blueprint(health_bp)

    from routes.super_admin import super_admin_bp
    app.register_blueprint(super_admin_bp)
    app.register_blueprint(integrations_bp, url_prefix='/api/integrations')

    from routes.admin_market import admin_market_bp
    app.register_blueprint(admin_market_bp, url_prefix='/api/admin/tenants/<slug>')
    app.register_blueprint(encuestas_public_bp)
    if FEATURE_ENCUESTAS:
        app.register_blueprint(encuestas_admin_api_bp)
        app.register_blueprint(encuestas_admin_legacy_bp)
        app.register_blueprint(encuestas_public_legacy_bp)
        app.register_blueprint(encuestas_public_share_bp)
        app.register_blueprint(encuestas_analytics_bp)
        app.register_blueprint(encuestas_analytics_legacy_bp)
        app.register_blueprint(encuestas_anchor_bp)
        app.register_blueprint(encuestas_anchor_legacy_bp)

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

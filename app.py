import ssl
import os
import sys
import uuid

# --- Modo "solo migraciones" o "testing" para evitar carga pesada de eventlet ---
MIGRATIONS_ONLY = os.getenv("FLASK_MIGRATIONS_ONLY") == "1"
TESTING_MODE = (
    os.getenv("TESTING") == "1"
    or "pytest" in sys.modules
    or "unittest" in sys.modules
)
NON_WEB_PROCESS = os.getenv("CHATBOC_PROCESS_ROLE", "").strip().lower() in {
    "whatsapp-durable-worker",
    "domain-effect-worker",
    "survey-effect-worker",
}
os.environ.setdefault("EVENTLET_NO_GREENDNS", "YES")

# Monkey patch must happen before importing any other modules that might use threads/sockets
# Solo en runtime normal (no migraciones, no testing)
if not MIGRATIONS_ONLY and not TESTING_MODE and not NON_WEB_PROCESS:
    import eventlet
    eventlet.monkey_patch()
import logging
from typing import Pattern

# Cargar variables de entorno desde .env lo más temprano posible para que Config
# y el resto de la app vean las credenciales (e.g., SMTP) incluso cuando el
# proceso se inicia fuera del CLI de Flask.
from dotenv import load_dotenv

# ``utf-8-sig`` accepts regular UTF-8 and also strips a Windows BOM. Without
# it, a BOM-prefixed first variable is parsed as ``\ufeffOPENAI_API_KEY`` and
# the application silently behaves as if the configured key did not exist.
load_dotenv(encoding="utf-8-sig")  # override=False: respect process secrets
from flask import Flask, request, current_app, g, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

# Logging básico del proyecto
from services.logging_config import (
    PrivacyRedactionFilter,
    TruncatingFormatter,
    setup_logging,
)
setup_logging()


_SENSITIVE_THIRD_PARTY_LOGGERS = (
    "twilio.http_client",
    "httpx",
    "httpcore",
)


def _suppress_sensitive_third_party_info_logs() -> None:
    """Keep provider diagnostics at warning/error without request-level payload logs."""

    for logger_name in _SENSITIVE_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


_suppress_sensitive_third_party_info_logs()

# Ruta del proyecto al sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from flask_cors import CORS
from flask_session import Session
from sqlalchemy import event as sa_event

from config import (
    ALLOWED_ORIGINS,
    LOCAL_DEV_ORIGIN_PATTERN,
    Config,
    is_same_site_credential_origin,
    validate_runtime_security,
)
from config.feature_flags import FEATURE_ENCUESTAS
from extensions import db, migrate, login_manager, sock, limiter  # livianos + limiter
from middleware import tenant_middleware
from utils.errors import ApiError
from utils.contact_identity import resolve_contact_identity_from_request
from utils.safe_logging import describe_database_uri


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "t", "yes", "y"}


def _running_on_render() -> bool:
    return os.getenv("RENDER", "").strip().lower() == "true" or bool(os.getenv("RENDER_EXTERNAL_URL"))


_PUBLIC_CORS_TREE_PREFIXES = (
    "/public",
    "/api/public",
    "/api/v2/public",
    "/api/pwa/public",
    "/api/pwa/kits",
    "/widget",
    "/api/tickets/public",
)
_PUBLIC_CORS_EXACT_PATHS = {
    "/pwa/anon-id",
    "/api/pwa/anon-id",
    "/pwa/tenant-info",
    "/api/pwa/tenant-info",
}
_PUBLIC_WIDGET_AUTH_PREFIXES = ("/auth/widget", "/api/auth/widget")


def _is_public_cross_origin_path(path: str) -> bool:
    normalized = str(path or "").rstrip("/") or "/"
    if normalized in _PUBLIC_CORS_EXACT_PATHS:
        return True
    if normalized == "/api/rubros" or normalized.startswith("/api/rubros/"):
        return True
    if any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _PUBLIC_CORS_TREE_PREFIXES
    ):
        return True
    return any(
        normalized == prefix
        or normalized.startswith(f"{prefix}/")
        or normalized.startswith(f"{prefix}-")
        for prefix in _PUBLIC_WIDGET_AUTH_PREFIXES
    )

# En migraciones NO importamos socket_service ni blueprints
if not MIGRATIONS_ONLY:
    # SocketIO real
    from socket_service import (  # usa eventlet
        build_fail_closed_socketio_redis_manager,
        socketio,
    )
else:
    socketio = None  # marcador para evitar usarlo
    build_fail_closed_socketio_redis_manager = None

# Solo en desarrollo local seteamos credenciales de Google (y no en migraciones)
if os.environ.get("FLASK_ENV") != "production" and not MIGRATIONS_ONLY:
    local_cred_path = os.path.join("data", "google_service_key.json")
    if os.path.exists(local_cred_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = local_cred_path
        print("LOCAL DEV: Google application credentials configured")
    else:
        print("LOCAL DEV: Google application credentials are not configured")

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
    app.logger.info("Creating app")

    # Detrás de proxy/reverse-proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Cargar configuración
    app.config.from_object(config_class)
    if not callable(app.config.get("META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER")):
        from services.meta_flow_runtime import create_meta_flow_runtime_resolver

        app.config["META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER"] = (
            create_meta_flow_runtime_resolver()
        )
    security_errors = validate_runtime_security(app.config)
    if security_errors:
        raise RuntimeError(" ".join(security_errors))
    app.logger.info("Loaded config: %s", config_class)
    app.logger.info(
        "Database URI: %s",
        describe_database_uri(app.config.get("SQLALCHEMY_DATABASE_URI")),
    )
    app.logger.debug("DB object: %s", db)

    if str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite"):
        configured_engine_opts = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        engine_opts = {
            key: value
            for key, value in configured_engine_opts.items()
            if key in {"connect_args", "execution_options"}
        }
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts
        engine_opts.setdefault("execution_options", {}).setdefault(
            "sqlite_foreign_keys", False
        )

    if app.config.get("TESTING"):
        app.config.setdefault("DISABLE_SQLITE_FOREIGN_KEYS", True)

    @app.route("/", methods=["GET", "HEAD"])
    def root():
        return jsonify({"status": "ok"})

    @app.route("/health", methods=["GET", "HEAD"])
    def health():
        return jsonify({"status": "ok"})

    # Error handling unificado JSON
    def _request_id() -> str:
        incoming = (
            request.headers.get("X-Request-Id")
            or request.headers.get("X-Correlation-Id")
            or getattr(g, "request_id", None)
            or ""
        )
        request_id = str(incoming).strip() or uuid.uuid4().hex
        g.request_id = request_id
        return request_id

    def _shared_error_response(
        *,
        status_code: int,
        message: str,
        reason_code: str,
        retryable: bool = False,
        action_hint: str | None = None,
    ):
        request_id = _request_id()
        payload = {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": retryable,
            "action_hint": action_hint or reason_code,
            "request_id": request_id,
            "error": {"code": status_code, "message": message},
            "message": message,
            "detail": message,
        }
        response = jsonify(payload)
        response.status_code = status_code
        response.headers["X-Request-Id"] = request_id
        return response

    @app.errorhandler(400)
    def handle_bad_request(error):
        detail = getattr(error, "description", None) or str(error)
        return _shared_error_response(
            status_code=400,
            message=detail,
            reason_code="bad_request",
            action_hint="fix_request",
        )

    @app.errorhandler(404)
    def handle_not_found(error):
        detail = getattr(error, "description", None) or "Recurso no encontrado"
        return _shared_error_response(
            status_code=404,
            message=detail,
            reason_code="not_found",
            action_hint="check_url",
        )

    @app.errorhandler(500)
    def handle_server_error(error):
        app.logger.error(
            "Unhandled server error error_type=%s",
            type(error).__name__,
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return _shared_error_response(
            status_code=500,
            message="Internal server error",
            reason_code="server_error",
            action_hint="retry_later",
            retryable=True,
        )

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException):
        status = error.code or 500
        return _shared_error_response(
            status_code=status,
            message=error.description,
            reason_code="http_error" if status != 404 else "not_found",
            action_hint="check_request",
            retryable=500 <= status < 600,
        )

    @app.errorhandler(ApiError)
    def handle_custom_api_error(error: ApiError):
        status = getattr(error, "status_code", 400) or 400
        return _shared_error_response(
            status_code=status,
            message=error.message,
            reason_code=getattr(error, "reason_code", None) or "api_error",
            action_hint=getattr(error, "action_hint", None) or "check_request",
            retryable=bool(getattr(error, "retryable", False)),
        )

    # --- Diagnóstico de sesión (solo en runtime normal) ---
    if not MIGRATIONS_ONLY:
        session_ext = Session()
        secret_key = app.config.get("SECRET_KEY")
        app.logger.info(
            "Session config: secret_key=%s secure=%s samesite=%s type=%s domain=%s",
            "configured" if secret_key else "missing",
            app.config.get("SESSION_COOKIE_SECURE"),
            app.config.get("SESSION_COOKIE_SAMESITE"),
            app.config.get("SESSION_TYPE"),
            app.config.get("SESSION_COOKIE_DOMAIN"),
        )

        # Rutas de prueba de sesión
        if app.config.get("ENABLE_DEBUG_SESSION_ROUTES"):
            @app.route("/poner-memoria")
            def poner_memoria():
                from flask import session

                session["clave_de_prueba"] = "funciona!"
                return jsonify({"ok": True, "next": "/leer-memoria"})

            @app.route("/leer-memoria")
            def leer_memoria():
                from flask import session

                valor = session.get("clave_de_prueba")
                return jsonify({"ok": bool(valor), "value": valor})
        # Safe request diagnostics: never emit cookie/header values or tokens.
        @app.before_request
        def log_headers():
            current_app.logger.debug(
                "Request received method=%s endpoint=%s header_count=%s cookie_count=%s",
                request.method,
                request.endpoint or "unmatched",
                len(request.headers),
                len(request.cookies),
            )

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
                current_app.logger.debug("Authenticated request source=flask_login")
                return

            token = obtener_token()
            if token:
                user = user_from_token(token)
                if user:
                    g.viewer = user
                    current_app.logger.debug("Authenticated request source=bearer_token")

        @app.before_request
        def attach_contact_identity():
            g.contact_identity = resolve_contact_identity_from_request(request)

        tenant_middleware(app)

    # --- Inicialización de extensiones base (seguras para migraciones) ---
    with app.app_context():
        db.init_app(app)
        migrate.init_app(app, db)
        limiter.init_app(app) # Initializing limiter here

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
                from utils.auth_helpers import (
                    is_clerk_managed_user,
                    is_demo_user_account,
                    is_user_auth_disabled,
                )
                from utils.roles import is_super_admin_role

                user = User.query.get(int(user_id))
                if is_user_auth_disabled(user) or is_demo_user_account(user):
                    return None
                if user and is_clerk_managed_user(user):
                    return None
                if user and is_super_admin_role(getattr(user, "rol", None)):
                    return None
                return user
            except Exception:
                return None

    # Sesiones en servidor (solo runtime normal)
    if not MIGRATIONS_ONLY:
        app.config['SESSION_SQLALCHEMY'] = db
        session_ext = Session()
        if app.config.get("TESTING"):
            from cachelib.simple import SimpleCache

            app.config['SESSION_TYPE'] = 'cachelib'
            app.config['SESSION_CACHELIB'] = SimpleCache(default_timeout=300)
        session_ext.init_app(app)

    # Logging de app
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(PrivacyRedactionFilter())
    handler.setFormatter(TruncatingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    app.logger.propagate = False
    app.logger.info(f"Aplicación creada. Nivel de logging: {log_level}")
    app.logger.info(
        "Usando base de datos: %s",
        describe_database_uri(app.config.get("SQLALCHEMY_DATABASE_URI")),
    )

    # CORS y headers (solo runtime normal)
    if not MIGRATIONS_ONLY:
        env_name = str(app.config.get("ENV") or "").strip().lower()
        production_runtime = env_name in {"prod", "production"} or _running_on_render()
        credentialed_origins = list(
            app.config.get("CORS_CREDENTIALS_ALLOWED_ORIGINS") or ALLOWED_ORIGINS
        )
        if app.config.get("CORS_ALLOW_LOCAL_DEV") and not production_runtime:
            if LOCAL_DEV_ORIGIN_PATTERN not in credentialed_origins:
                credentialed_origins.append(LOCAL_DEV_ORIGIN_PATTERN)
        if production_runtime:
            credentialed_origins = [
                origin
                for origin in credentialed_origins
                if not isinstance(origin, Pattern)
                and isinstance(origin, str)
                and origin.strip() != "*"
                and is_same_site_credential_origin(
                    origin,
                    backend_url=app.config.get("BACKEND_URL"),
                    public_root_domain=app.config.get("PUBLIC_ROOT_DOMAIN"),
                )
            ]

        public_cors = {"origins": "*", "supports_credentials": False}
        credentialed_cors = {
            "origins": credentialed_origins,
            "supports_credentials": True,
        }
        cors_resources = {
            r"/api/auth/widget(?:/.*|-.*)?$": public_cors,
            r"/auth/widget(?:/.*|-.*)?$": public_cors,
            r"/api/tickets/public(?:/.*)?$": public_cors,
            r"/api/v2/public(?:/.*)?$": public_cors,
            r"/api/public(?:/.*)?$": public_cors,
            r"/api/pwa/public(?:/.*)?$": public_cors,
            r"/api/pwa/kits(?:/.*)?$": public_cors,
            r"/api/pwa/(?:anon-id|tenant-info)$": public_cors,
            r"/public(?:/.*)?$": public_cors,
            r"/pwa/(?:anon-id|tenant-info)$": public_cors,
            r"/widget(?:/.*)?$": public_cors,
            r"/api/rubros(?:/.*)?$": public_cors,
            r"/api/analytics(?:/.*)?$": credentialed_cors,
            r"/admin(?:/.*)?$": credentialed_cors,
            r"/integracion(?:/.*)?$": credentialed_cors,
            r"/api(?:/.*)?$": credentialed_cors,
            r"/.*": credentialed_cors,
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
            "X-Demo-Session",
            "X-Demo-Session-Id",
            "Idempotency-Key",
            "X-Token",
            "x-token",
            "X-Whatsapp-Dst",
            "X-Checkout-Origin",
            "X-Turnstile-Token",
            "X-Contact-Key",
            "X-Conversation-Id",
            "X-Tracking-Pin",
            "X-Tracking-Token",
            "pin",
        ]

        allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

        @app.after_request
        def add_permissions_policy(resp):
            policy = current_app.config.get("PERMISSIONS_POLICY_HEADER", "geolocation=(self)")
            resp.headers.setdefault("Permissions-Policy", policy)
            return resp

        @app.after_request
        def expose_contact_identity(resp):
            identity = getattr(g, "contact_identity", None)
            if not isinstance(identity, dict):
                return resp

            contact_key = identity.get("contact_key")
            conversation_id = identity.get("conversation_id")

            if contact_key:
                resp.headers.setdefault("X-Contact-Key", str(contact_key))
            if conversation_id:
                resp.headers.setdefault("X-Conversation-Id", str(conversation_id))

            return resp

        def _credentialed_origin_is_allowed(origin: str | None) -> bool:
            if not origin:
                return False
            for allowed in credentialed_origins:
                if isinstance(allowed, Pattern):
                    if allowed.match(origin):
                        return True
                elif origin.rstrip("/") == str(allowed).rstrip("/"):
                    return True
            return False

        def _set_single_header(resp, header_name: str, value: str) -> None:
            while header_name in resp.headers:
                del resp.headers[header_name]
            resp.headers[header_name] = value

        cors_header_names = (
            "Access-Control-Allow-Origin",
            "Access-Control-Allow-Credentials",
            "Access-Control-Allow-Headers",
            "Access-Control-Allow-Methods",
            "Access-Control-Expose-Headers",
        )

        def _clear_cors_headers(resp) -> None:
            for header_name in cors_header_names:
                while header_name in resp.headers:
                    del resp.headers[header_name]

        def _set_cors_vary(resp) -> None:
            vary_values = {
                item.strip()
                for item in str(resp.headers.get("Vary") or "").split(",")
                if item.strip()
            }
            vary_values.add("Origin")
            resp.headers["Vary"] = ", ".join(sorted(vary_values))

        exposed_headers = (
            "Content-Type, Authorization, X-Request-Id, X-Correlation-Id, "
            "X-Anon-Id, Anon-Id, X-Contact-Key, X-Conversation-Id"
        )

        @app.after_request
        def ensure_cors_headers(resp):
            origin = request.headers.get("Origin")
            if not origin:
                return resp

            if _is_public_cross_origin_path(request.path):
                _clear_cors_headers(resp)
                resp.headers.setdefault("X-Request-Id", _request_id())
                _set_single_header(resp, "Access-Control-Allow-Origin", origin)
                _set_single_header(resp, "Access-Control-Allow-Headers", ", ".join(allow_headers))
                _set_single_header(resp, "Access-Control-Allow-Methods", ", ".join(allow_methods))
                _set_single_header(resp, "Access-Control-Expose-Headers", exposed_headers)
                _set_cors_vary(resp)
                return resp

            if not _credentialed_origin_is_allowed(origin):
                _clear_cors_headers(resp)
                return resp

            resp.headers.setdefault("X-Request-Id", _request_id())

            # Override any duplicate CORS headers emitted upstream so browsers
            # don't reject responses with repeated origins.
            _set_single_header(resp, "Access-Control-Allow-Origin", origin)
            _set_single_header(resp, "Access-Control-Allow-Credentials", "true")

            # Echo CORS allowances for preflight responses to ensure custom headers like
            # "x-anon-id" are accepted by browsers.
            _set_single_header(resp, "Access-Control-Allow-Headers", ", ".join(allow_headers))
            _set_single_header(resp, "Access-Control-Allow-Methods", ", ".join(allow_methods))
            _set_single_header(resp, "Access-Control-Expose-Headers", exposed_headers)
            _set_cors_vary(resp)

            return resp

        CORS(
            app,
            resources=cors_resources,
            supports_credentials=False,
            methods=allow_methods,
            allow_headers=allow_headers,
            expose_headers=[
                "Content-Type",
                "Authorization",
                "X-Request-Id",
                "X-Correlation-Id",
                "X-Anon-Id",
                "Anon-Id",
                "X-Contact-Key",
                "X-Conversation-Id",
            ],
        )

    # --- Blueprints (solo runtime normal) ---
    # Register blueprints carefully to avoid circular imports
    from routes.config import config_bp
    from routes.auth import auth_api_bp, auth_bp, login_view_func, me_perfil_view_func
    from routes.legacy_auth import legacy_auth_bp
    from routes.chat import chat_bp
    from routes.ticket import ticket_bp
    from routes.crm.routes import crm_bp
    from routes.analytics import analytics_bp
    from routes.admin_analytics import admin_analytics_bp
    from routes.admin_ai import admin_ai_bp
    from routes.analytics_routes import analytics_v2_bp
    from routes.analytics_kpis import analytics_kpis_bp
    from routes.education_routes import education_bp
    from routes.education_kb import education_kb_bp
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
    from routes.backoffice import backoffice_bp, backoffice_v2_bp
    from routes.whatsapp_webhook import webhook_bp as whatsapp_webhook_bp
    from routes.whatsapp_promocionar import whatsapp_promocionar_bp
    from routes.omnichannel import omnichannel_bp
    from routes.mercadopago_webhook import mp_bp
    from routes.estacionamiento import bp_est
    from routes.media import media_bp
    from routes.accessibility import accessibility_bp

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
    from routes.public_flow_runtime import public_flow_runtime_bp
    from routes.public_finance import public_finance_bp
    from routes.integrations import integrations_bp
    from routes.pyme_catalog_fixes import pyme_catalog_fix_bp
    from routes.pyme_api import pyme_api_bp
    from routes.health import health_bp
    from routes.voice_routes import voice_bp
    from routes.catalog_routes import catalog_bp as catalog_v2_bp
    from routes.orders import orders_bp
    from routes.admin_fulfillment import admin_fulfillment_bp
    from routes.widget_config_routes import widget_config_bp # NEW
    from routes.geo_routes import geo_bp
    from routes.conversations import conversations_bp
    from routes.access_control import access_control_bp
    from routes.whatsapp_rules import whatsapp_rules_bp
    from routes.meta_flow_data_exchange import meta_flow_data_exchange_bp
    from routes.v2 import register_v2_blueprints
    from cli_commands import register_commands

    # Public survey URLs always resolve through the canonical EncEncuesta stack.
    # FEATURE_ENCUESTAS may disable that stack through its request guard, but it
    # must never select the retired PublicSurvey persistence implementation.
    from routes.encuestas_public import (
        encuestas_public_bp,
        encuestas_public_legacy_bp,
        encuestas_public_share_bp,
    )

    if FEATURE_ENCUESTAS:
        from routes.encuestas_admin import (
            encuestas_admin_api_bp,
            encuestas_admin_bp,
            encuestas_admin_legacy_bp,
            encuestas_admin_surveys_api_bp,
            encuestas_admin_surveys_legacy_bp,
            encuestas_municipal_api_bp,
            encuestas_municipal_surveys_api_bp,
        )

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
        encuestas_anchor_admin_surveys_bp,
        encuestas_anchor_legacy_surveys_bp,
        encuestas_anchor_municipal_surveys_bp,
    )

    # Register
    app.register_blueprint(config_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(auth_api_bp)
    register_v2_blueprints(app)

    # Alias de login para clientes que aún llaman a  en lugar de
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
    app.register_blueprint(admin_analytics_bp)
    app.register_blueprint(admin_ai_bp)
    app.register_blueprint(analytics_v2_bp)
    app.register_blueprint(analytics_kpis_bp)
    app.register_blueprint(education_bp)
    app.register_blueprint(education_kb_bp)
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
    app.register_blueprint(backoffice_bp)
    app.register_blueprint(backoffice_v2_bp)
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
    app.register_blueprint(empleados_bp, url_prefix="/api/empleados", name="empleados_bp_api")
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
    app.register_blueprint(public_flow_runtime_bp)
    app.register_blueprint(public_finance_bp)

    if FEATURE_ENCUESTAS:
        app.register_blueprint(encuestas_admin_bp)

    app.register_blueprint(pyme_catalog_fix_bp)
    app.register_blueprint(pyme_api_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(voice_bp)
    app.register_blueprint(catalog_v2_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(admin_fulfillment_bp)
    app.register_blueprint(widget_config_bp) # Register new BP
    app.register_blueprint(geo_bp)
    app.register_blueprint(conversations_bp)
    app.register_blueprint(access_control_bp)
    app.register_blueprint(whatsapp_rules_bp)
    app.register_blueprint(meta_flow_data_exchange_bp)

    from routes.tracking_ui import tracking_ui_bp
    app.register_blueprint(tracking_ui_bp)

    from routes.super_admin import super_admin_bp
    app.register_blueprint(super_admin_bp)
    app.register_blueprint(integrations_bp, url_prefix='/api/integrations')

    from routes.admin_market import admin_market_bp
    app.register_blueprint(admin_market_bp, url_prefix='/api/admin/tenants/<slug>')

    app.register_blueprint(encuestas_public_bp)
    app.register_blueprint(encuestas_public_legacy_bp)
    app.register_blueprint(encuestas_public_share_bp)

    if FEATURE_ENCUESTAS:
        app.register_blueprint(encuestas_admin_api_bp)
        app.register_blueprint(encuestas_admin_legacy_bp)
        app.register_blueprint(encuestas_admin_surveys_api_bp)
        app.register_blueprint(encuestas_admin_surveys_legacy_bp)
        app.register_blueprint(encuestas_municipal_api_bp)
        app.register_blueprint(encuestas_municipal_surveys_api_bp)
        app.register_blueprint(encuestas_analytics_bp)
        app.register_blueprint(encuestas_analytics_legacy_bp)
        app.register_blueprint(encuestas_analytics_admin_bp)
        app.register_blueprint(encuestas_analytics_municipal_bp)
        app.register_blueprint(encuestas_anchor_bp)
        app.register_blueprint(encuestas_anchor_legacy_bp)
        app.register_blueprint(encuestas_anchor_admin_bp)
        app.register_blueprint(encuestas_anchor_municipal_bp)
        app.register_blueprint(encuestas_anchor_admin_surveys_bp)
        app.register_blueprint(encuestas_anchor_legacy_surveys_bp)
        app.register_blueprint(encuestas_anchor_municipal_surveys_bp)

    # Comandos CLI
    register_commands(app)

    # Runtime bootstrap only when explicitly enabled (recommended: migrations/CLI in production).
    if not MIGRATIONS_ONLY:
        runtime_schema_sync_enabled = bool(app.config.get("ENABLE_RUNTIME_SCHEMA_SYNC"))
        runtime_tenant_init_enabled = bool(app.config.get("ENABLE_RUNTIME_TENANT_INIT"))
        if _running_on_render() and not _truthy_env("ALLOW_RUNTIME_BOOTSTRAP_ON_RENDER"):
            if runtime_schema_sync_enabled or runtime_tenant_init_enabled:
                app.logger.warning(
                    "Runtime bootstrap ignored on Render. "
                    "Run migrations/bootstrap explicitly or set ALLOW_RUNTIME_BOOTSTRAP_ON_RENDER=true."
                )
            runtime_schema_sync_enabled = False
            runtime_tenant_init_enabled = False

        if runtime_schema_sync_enabled or runtime_tenant_init_enabled:
            with app.app_context():
                try:
                    if runtime_schema_sync_enabled:
                        # Force import of models so table metadata is fully loaded.
                        import models  # noqa: F401
                        db.create_all()
                        app.logger.warning(
                            "Startup runtime schema sync executed (db.create_all). "
                            "Use Flask-Migrate in production deployments."
                        )

                    if runtime_tenant_init_enabled and not app.config.get("SKIP_INIT_TENANTS"):
                        from init_tenants import init_tenants

                        init_tenants()
                        app.logger.warning(
                            "Startup runtime tenant init executed. "
                            "Prefer explicit CLI/bootstrap commands in production."
                        )

                except Exception as exc:
                    app.logger.warning(
                        "Startup runtime bootstrap failed error_type=%s",
                        type(exc).__name__,
                    )
                finally:
                    try:
                        db.session.remove()
                    except Exception:
                        pass
        else:
            app.logger.info(
                "Startup runtime bootstrap disabled. "
                "Apply migrations explicitly (e.g. 'flask db upgrade')."
            )

    # Inicializar SocketIO solo en runtime normal. Web processes subscribe to
    # the shared Redis channel; the durable survey worker is an external,
    # write-only publisher so it cannot mistake its own process for a client
    # delivery boundary.
    if socketio is not None:
        socket_queue_url = str(
            app.config.get("SOCKETIO_MESSAGE_QUEUE_URL") or ""
        ).strip()
        socket_channel = str(
            app.config.get(
                "SOCKETIO_MESSAGE_QUEUE_CHANNEL",
                "chatboc-realtime-v1",
            )
            or ""
        ).strip()
        socket_healthcheck_timeout = float(
            app.config.get(
                "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS",
                2,
            )
        )
        socket_init_kwargs = {
            "cookie": {"name": "io", "path": "/", "httponly": True},
            "path": "/api/socket.io",
        }
        process_role = str(
            app.config.get("CHATBOC_PROCESS_ROLE")
            or os.getenv("CHATBOC_PROCESS_ROLE", "")
        ).strip().lower()
        if socket_queue_url and process_role == "survey-effect-worker":
            socket_init_kwargs["client_manager"] = (
                build_fail_closed_socketio_redis_manager(
                    socket_queue_url,
                    channel=socket_channel,
                    timeout_seconds=socket_healthcheck_timeout,
                )
            )
        elif socket_queue_url:
            socket_init_kwargs.update(
                message_queue=socket_queue_url,
                channel=socket_channel,
            )
        socketio.init_app(
            None if process_role == "survey-effect-worker" else app,
            **socket_init_kwargs,
        )
        if process_role == "survey-effect-worker":
            app.extensions["socketio_external_emitter"] = socketio

    # Inicializar Flask-Sock
    sock.init_app(app)

    return app

# Objeto global de app para Gunicorn (se puede omitir en tests configurando
# FLASK_SKIP_GLOBAL_APP=1)
if os.getenv("FLASK_SKIP_GLOBAL_APP") != "1":
    if TESTING_MODE:
        from config import TestingConfig

        app = create_app(TestingConfig)
    else:
        app = create_app(Config)
else:
    app = None

if __name__ == '__main__':
    if socketio is not None:
        socketio.run(app, debug=True, host='0.0.0.0', port=8080, allow_unsafe_werkzeug=True)
    else:
        # Fallback simple si alguien ejecuta con FLASK_MIGRATIONS_ONLY=1 localmente
        app.run(debug=True, host='0.0.0.0', port=8080)

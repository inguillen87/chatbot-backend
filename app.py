import os
os.environ["EVENTLET_NO_GREENDNS"] = "1"  # desactiva el resolver de eventlet
import os
if os.environ.get("FLASK_ENV") != 'testing':
    import eventlet
    eventlet.monkey_patch()
import logging
import sys
from flask import Flask, request, current_app, jsonify, g
# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from flask_cors import CORS
from flask_session import Session
from sqlalchemy import event
from socket_service import socketio

# Set credentials for local development only, BEFORE any service that needs them is imported.
if os.environ.get("FLASK_ENV") != "production":
    local_cred_path = os.path.join("data", "google_service_key.json")
    if os.path.exists(local_cred_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = local_cred_path
        print(f"✅ LOCAL DEV: Set GOOGLE_APPLICATION_CREDENTIALS to '{local_cred_path}'")
    else:
        print(f"⚠️ LOCAL DEV: Credential file not found at '{local_cred_path}'. Google services may fail.")

from config import Config, ALLOWED_ORIGINS
from extensions import db, migrate, login_manager # Import login_manager
from celery_utils import celery_app, init_celery # Importar Celery y su inicializador
from models import User
from utils.auth_helpers import obtener_token, user_from_token

# Importación de todas tus rutas (Blueprints)
from routes.config import config_bp
from routes.auth import auth_bp
from routes.legacy_auth import legacy_auth_bp
from routes.chat import chat_bp
from routes.ticket import ticket_bp
from routes.crm import crm_bp
from routes.rubros import rubros_bp
from routes.metricas import metricas_bp
from services.upload_processor import upload_bp
from routes.archivos import archivos_bp
from cli_commands import register_commands
from routes.pedidos import pedidos_bp
from routes.catalogo import catalogo_bp
from routes.estadisticas import estadisticas_bp
from routes.empleados import empleados_bp
from routes.categorias import categorias_bp
from routes.recordatorios import recordatorios_bp
from routes.historial import historial_bp
from routes.notifications import notifications_bp
from routes.municipal_legacy import municipal_bp
from routes.reacciones import reacciones_bp
from routes.carrito import carrito_bp
# from routes.cart import cart_bp # This line caused an ImportError
from routes.productos import productos_bp
from routes.ai_templates import ai_templates_bp
from routes.ai import ai_bp as ai_suggest_bp
from routes.promociones import promociones_bp # <--- NUEVA IMPORTACIÓN PROMOCIONES
from routes.catalog_mappings import catalog_mappings_bp
from routes.whatsapp_webhook import webhook_bp as whatsapp_webhook_bp # <--- NUEVA IMPORTACIÓN WHATSAPP

# --- Listener de ejemplo (reemplazalo por el tuyo si corresponde) ---
def my_on_connect_listener(dbapi_connection, connection_record):
    # Ejemplo: Forzar foreign keys en SQLite (sólo si usás SQLite)
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        pass

def create_app(config_class=Config):
    app = Flask(__name__)
    print("Creating app...")

    # Apply ProxyFix if behind a proxy, BEFORE other configurations if they depend on URL scheme
    # This helps Flask correctly identify the protocol (http/https) and other details
    # when running behind a reverse proxy (like Nginx, Heroku, Render, etc.)
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    if os.environ.get("FLASK_ENV") == 'testing':
        app.config.from_object('config.TestingConfig')
    else:
        app.config.from_object(config_class)
    print(f"Loaded config: {config_class}")
    print(f"Database URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    print(f"DB object: {db}")

    # SESSION_COOKIE_DOMAIN is now directly set by Config based on environment variables.
    # The complex inference logic below is removed.
    # if app.config.get('SESSION_COOKIE_DOMAIN') is None:
    #     server_name = app.config.get('SERVER_NAME')
    #     if server_name and server_name.count('.') > 1 and not server_name.startswith("localhost"):
    #         parts = server_name.split('.')
    #         app.config['SESSION_COOKIE_DOMAIN'] = f".{parts[-2]}.{parts[-1]}"
    #         print(f"Inferred SESSION_COOKIE_DOMAIN: {app.config['SESSION_COOKIE_DOMAIN']}")
    #     elif "chatboc.ar" in (os.environ.get("RENDER_EXTERNAL_URL", "") or ""):
    #          app.config['SESSION_COOKIE_DOMAIN'] = ".chatboc.ar"
    #          print(f"Set SESSION_COOKIE_DOMAIN to: .chatboc.ar (based on RENDER_EXTERNAL_URL)")
    #     else:
    #         print("SESSION_COOKIE_DOMAIN not set and could not be reliably inferred. Session might not work across subdomains.")

    # --- Diagnóstico de Sesión ---
    print("--- DIAGNÓSTICO DE SESIÓN (desde app.py) ---")
    session_ext = Session()
    print(f"SECRET_KEY leída por Flask: {app.config.get('SECRET_KEY')}")
    print(f"SESSION_COOKIE_SECURE: {app.config.get('SESSION_COOKIE_SECURE')}")
    print(f"SESSION_COOKIE_SAMESITE: {app.config.get('SESSION_COOKIE_SAMESITE')}")
    print(f"SESSION_TYPE: {app.config.get('SESSION_TYPE')}")
    print(f"SESSION_COOKIE_DOMAIN: {app.config.get('SESSION_COOKIE_DOMAIN')}")
    print("-----------------------------")

    # --- RUTAS DE PRUEBA PARA DEPURAR LA SESIÓN ---
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

    # --- Diagnóstico de Headers ---
    @app.before_request
    def log_headers():
        # request and current_app are now imported at the top of the module
        # Loguear las cookies que Flask ve directamente
        current_app.logger.info(f"--- RAW FLASK REQUEST.COOKIES: {request.cookies} ---") 
        # Loguear todos los encabezados (como ya lo hacías, útil para comparar)
        current_app.logger.debug(f"Request Headers (complete): {dict(request.headers)}") 

    @app.before_request
    def attach_current_user():
        """
        Adjunta el usuario actual a `g.viewer` para cada solicitud.
        Prioriza la sesión de Flask-Login, pero recurre a la autenticación por
        token para soportar clientes sin estado (como el widget).
        """
        from flask_login import current_user

        # g.viewer se usará para el usuario identificado en la solicitud actual.
        g.viewer = None

        # 1. Prioridad: Usuario autenticado con Flask-Login (sesión de cookie)
        if current_user and current_user.is_authenticated:
            g.viewer = current_user
            current_app.logger.debug(f"User {current_user.id} loaded from Flask-Login session into g.viewer.")
            return

        # 2. Fallback: Intentar cargar usuario desde un token (para el widget u otras APIs)
        token = obtener_token()
        if token:
            user = user_from_token(token)
            if user:
                g.viewer = user
                current_app.logger.debug(f"User {user.id} loaded from token into g.viewer.")
            else:
                current_app.logger.debug("Token provided but no matching user found.")
        else:
            current_app.logger.debug("No user session and no token provided. g.viewer remains None.")

    # --- Inicialización de Extensiones ---
    init_celery(app) # Inicializar Celery con la app Flask
    login_manager.init_app(app) # Initialize Flask-Login
    login_manager.session_protection = "strong" # Configure session protection
    login_manager.login_view = "auth.login" # Set the login view
    with app.app_context():
        db.init_app(app)
        migrate.init_app(app, db)
        if app.config.get("TESTING"):
            db.create_all()

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # --- Registrar SQLAlchemy event listener SOLO dentro de app_context ---
    with app.app_context():
        if hasattr(db.engine, 'connect'):
            event.listen(db.engine, "connect", my_on_connect_listener)
        else:
            app.logger.warning("Database engine not available for event listener registration. This might be an issue if an on-connect event was expected.")

    # Configuración y activación de Sesiones en el Servidor
    if app.config.get("TESTING"):
        app.config['SESSION_TYPE'] = 'filesystem'
    app.config['SESSION_SQLALCHEMY'] = db
    if not hasattr(app, 'session_interface'):
        session_ext.init_app(app)

    # --- Configuración de Logging ---
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    # Prevent duplicate log lines by stopping propagation to the root logger
    app.logger.propagate = False
    app.logger.info(f"Aplicación creada. Nivel de logging: {log_level}")
    app.logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # --- Configuración de CORS ---
    CORS(
        app,
        origins=ALLOWED_ORIGINS,
        supports_credentials=True,
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Entity-Token",
            "X-Chat-Session-Id",
            "X-Anon-Id",
            "Anon-Id",
        ],
        expose_headers=["X-Anon-Id", "Anon-Id"],
    )

    @app.after_request
    def add_permissions_policy(resp):
        policy = current_app.config.get("PERMISSIONS_POLICY_HEADER", "geolocation=(self)")
        resp.headers.setdefault("Permissions-Policy", policy)
        return resp

    # --- Registro de Blueprints (Rutas) ---
    app.register_blueprint(config_bp)
    app.register_blueprint(auth_bp)
    # Alias para compatibilidad: permite acceder a /login sin prefijo /auth
    app.add_url_rule(
        '/login',
        endpoint='login',
        view_func=app.view_functions['auth.login'],
        methods=['POST', 'OPTIONS']
    )
    app.register_blueprint(legacy_auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(ticket_bp)
    app.register_blueprint(crm_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(archivos_bp)
    app.register_blueprint(rubros_bp)
    app.register_blueprint(metricas_bp)
    app.register_blueprint(catalogo_bp)
    app.register_blueprint(productos_bp)
    app.register_blueprint(pedidos_bp)
    app.register_blueprint(carrito_bp)
    # app.register_blueprint(cart_bp) # Corresponds to the removed import
    app.register_blueprint(estadisticas_bp)
    app.register_blueprint(empleados_bp)
    app.register_blueprint(categorias_bp)
    app.register_blueprint(recordatorios_bp)
    app.register_blueprint(historial_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(municipal_bp)
    app.register_blueprint(reacciones_bp)
    app.register_blueprint(ai_templates_bp)
    app.register_blueprint(ai_suggest_bp)
    app.register_blueprint(promociones_bp) # <--- REGISTRO DEL BLUEPRINT DE PROMOCIONES
    app.register_blueprint(catalog_mappings_bp)
    app.register_blueprint(whatsapp_webhook_bp) # <--- REGISTRO DEL BLUEPRINT DE WHATSAPP (sin prefijo aquí)

    # --- Registro de comandos CLI ---
    register_commands(app)

    socketio.init_app(app)

    return app

# Esto crea el objeto 'app' global para Gunicorn:
app = create_app(Config)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080, allow_unsafe_werkzeug=True)
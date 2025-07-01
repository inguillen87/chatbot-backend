import os
import logging
import sys
from flask import Flask
from flask_cors import CORS
from flask_session import Session
from sqlalchemy import event # Added import

# Reuse the same Session extension across multiple app instances to avoid
# redefining the 'Session' model when tests create the app several times.
session_ext = Session()

from config import Config
from extensions import db, migrate
from models import User

# Importación de todas tus rutas (Blueprints)
from routes.auth import auth_bp
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
from routes.cart import cart_bp
from routes.productos import productos_bp

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- Diagnóstico de Sesión ---
    print("--- DIAGNÓSTICO DE SESIÓN ---")
    print(f"SECRET_KEY leída por Flask: {app.config.get('SECRET_KEY')}")
    print(f"SESSION_COOKIE_SECURE: {app.config.get('SESSION_COOKIE_SECURE')}")
    print(f"SESSION_COOKIE_SAMESITE: {app.config.get('SESSION_COOKIE_SAMESITE')}")
    print(f"SESSION_TYPE: {app.config.get('SESSION_TYPE')}")
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

    # --- Inicialización de Extensiones ---
    db.init_app(app)
    migrate.init_app(app, db)

    # Register the SQLAlchemy event listener after db.init_app
    # IMPORTANT: Replace 'actual_listener_function_name_here' 
    # with the actual name of your listener function.
    # Also, ensure the old decorator @event.listens_for(db.engine, "connect")
    # is removed from line 73 (or wherever it is).
    if hasattr(db.engine, 'connect'): # Ensure engine is available
        # User needs to define/ensure 'actual_listener_function_name_here' is correct
        # For example, if the listener was:
        # @event.listens_for(db.engine, "connect")
        # def my_on_connect_listener(dbapi_connection, connection_record):
        #    ...
        # Then use 'my_on_connect_listener' below.
        # We are assuming a function named 'actual_listener_function_name_here' exists.
        # This function should be defined in app.py or imported.
        # As I cannot see the original definition, this is a placeholder.
        # Ensure this function is defined or imported, e.g.:
        # def actual_listener_function_name_here(dbapi_connection, connection_record):
        #     # Your pragma or other on-connect logic here
        #     pass 
        event.listen(db.engine, "connect", actual_listener_function_name_here)
    else:
        app.logger.warning("Database engine not available for event listener registration. This might be an issue if an on-connect event was expected.")

    # Configuración y activación de Sesiones en el Servidor
    app.config['SESSION_SQLALCHEMY'] = db
    # Usar solo session_ext para evitar redefinición en tests o múltiples apps
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
    app.logger.info(f"Aplicación creada. Nivel de logging: {log_level}")
    app.logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # --- Configuración de CORS ---
    allowed_origins_env = os.environ.get("CORS_ALLOWED_ORIGINS")
    if allowed_origins_env:
        allowed_origins_env = allowed_origins_env.strip()
        if allowed_origins_env == "*":
            allowed_origins = "*"
        else:
            allowed_origins = [o.strip() for o in allowed_origins_env.split(',') if o.strip()]
    else:
        allowed_origins = [
            "http://localhost",
            "http://localhost:3000",
            "http://localhost:8080",
            "https://chatboc.ar",
            "https://www.chatboc.ar",
            "https://api.chatboc.ar"
        ]

    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization", "Content-Type", "Origin", "Accept",
            "Anon-Id", "x-entity-token"
        ],
    )

    # --- Fix universal de headers custom para CORS ---
    @app.after_request
    def ensure_custom_cors_headers(resp):
        """
        Garantiza que TODOS los headers custom que tu app pueda llegar a usar,
        queden siempre incluidos en Access-Control-Allow-Headers de la respuesta,
        para que ningún preflight se los rechace, no importa si Flask-CORS los olvidó.
        """
        needed = [
            "Authorization", "Content-Type", "Origin", "Accept",
            "Anon-Id", "x-entity-token"
        ]
        prev = resp.headers.get("Access-Control-Allow-Headers", "")
        actual = [h.strip() for h in prev.split(",") if h.strip()]
        actual_lower = [h.lower() for h in actual]
        for n in needed:
            if n.lower() not in actual_lower:
                actual.append(n)
        resp.headers["Access-Control-Allow-Headers"] = ", ".join(actual)
        return resp

    @app.after_request
    def add_permissions_policy(resp):
        """Ensure geolocation is allowed inside iframes."""
        resp.headers.setdefault("Permissions-Policy", "geolocation=(self)")
        return resp

    # --- 4. Registro de Blueprints (Rutas) ---
    app.register_blueprint(auth_bp)
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
    app.register_blueprint(cart_bp)
    app.register_blueprint(estadisticas_bp)
    app.register_blueprint(empleados_bp)
    app.register_blueprint(categorias_bp)
    app.register_blueprint(recordatorios_bp)
    app.register_blueprint(historial_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(municipal_bp)
    app.register_blueprint(reacciones_bp)

    # --- Registro de comandos CLI ---
    register_commands(app)

    # --- Devolución de la app ---
    return app

# --- Creación de la instancia de la aplicación ---
app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

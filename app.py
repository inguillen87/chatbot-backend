import os
import logging
import sys
from flask import Flask

from config import Config
from extensions import db, migrate
from models import User

from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.ticket import ticket_bp
from routes.rubros import rubros_bp
from services.upload_processor import upload_bp
from cli_commands import register_commands

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Logging profesional
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

    # Inicialización de extensiones
    db.init_app(app)
    migrate.init_app(app, db)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError as e:
        app.logger.error(f"Error creando instance_path: {e}")

    app.logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # ---- CONFIGURACIÓN DE CORS ----
    from flask_cors import CORS

    allowed_origins = [
        "https://chatboc.ar",
        "https://www.chatboc.ar",
        "http://localhost:5173",
        "http://localhost:8080",
        "https://chatboc-frontend-2cmzvzayk-marcelos-projects-c26aa499.vercel.app"
    ]
    # Config global: ¡aplica a todas las rutas y métodos!
    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Origin", "Accept"],
        expose_headers=["Content-Disposition"],
        max_age=86400   # 1 día para que el preflight se cachee
    )

    # Registro de Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(ticket_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(rubros_bp)

    # Registro de comandos CLI
    register_commands(app)

    # --- CORS TEST ROUTE (solo para debug, podés borrarla en prod) ---
    @app.route('/cors-test', methods=['OPTIONS', 'GET'])
    def cors_test():
        return '', 204

    return app

app = create_app()

if __name__ == '__main__':
    app.logger.setLevel(logging.DEBUG)
    app.logger.info("Iniciando aplicación Flask con el servidor de desarrollo (DEBUG)...")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

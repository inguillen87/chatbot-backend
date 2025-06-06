# app.py
import os
import logging
import sys
import click
from flask import Flask
from flask.cli import with_appcontext
from dotenv import load_dotenv
from datetime import timedelta

from config import Config
from extensions import db, migrate, login_manager
from models import User

# --- IMPORTACIÓN DE BLUEPRINTS ---
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.sugerencias import sugerencia_bp
from routes.rubros import rubros_bp
from routes.metricas import metricas_bp
from services.upload_processor import upload_bp
from routes.uala_webhook import uala_bp
from routes.ticket import ticket_bp

load_dotenv()

def create_app(config_class=Config):
    """
    Fábrica de la aplicación Flask. Crea y configura la instancia de la app.
    """
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- LOGGING PROFESIONAL (Mejorado para Flask y Render) ---
    # En lugar de basicConfig, configuramos el logger de la app directamente.
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s [%(module)s.%(funcName)s:%(lineno)d] - %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    # Limpiamos handlers existentes y añadimos el nuestro.
    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    # Hacemos que otros loggers (como el de SQLAlchemy) también reporten.
    logging.getLogger('sqlalchemy').addHandler(handler)
    logging.getLogger('sqlalchemy').setLevel(logging.WARNING) # Para no llenar de logs de SQL

    app.logger.info(f"Aplicación creada. Nivel de logging: {log_level}")

    # --- INICIALIZACIÓN DE EXTENSIONES ---
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login" # Redirige a la vista de login si no está autenticado

    # --- EL DETECTIVE MÁS IMPORTANTE: USER LOADER ---
    # Lo ponemos aquí, dentro de la fábrica, para que esté asociado a esta instancia de la app.
    @login_manager.user_loader
    def load_user(user_id):
        app.logger.info("\n--- DETECTIVE USER_LOADER ---")
        app.logger.info(f"1. Intentando cargar usuario con el ID recibido de la sesión: '{user_id}'")
        app.logger.info(f"2. Tipo de dato del ID recibido: {type(user_id)}")
        
        try:
            # Forzamos la conversión a entero para estar seguros
            user = User.query.get(int(user_id))
            app.logger.info(f"3. Resultado de la búsqueda en la DB: {user}")
            if user is None:
                app.logger.warning("4. ¡ALARMA! No se encontró ningún usuario con ese ID. Devolviendo None.")
            else:
                app.logger.info("4. Usuario encontrado exitosamente. Devolviendo el objeto User.")
            app.logger.info("-----------------------------\n")
            return user
        except Exception as e:
            app.logger.error(f"¡ERROR FATAL EN USER_LOADER! No se pudo convertir el ID a entero o hubo otro error. Excepción: {e}")
            app.logger.error("-----------------------------\n")
            return None

    # --- Crear carpeta de instancia ---
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError as e:
        app.logger.error(f"Error creando instance_path {app.instance_path}: {e}")
    
    app.logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # --- Configuración de CORS ---
    allowed_origins = [
        "https://chatboc.ar", "https://www.chatboc.ar",
        "http://localhost:5173", "http://localhost:8080",
        "https://chatboc-frontend-2cmzvzayk-marcelos-projects-c26aa499.vercel.app"
    ]
    from_env = os.getenv("CORS_ALLOWED_ORIGINS")
    if from_env:
        allowed_origins.extend(from_env.split(','))
    
    CORS(app, origins=allowed_origins, supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    app.logger.info(f"CORS aplicado para los orígenes: {allowed_origins}")


    # --- Registro de Blueprints ---
    blueprints_to_register = [
        auth_bp, chat_bp, sugerencia_bp, rubros_bp,
        metricas_bp, upload_bp, uala_bp, ticket_bp
    ]
    for bp in blueprints_to_register:
        app.register_blueprint(bp)
        app.logger.info(f"Blueprint '{bp.name}' registrado.")
        
    # --- Registro de Comandos CLI ---
    from cli_commands import register_commands
    register_commands(app)

    return app

# --- Creación de la instancia de la aplicación ---
app = create_app()

if __name__ == '__main__':
    # Forzar modo DEBUG solo cuando se ejecuta directamente con 'python app.py'
    app.logger.setLevel(logging.DEBUG)
    app.logger.info("Iniciando aplicación Flask con el servidor de desarrollo (DEBUGGING LOCAL)...")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
# app.py
import os
import logging
import sys # Necesario para sys.stderr
import click
from flask import Flask
from flask_cors import CORS
from flask_migrate import upgrade
from flask.cli import with_appcontext
from dotenv import load_dotenv
from datetime import timedelta
import traceback

from config import Config
from extensions import db, migrate, login_manager
from models import User

# --- IMPORTACIÓN DE BLUEPRINTS ---
# Asegúrate que las rutas de importación sean correctas según tu estructura
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.sugerencias import sugerencia_bp
from routes.rubros import rubros_bp
from routes.metricas import metricas_bp
from services.upload_processor import upload_bp # Asumo que está en services
from routes.uala_webhook import uala_bp
from routes.ticket import ticket_bp

load_dotenv()

# --- Configuración de Logging ---
# Configuración global y directa para asegurar que los logs se vean en Render (stdout/stderr)
LOG_LEVEL_CONFIG = os.environ.get('LOG_LEVEL', 'INFO').upper() # Puedes usar una variable de entorno en Render para cambiar el nivel
numeric_level = getattr(logging, LOG_LEVEL_CONFIG, logging.INFO)

logging.basicConfig(
    level=numeric_level,
    format="%(asctime)s [%(levelname)s] %(name)s [%(module)s.%(funcName)s:%(lineno)d] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr # Forzar salida a stderr, que Render captura
)

# Logger específico para este archivo app.py
app_module_logger = logging.getLogger(__name__) # Esto será 'app' si el archivo se llama app.py


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Hacer que el logger de Flask use la configuración de basicConfig
    # Esto es importante para que app.logger y los loggers de tus módulos
    # (como el de logic.py) hereden el nivel y el handler.
    app.logger.handlers = logging.getLogger().handlers # Usa los handlers del root logger
    app.logger.setLevel(numeric_level) # Usa el mismo nivel que basicConfig

    app_module_logger.info(f"App Creada. Nivel de logging de app.logger: {logging.getLevelName(app.logger.getEffectiveLevel())}")
    app_module_logger.info(f"Nivel de logging del root logger: {logging.getLevelName(logging.getLogger().getEffectiveLevel())}")
    
    login_manager.init_app(app)
    db.init_app(app)
    migrate.init_app(app, db)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError as e:
        app_module_logger.error(f"Error creando instance_path {app.instance_path}: {e}")
    
    app_module_logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # --- Configuración de CORS (tu configuración es buena) ---
    allowed_origins = [
        "https://chatboc.ar",
        "https://www.chatboc.ar",
        "http://localhost:5173", 
        "http://localhost:8080",   # <- AGREGALO SI USÁS ESTE PUERTO EN LOCAL
        "https://chatboc-frontend-2cmzvzayk-marcelos-projects-c26aa499.vercel.app" # Tu URL de Vercel
        # Considera añadir una variable de entorno para más URLs de Vercel si es necesario
        # ej. os.getenv("VERCEL_PREVIEW_URL")
    ]
    # Añadir el origen actual de Vercel si está en una variable de entorno
    current_vercel_url = os.getenv("VERCEL_URL") # Vercel setea esta variable automáticamente en sus deploys
    if current_vercel_url and f"https://{current_vercel_url}" not in allowed_origins:
        allowed_origins.append(f"https://{current_vercel_url}")
        app_module_logger.info(f"Añadido origen VERCEL_URL a CORS: https://{current_vercel_url}")


    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        max_age=timedelta(hours=1)
    )
    app_module_logger.info(f"CORS aplicado para los orígenes: {allowed_origins}")

    # --- Registro de Blueprints ---
    app.register_blueprint(auth_bp) 
    app.register_blueprint(chat_bp)
    app.register_blueprint(sugerencia_bp)
    app.register_blueprint(rubros_bp)
    app.register_blueprint(metricas_bp)
    app.register_blueprint(upload_bp) 
    app.register_blueprint(uala_bp)
    app.register_blueprint(ticket_bp)



    for bp_name_key in app.blueprints:
        app_module_logger.info(f"Blueprint '{bp_name_key}' registrado.")
        
    return app

app = create_app()

# --- Comandos CLI (sin cambios, ya estaban bien) ---
@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos_command():
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    app_module_logger.info("Iniciando carga de datos iniciales CLI...")
    try:
        app_module_logger.info("Asegurando que todas las tablas estén creadas (db.create_all())...")
        db.create_all() 
        cargar_usuarios_demo(); cargar_faqs(); cargar_sugerencias()
        app_module_logger.info("Datos iniciales cargados correctamente CLI.")
    except Exception as e:
        app_module_logger.error(f"❌ Error cargando datos iniciales CLI: {e}", exc_info=True)
app.cli.add_command(cargar_datos_command, name="cargar_datos_iniciales")

@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones_command():
    app_module_logger.info("Intentando aplicar migraciones de base de datos CLI...")
    try:
        upgrade() 
        app_module_logger.info("Migraciones de base de datos aplicadas correctamente CLI.")
    except Exception as e:
        app_module_logger.error(f"❌ Error durante la aplicación de migraciones CLI (upgrade): {e}", exc_info=True)
app.cli.add_command(aplicar_migraciones_command, name="aplicar_migraciones")

if __name__ == '__main__':
    # Para desarrollo local con 'python app.py', forzar DEBUG
    logging.getLogger().setLevel(logging.DEBUG) # Logger raíz a DEBUG
    app.logger.setLevel(logging.DEBUG)
    for handler in app.logger.handlers: handler.setLevel(logging.DEBUG)
    app_module_logger.info("Iniciando aplicación Flask con el servidor de desarrollo (DEBUGGING LOCAL)...")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
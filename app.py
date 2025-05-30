# app.py

import os
import logging
from logging.handlers import RotatingFileHandler
import click
from flask import Flask # request, make_response ya no se usan directamente aquí
from flask_cors import CORS
from flask_migrate import upgrade
from flask.cli import with_appcontext
from dotenv import load_dotenv
from datetime import timedelta
import traceback

# --- Importaciones de la Aplicación ---
from config import Config
from extensions import db, migrate, login_manager
from models import User # Necesario para el user_loader

# --- Importar Blueprints Directamente ---
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.sugerencias import sugerencia_bp
# Descomenta la siguiente línea y la entrada en 'blueprints_to_register'
# si restauraste el archivo routes/rubros.py y necesitas ese endpoint.
# from routes.rubros import rubros_bp 
from routes.metricas import metricas_bp
from services.upload_processor import upload_bp # Este se importa directamente

# Cargar variables de entorno desde .env
load_dotenv()

# --- Configuración de Logging ---
if not os.path.exists("logs"):
    try:
        os.makedirs("logs")
    except OSError as e:
        print(f"Advertencia: No se pudo crear el directorio 'logs': {e}")

file_handler = RotatingFileHandler("logs/chatbot.log", maxBytes=10 * 1024 * 1024, backupCount=5)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s:%(module)s:%(lineno)d] %(message)s")
file_handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler])
logger = logging.getLogger(__name__) # Logger para uso en este archivo (app.py)
# --- Fin Configuración de Logging ---

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except ValueError:
        logger.warning(f"Intento de cargar usuario con user_id no entero: {user_id}")
        return None

def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    logger.info(f"SQLALCHEMY_DATABASE_URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    logger.info(f"SECRET_KEY está configurada: {'Sí' if app.config.get('SECRET_KEY') else 'No (Usando default si existe)'}")

    try:
        os.makedirs(app.instance_path, exist_ok=True)
        logger.info(f"Directorio de instancia asegurado en: {app.instance_path}")
    except OSError as e:
        logger.error(f"No se pudo crear el directorio de instancia {app.instance_path}: {e}")

    upload_folder_config = app.config.get('UPLOAD_FOLDER')
    if upload_folder_config:
        try:
            os.makedirs(upload_folder_config, exist_ok=True)
            logger.info(f"Directorio de subida asegurado en: {upload_folder_config}")
        except OSError as e:
             logger.error(f"No se pudo crear el directorio de subida {upload_folder_config}: {e}")
    else:
        logger.warning("UPLOAD_FOLDER no está definido en la configuración de la app. La subida de archivos podría no funcionar como se espera en todos los entornos.")

    login_manager.init_app(app)
    login_manager.login_view = "auth.login" # Redirige a esta vista si @login_required falla
    
    db.init_app(app)
    migrate.init_app(app, db)
    logger.info("Extensiones Flask (LoginManager, SQLAlchemy, Migrate) inicializadas.")

    allowed_origins = app.config.get("ALLOWED_ORIGINS", [
        "http://localhost:5173", # Tu frontend local
        "https://www.chatboc.ar",
        "https://chatboc.ar"
        # Añade URLs de Vercel aquí si es necesario
    ])
    
    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        max_age=int(timedelta(days=1).total_seconds())
    )
    logger.info(f"CORS aplicado centralmente para orígenes: {allowed_origins}")

    blueprints_to_register = [
        (auth_bp, "/api/v1/auth"),    
        (chat_bp, "/api/v1/chat"),    
        (sugerencia_bp, "/api/v1/sugerencias"), 
        # (rubros_bp, "/api/v1/rubros"), # Descomenta si restauraste routes/rubros.py
        (metricas_bp, "/api/v1/metricas"),   
        (upload_bp, "/api/v1/catalogo") 
    ]

    for blueprint_object, url_prefix in blueprints_to_register:
        if blueprint_object is not None:
            try:
                app.register_blueprint(blueprint_object, url_prefix=url_prefix)
                logger.info(f"✅ Blueprint '{blueprint_object.name}' registrado en '{url_prefix}'.")
            except Exception as e:
                logger.error(f"❌ Error registrando Blueprint '{blueprint_object.name}': {e}", exc_info=True)
        else:
            logger.warning(f"Intento de registrar un Blueprint None con prefijo '{url_prefix}'. Revisar importaciones de blueprints.")

    app.cli.add_command(cargar_datos_cmd_cli) # Nombre de la función Python
    app.cli.add_command(aplicar_migraciones_cmd_cli) # Nombre de la función Python

    logger.info("Aplicación Flask creada y configurada exitosamente.")
    return app

# --- Comandos CLI ---
@click.command("cargar_datos_iniciales") # Este es el nombre que usas en la terminal: flask cargar_datos_iniciales
@with_appcontext
def cargar_datos_cmd_cli(): # Nombre de la función Python
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    
    logger.info("⚙️ Iniciando carga de datos iniciales...") # Usar el logger del módulo app.py
    logger.info("(Asegúrate que el esquema de BD ya esté migrado con 'flask aplicar_migraciones')")
    
    # NO db.create_all() aquí.
    
    try:
        cargar_usuarios_demo()
        cargar_faqs()
        cargar_sugerencias()
        logger.info("✅ Datos iniciales cargados/verificados correctamente.")
    except Exception as e:
        logger.error(f"❌ Error durante la carga de datos iniciales: {e}", exc_info=True)

@click.command("aplicar_migraciones") # Nombre para la terminal: flask aplicar_migraciones
@with_appcontext
def aplicar_migraciones_cmd_cli(): # Nombre de la función Python
    logger.info("⚙️ Aplicando migraciones de base de datos...") # Usar el logger del módulo app.py
    try:
        upgrade() 
        logger.info("✅ Migraciones aplicadas correctamente.")
    except Exception as e:
        logger.error(f"❌ Error al aplicar migraciones (flask db upgrade): {e}", exc_info=True)

# Crear la instancia de la app
app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv("PORT", 10000)))
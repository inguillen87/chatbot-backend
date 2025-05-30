# app.py

import os
import logging
from logging.handlers import RotatingFileHandler
import click # Asegúrate de tener 'import click'
from flask import Flask
# from flask import request, make_response # Ya no se usan directamente aquí
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
# (Se mantiene tu configuración, es buena. Solo ajusté el formato un poco.)
if not os.path.exists("logs"):
    try:
        os.makedirs("logs")
    except OSError as e:
        # Esto podría pasar si el directorio ya existe pero fue creado por otro proceso,
        # o si no hay permisos. Es mejor loguear y continuar.
        print(f"Advertencia: No se pudo crear el directorio 'logs': {e}")

file_handler = RotatingFileHandler("logs/chatbot.log", maxBytes=10 * 1024 * 1024, backupCount=5) # 10MB por archivo
file_handler.setLevel(logging.INFO)
# Formato más detallado incluyendo nombre del logger y módulo
formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s:%(module)s:%(lineno)d] %(message)s")
file_handler.setFormatter(formatter)

# Configurar el logger raíz
logging.basicConfig(level=logging.INFO, handlers=[file_handler])

# Logger específico para este módulo (app.py)
logger = logging.getLogger(__name__)
# --- Fin Configuración de Logging ---

@login_manager.user_loader
def load_user(user_id):
    # Considerar manejo de error si user_id no es un int válido
    try:
        return User.query.get(int(user_id))
    except ValueError:
        logger.warning(f"Intento de cargar usuario con user_id no entero: {user_id}")
        return None

def create_app(config_class=Config): # Permitir pasar una clase de config para testing
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    logger.info(f"SQLALCHEMY_DATABASE_URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    logger.info(f"SECRET_KEY está configurada: {'Sí' if app.config.get('SECRET_KEY') else 'No (Usando default si existe)'}")


    # Crear directorio de instancia si no existe
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
        logger.warning("UPLOAD_FOLDER no está definido en la configuración de la app. La subida de archivos podría no funcionar como se espera.")

    # Inicializar extensiones
    login_manager.init_app(app)
    login_manager.login_view = "auth.login" # Redirigir a la vista de login si se accede a una ruta protegida sin estar logueado
    
    db.init_app(app)
    migrate.init_app(app, db)
    logger.info("Extensiones Flask (LoginManager, SQLAlchemy, Migrate) inicializadas.")

    # Configuración de CORS centralizada
    allowed_origins = app.config.get("ALLOWED_ORIGINS", [
        "http://localhost:5173", # Tu frontend local
        "https://www.chatboc.ar",
        "https://chatboc.ar"
        # Añade aquí las URLs de tus deploys de Vercel si son diferentes
        # Ejemplo: "https://chatboc-frontend-xxxxxxxx.vercel.app"
    ])
    
    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"], # X-Requested-With es común
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        max_age=int(timedelta(days=1).total_seconds()) # Aumentar max_age para preflight
    )
    logger.info(f"CORS aplicado centralmente para orígenes: {allowed_origins}")

    # Registrar Blueprints
    # Añade el prefijo que desees para organizar tus rutas de API
    blueprints_to_register = [
        (auth_bp, "/api/v1/auth"),    
        (chat_bp, "/api/v1/chat"),    
        (sugerencia_bp, "/api/v1/sugerencias"), 
        # Descomenta la siguiente línea si restauraste routes/rubros.py
        # (rubros_bp, "/api/v1/rubros"), 
        (metricas_bp, "/api/v1/metricas"),   
        (upload_bp, "/api/v1/catalogo") # upload_bp se importa directamente, prefijo de ejemplo
    ]

    for blueprint_object, url_prefix in blueprints_to_register:
        if blueprint_object is not None: # Chequeo por si alguna importación de BP falló o está comentada
            try:
                app.register_blueprint(blueprint_object, url_prefix=url_prefix)
                logger.info(f"✅ Blueprint '{blueprint_object.name}' registrado en '{url_prefix}'.")
            except Exception as e:
                logger.error(f"❌ Error registrando Blueprint '{blueprint_object.name}' desde '{url_prefix}': {e}", exc_info=True)
        else:
            # Esto pasaría si, por ejemplo, rubros_bp es None porque la importación está comentada
            logger.warning(f"Intento de registrar un Blueprint None con prefijo '{url_prefix}'. Revisar importaciones.")


    # Comandos CLI
    app.cli.add_command(cargar_datos_cmd)
    app.cli.add_command(aplicar_migraciones_cmd)

    logger.info("Aplicación Flask creada y configurada exitosamente.")
    return app

# --- Comandos CLI Definidos Fuera de create_app ---
@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos_cmd():
    # Importar dentro de la función para evitar problemas de importación circular
    # y asegurar que se ejecuten en el contexto de la app
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    
    # Usar current_app.logger aquí porque estamos dentro de un comando CLI con contexto de app
    current_app.logger.info("⚙️ Iniciando carga de datos iniciales...")
    current_app.logger.info("(Asegúrate de que el esquema de BD ya esté migrado con 'flask aplicar_migraciones')")
    
    # NO LLAMAR a db.create_all() aquí. Las migraciones manejan el esquema.
    
    try:
        cargar_usuarios_demo()
        cargar_faqs()
        cargar_sugerencias()
        current_app.logger.info("✅ Datos iniciales cargados/verificados correctamente.")
    except Exception as e:
        current_app.logger.error(f"❌ Error durante la carga de datos iniciales: {e}", exc_info=True)


@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones_cmd():
    current_app.logger.info("⚙️ Aplicando migraciones de base de datos...")
    try:
        upgrade() # de flask_migrate
        current_app.logger.info("✅ Migraciones aplicadas correctamente.")
    except Exception as e:
        current_app.logger.error(f"❌ Error al aplicar migraciones (comando flask db upgrade): {e}", exc_info=True)

# Crear la instancia de la app para Gunicorn y desarrollo local
app = create_app()

# El manejo de preflight OPTIONS ahora debería ser manejado por Flask-CORS.
# Si sigues teniendo problemas de CORS con OPTIONS, podrías necesitar un before_request,
# pero asegúrate que no entre en conflicto con Flask-CORS.
# Generalmente, si Flask-CORS está bien configurado (con los methods y headers correctos),
# se encarga de las respuestas a OPTIONS.

if __name__ == '__main__':
    # Para desarrollo local. Render usa la instancia 'app' exportada y Gunicorn.
    # El puerto 10000 es el que Gunicorn usa en tus logs de Render.
    # Usar os.getenv para el puerto es una buena práctica.
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv("PORT", 10000)))
# app.py (Versión Final Sugerida)

import os
import logging
from logging.handlers import RotatingFileHandler # Lo mantienes para logs a archivo local
import click
from flask import Flask # Quitado request y make_response si no se usan directamente aquí
from flask_cors import CORS
from flask_migrate import upgrade
from flask.cli import with_appcontext
from dotenv import load_dotenv
from datetime import timedelta
import traceback # Lo mantienes por si acaso, aunque el logger ya captura tracebacks

from config import Config
from extensions import db, migrate, login_manager
# Asumo que todos tus blueprints se importan y se llaman como xx_bp
from routes.auth import auth_bp
from routes.chat import chat_bp # Asegúrate que este es el blueprint con /ask
from routes.sugerencias import sugerencia_bp
from routes.rubros import rubros_bp
from routes.metricas import metricas_bp
from services.upload_processor import upload_bp # Desde services, como lo tenías
from models import User

# Cargar variables de entorno desde .env (principalmente para desarrollo local)
load_dotenv()

# --- Configuración de Logging Global para la Aplicación ---
# Esta configuración es para asegurar que los logs se vean bien en Render (stdout/stderr)
# y también en tu archivo local si así lo deseas.

# Configurar el logger raíz para que capture al menos INFO.
# Flask y Gunicorn suelen enviar sus logs a stderr, que Render captura.
# Esta configuración básica es a menudo suficiente.
logging.basicConfig(level=logging.INFO, 
                    format="%(asctime)s | %(levelname)s | %(name)s (%(module)s.%(funcName)s:%(lineno)d) | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")

# Crear un logger específico para este archivo app.py (opcional, pero buena práctica)
app_file_logger = logging.getLogger(__name__)


# --- Flask-Login User Loader ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Factoría de la Aplicación ---
def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- AJUSTE IMPORTANTE PARA LOGGING EN RENDER/GUNICORN ---
    # Hacer que el logger de Flask use el nivel configurado por basicConfig o Gunicorn.
    # Y que los handlers de Gunicorn (si los hay) se usen, para que los logs de Flask aparezcan en Render.
    if __name__ != '__main__': # Cuando es corrido por un servidor WSGI como Gunicorn
        gunicorn_logger = logging.getLogger('gunicorn.error')
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level if gunicorn_logger.level != 0 else logging.INFO) # Heredar nivel o default a INFO
        # Si quieres forzar DEBUG temporalmente para ver TODO:
        # app.logger.setLevel(logging.DEBUG)
        # for handler in gunicorn_logger.handlers: # Asegurar que los handlers de gunicorn también estén en DEBUG
        #     handler.setLevel(logging.DEBUG)
    else: # Cuando corres localmente con 'flask run'
        app.logger.setLevel(logging.DEBUG) # Nivel DEBUG para desarrollo local

    app_file_logger.info("Logger de Flask configurado.")
    # --- FIN AJUSTE LOGGING ---


    # --- Tu FileHandler local (opcional para Render, pero útil para desarrollo) ---
    # Render captura stdout/stderr, por lo que un FileHandler no es estrictamente necesario para ver logs en Render,
    # pero puedes mantenerlo si quieres logs persistentes en un archivo en tu disco de Render (ej. en /data/logs).
    # Si lo mantienes, asegúrate que la ruta sea escribible en Render.
    # Por ahora, lo dejaré como lo tenías, asumiendo que "logs/chatbot.log" es para local.
    if not os.path.exists("logs"): # Para logs locales
        try:
            os.makedirs("logs")
        except OSError as e_mkdir: # Más específico para error de creación de directorio
             app_file_logger.error(f"No se pudo crear el directorio 'logs': {e_mkdir}")

    log_file_path = os.path.join("logs", "chatbot.log")
    try:
        file_handler = RotatingFileHandler(log_file_path, maxBytes=10240, backupCount=5, encoding='utf-8') # Añadir encoding
        file_handler.setLevel(logging.INFO) # O logging.DEBUG si quieres más detalle en archivo
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s (%(module)s.%(funcName)s:%(lineno)d) | %(message)s"
        )
        file_handler.setFormatter(formatter)
        # Añadir este handler al logger raíz o al app.logger si quieres que los logs de Flask también vayan al archivo
        logging.getLogger().addHandler(file_handler) # Añadir al root logger
        app_file_logger.info(f"FileHandler configurado para: {log_file_path}")
    except Exception as e_fh:
        app_file_logger.error(f"No se pudo configurar FileHandler para {log_file_path}: {e_fh}")
    # --- Fin FileHandler ---


    # Inicializar extensiones
    login_manager.init_app(app)
    db.init_app(app)
    migrate.init_app(app, db)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError as e:
        app_file_logger.error(f"Error creando el directorio instance_path {app.instance_path}: {e}")
    
    app_file_logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # --- Configuración de CORS ---
    # (Tu configuración CORS es buena, la mantengo. Asegúrate que las URLs de Vercel sean las correctas)
    default_allowed_origins = [
        "https://chatboc.ar",
        "https://www.chatboc.ar",
        "http://localhost:5173", # Para desarrollo local del frontend
        # Reemplaza esta con tu URL de producción de Vercel si es diferente a chatboc.ar
        # O si tienes múltiples previews, considera usar una variable de entorno para más flexibilidad.
        "https://chatboc-frontend-2cmzvzayk-marcelos-projects-c26aa499.vercel.app" 
    ]
    
    # Para manejar múltiples URLs de preview de Vercel o dominios adicionales
    # podrías leerlos de una variable de entorno:
    # EXTRA_CORS_ORIGINS = os.getenv("EXTRA_CORS_ORIGINS", "") # Ej: "https://preview1.app,https://preview2.app"
    # if EXTRA_CORS_ORIGINS:
    #     allowed_origins.extend([origin.strip() for origin in EXTRA_CORS_ORIGINS.split(',')])
    
    CORS(
        app,
        origins=default_allowed_origins, # Usar la lista
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], # OPTIONS es crucial
        max_age=timedelta(hours=1)
    )
    app_file_logger.info(f"CORS aplicado para los orígenes: {default_allowed_origins}")


    # --- Registro de Blueprints ---
    # Asegúrate que los nombres de los blueprints importados coincidan con los usados aquí.
    app.register_blueprint(auth_bp) 
    app.register_blueprint(chat_bp) # Este es el que tiene /ask
    app.register_blueprint(sugerencia_bp)
    app.register_blueprint(rubros_bp)
    app.register_blueprint(metricas_bp)
    app.register_blueprint(upload_bp) 
    # Loguear el registro de cada blueprint
    for bp_name_key in app.blueprints:
        app_file_logger.info(f"Blueprint '{bp_name_key}' registrado.")
        
    return app

# Crear la instancia de la aplicación
app = create_app()

# --- Comandos CLI Personalizados ---
# (Tus comandos CLI se mantienen igual, ya estaban bien)
@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos_command():
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    app_file_logger.info("Iniciando carga de datos iniciales CLI...")
    try:
        app_file_logger.info("Asegurando que todas las tablas estén creadas (db.create_all())...")
        db.create_all() 
        cargar_usuarios_demo()
        cargar_faqs()
        cargar_sugerencias()
        app_file_logger.info("Datos iniciales cargados correctamente CLI.")
    except Exception as e:
        app_file_logger.error(f"❌ Error cargando datos iniciales CLI: {e}\n{traceback.format_exc()}")
app.cli.add_command(cargar_datos_command, name="cargar_datos_iniciales")

@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones_command():
    app_file_logger.info("Intentando aplicar migraciones de base de datos CLI...")
    try:
        upgrade() 
        app_file_logger.info("Migraciones de base de datos aplicadas correctamente CLI.")
    except Exception as e:
        app_file_logger.error(f"❌ Error durante la aplicación de migraciones CLI (upgrade): {e}\n{traceback.format_exc()}")
app.cli.add_command(aplicar_migraciones_command, name="aplicar_migraciones")

# --- Punto de Entrada para Servidor WSGI (opcional para desarrollo local) ---
if __name__ == '__main__':
    # Esta parte solo se ejecuta si corres 'python app.py' directamente.
    # Gunicorn en Render llamará directamente al objeto 'app'.
    # Forzar nivel DEBUG para desarrollo local si se corre así.
    logging.getLogger().setLevel(logging.DEBUG) # Logger raíz a DEBUG
    app.logger.setLevel(logging.DEBUG)          # Logger de Flask a DEBUG
    for handler in app.logger.handlers:         # Handlers de Flask a DEBUG
        handler.setLevel(logging.DEBUG)
    app_file_logger.info("Iniciando aplicación Flask con el servidor de desarrollo (DEBUGGING LOCAL)...")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
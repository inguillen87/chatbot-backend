# app.py (o el nombre de tu archivo principal)

import os
import logging
from logging.handlers import RotatingFileHandler
import click
from flask import Flask, request, make_response # make_response podría no ser necesario si Flask-CORS maneja todo
from flask_cors import CORS
from flask_migrate import upgrade
from flask.cli import with_appcontext
from dotenv import load_dotenv
from datetime import timedelta
import traceback

from config import Config # Tu config.py que ya revisamos
from extensions import db, migrate, login_manager
from services.upload_processor import upload_bp # Asumo que este es un Blueprint
from models import User # Para el user_loader

# Cargar variables de entorno desde .env (ideal para desarrollo local)
load_dotenv()

# --- Configuración de Logging ---
# Es bueno tener esto al principio.
# Considera mover la configuración detallada de logging a una función o módulo separado
# si se vuelve muy extensa, pero para este tamaño está bien aquí.
if not os.path.exists("logs"):
    os.makedirs("logs")

# Usar el logger de la aplicación si está disponible, o el logger raíz.
# Esto ayuda si más adelante decides configurar el logger de Flask de forma más específica.
log_file_path = os.path.join("logs", "chatbot.log") # Más seguro para construir rutas
file_handler = RotatingFileHandler(log_file_path, maxBytes=10240, backupCount=5)
# Es buena idea establecer el nivel en el handler también
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(module)s:%(lineno)d | %(message)s" # Añadido módulo y línea para mejor depuración
)
file_handler.setFormatter(formatter)

# Configurar el logger raíz o el logger específico de tu aplicación
# Si solo usas un logger global, logging.getLogger() está bien.
# Si quisieras un logger específico para tu app: app_logger = logging.getLogger(__name__) o logging.getLogger('mi_app')
root_logger = logging.getLogger()
root_logger.addHandler(file_handler)
root_logger.setLevel(logging.INFO) # Nivel general para el logger raíz

logger = logging.getLogger(__name__) # Logger específico para este módulo (app.py)

# --- Flask-Login User Loader ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Factoría de la Aplicación ---
def create_app(config_class=Config): # Permitir pasar diferentes clases de configuración (útil para testing)
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Inicializar extensiones
    login_manager.init_app(app)
    db.init_app(app)
    migrate.init_app(app, db) # Correcto: db debe estar inicializado antes que migrate

    # Crear el directorio 'instance' si no existe (Flask lo usa para ciertas cosas, como bases de datos SQLite si no se especifica ruta absoluta)
    # Aunque en tu config.py ya manejas rutas absolutas/relativas, esto no hace daño.
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError as e:
        logger.error(f"Error creando el directorio instance_path {app.instance_path}: {e}")
    
    logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}") # Log útil

    # --- Configuración de CORS ---
    # Flask-CORS es la forma recomendada y más limpia.
    # El @app.after_request y @app.before_request para CORS que tenías son redundantes
    # si Flask-CORS está configurado correctamente.
    allowed_origins = [
        "https://chatboc.ar",
        "https://www.chatboc.ar",
        "http://localhost:5173", # Para desarrollo local del frontend
    ]
    # Si tienes una variable de entorno para orígenes adicionales, puedes añadirla aquí.
    # por ejemplo: extra_origins = os.getenv("CORS_EXTRA_ORIGINS")
    # if extra_origins:
    # allowed_origins.extend(extra_origins.split(','))

    try:
        CORS(
            app,
            origins=allowed_origins,
            supports_credentials=True,
            allow_headers=["Content-Type", "Authorization"], # Asegúrate que "Authorization" sea necesario
            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            max_age=timedelta(hours=1) # Opcional, pero bueno para el rendimiento de preflights
        )
        logger.info(f"CORS aplicado para los orígenes: {allowed_origins}")
    except Exception as e:
        logger.error(f"Error aplicando CORS con Flask-CORS: {e}")

    # --- Registro de Blueprints ---
    # Tu forma de registrar Blueprints con manejo de errores es buena.
    blueprints_to_register = [
        ("routes.auth", "auth_bp"),
        ("routes.chat", "chat_bp"),
        ("routes.sugerencias", "sugerencia_bp"),
        ("routes.rubros", "rubros_bp"),
        ("routes.metricas", "metricas_bp")
        # Añade aquí más blueprints en el mismo formato
    ]

    for bp_import_path, bp_name in blueprints_to_register:
        try:
            module = __import__(bp_import_path, fromlist=[bp_name])
            blueprint = getattr(module, bp_name)
            app.register_blueprint(blueprint)
            logger.info(f" Blueprint '{bp_name}' registrado desde '{bp_import_path}'.")
        except ImportError:
            logger.error(f"❌ Error de importación: No se pudo encontrar el módulo o blueprint '{bp_name}' en '{bp_import_path}'.\n{traceback.format_exc()}")
        except AttributeError:
            logger.error(f"❌ Error de atributo: No se pudo encontrar el objeto blueprint '{bp_name}' dentro del módulo '{bp_import_path}'.\n{traceback.format_exc()}")
        except Exception as e:
            logger.error(f"❌ Error desconocido registrando blueprint '{bp_name}' desde '{bp_import_path}': {e}\n{traceback.format_exc()}")

    # Registrar upload_bp (asumiendo que es un objeto Blueprint ya importado)
    try:
        app.register_blueprint(upload_bp) # Si upload_bp es importado directamente
        # Si upload_bp también sigue el patrón de string:
        # module_upload = __import__("services.upload_processor", fromlist=["upload_bp"])
        # bp_upload = getattr(module_upload, "upload_bp")
        # app.register_blueprint(bp_upload)
        logger.info(" Blueprint 'upload_bp' registrado.")
    except Exception as e:
        logger.error(f"❌ Error registrando 'upload_bp': {e}\n{traceback.format_exc()}")
        
    # Aquí podrías añadir más inicializaciones específicas de la app si es necesario

    return app

# Crear la instancia de la aplicación
# Esta instancia 'app' será la que use Gunicorn/WSGI server.
app = create_app()

# --- Comandos CLI Personalizados ---
# Estos comandos se ejecutarán en el contexto de la aplicación.

@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos_command(): # Cambiado el nombre de la función para evitar conflicto con el nombre del comando
    """Carga datos iniciales: FAQs, sugerencias y usuarios demo."""
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo # Importación local al comando
    
    logger.info("Iniciando carga de datos iniciales...")
    
    # Consideración sobre db.create_all():
    # Si estás usando Flask-Migrate para TODA la gestión del esquema de tu BD,
    # 'flask db upgrade' (o la función 'upgrade()' que usas abajo) debería crear las tablas.
    # Llamar a db.create_all() aquí podría ser redundante o, en algunos casos, problemático
    # si hay inconsistencias entre tus modelos y las migraciones.
    # Si tienes tablas que NO están gestionadas por Flask-Migrate, entonces sí necesitas db.create_all().
    # Si es solo para asegurar que la BD exista antes de cargar datos y las migraciones ya corrieron,
    # podría ser seguro, pero es algo a tener en cuenta.
    # Por ahora lo dejamos, pero evalúa si es estrictamente necesario en tu flujo.
    try:
        # Considera ejecutar 'upgrade()' aquí si es un setup inicial absoluto
        # y quieres asegurar que el esquema esté al día ANTES de cargar datos.
        # upgrade() # Si quieres asegurar que las migraciones estén aplicadas
        # db.create_all() # Si es para un setup inicial y confías en que esto no chocará con migraciones
        logger.info("Asegurando que todas las tablas estén creadas (db.create_all())...")
        db.create_all() # Mantenido según tu código original
    except Exception as e:
        logger.error(f"Error durante db.create_all() en cargar_datos_iniciales: {e}\n{traceback.format_exc()}")
        return # No continuar si la creación de tablas falla

    try:
        cargar_usuarios_demo()
        cargar_faqs()
        cargar_sugerencias()
        logger.info(" Datos iniciales cargados correctamente.")
    except Exception as e:
        logger.error(f"❌ Error cargando datos iniciales: {e}\n{traceback.format_exc()}")

app.cli.add_command(cargar_datos_command, name="cargar_datos_iniciales") # Puedes especificar 'name' si quieres que el comando sea diferente al nombre de la función


@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones_command(): # Nombre de función cambiado
    """Aplica las migraciones de la base de datos usando Flask-Migrate."""
    logger.info("Intentando aplicar migraciones de base de datos...")
    try:
        upgrade() # Esta es la función de Flask-Migrate para aplicar migraciones.
        logger.info(" Migraciones de base de datos aplicadas correctamente.")
    except Exception as e:
        logger.error(f"❌ Error durante la aplicación de migraciones (upgrade): {e}\n{traceback.format_exc()}")

app.cli.add_command(aplicar_migraciones_command, name="aplicar_migraciones")


# --- Eliminación de Handlers CORS Manuales ---
# Las siguientes funciones @app.after_request y @app.before_request para CORS
# ya no son necesarias si Flask-CORS está configurado como arriba.
# Flask-CORS maneja las solicitudes OPTIONS (preflight) y añade las cabeceras necesarias.
# Eliminarlas simplificará tu código y evitará posibles conflictos.

# @app.after_request
# def apply_cors_headers(response):
# ... (código anterior) ...

# @app.before_request
# def handle_preflight():
# ... (código anterior) ...

# --- Punto de Entrada para Servidor WSGI (opcional, si no usas 'flask run' o gunicorn directamente con app:app) ---
# if __name__ == '__main__':
# logger.info("Iniciando aplicación Flask con el servidor de desarrollo.")
# app.run(debug=app.config.get("DEBUG", False)) # Usar DEBUG de la config
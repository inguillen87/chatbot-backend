import os
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask
from flask_cors import CORS
from config import Config
from extensions import db, migrate
from dotenv import load_dotenv
from flask_migrate import upgrade

# Importar funciones opcionales
from faq_loader import cargar_datos_iniciales

load_dotenv()

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    # Crear carpeta de logs si no existe
    os.makedirs("logs", exist_ok=True)

    # Configurar logging con rotación
    handler = RotatingFileHandler("logs/app.log", maxBytes=1000000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    # Soporte CORS
    CORS(app)

    # Inicializar extensiones
    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        try:
            db_path = app.config.get("SQLALCHEMY_DATABASE_URI", "").replace("sqlite:///", "")
            logger.info(f"📦 Ruta de la base de datos: {db_path}")

            if os.getenv("FORZAR_RESET") == "1" and db_path and os.path.exists(db_path):
                os.remove(db_path)
                logger.warning("💣 Base de datos borrada manualmente por FORZAR_RESET=1")

            if not os.path.exists(db_path):
                logger.info("🆕 Base no encontrada, creando nueva y cargando datos iniciales")
                db.create_all()
                cargar_datos_iniciales()
            else:
                logger.info("✅ Base ya existe. Ejecutando migraciones...")
                upgrade()

            if os.getenv("ALLOW_DB_INIT") == "1":
                logger.info("🚀 Ejecutando carga de datos iniciales por ALLOW_DB_INIT=1")
                cargar_datos_iniciales()

        except Exception as e:
            logger.exception("❌ Error crítico durante la inicialización")

    # Registro de Blueprints
    from routes.auth_bp import auth_bp
    from routes.chat_bp import chat_bp
    from routes.upload_bp import upload_bp
    from routes.rubros_bp import rubros_bp
    from routes.metricas_bp import metricas_bp
    from routes.sugerencia_bp import sugerencia_bp

    for bp in [auth_bp, chat_bp, upload_bp, rubros_bp, metricas_bp, sugerencia_bp]:
        app.register_blueprint(bp)
        logger.info(f"✅ Blueprint {bp.name} registrado.")

    return app

app = create_app()

# Comando personalizado para carga manual desde consola
@app.cli.command("cargar_datos_iniciales")
def cargar_datos():
    try:
        cargar_datos_iniciales()
        logging.info("✅ Datos iniciales cargados.")
    except Exception as e:
        logging.exception("❌ Error al cargar datos iniciales")

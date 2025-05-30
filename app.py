import os
import logging
from logging.handlers import RotatingFileHandler
import click
from flask import Flask, request, make_response
from flask_cors import CORS
from flask_migrate import upgrade
from flask.cli import with_appcontext
from dotenv import load_dotenv
from datetime import timedelta
import traceback

from config import Config
from extensions import db, migrate, login_manager
from services.upload_processor import upload_bp
from models import User

# Cargar entorno
load_dotenv()

# Configurar logs
if not os.path.exists("logs"):
    os.makedirs("logs")

file_handler = RotatingFileHandler("logs/chatbot.log", maxBytes=10240, backupCount=5)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)
logging.getLogger().setLevel(logging.INFO)

logger = logging.getLogger(__name__)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    login_manager.init_app(app)
    db.init_app(app)
    migrate.init_app(app, db)

    os.makedirs(app.instance_path, exist_ok=True)
    logger.info("Base de datos y migraciones listas.")

    # CORS
    try:
        CORS(
            app,
            origins=[
                "https://chatboc.ar",
                "https://www.chatboc.ar",
                "http://localhost:5173"
            ],
            supports_credentials=True,
            allow_headers=["Content-Type", "Authorization"],
            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            max_age=timedelta(hours=1)
        )
        logger.info("CORS aplicado correctamente.")
    except Exception as e:
        logger.error(f"Error aplicando CORS: {e}")

    # Blueprints - REGISTRO A PRUEBA DE ERRORES
    blueprints = [
        ("routes.auth", "auth_bp"),
        ("routes.chat", "chat_bp"),
        ("routes.sugerencias", "sugerencia_bp"),
        ("routes.metricas", "metricas_bp")
    ]

    for bp_import, name in blueprints:
        try:
            bp_module = __import__(bp_import, fromlist=[name])
            app.register_blueprint(getattr(bp_module, name))
            logger.info(f"✅ Blueprint {name} registrado.")
        except Exception as e:
            logger.error(f"❌ Error registrando {name}: {e}\n{traceback.format_exc()}")

    try:
        app.register_blueprint(upload_bp)
        logger.info("Blueprint upload_bp registrado.")
    except Exception as e:
        logger.error(f"Error registrando upload_bp: {e}")

    return app

app = create_app()

# CLI: Cargar datos iniciales
@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos():
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    logger.info("Cargando datos iniciales...")
    db.create_all()
    cargar_usuarios_demo()
    cargar_faqs()
    cargar_sugerencias()
    logger.info("Datos iniciales cargados correctamente.")

app.cli.add_command(cargar_datos)

@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones():
    try:
        upgrade()
        logger.info("Migraciones aplicadas correctamente.")
    except Exception as e:
        logger.error(f"Error en upgrade de migraciones: {e}\n{traceback.format_exc()}")

app.cli.add_command(aplicar_migraciones)

@app.after_request
def apply_cors_headers(response):
    origin = request.headers.get("Origin")
    allowed_origins = [
        "https://chatboc.ar",
        "https://www.chatboc.ar",
        "http://localhost:5173"
    ]
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Vary"] = "Origin"
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        logger.info(f"Preflight OPTIONS recibido en {request.path}")
        response = make_response()
        response.status_code = 200
        return response

import logging
from logging.handlers import RotatingFileHandler
import os
import click
from flask import Flask, request, make_response
from flask_cors import CORS
from config import Config
from extensions import db, migrate, login_manager
from dotenv import load_dotenv
from flask_migrate import upgrade
from flask.cli import with_appcontext
from datetime import timedelta
from models import User
from services.upload_processor import upload_bp

# Cargar entorno
load_dotenv()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Crear carpeta de logs si no existe
if not os.path.exists("logs"):
    os.makedirs("logs")

# Logging
file_handler = RotatingFileHandler("logs/chatbot.log", maxBytes=10240, backupCount=5)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    login_manager.init_app(app)

    # Mostrar info de base de datos
    db_path = Config.SQLALCHEMY_DATABASE_URI.replace("sqlite:///", "")
    print(f"CHECK db_path = {db_path}")
    print(f"ALLOW_DB_INIT = {os.getenv('ALLOW_DB_INIT')}")

    if not os.path.exists(db_path):
        if not os.getenv("ALLOW_DB_INIT"):
            print("La base de datos no existe y ALLOW_DB_INIT no está seteado.")
        else:
            print("La base no existe, pero ALLOW_DB_INIT está presente. Se permitirá crear.")
    else:
        print("La base de datos ya existe.")

    os.makedirs(app.instance_path, exist_ok=True)

    try:
        CORS(
            app,
            origins=[
                "https://chatboc.ar",
                "https://www.chatboc.ar",
                "http://localhost:5173"  # para pruebas locales
            ],
            supports_credentials=True,
            allow_headers=["Content-Type", "Authorization"],
            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            max_age=timedelta(hours=1)
        )
        print("✅ CORS aplicado correctamente.")
    except Exception as e:
        print("❌ Error aplicando CORS:", e)

    try:
        db.init_app(app)
        migrate.init_app(app, db)
    except Exception as e:
        print("❌ Error inicializando extensiones:", e)

        # Registrar blueprints
    for bp_import, name in [
        ("routes.auth", "auth_bp"),
        ("routes.chat", "chat_bp"),
        ("routes.sugerencias", "sugerencia_bp"),
        ("routes.rubros", "rubros_bp"),
        ("routes.metricas", "metricas_bp"),
    ]:
        try:
            bp_module = __import__(bp_import, fromlist=[name])
            app.register_blueprint(getattr(bp_module, name))
        except Exception as e:
            print(f"❌ Error registrando {name}:", e)

    # Registrar blueprint para subir catálogos embebidos
    try:
        app.register_blueprint(upload_bp)
        print("✅ Blueprint upload_bp registrado.")
    except Exception as e:
        print("❌ Error registrando upload_bp:", e)

    return app

# Crear app
app = create_app()

# Importar modelos
import models

# Comando CLI para cargar datos iniciales
@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos():
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    print("🚀 Iniciando carga de datos iniciales...")
    db.create_all()
    cargar_usuarios_demo()
    cargar_faqs()
    cargar_sugerencias()
    print("✅ Datos iniciales cargados correctamente.")

# Registrar comando
app.cli.add_command(cargar_datos)

# Comando CLI opcional para aplicar migraciones manualmente
@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones():
    try:
        upgrade()
        print("✅ Migraciones aplicadas correctamente.")
    except Exception as e:
        logging.error(f"❌ Error en upgrade de migraciones: {e}")

app.cli.add_command(aplicar_migraciones)

# Agregar headers de CORS a todas las respuestas
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

# Manejar preflight OPTIONS devolviendo 200 OK
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        print(f"🟡 Preflight OPTIONS recibido en {request.path}")
        response = make_response()
        response.status_code = 200
        return response

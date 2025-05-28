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
from models import User  # ✅ NECESARIO PARA evitar error en @user_loader

# Cargar entorno
load_dotenv()

# 🔐 Cargar usuario desde la sesión
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# 📂 Crear carpeta de logs si no existe
if not os.path.exists("logs"):
    os.makedirs("logs")

# 📝 Configurar logging a archivo
file_handler = RotatingFileHandler("logs/chatbot.log", maxBytes=10240, backupCount=5)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)

# 🚀 Crear app principal
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    login_manager.init_app(app)

    # ℹ️ Mostrar info de DB
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

    # ✅ CORS
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
        print("✅ CORS aplicado correctamente.")
    except Exception as e:
        print("❌ Error aplicando CORS:", e)

    # ✅ Inicializar extensiones
    try:
        db.init_app(app)
        migrate.init_app(app, db)
        print("✅ DB y migraciones iniciadas.")
    except Exception as e:
        print("❌ Error inicializando extensiones:", e)

    # ✅ Registrar blueprints
    blueprints = [
        ("routes.auth", "auth_bp"),
        ("routes.chat", "chat_bp"),
        ("routes.sugerencias", "sugerencia_bp"),
        ("routes.rubros", "rubros_bp"),
        ("routes.metricas", "metricas_bp")
    ]

    for bp_import, name in blueprints:
        try:
            bp_module = __import__(bp_import, fromlist=[name])
            app.register_blueprint(getattr(bp_module, name))
            print(f"✅ Blueprint {name} registrado.")
        except Exception as e:
            print(f"❌ Error registrando {name}: {e}")
            traceback.print_exc()

    # ✅ Blueprint para subir catálogos
    try:
        app.register_blueprint(upload_bp)
        print("✅ Blueprint upload_bp registrado.")
    except Exception as e:
        print("❌ Error registrando upload_bp:", e)

    return app

# ✅ Crear instancia de la app
app = create_app()

# 📦 Importar modelos (por si hace falta desde consola)
import models

# 📦 Comando CLI: cargar datos iniciales
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

app.cli.add_command(cargar_datos)

# 📦 Comando CLI: aplicar migraciones
@click.command("aplicar_migraciones")
@with_appcontext
def aplicar_migraciones():
    try:
        upgrade()
        print("✅ Migraciones aplicadas correctamente.")
    except Exception as e:
        logging.error(f"❌ Error en upgrade de migraciones: {e}")

app.cli.add_command(aplicar_migraciones)

# 🌐 CORS global para todas las respuestas
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

# ⚙️ Preflight OPTIONS handler
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        print(f"🟡 Preflight OPTIONS recibido en {request.path}")
        response = make_response()
        response.status_code = 200
        return response

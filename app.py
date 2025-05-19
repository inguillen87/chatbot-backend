import logging
from logging.handlers import RotatingFileHandler
import os
from flask import Flask
from flask_cors import CORS
from config import Config
from extensions import db, migrate
from dotenv import load_dotenv
from flask_migrate import upgrade

# ⬇️ Importar las funciones de carga
from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo

load_dotenv()

# Crear carpeta de logs si no existe
if not os.path.exists("logs"):
    os.makedirs("logs")

# Configuración de logging
file_handler = RotatingFileHandler("logs/chatbot.log", maxBytes=10240, backupCount=5)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    try:
        CORS(app, resources={r"/*": {"origins": [
            "https://chatboc.ar",
            "https://www.chatboc.ar"
        ]}}, supports_credentials=True)
        print("✅ CORS aplicado globalmente.")
    except Exception as e:
        print("❌ Error aplicando CORS:", e)

    try:
        db.init_app(app)
        migrate.init_app(app, db)
    except Exception as e:
        print("❌ Error inicializando extensiones:", e)

    try:
        from routes.auth import auth_bp
        app.register_blueprint(auth_bp)
    except Exception as e:
        print("❌ Error registrando auth_bp:", e)

    try:
        from routes.chat import chat_bp
        app.register_blueprint(chat_bp)
    except Exception as e:
        print("❌ Error registrando chat_bp:", e)

    try:
        from routes.sugerencias import sugerencia_bp
        app.register_blueprint(sugerencia_bp)
    except Exception as e:
        print("❌ Error registrando sugerencia_bp:", e)

    try:
        with app.app_context():
            from models import QA
            db.create_all()
    except Exception as e:
        print("❌ Error creando tablas:", e)

    # Comando CLI para inicializar datos
    @app.cli.command("cargar_datos_iniciales")
    def cargar_datos_iniciales():
        with app.app_context():
            print("🚀 Iniciando carga de datos iniciales...")
            cargar_faqs()
            cargar_sugerencias()
            cargar_usuarios_demo()
            print("✅ Todo cargado correctamente.")

    return app

# App principal
app = create_app()

# Ejecutar migraciones al iniciar
with app.app_context():
    try:
        upgrade()
        print("✅ Migraciones aplicadas.")
    except Exception as e:
        print("❌ Error en upgrade de migraciones:", e)

if __name__ == '__main__':
    os.environ["FLASK_ENV"] = "development"
    os.environ["FLASK_RUN_FROM_CLI"] = "false"
    print("✅ Servidor iniciado en modo desarrollo.")
    app.run(debug=True, port=5000, use_reloader=False)

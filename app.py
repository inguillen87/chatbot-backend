import logging
from logging.handlers import RotatingFileHandler
import os
from flask import Flask
from flask_cors import CORS
from config import Config
from extensions import db, migrate
from dotenv import load_dotenv
from sqlalchemy import text
from flask_migrate import upgrade  # 👈 esto es lo correcto

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

    # Activar CORS
    try:
        CORS(app, resources={r"/*": {"origins": "*"}})
    except Exception as e:
        print("❌ Error en CORS:", e)

    # Inicializar extensiones
    try:
        db.init_app(app)
        migrate.init_app(app, db)
    except Exception as e:
        print("❌ Error en init_app:", e)

    # Registrar Blueprints
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

    # Crear tablas si no existen (solo útil en desarrollo local)
    try:
        with app.app_context():
            from models import QA
            db.create_all()
    except Exception as e:
        print("❌ Error en db.create_all:", e)

    return app

# App para producción (gunicorn o flask run)
app = create_app()

# 👇 Ejecutar migraciones automáticamente al iniciar
with app.app_context():
    try:
        upgrade()
        print("✅ Migraciones aplicadas automáticamente.")
    except Exception as e:
        print("❌ Error aplicando migraciones:", e)

if __name__ == '__main__':
    os.environ["FLASK_ENV"] = "development"
    os.environ["FLASK_RUN_FROM_CLI"] = "false"
    print("✅ App creada, ahora intento iniciar el servidor...")
    app.run(debug=True, port=5000, use_reloader=False)

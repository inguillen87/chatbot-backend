import logging
from logging.handlers import RotatingFileHandler
import os
from sqlalchemy import text
from flask import Flask
from flask_cors import CORS
from config import Config
from extensions import db
from dotenv import load_dotenv
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
        CORS(app, resources={r"/*": {"origins": "*"}})
    except Exception as e:
        print("❌ Error en CORS:", e)

    try:
        db.init_app(app)
    except Exception as e:
        print("❌ Error en db.init_app:", e)

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
        with app.app_context():
            from models import QA
            db.create_all()
    except Exception as e:
        print("❌ Error en db.create_all:", e)

    return app

# 👇 ESTA LÍNEA VA ACÁ (fuera del if), PARA GUNICORN
app = create_app()

with app.app_context():
    try:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE user ADD COLUMN rubro_id INTEGER"))
            print("✅ Columna rubro_id agregada a la tabla user.")
    except Exception as e:
        if "duplicate column name" in str(e) or "already exists" in str(e):
            print("ℹ️ La columna rubro_id ya existe. Todo ok.")
        else:
            print("❌ Error al agregar columna rubro_id:", e)
            
if __name__ == '__main__':
    os.environ["FLASK_ENV"] = "development"
    os.environ["FLASK_RUN_FROM_CLI"] = "false"
    print("✅ App creada, ahora intento iniciar el servidor...")
    app.run(debug=True, port=5000, use_reloader=False)

import logging
from logging.handlers import RotatingFileHandler
import os
import click
from flask import Flask
from flask_cors import CORS
from config import Config
from extensions import db, migrate
from dotenv import load_dotenv
from flask_migrate import upgrade
from flask.cli import with_appcontext

# ⬇️ Cargar entorno
load_dotenv()

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

    # 🧪 Mostrar la ruta de la base y el estado de la variable de entorno
    db_path = Config.SQLALCHEMY_DATABASE_URI.replace("sqlite:///", "")
    print(f"🧪 CHECK db_path = {db_path}")
    print(f"🧪 ALLOW_DB_INIT = {os.getenv('ALLOW_DB_INIT')}")

    # 🟢 Avisamos si no existe, pero no cortamos
    if not os.path.exists(db_path):
        if not os.getenv("ALLOW_DB_INIT"):
            print("🛑 La base de datos no existe y ALLOW_DB_INIT no está seteado.")
        else:
            print("🟢 La base no existe, pero ALLOW_DB_INIT está presente. Se permitirá crear.")
    else:
        print("📦 La base de datos ya existe.")

    # 🧱 Crear carpeta instance si no existe
    os.makedirs(app.instance_path, exist_ok=True)

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

    # Blueprints
    for bp_import, name in [
        ("routes.auth", "auth_bp"),
        ("routes.chat", "chat_bp"),
        ("routes.sugerencias", "sugerencia_bp"),
        ("routes.rubros", "rubros_bp"),
        ("routes.metricas", "metricas_bp")
    ]:
        try:
            bp_module = __import__(bp_import, fromlist=[name])
            app.register_blueprint(getattr(bp_module, name))
        except Exception as e:
            print(f"❌ Error registrando {name}:", e)

    return app

# Crear app
app = create_app()

# Importar modelos
import models

# Comando CLI para cargar datos
@click.command("cargar_datos_iniciales")
@with_appcontext
def cargar_datos():
    from faq_loader import cargar_faqs, cargar_sugerencias, cargar_usuarios_demo
    print("⚙️ Iniciando carga de datos iniciales...")

    db.create_all()
    cargar_usuarios_demo()
    cargar_faqs()
    cargar_sugerencias()
    print("✅ Datos iniciales cargados correctamente.")

# Registrar comando
app.cli.add_command(cargar_datos)

# Aplicar migraciones
with app.app_context():
    try:
        upgrade()
        print("✅ Migraciones aplicadas correctamente.")
    except Exception as e:
        logging.error(f"❌ Error en upgrade de migraciones: {e}")

# Local dev
if __name__ == '__main__':
    os.environ["FLASK_ENV"] = "development"
    os.environ["FLASK_RUN_FROM_CLI"] = "false"
    print("✅ Servidor iniciado en modo desarrollo.")
    app.run(debug=True, port=5000, use_reloader=False)

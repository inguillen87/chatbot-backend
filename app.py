import logging
from logging.handlers import RotatingFileHandler
import os
from flask import Flask
from flask_cors import CORS
from config import Config
from extensions import db, migrate
from dotenv import load_dotenv
from flask_migrate import upgrade

# ⬇️ Importar funciones de carga manual (opcional desde consola)
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
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/database.db"

    # 🔒 Seguridad: abortar si no existe la base
    db_path = "/data/database.db"
    if not os.path.exists(db_path):
        print("🚨 ERROR CRÍTICO: /data/database.db no existe.")
        print("🛑 Abortando para evitar pérdida de datos.")
        exit(1)

    # ✅ CORS
    try:
        CORS(app, resources={r"/*": {"origins": [
            "https://chatboc.ar",
            "https://www.chatboc.ar"
        ]}}, supports_credentials=True)
        print("✅ CORS aplicado globalmente.")
    except Exception as e:
        print("❌ Error aplicando CORS:", e)

    # ✅ Inicializar extensiones
    try:
        db.init_app(app)
        migrate.init_app(app, db)
    except Exception as e:
        print("❌ Error inicializando extensiones:", e)

    # ✅ Registrar Blueprints
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

    # ✅ Comando CLI para cargar datos iniciales desde consola
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

# ✅ Aplicar migraciones seguras al iniciar
with app.app_context():
    try:
        upgrade()
        print("✅ Migraciones aplicadas correctamente.")
    except Exception as e:
        logging.error(f"❌ Error en upgrade de migraciones (ignorado en producción): {e}")

# ✅ Modo local
if __name__ == '__main__':
    os.environ["FLASK_ENV"] = "development"
    os.environ["FLASK_RUN_FROM_CLI"] = "false"
    print("✅ Servidor iniciado en modo desarrollo.")
    app.run(debug=True, port=5000, use_reloader=False)

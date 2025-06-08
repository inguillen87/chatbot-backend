# app.py

import os
import logging
import sys
from flask import Flask
from flask_cors import CORS
from flask_session import Session  # <-- 1. IMPORTACIÓN AÑADIDA

from config import Config
from extensions import db, migrate
from models import User

# Importación de todas tus rutas (Blueprints)
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.ticket import ticket_bp
from routes.rubros import rubros_bp
from services.upload_processor import upload_bp
from cli_commands import register_commands

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- Bloque de Diagnóstico (lo dejamos temporalmente) ---
    print("--- DIAGNÓSTICO DE SESIÓN ---")
    print(f"SECRET_KEY leída por Flask: {app.config.get('SECRET_KEY')}")
    print(f"SESSION_COOKIE_SECURE: {app.config.get('SESSION_COOKIE_SECURE')}")
    print(f"SESSION_COOKIE_SAMESITE: {app.config.get('SESSION_COOKIE_SAMESITE')}")
    print(f"SESSION_TYPE: {app.config.get('SESSION_TYPE')}")
    print("-----------------------------")

    # --- 2. Bloque único y ordenado de Inicialización de Extensiones ---
    db.init_app(app)
    migrate.init_app(app, db)
    
    # Configuración y activación de Sesiones en el Servidor
    app.config['SESSION_SQLALCHEMY'] = db
    Session(app)

    # --- Configuración de Logging ---
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    app.logger.info(f"Aplicación creada. Nivel de logging: {log_level}")
    app.logger.info(f"Usando base de datos: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # --- 3. Configuración de CORS ---
    allowed_origins = [
        "https://chatboc.ar",
        "https://www.chatboc.ar",
        "http://localhost:5173",
        "http://localhost:8080",
        "https://chatboc-frontend-2cmzvzayk-marcelos-projects-c26aa499.vercel.app",
        "https://chatboc-frontend-git-main-marcelos-projects-c26aa499.vercel.app",
        # Podrías agregar aquí cualquier otra URL de preview de Vercel si es necesario
    ]
    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Origin", "Accept"]
    )

    # --- 4. Registro de Blueprints (Rutas) ---
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(ticket_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(rubros_bp)

    # Registro de comandos CLI
    register_commands(app)

    # --- 5. La función devuelve la app al final de todo ---
    return app

# --- Creación de la instancia de la aplicación ---
app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
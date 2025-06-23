import os
import logging
import sys
from flask import Flask
from flask_cors import CORS
from flask_session import Session

from config import Config
from extensions import db, migrate
from models import User

# Importación de todas tus rutas (Blueprints)
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.ticket import ticket_bp
from routes.crm import crm_bp
from routes.rubros import rubros_bp
from services.upload_processor import upload_bp
from routes.archivos import archivos_bp
from cli_commands import register_commands
from routes.pedidos import pedidos_bp
from routes.catalogo import catalogo_bp
from routes.estadisticas import estadisticas_bp
from routes.empleados import empleados_bp
from routes.recordatorios import recordatorios_bp
from routes.historial import historial_bp
from routes.notifications import notifications_bp

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
    # --- RUTAS DE PRUEBA PARA DEPURAR LA SESIÓN ---
    @app.route('/poner-memoria')
    def poner_memoria():
        from flask import session
        session['clave_de_prueba'] = 'funciona!'
        return "<h1>Memoria establecida. Ahora andá a /leer-memoria</h1>"

    @app.route('/leer-memoria')
    def leer_memoria():
        from flask import session
        valor = session.get('clave_de_prueba', '¡LA MEMORIA ESTÁ VACÍA!')
        return f"<h1>El valor guardado en la memoria es: {valor}</h1>"
    # --- FIN DE RUTAS DE PRUEBA ---
@@ -100,50 +102,60 @@ def create_app(config_class=Config):
        ],
    )

    # --- FIX UNIVERSAL DE HEADERS CUSTOM PARA CORS ---
    @app.after_request
    def ensure_custom_cors_headers(resp):
        """
        Garantiza que TODOS los headers custom que tu app pueda llegar a usar,
        queden siempre incluidos en Access-Control-Allow-Headers de la respuesta,
        para que ningún preflight se los rechace, no importa si Flask-CORS los olvidó.
        """
        # Define la lista completa de headers custom que podés llegar a necesitar (sumá acá si agregás más)
        needed = [
            "Authorization", "Content-Type", "Origin", "Accept",
            "Anon-Id", "x-entity-token"
        ]
        prev = resp.headers.get("Access-Control-Allow-Headers", "")
        actual = [h.strip() for h in prev.split(",") if h.strip()]
        actual_lower = [h.lower() for h in actual]
        for n in needed:
            if n.lower() not in actual_lower:
                actual.append(n)
        resp.headers["Access-Control-Allow-Headers"] = ", ".join(actual)
        return resp

    @app.before_request
    def catch_all_options():
        """Handle any CORS preflight with a basic response."""
        from flask import request
        if request.method == "OPTIONS":
            from routes.chat import cors_options_response
            return cors_options_response()

    # --- 4. Registro de Blueprints (Rutas) ---
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(ticket_bp)
    app.register_blueprint(crm_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(archivos_bp)
    app.register_blueprint(rubros_bp)
    app.register_blueprint(catalogo_bp)
    app.register_blueprint(pedidos_bp)
    app.register_blueprint(estadisticas_bp)
    app.register_blueprint(empleados_bp)
    app.register_blueprint(recordatorios_bp)
    app.register_blueprint(historial_bp)
    app.register_blueprint(notifications_bp)

    # Registro de comandos CLI
    register_commands(app)

    # --- 5. La función devuelve la app al final de todo ---
    return app

# --- Creación de la instancia de la aplicación ---
app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
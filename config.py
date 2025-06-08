# En config.py
import os

# Directorio base de la aplicación
basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    """
    Clase de configuración principal de la aplicación.
    Contiene todas las variables de configuración.
    """
    
    # 1. LLAVE SECRETA: Crucial para la seguridad de la sesión.
    SECRET_KEY = os.getenv("SECRET_KEY", "una-llave-secreta-muy-segura-para-desarrollo-local")

    # 2. CONFIGURACIÓN DE LA BASE DE DATOS:
    if os.getenv("RENDER") == "true":
        SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
    else:
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 3. CONFIGURACIÓN DE COOKIES DE SESIÓN:
    # Para que funcionen en un entorno con dominios separados (Vercel + Render).
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'
    SESSION_COOKIE_DOMAIN = '.chatboc.ar'

    # 4. CONFIGURACIÓN PARA SESIONES EN EL LADO DEL SERVIDOR (Flask-Session)
    # Le decimos a Flask-Session que guarde la "memoria" en nuestra base de datos.
    SESSION_TYPE = 'sqlalchemy'
    SESSION_SQLALCHEMY_TABLE = 'sessions'
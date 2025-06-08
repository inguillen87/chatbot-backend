# En config.py
import os

# Directorio base de la aplicación
basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    """
    Clase de configuración principal de la aplicación.
    Contiene todas las variables de configuración.
    """
    
    # 1. LLAVE SECRETA: La lee desde las variables de entorno.
    # Es crucial para que la sesión (la memoria del bot) funcione.
    # Se agrega una llave por defecto para facilitar el desarrollo en local.
    SECRET_KEY = os.getenv("SECRET_KEY", "una-llave-secreta-muy-segura-para-desarrollo-local")

    # 2. CONFIGURACIÓN DE LA BASE DE DATOS:
    # Detecta si está corriendo en Render para usar la ruta de la base de datos correcta.
    if os.getenv("RENDER") == "true":
        # En Render, la base de datos se guarda en un disco persistente en /data/
        SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
    else:
        # En local, la guarda en la carpeta 'instance' del proyecto.
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}"

    # Desactiva una función de Flask-SQLAlchemy que consume recursos y no es necesaria.
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 3. CONFIGURACIÓN DE COOKIES DE SESIÓN (LA SOLUCIÓN A LA AMNESIA):
    # Estas dos líneas son las que arreglan el problema de la sesión entre dominios.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'
    SESSION_COOKIE_DOMAIN = '.chatboc.ar'

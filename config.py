import os

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "clave-generica")
    if os.getenv("RENDER") == "true":
        SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
    else:
        # Ruta relativa al path real de Flask
        SQLALCHEMY_DATABASE_URI = "sqlite:///database.db"  # ✅ Esto lo guarda en /instance/database.db real de Flask
    SQLALCHEMY_TRACK_MODIFICATIONS = False

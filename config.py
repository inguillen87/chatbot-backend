# En config.py (versión para depuración en Render)
import os
print("--- DEBUG CONFIG: Archivo config.py importado ---") # Para ver si se importa temprano
basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    RENDER_ENV_VAR = os.getenv("RENDER")

    print(f"--- DEBUG CONFIG: Dentro de la clase Config ---")
    print(f"Valor de os.getenv('RENDER') en Config: '{RENDER_ENV_VAR}' (Tipo: {type(RENDER_ENV_VAR)})")

    if RENDER_ENV_VAR == "true":
        print("--- DEBUG CONFIG: Condición RENDER_ENV_VAR == 'true' es VERDADERA.")
        SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
    else:
        print("--- DEBUG CONFIG: Condición RENDER_ENV_VAR == 'true' es FALSA.")
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}"

    print(f"--- DEBUG CONFIG: SQLALCHEMY_DATABASE_URI seleccionada en Config: '{SQLALCHEMY_DATABASE_URI}'")

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    class Config:
    # ... (acá ya tenés tu SECRET_KEY y SQLALCHEMY_DATABASE_URI) ...
    
    # 👇👇 AGREGAR O VERIFICAR ESTAS DOS LÍNEAS 👇👇
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'
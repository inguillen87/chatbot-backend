# En config.py
import os  # <--- AÑADE ESTA LÍNEA
import logging # Añade esto si usas logging dentro de Config, como te sugerí

# basedir se define usando 'os', así que el import debe estar antes
basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    RENDER_ENV_VAR = os.getenv("RENDER")
    # Si añadiste el logging que te sugerí:
    # logging.info(f"RENDER environment variable en Config: '{RENDER_ENV_VAR}' (type: {type(RENDER_ENV_VAR)})")

    if RENDER_ENV_VAR == "true":
        SQLALCHEMY_DATABASE_URI = "sqlite:////data/database.db"
        # logging.info(f"✅ Usando DB de Render desde Config: {SQLALCHEMY_DATABASE_URI}")
    else:
        # TU LÍNEA ACTUAL para local, asegúrate que la ruta sea correcta para tu estructura
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}"
        # logging.info(f"✅ Usando DB Local desde Config: {SQLALCHEMY_DATABASE_URI}")
        # Opcional: Crear el directorio instance localmente si no existe
        # try:
        #     os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
        # except OSError as e:
        #     logging.error(f"No se pudo crear el directorio para la DB local {os.path.dirname(local_db_path)}: {e}")
            
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Si tienes otras configuraciones como UPLOAD_FOLDER, irían aquí
    # UPLOAD_FOLDER = os.path.join(os.path.dirname(basedir) if RENDER_ENV_VAR != "true" else "/data", 'uploads') # Ejemplo
# scrap_cron.py

import os
import logging # Usaremos logging para un mejor control
# --- AJUSTE IMPORTANTE: La configuración de la app debe ir primero ---
# No necesitas 'django', esto parece ser para Flask.
# os.environ['FLASK_APP'] = 'app.py' # Esto generalmente no es necesario si usas el patrón de create_app
from app import create_app
from models import User
# --- IMPORTAMOS NUESTRA TAREA ORQUESTADORA ---
from tasks import actualizar_info_pyme_desde_web

# Configura un logger básico para ver la salida del cron en un archivo si quieres
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

app = create_app()

with app.app_context():
    logging.info("[CRON] Iniciando tarea de scraping para todos los usuarios con link_web.")
    
    # Obtenemos todos los usuarios que tienen un link_web no nulo y no vacío.
    users_a_scrapear = User.query.filter(User.link_web != None, User.link_web != '').all()
    
    logging.info(f"[CRON] Se encontraron {len(users_a_scrapear)} usuarios para procesar.")

    for user in users_a_scrapear:
        try:
            # --- LÓGICA SIMPLIFICADA ---
            # Simplemente llamamos a la función orquestadora que ya hace todo el trabajo.
            # No necesitamos traer el Rubro aquí ni llamar a los servicios por separado.
            logging.info(f"[CRON] Procesando ID de usuario: {user.id} ({user.nombre_empresa})")
            actualizar_info_pyme_desde_web(user.id)

        except Exception as e:
            # Si algo falla dentro de la tarea, la propia tarea lo registrará.
            # Este es un seguro por si la llamada a la tarea falla por completo.
            logging.error(f"[CRON] Error fatal procesando al usuario {user.id} ({user.link_web}): {e}", exc_info=True)

    logging.info("[CRON] Tarea de scraping finalizada.")
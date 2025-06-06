# scrap_cron.py

import os
import django
os.environ['FLASK_APP'] = 'app.py'  # ajusta si tu entrypoint se llama distinto

from app import create_app
from models import User, Rubro
from services.scraper import scrapear_info_entidad
from services.webinfo import guardar_info_web

app = create_app()

with app.app_context():
    users = User.query.filter(User.link_web != None).all()
    for user in users:
        try:
            rubro = Rubro.query.filter_by(id=user.rubro_id).first()
            print(f"[CRON] Scrapeando {user.link_web} ({user.nombre_empresa})")
            data = scrapear_info_entidad(user.link_web)
            guardar_info_web(user.id, rubro.id if rubro else None, user.link_web, data)
        except Exception as e:
            print(f"Error en {user.link_web}: {e}")

# services/webinfo.py

import json
from datetime import datetime
from models import SitioWebInfo, db

def guardar_info_web(user_id, rubro_id, url, data_dict):
    """
    Guarda o actualiza la info scrapeada de una web.
    """
    existente = SitioWebInfo.query.filter_by(user_id=user_id, url=url).first()
    datos_serializados = json.dumps(data_dict, ensure_ascii=False)
    if existente:
        existente.datos_json = datos_serializados
        existente.fecha_scraping = datetime.utcnow()
        existente.actualizado = True
        db.session.commit()
        return existente
    else:
        nuevo = SitioWebInfo(
            user_id=user_id,
            rubro_id=rubro_id,
            url=url,
            datos_json=datos_serializados,
            actualizado=True
        )
        db.session.add(nuevo)
        db.session.commit()
        return nuevo

def obtener_info_web(user_id, url):
    info = SitioWebInfo.query.filter_by(user_id=user_id, url=url).first()
    if info:
        return json.loads(info.datos_json)
    return {}

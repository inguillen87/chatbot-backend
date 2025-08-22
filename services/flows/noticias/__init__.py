from models import MunicipioTicket
from database import db
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

ALL_NEWS_URL = "https://juninmendoza.gov.ar/noticias/"

def handle(msg, meta):
    """
    Handles the Noticias (news) flow by fetching from the database.
    """
    logger.info("Handling 'noticias' query from database")

    # This is a mock value. In a real scenario, this would come from the user's session or context.
    municipio_id = meta.get("municipio_id", 1)

    if not municipio_id:
        # Fallback or error if no municipio_id is provided
        return {
            "type": "error",
            "title": "Error de configuración",
            "summary": "No se pudo determinar el municipio para buscar noticias."
        }

    # Query the database for the latest news and events
    try:
        news_and_events = db.session.query(MunicipioTicket).filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.categoria.in_(['Noticia', 'Evento'])
        ).order_by(MunicipioTicket.fecha.desc()).limit(3).all()
    except Exception as e:
        logger.error(f"Database error when fetching news: {e}")
        return {
            "type": "error",
            "title": "Error de base de datos",
            "summary": "No se pudieron obtener las noticias en este momento."
        }


    if not news_and_events:
        return {
            "type": "noticias",
            "title": "No se encontraron noticias",
            "summary": f"No pude encontrar noticias o eventos recientes.",
            "data": {"items": []},
            "cta": [
                {"type": "url", "label": "Ver archivo de noticias", "url": ALL_NEWS_URL}
            ],
            "source": "database"
        }

    # Format the results
    items = []
    for item in news_and_events:
        items.append({
            "title": item.asunto,
            "summary": item.detalles,
            "url": None, # Or a link to a detail page if it exists
            "image_url": item.foto_url_directa,
            "date": item.fecha.strftime("%Y-%m-%d")
        })

    # Build the payload
    payload = {
        "type": "noticias",
        "title": "Últimas noticias y eventos",
        "summary": "Aquí están los 3 titulares más recientes:",
        "data": {
            "items": items
        },
        "cta": [
            {"type": "url", "label": "Ver todas las noticias", "url": ALL_NEWS_URL}
        ],
        "source": "database"
    }

    return payload

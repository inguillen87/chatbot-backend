from services.tools import websearch
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# This should come from a config file
TRUSTED_SOURCES = ["juninmendoza.gov.ar"]
ALL_NEWS_URL = "https://juninmendoza.gov.ar/noticias/"

def handle(msg, meta):
    """
    Handles the Noticias (news) flow.
    """
    logger.info("Handling 'noticias' query")

    # The query can be generic ("noticias") or specific ("noticias sobre deportes")
    query = msg if len(msg.split()) > 1 else "noticias"

    # 1. Search for news on trusted sources
    search_results = websearch.query(query, site_filters=TRUSTED_SOURCES)

    if not search_results:
        return {
            "type": "error",
            "title": "No se encontraron noticias",
            "summary": f"No pude encontrar noticias recientes."
        }

    # 2. Format the top 3 results
    items = []
    for result in search_results[:3]:
        items.append({
            "title": result.get("title"),
            "url": result.get("link"),
            "date": datetime.now().strftime("%Y-%m-%d") # Placeholder for date
        })

    # 3. Build the payload
    payload = {
        "type": "noticias",
        "title": "Últimas noticias",
        "summary": "Aquí están los 3 titulares más recientes:",
        "data": {
            "items": items
        },
        "cta": [
            {"type": "url", "label": "Ver todas las noticias", "url": ALL_NEWS_URL}
        ],
        "source": "websearch"
    }

    return payload

import logging
from services.handlers.base_handler import BaseMunicipioHandler
from services.google_search import google_search

logger = logging.getLogger(__name__)

class PointsOfInterestHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        query = payload.get("pregunta", "")
        location = payload.get("location")

        if not query:
            return {
                "message_body": "Por favor, decime qué punto de interés estás buscando.",
                "options_list": [],
                "message_type": "text",
                "fuente": "poi_handler_no_query"
            }

        if location:
            search_query = f"{query} cerca de {location}"
        else:
            search_query = f"{query} en {self.context.get('municipio_config_actual', {}).get('nombre_display', 'el municipio')}"

        search_results = google_search(search_query)

        if not search_results:
            return {
                "message_body": f"No se encontraron resultados para '{query}'.",
                "options_list": [],
                "message_type": "text",
                "fuente": "poi_handler_no_results"
            }

        poi_items = []
        for result in search_results[:3]:
            poi_items.append(f"- {result.get('title')}\n{result.get('snippet')}\n[Ver más]({result.get('link')})")

        return {
            "message_body": f"Aquí hay algunos resultados para '{query}':\n" + "\n\n".join(poi_items),
            "options_list": [],
            "message_type": "text",
            "fuente": "poi_handler_with_results"
        }

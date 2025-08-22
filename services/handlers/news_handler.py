import logging
from services.handlers.base_handler import BaseMunicipioHandler
from services.conversation_state import ConversationState
from services.google_search import google_search

logger = logging.getLogger(__name__)

class NewsHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        contexto_municipio_actual = self.context.get("contexto_municipio_v2", {})
        municipio_config = self.context.get('municipio_config_actual', {})
        municipio_name = municipio_config.get('nombre_display', 'del municipio')
        municipio_website = municipio_config.get('website')

        if municipio_website:
            query = f"site:{municipio_website} noticias de {municipio_name}"
        else:
            query = f"noticias de {municipio_name}"

        search_results = google_search(query, days=1)

        if not search_results:
            return {
                "message_body": "No se encontraron noticias recientes.",
                "options_list": [],
                "message_type": "text",
                "fuente": "news_handler_no_results"
            }

        options = []
        for result in search_results[:5]:
            options.append({
                "id": f"news_{result.get('link')}",
                "texto": result.get('title'),
                "url": result.get('link'),
                "type": "url"
            })

        # Set context for the next turn
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_DE_LISTA.name
        contexto_municipio_actual['opciones_en_pantalla'] = options


        return {
            "message_body": "Aquí están las últimas noticias:",
            "options_list": options,
            "message_type": "interactive_list",
            "fuente": "news_handler_with_results"
        }

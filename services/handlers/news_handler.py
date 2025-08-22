import logging
from services.handlers.base_handler import BaseMunicipioHandler
from services.conversation_state import ConversationState
import os
import json
from datetime import datetime

logger = logging.getLogger(__name__)

MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")

class NewsHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        """
        Lee las noticias desde agenda_cultural.json, las filtra y las muestra.
        """
        logger.info("[NewsHandler] Consultando noticias desde archivo JSON local.")

        try:
            agenda_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'municipios', MUNICIPIO_ID, 'agenda_cultural.json')
            with open(agenda_path, 'r', encoding='utf-8') as f:
                agenda_data = json.load(f).get('eventos', [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"No se pudo cargar o parsear el archivo de agenda cultural: {e}")
            return {
                "message_body": "Lo siento, no pude acceder a las noticias en este momento.",
                "message_type": "text"
            }

        # 1. Filtrar solo los que son 'noticia'
        noticias = [post for post in agenda_data if post.get('tipo_post') == 'noticia']

        # 2. Ordenar por fecha de publicación descendente
        noticias.sort(key=lambda x: x.get('fecha_publicacion', ''), reverse=True)

        if not noticias:
            return {
                "message_body": "No hay noticias recientes para mostrar.",
                "message_type": "text"
            }

        # 3. Formatear la respuesta (mostrando las 5 más recientes)
        lista_noticias_str = []
        for noticia in noticias[:5]:
            titulo = noticia.get('titulo', 'Sin título')
            subtitulo = noticia.get('subtitulo')

            noticia_str = f"*{titulo}*"
            if subtitulo:
                noticia_str += f"\n_{subtitulo}_"

            lista_noticias_str.append(noticia_str)

        respuesta = "Aquí están las últimas noticias:\n\n" + "\n\n---\n\n".join(lista_noticias_str)

        # Add social media links
        respuesta += "\n\n---\n"
        respuesta += "Seguinos en nuestras redes:\n"
        respuesta += "Facebook: https://www.facebook.com/JuninMunicipio\n"
        respuesta += "Instagram: https://www.instagram.com/munijuninmdz"

        return {
            "message_body": respuesta,
            "message_type": "text"
        }

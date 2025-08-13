"""
Handles the flow for "trámites" (procedures).
"""
from services.google_search import google_search
import time

CACHE = {}
CACHE_TTL = 3600 # 1 hour

def handle(msg: str, ctx: dict):
    """
    Handles the conversation flow for procedures.
    """
    # The `msg` is the raw text or the action_id, which we can use as the query.
    tramite_query = msg.lower().strip().replace('_', ' ')

    # Check cache first
    cached_result = CACHE.get(tramite_query)
    if cached_result and (time.time() - cached_result['timestamp']) < CACHE_TTL:
        search_results = cached_result['results']
    else:
        user = ctx.get('user_obj')
        municipio_name = user.nombre_empresa if user and hasattr(user, 'nombre_empresa') and user.nombre_empresa else "Junín"
        full_query = f"tramite {tramite_query} en {municipio_name}"
        search_results = google_search(full_query)
        if search_results:
            CACHE[tramite_query] = {'results': search_results, 'timestamp': time.time()}

    if not search_results:
        return {
            "message_body": f"No encontré información sobre el trámite '{tramite_query}'.",
            "message_type": "text", "options_list": []
        }

    top_result = search_results[0]

    return {
        "message_body": (
            f"Encontré esto sobre '{tramite_query}':\n\n"
            f"**{top_result.get('title')}**\n"
            f"{top_result.get('snippet')}\n\n"
            f"Podés ver más en: {top_result.get('link')}"
        ),
        "message_type": "text",
        "options_list": [{"texto": "Ver en la web", "url": top_result.get('link')}]
    }

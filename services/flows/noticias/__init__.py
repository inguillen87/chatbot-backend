"""
Handles the flow for "noticias" (news).
"""
from services.google_search import google_search

def handle(msg, ctx):
    """
    Handles the conversation flow for news.
    """
    municipio_name = "Junín" # Placeholder
    if ctx.get('user_obj') and ctx.get('user_obj').nombre_empresa:
        municipio_name = ctx.get('user_obj').nombre_empresa

    # Search for recent news
    query = f"noticias de {municipio_name}"
    search_results = google_search(query, days=7) # Search for news from the last 7 days

    if not search_results:
        return {
            "message_body": f"No encontré noticias recientes sobre {municipio_name}.",
            "message_type": "text",
            "options_list": []
        }

    options = []
    for result in search_results[:5]: # Show top 5 results
        options.append({
            "texto": result.get('title'),
            "url": result.get('link'),
            "type": "url"
        })

    return {
        "message_body": f"Aquí están las últimas noticias sobre {municipio_name}:",
        "message_type": "interactive_list",
        "options_list": options
    }

from services.tools import gsc, scraper
import logging

logger = logging.getLogger(__name__)

# This should come from a config file per municipality
MUNI_URL = "juninmendoza.gov.ar"

def handle(msg, meta):
    """
    Handles the Trámites (procedures) flow.
    """
    logger.info(f"Handling 'trámite' query: {msg}")

    # 1. Search for the query on the municipality's website
    search_results = gsc.search(site=MUNI_URL, query=msg)

    if not search_results:
        return {
            "type": "error",
            "title": "No se encontró información",
            "summary": f"No pude encontrar información sobre '{msg}' en el sitio web del municipio."
        }

    # 2. Use the top result to scrape for more info
    top_result = search_results[0]
    page_url = top_result.get('link')

    scraped_data = {}
    if page_url:
        scraped_data = scraper.fetch(page_url)

    # 3. Combine the data into a neutral payload
    # The user's spec shows a very rich payload. I'll build a subset of it for now.
    payload = {
        "type": "tramite",
        "title": top_result.get('title', msg),
        "summary": top_result.get('snippet', 'Aquí tienes la información que encontré.'),
        "links": search_results, # The GSC tool already returns a list of link dicts
        "phones": scraped_data.get("phones", []),
        "emails": scraped_data.get("emails", []),
        "location": scraped_data.get("location"),
        "hours": scraped_data.get("hours"),
        "source": "gsc/scraper"
    }

    return payload

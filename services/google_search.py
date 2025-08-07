import os
import requests
import logging
import time
from cachetools import TTLCache

logger = logging.getLogger(__name__)
cache = TTLCache(maxsize=100, ttl=86400)

def google_search(query: str):
    """
    Performs a Google search using the Custom Search JSON API, with caching.
    """
    if query in cache:
        logger.info(f"Returning cached results for query: {query}")
        return cache[query]

    api_key = os.environ.get("GOOGLE_API_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        logger.error("GOOGLE_API_KEY and GOOGLE_CSE_ID must be set in the environment.")
        return None

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "searchType": "image",
        "num": 1
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        results = response.json().get("items", [])
        cache[query] = results
        return results
    except requests.exceptions.RequestException as e:
        logger.error(f"Error performing Google search: {e}")
        return None

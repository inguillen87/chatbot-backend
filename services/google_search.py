import os
import requests
import logging
import time
import re
from cachetools import TTLCache

logger = logging.getLogger(__name__)
cache = TTLCache(maxsize=100, ttl=86400)

def google_search(query: str, days=None):
    """
    Performs a Google search using the Custom Search JSON API, with caching.
    """
    # Sanitize the query to remove emojis and other non-standard characters
    try:
        # The following pattern covers most common emojis.
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags (iOS)
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "]+",
            flags=re.UNICODE,
        )
        sanitized_query = emoji_pattern.sub(r"", query)
        # Also remove leading/trailing whitespace that might result
        sanitized_query = sanitized_query.strip()
    except Exception:
        sanitized_query = query # Fallback to original query in case of error

    cache_key = f"{sanitized_query}_{days}"
    if cache_key in cache:
        logger.info(f"Returning cached results for query: {sanitized_query}")
        return cache[cache_key]

    api_key = os.environ.get("GOOGLE_API_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        logger.error("GOOGLE_API_KEY and GOOGLE_CSE_ID must be set in the environment.")
        return None

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": sanitized_query,
        "num": 5
    }
    if days:
        params["dateRestrict"] = f"d[{days}]"

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        results = response.json().get("items", [])
        cache[cache_key] = results
        return results
    except requests.exceptions.RequestException as e:
        logger.error(f"Error performing Google search: {e}")
        return None

"""
General web search tool for news, etc.
"""
from services.google_search import google_search

def query(q: str, site_filters: list = None) -> list:
    """
    Performs a web search, optionally restricted to a list of sites.
    """
    if site_filters:
        q = f"{q} " + " OR ".join([f"site:{site}" for site in site_filters])

    return google_search(q)

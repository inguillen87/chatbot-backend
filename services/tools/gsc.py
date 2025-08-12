"""
Google Custom Search tool.
"""
from services.google_search import google_search as search_google

def search(site: str, query: str) -> list:
    """
    Performs a site-restricted Google search.
    """
    full_query = f"site:{site} {query}"
    # The existing google_search function can be used here.
    return search_google(full_query)

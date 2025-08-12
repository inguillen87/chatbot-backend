"""
Scraper tool to fetch information from official websites.
"""
import requests
from bs4 import BeautifulSoup
import re
import logging

logger = logging.getLogger(__name__)

# Regex patterns for finding contact info
# This is a simple regex and might need improvement.
PHONE_REGEX = re.compile(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}')
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

def fetch(url: str) -> dict:
    """
    Scrapes a URL and returns a dictionary of information like
    phone numbers, emails, and links.
    """
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Scraper could not fetch URL {url}: {e}")
        return {}

    soup = BeautifulSoup(response.text, 'html.parser')
    text = soup.get_text()

    # Using set to avoid duplicates
    phones = set(match.group(0) for match in PHONE_REGEX.finditer(text))
    emails = set(EMAIL_REGEX.findall(text))

    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        # A simple way to get absolute URLs
        if href.startswith('http'):
            links.append({
                "label": a.get_text(strip=True),
                "url": href
            })
        elif href.startswith('/'):
            from urllib.parse import urljoin
            links.append({
                "label": a.get_text(strip=True),
                "url": urljoin(url, href)
            })


    # The other fields (hours, address, gmap) are harder to get with simple regex
    # and will require more advanced parsing, possibly specific to each site.
    # For now, this is a good starting point.

    return {
        "phones": list(phones),
        "emails": list(emails),
        "links": links,
        "hours": [],
        "address": None,
        "gmap": None
    }

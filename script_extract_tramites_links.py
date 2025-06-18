import json
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.juninmendoza.gov.ar/tramites/"


def fetch_links():
    """Fetch municipal tramites links from the website and save as JSON."""
    resp = requests.get(BASE_URL, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    enlaces = {}
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        texto = tag.get_text(strip=True)
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.juninmendoza.gov.ar" + href
        if "juninmendoza" in href:
            enlaces[texto] = href
    with open("data/municipios/default/tramites_links.json", "w", encoding="utf-8") as f:
        json.dump(enlaces, f, indent=2, ensure_ascii=False)
    print("Enlaces guardados en data/municipios/default/tramites_links.json")


if __name__ == "__main__":
    fetch_links()

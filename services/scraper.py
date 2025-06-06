# services/scraper.py

import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def scrapear_info_entidad(link_web):
    """
    Extrae información útil de cualquier web pública:
    - Emails, teléfonos, direcciones, horarios, secciones, noticias.
    Devuelve un dict listo para guardar como JSON o en la base.
    """
    resultado = {
        "web_oficial": link_web,
        "emails": [],
        "telefonos": [],
        "direcciones": [],
        "horarios": [],
        "otras_secciones": {},
        "noticias": [],
        "scrap_fecha": datetime.utcnow().isoformat()
    }
    try:
        res = requests.get(link_web, timeout=12)
        res.raise_for_status()
    except Exception as e:
        resultado["error"] = f"No se pudo acceder a la web: {str(e)}"
        return resultado

    soup = BeautifulSoup(res.text, "html.parser")
    text_content = res.text

    resultado["emails"] = list(set(re.findall(
        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text_content)))
    resultado["telefonos"] = list(set(re.findall(
        r"\+?\d{2,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}", text_content)))
    resultado["direcciones"] = [tag.get_text(strip=True) for tag in soup.find_all(
        string=re.compile(r"\b(calle|avenida|av\.|ruta|barrio|esquina|n°|numero|oficina|localidad|ciudad|provincia)\b", re.I))]
    resultado["horarios"] = [tag.get_text(strip=True) for tag in soup.find_all(
        string=re.compile(r"\d{1,2}(:|\.)\d{2}.*(hs|h|am|pm)", re.I))]
    for link in soup.find_all("a"):
        texto = link.get_text(strip=True)
        href = link.get("href", "")
        if texto and href and len(texto) < 40:
            resultado["otras_secciones"][texto] = href
    noticias = []
    for tag in soup.find_all(["li", "a", "span", "p"]):
        txt = tag.get_text(strip=True)
        if re.search(r"(notici|novedad|evento|comunicado|anuncio|prensa|importante)", txt, re.I):
            noticias.append(txt)
    resultado["noticias"] = list(set(noticias))

    return resultado

# src/services/scraper_avanzado.py
import requests
from bs4 import BeautifulSoup
import re
import logging
from urllib.parse import urljoin
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def descubrir_links_relevantes(base_url: str) -> set:
    logger.info(f"[CRAWLER] Descubriendo links relevantes desde: {base_url}")
    try:
        res = requests.get(base_url, timeout=15, headers=HEADERS)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        links_encontrados = set()
        KEYWORDS_RELEVANTES = ['noticia', 'blog', 'producto', 'tienda', 'catalogo', 'tramite', 'servicio']
        EXT_IGNORAR = ['.pdf', '.jpg', '.png', '.zip', '.mp4', 'login', 'cart', 'checkout']

        for link in soup.find_all("a", href=True):
            href = link.get("href")
            if not href or href.startswith('#'): continue
            
            link_absoluto = urljoin(base_url, href)
            
            if not link_absoluto.startswith(base_url.split('/')[0] + '//' + base_url.split('/')[2]): continue
            if any(ext in link_absoluto.lower() for ext in EXT_IGNORAR): continue
            
            links_encontrados.add(link_absoluto)

        links_encontrados.add(base_url)
        logger.info(f"[CRAWLER] Se encontraron {len(links_encontrados)} links relevantes.")
        return links_encontrados
    except Exception as e:
        logger.error(f"[CRAWLER] Error al descubrir links en {base_url}: {e}")
        return {base_url}

def extraer_contenido_general(url: str) -> dict:
    logger.info(f"[SCRAPER_CONTENIDO] Extrayendo texto de: {url}")
    try:
        res = requests.get(url, timeout=20, headers=HEADERS)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        main_content = soup.find("main") or soup.find("article") or soup.body
        texto_plano = main_content.get_text(separator=' ', strip=True)
        texto_limpio = re.sub(r'\s+', ' ', texto_plano).strip()
        return {"tipo": "contenido_general", "contenido": texto_limpio}
    except Exception as e:
        logger.error(f"[SCRAPER_CONTENIDO] Error en {url}: {e}")
        return {"error": str(e)}

def extraer_productos_de_url(url: str) -> dict:
    logger.info(f"[SCRAPER_PRODUCTOS] Extrayendo productos de: {url}")
    productos = []
    try:
        respuesta = requests.get(url, headers=HEADERS, timeout=20)
        respuesta.raise_for_status()
        soup = BeautifulSoup(respuesta.content, 'html.parser')
        
        contenedores = soup.select('div.product-item, div.product, li.product')
        for item in contenedores:
            nombre = item.select_one('.product-title, .product-name, .woocommerce-loop-product__title')
            precio = item.select_one('.price, .product-price, .woocommerce-Price-amount')
            link = item.select_one('a.product-link, a.woocommerce-LoopProduct-link')
            if nombre:
                productos.append({
                    "nombre": nombre.text.strip(),
                    "precio_str": precio.text.strip() if precio else "Consultar",
                    "link_producto": urljoin(url, link['href']) if link else None
                })
        return {"tipo": "productos", "productos": productos}
    except Exception as e:
        logger.error(f"[SCRAPER_PRODUCTOS] Error en {url}: {e}")
        return {"error": str(e)}


def extraer_info_contacto_web(base_url: str) -> dict:
    """Extrae teléfonos, correos y direcciones de una página web."""
    logger.info(f"[SCRAPER_CONTACTO] Extrayendo información de contacto de: {base_url}")
    resultados = {
        "tipo": "contacto",
        "telefonos": [],
        "emails": [],
        "direcciones": [],
    }

    try:
        urls = {base_url}
        # Buscamos links que parezcan ser de contacto para obtener más datos
        for link in descubrir_links_relevantes(base_url):
            if any(pal in link.lower() for pal in ["contact", "contacto", "ubicacion", "ubicación", "about", "quienes"]):
                urls.add(link)

        for url in list(urls)[:5]:  # Limitar a unas pocas páginas
            res = requests.get(url, timeout=15, headers=HEADERS)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            texto = soup.get_text(separator=" ", strip=True)

            # Emails
            emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", texto)
            for email in emails:
                if email not in resultados["emails"]:
                    resultados["emails"].append(email)

            # Teléfonos
            telefonos_raw = re.findall(r"(?:\+?\d{1,4}[\s-]*)?(?:\(?\d{2,4}\)?[\s-]*)?\d{3,4}[\s-]*\d{3,4}", texto)
            for tel in telefonos_raw:
                limpio = re.sub(r"\D", "", tel)
                if len(limpio) >= 7 and limpio not in resultados["telefonos"]:
                    resultados["telefonos"].append(limpio)

            # Direcciones a partir de etiquetas <address>
            for addr in soup.find_all("address"):
                texto_addr = addr.get_text(separator=" ", strip=True)
                if texto_addr and texto_addr not in resultados["direcciones"]:
                    resultados["direcciones"].append(texto_addr)

            # También buscamos palabras clave comunes
            for kw in ["dirección", "direccion", "ubicación", "ubicacion"]:
                for tag in soup.find_all(string=re.compile(kw, re.IGNORECASE)):
                    fragmento = tag.parent.get_text(separator=" ", strip=True)
                    if fragmento and fragmento not in resultados["direcciones"] and len(fragmento) <= 120:
                        resultados["direcciones"].append(fragmento)

        return resultados
    except Exception as e:
        logger.error(f"[SCRAPER_CONTACTO] Error en {base_url}: {e}")
        return {"error": str(e)}

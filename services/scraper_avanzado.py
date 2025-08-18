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


def extraer_noticias(url: str, limit: int = 5) -> dict:
    """
    Extrae las últimas noticias de la página de un municipio.
    """
    logger.info(f"[SCRAPER_NOTICIAS] Extrayendo noticias de: {url}")
    noticias = []
    try:
        respuesta = requests.get(url, headers=HEADERS, timeout=20)
        # It's common for sites to fail with 4xx, but we might still get content
        if not respuesta.ok:
            logger.warning(f"[SCRAPER_NOTICIAS] URL {url} devolvió status {respuesta.status_code}. Se intentará parsear de todas formas.")

        soup = BeautifulSoup(respuesta.content, 'html.parser')

        # This selector is a guess. Common patterns for news articles.
        # We look for article tags, or divs with classes like 'news-item', 'post', 'card'
        contenedores = soup.select('article, div.news-item, div.post, div.card, .entry-content')

        if not contenedores:
            logger.warning(f"No se encontraron contenedores de noticias con los selectores comunes en {url}")
            # Fallback to a more generic search if specific containers fail
            contenedores = soup.find_all('div')

        for item in contenedores:
            if len(noticias) >= limit:
                break

            # Find title
            titulo_tag = item.select_one('h2, h3, .entry-title, .post-title')
            titulo = titulo_tag.text.strip() if titulo_tag else None

            # Find link
            link_tag = item.find('a', href=True)
            link = urljoin(url, link_tag['href']) if link_tag else None

            # Find summary
            resumen_tag = item.select_one('p, .entry-summary, .post-excerpt')
            resumen = resumen_tag.text.strip() if resumen_tag else None

            # Basic validation: we need at least a title and a link
            if titulo and link:
                # Avoid adding the same news multiple times if selectors overlap
                if not any(n['link'] == link for n in noticias):
                    noticias.append({
                        "titulo": titulo,
                        "resumen": resumen or "No hay resumen disponible.",
                        "link": link
                    })

        if not noticias:
             return {"error": "No se pudieron encontrar noticias en la página. Puede que el formato haya cambiado."}

        return {"tipo": "noticias", "noticias": noticias[:limit]}

    except requests.exceptions.RequestException as e:
        logger.error(f"[SCRAPER_NOTICIAS] Error de conexión en {url}: {e}")
        return {"error": f"No se pudo conectar con el sitio de noticias: {e}"}
    except Exception as e:
        logger.error(f"[SCRAPER_NOTICIAS] Error general en {url}: {e}", exc_info=True)
        return {"error": f"Ocurrió un error inesperado al procesar la página de noticias: {e}"}

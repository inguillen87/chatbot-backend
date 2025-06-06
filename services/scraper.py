# services/scraper.py

import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import logging
from urllib.parse import urljoin # Para construir URLs absolutas

# --- MEJORADO: Usamos logging para un mejor diagnóstico ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def extraer_info_contacto_web(link_web: str) -> dict:
    """
    --- TU FUNCIÓN, MEJORADA Y MÁS ROBUSTA ---
    Extrae información de contacto y general de una página web.

    Args:
        link_web (str): La URL de la página a analizar.

    Returns:
        dict: Un diccionario con toda la información encontrada.
    """
    logger.info(f"[SCRAPER_CONTACTO] Iniciando scrape de contacto para: {link_web}")
    resultado = {
        "web_oficial": link_web,
        "emails": [],
        "telefonos": [],
        "direcciones": [],
        "horarios": [],
        "links_redes": {}, # MEJORADO: Sección específica para redes sociales
        "otras_secciones": {},
        "scrap_fecha": datetime.utcnow().isoformat()
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        res = requests.get(link_web, timeout=15, headers=headers)
        res.raise_for_status()
    except requests.exceptions.RequestException as e:
        resultado["error"] = f"No se pudo acceder a la web: {e}"
        logger.error(f"[SCRAPER_CONTACTO] Error al acceder a {link_web}: {e}")
        return resultado

    soup = BeautifulSoup(res.text, "html.parser")
    # --- MEJORADO: Buscamos en todo el body para más robustez ---
    body_text = soup.body.get_text(' ', strip=True) if soup.body else ""

    # --- MEJORADO: Regex más precisas ---
    # Email: estándar y robusto.
    resultado["emails"] = list(set(re.findall(r'[\w\.\-]+@[\w\d\.\-]+\.\w+', res.text)))

    # Teléfono: busca formatos comunes en Argentina (con o sin +54, con o sin 11/15, etc.)
    resultado["telefonos"] = list(set(re.findall(r'(?:\+?54[\s\-]?)?(?:9?11|15)[\s\-]?\d{4}[\s\-]?\d{4}|\b\d{4}[\s\-]\d{4}\b', body_text)))

    # Direcciones y Horarios: Búsqueda más inteligente
    resultado["direcciones"] = [tag.get_text(strip=True) for tag in soup.find_all(string=re.compile(r"\b(calle|avenida|av\.|ruta|barrio|localidad|ciudad|provincia|cp)\b", re.I))]
    resultado["horarios"] = [tag.get_text(strip=True) for tag in soup.find_all(string=re.compile(r"\b(Lunes a Viernes|lunes a viernes|horario|atención|hs|h|am|pm)\b", re.I))]

    # --- MEJORADO: Procesamiento de links ---
    for link in soup.find_all("a", href=True):
        texto = link.get_text(strip=True)
        href_raw = link.get("href")
        # --- MEJORADO: Convertir links relativos a absolutos ---
        href_abs = urljoin(link_web, href_raw)

        # Clasificar links
        if re.search(r'facebook\.com|fb\.me', href_abs):
            resultado["links_redes"]["facebook"] = href_abs
        elif re.search(r'instagram\.com', href_abs):
            resultado["links_redes"]["instagram"] = href_abs
        elif re.search(r'twitter\.com|x\.com', href_abs):
            resultado["links_redes"]["twitter"] = href_abs
        elif re.search(r'linkedin\.com', href_abs):
            resultado["links_redes"]["linkedin"] = href_abs
        elif re.search(r'wa\.me|api\.whatsapp\.com', href_abs):
            resultado["links_redes"]["whatsapp"] = href_abs
        elif texto and len(texto) < 40: # Links genéricos a otras secciones
            resultado["otras_secciones"][texto] = href_abs
    
    # Limpiar duplicados y valores vacíos
    resultado["direcciones"] = list(set(d for d in resultado["direcciones"] if len(d) > 10))
    resultado["horarios"] = list(set(h for h in resultado["horarios"] if len(h) > 8))

    logger.info(f"[SCRAPER_CONTACTO] Scrape de contacto para {link_web} finalizado.")
    return resultado


def extraer_productos_de_url(url: str) -> list:
    """
    --- NUEVO: EL SCRAPER DE PRODUCTOS QUE TE PROPUSE ---
    Visita una URL de e-commerce y extrae productos (nombre, precio, etc.).

    Args:
        url (str): La dirección web de la tienda o catálogo de productos.

    Returns:
        list: Una lista de diccionarios, donde cada diccionario es un producto.
    """
    logger.info(f"[SCRAPER_PRODUCTOS] Iniciando scrape de productos para: {url}")
    productos = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        respuesta = requests.get(url, headers=headers, timeout=15)
        respuesta.raise_for_status()
        soup = BeautifulSoup(respuesta.content, 'html.parser')

        # --- ¡IMPORTANTE! PERSONALIZA ESTOS SELECTORES PARA LA TIENDA DE TU CLIENTE ---
        # Haz clic derecho en la web del producto -> Inspeccionar para encontrar las clases correctas.
        # EJEMPLO PARA UNA TIENDA TIPO "TIENDANUBE" O SIMILAR:
        contenedores_productos = soup.select('.js-item-product') # El div que contiene toda la info de un producto

        for item in contenedores_productos:
            nombre_elem = item.select_one('.js-item-name')
            precio_elem = item.select_one('.js-item-price')
            link_elem = item.select_one('.js-item-link')

            nombre = nombre_elem.text.strip() if nombre_elem else None
            precio = precio_elem.text.strip() if precio_elem else "Consultar"
            
            # El link al producto es muy valioso
            link_producto = urljoin(url, link_elem['href']) if link_elem and link_elem.has_attr('href') else None

            if nombre:
                productos.append({
                    "nombre": nombre,
                    "precio_str": precio,
                    "link": link_producto
                    # Podrías añadir más campos como "descripcion", "sku", etc. si están disponibles.
                })

        logger.info(f"[SCRAPER_PRODUCTOS] Se extrajeron {len(productos)} productos de {url}")
        return productos

    except requests.exceptions.RequestException as e:
        logger.error(f"[SCRAPER_PRODUCTOS] Error al intentar acceder a {url}: {e}")
        return []
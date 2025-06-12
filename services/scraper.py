import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import logging
from urllib.parse import urljoin
import json # Usaremos json para imprimir de forma ordenada

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CABECERAS PARA SIMULAR UN NAVEGADOR ---
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def extraer_contenido_general(url: str) -> dict:
    """
    NUEVA FUNCIÓN PARA MUNICIPIOS Y GOBIERNOS.
    Extrae todo el texto legible de una URL para crear una base de conocimiento.
    """
    logger.info(f"[SCRAPER_CONTENIDO] Iniciando extracción de texto general de: {url}")
    try:
        res = requests.get(url, timeout=20, headers=HEADERS)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        # Intentamos buscar la etiqueta <main> o <article> para contenido principal
        main_content = soup.find("main") or soup.find("article") or soup.body
        
        # Extraemos todo el texto, usando un espacio como separador para que las palabras no se peguen
        texto_plano = main_content.get_text(separator=' ', strip=True)

        # Limpieza básica de texto (opcional, se puede mejorar)
        texto_limpio = re.sub(r'\s+', ' ', texto_plano).strip()

        logger.info(f"[SCRAPER_CONTENIDO] Se extrajeron {len(texto_limpio)} caracteres de {url}")
        
        return {
            "url_origen": url,
            "contenido_texto": texto_limpio,
            "scrap_fecha": datetime.utcnow().isoformat()
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"[SCRAPER_CONTENIDO] Error al acceder a {url}: {e}")
        return {"error": str(e)}

def extraer_productos_de_url(url: str) -> list:
    """
    FUNCIÓN MEJORADA PARA PYMES (E-COMMERCE).
    Extrae productos incluyendo nombre, precio, link y descripción.
    """
    logger.info(f"[SCRAPER_PRODUCTOS] Iniciando scrape de productos para: {url}")
    productos = []
    try:
        respuesta = requests.get(url, headers=HEADERS, timeout=20)
        respuesta.raise_for_status()
        soup = BeautifulSoup(respuesta.content, 'html.parser')

        # --- ¡ATENCIÓN! ESTOS SELECTORES SON EJEMPLOS. DEBES PERSONALIZARLOS PARA CADA TIENDA. ---
        # Haz clic derecho -> "Inspeccionar" en la web del cliente para encontrar las clases CSS correctas.
        contenedores_productos = soup.select('div.product-item, div.product, li.product') # El div/li que contiene un producto

        for item in contenedores_productos:
            nombre_elem = item.select_one('.product-title, .product-name, .woocommerce-loop-product__title')
            precio_elem = item.select_one('.price, .product-price, .woocommerce-Price-amount')
            link_elem = item.select_one('a.product-link, a.woocommerce-LoopProduct-link')
            desc_elem = item.select_one('.product-description, .description') # Intenta buscar descripción

            nombre = nombre_elem.text.strip() if nombre_elem else None
            precio_str = precio_elem.text.strip() if precio_elem else "Consultar"
            link = urljoin(url, link_elem['href']) if link_elem and link_elem.has_attr('href') else None
            descripcion = desc_elem.text.strip() if desc_elem else "No disponible"
            
            if nombre:
                productos.append({
                    "nombre": nombre,
                    "precio_str": precio_str,
                    "descripcion": descripcion,
                    "link_producto": link,
                    "origen_datos": "web_scraping"
                })
        
        logger.info(f"[SCRAPER_PRODUCTOS] Se extrajeron {len(productos)} productos de {url}")
        return productos

    except requests.exceptions.RequestException as e:
        logger.error(f"[SCRAPER_PRODUCTOS] Error al intentar acceder a {url}: {e}")
        return []

# --- Bloque principal para ejecutar desde la terminal (como en el Cron Job) ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scraper web avanzado para Chatbots.")
    parser.add_argument(
        '--task', type=str, required=True, 
        choices=['productos', 'contenido_general'],
        help="La tarea a realizar: 'productos' para e-commerce o 'contenido_general' para texto plano."
    )
    parser.add_argument('--url', type=str, required=True, help="La URL completa del sitio a scrapear.")

    args = parser.parse_args()
    logger.info(f"Tarea de Cron Job iniciada: {args.task} para la URL: {args.url}")

    resultado = {}
    if args.task == 'contenido_general':
        resultado = extraer_contenido_general(args.url)
    elif args.task == 'productos':
        resultado = extraer_productos_de_url(args.url)
    
    # Imprimimos el resultado en formato JSON para que sea fácil de leer y procesar
    print(json.dumps(resultado, indent=4, ensure_ascii=False))
    
    logger.info(f"Tarea de Cron Job finalizada.")
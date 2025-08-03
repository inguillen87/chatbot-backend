import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re

BASE_URL = "https://www.juninmendoza.gov.ar/tramites/"

def get_text_from_url(url):
    """Fetches and extracts clean text from a URL."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        if 'html' not in response.headers.get('Content-Type', ''):
            return ""
        soup = BeautifulSoup(response.text, 'html.parser')
        content_area = soup.find('div', class_='entry-content') or soup.find('main') or soup.body
        text_parts = [p.get_text(strip=True) for p in content_area.find_all(['p', 'h1', 'h2', 'h3', 'li'])]
        return ' '.join(text_parts)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching {url}: {e}")
        return ""

def fetch_and_structure_tramites():
    """
    Fetches and structures the municipal procedures from the website using the correct HTML structure.
    """
    try:
        main_response = requests.get(BASE_URL, timeout=10)
        main_response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Could not fetch the main tramites page: {e}")
        return {}

    main_soup = BeautifulSoup(main_response.text, 'html.parser')
    tramites_data = {}

    # Find all accordion items, which represent the sections
    accordion_items = main_soup.find_all('div', class_='et_pb_accordion_item')

    for item in accordion_items:
        title_tag = item.find('h5', class_='et_pb_toggle_title')
        if not title_tag:
            continue

        section_name = title_tag.get_text(strip=True)
        if not section_name or len(section_name) < 3:
            continue

        print(f"Processing section: '{section_name}'")
        tramites_data[section_name] = []

        content_div = item.find('div', class_='et_pb_toggle_content')
        if not content_div:
            continue

        links = content_div.find_all('a', href=True)
        for link in links:
            href = link['href'].strip()
            text = link.get_text(strip=True)

            if not href or not text or text.isspace():
                continue

            full_url = urljoin(BASE_URL, href)

            description = ""
            is_page = href.endswith(('.html', '.php', '/')) or "?page_id=" in href or ".gov.ar" in href
            is_document = any(href.endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.zip'])

            if is_page and not is_document:
                print(f"  -> Scraping page: {full_url}")
                description = get_text_from_url(full_url)
                if not description:
                    description = f"Más información sobre '{text}' disponible en el enlace."
            else:
                description = f"Documento '{text}' disponible para descargar."

            tramite_obj = {
                "nombre": text,
                "url": full_url,
                "descripcion": description[:600]
            }
            if tramite_obj not in tramites_data[section_name]:
                tramites_data[section_name].append(tramite_obj)

    return tramites_data

def format_for_final_json(structured_data):
    """
    Formats the structured data into the final flat JSON structure for the bot.
    """
    final_json = {}
    if not structured_data:
        return final_json

    for section_name, tramites_list in structured_data.items():
        if not tramites_list:
            continue

        key_name = re.sub(r'[^a-z0-9_]', '', section_name.replace(' ', '_').lower())

        tramite_names = [t['nombre'] for t in tramites_list]
        section_description = f"La sección de '{section_name}' incluye los siguientes trámites: {', '.join(tramite_names)}. Puedes pedirme información sobre cualquiera de ellos."

        section_buttons = [{"texto": t['nombre'], "url": t['url']} for t in tramites_list]

        final_json[key_name] = {
            "descripcion": section_description,
            "botones": section_buttons
        }

        for tramite in tramites_list:
            individual_key = re.sub(r'[^a-z0-9_]', '', tramite['nombre'].replace(' ', '_').lower())
            if individual_key and individual_key != key_name:
                final_json[individual_key] = {
                    "descripcion": tramite['descripcion'],
                    "botones": [{"texto": f"Ir a {tramite['nombre']}", "url": tramite['url']}]
                }

    return final_json

if __name__ == "__main__":
    print("Iniciando scraper de trámites (v5 - Corrected Logic)...")
    structured_data = fetch_and_structure_tramites()

    if structured_data:
        print(f"\nSe encontraron {len(structured_data)} secciones con trámites válidos.")
        final_data = format_for_final_json(structured_data)

        output_path = "data/municipios/default/tramites.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f, indent=4, ensure_ascii=False)

        print(f"\nScraper finalizado. Datos guardados en {output_path}")
    else:
        print("\nNo se pudo extraer datos de trámites estructurados.")

import os
import json
import logging
import shutil
from pathlib import Path
from flask import current_app

logger = logging.getLogger(__name__)

# Default templates content
DEFAULT_CONFIG_TEMPLATE = {
    "nombre": "{nombre_tenant}",
    "descripcion": "Descripción por defecto para {nombre_tenant}. Edite este archivo para personalizar.",
    "welcome_message": "Bienvenido a {nombre_tenant}. ¿En qué podemos ayudarte?",
    "prompt_context": "Eres un asistente virtual de {nombre_tenant}. Tu objetivo es ayudar a los clientes con información sobre productos y servicios.",
    "resources": [],
    "theme": {
        "primaryColor": "#007bff",
        "secondaryColor": "#6c757d",
        "logo_url": "{logo_url}"
    }
}

DEFAULT_FAQ_TEMPLATE = [
    {
        "pregunta": "¿Cuál es el horario de atención?",
        "respuesta": "Nuestro horario de atención es de Lunes a Viernes de 9:00 a 18:00hs."
    },
    {
        "pregunta": "¿Dónde están ubicados?",
        "respuesta": "Estamos ubicados en [DIRECCIÓN_DEL_TENANT]."
    }
]

def ensure_tenant_folder_structure(slug: str, nombre: str, tipo: str) -> str:
    """
    Ensures that the directory structure for a new tenant exists.
    It creates the folder `data/pyme/rubros/<slug>` and populates it with default files if missing.

    Args:
        slug: The unique identifier for the tenant.
        nombre: The display name of the tenant.
        tipo: 'pyme' or 'municipio'.

    Returns:
        The absolute path to the tenant's directory.
    """
    try:
        # Determine base path. We stick to data/pyme/rubros/ for now as per legacy structure,
        # but conceptually this should be data/tenants/.
        # However, to avoid breaking existing logic that scans data/pyme/rubros, we use that.

        base_dir = current_app.config.get('DATA_DIR', '/data')
        # Fallback to local repo data if DATA_DIR is absolute root like /data
        if base_dir == '/data' and not os.path.exists('/data'):
             # Use relative data/ folder in app root
             base_dir = os.path.join(current_app.root_path, 'data')

        target_dir = os.path.join(base_dir, 'pyme', 'rubros', slug)

        # Create directory
        os.makedirs(target_dir, exist_ok=True)
        logger.info(f"Verified/Created tenant directory: {target_dir}")

        # 1. config.json
        config_path = os.path.join(target_dir, 'config.json')
        if not os.path.exists(config_path):
            config_content = DEFAULT_CONFIG_TEMPLATE.copy()
            config_content['nombre'] = config_content['nombre'].format(nombre_tenant=nombre)
            config_content['descripcion'] = config_content['descripcion'].format(nombre_tenant=nombre)
            config_content['welcome_message'] = config_content['welcome_message'].format(nombre_tenant=nombre)
            config_content['prompt_context'] = config_content['prompt_context'].format(nombre_tenant=nombre)
            # Default logo placeholder or use passed one if we had it
            config_content['theme']['logo_url'] = ""

            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config_content, f, indent=2, ensure_ascii=False)
            logger.info(f"Created default config.json for {slug}")

        # 2. faq.json
        faq_path = os.path.join(target_dir, 'faq.json')
        if not os.path.exists(faq_path):
            with open(faq_path, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_FAQ_TEMPLATE, f, indent=2, ensure_ascii=False)
            logger.info(f"Created default faq.json for {slug}")

        # 3. Create subfolders for assets
        os.makedirs(os.path.join(target_dir, 'imagenes'), exist_ok=True)
        os.makedirs(os.path.join(target_dir, 'documentos'), exist_ok=True)

        return target_dir

    except Exception as e:
        logger.error(f"Error ensuring tenant folder structure for {slug}: {e}")
        raise e

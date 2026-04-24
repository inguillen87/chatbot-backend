import yaml
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

PROFILES_DIR = "data/catalog_profiles"

def load_profile(rubro_slug: str) -> Dict[str, Any]:
    """
    Loads the YAML configuration for a specific industry (rubro).
    Falls back to 'generic.yml' if the specific one doesn't exist.
    """
    filename = f"{rubro_slug}.yml"
    path = os.path.join(PROFILES_DIR, filename)

    if not os.path.exists(path):
        logger.info(f"Profile {filename} not found, falling back to generic.yml")
        path = os.path.join(PROFILES_DIR, "generic.yml")

    if not os.path.exists(path):
        logger.warning("generic.yml not found, returning empty config")
        return {}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Error loading profile {path}: {e}")
        return {}

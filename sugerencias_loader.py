import json
import logging

logger = logging.getLogger(__name__)

def load_sugerencias(filepath="data/sugerencias.json"):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Sugerencias file not found: {filepath}")
        return []
    except json.JSONDecodeError:
        logger.error(f"Error decoding sugerencias from {filepath}")
        return []

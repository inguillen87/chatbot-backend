import json
import logging
from typing import Any, Dict, List, Optional
from config.feature_flags import FEATURE_ENCUESTAS
from services.common_utils import (
    crear_mapa_de_columnas_inteligente,
    parse_precio_flexible,
    limpiar_texto_base,
    is_number,
    get_logger,
)

# Placeholder: In a real implementation, this would likely use a library or a separate service
# to classify intents based on text. For now, it might be a simple keyword matcher or
# a mock function.

logger = get_logger()

def classify_intent(text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Classifies the intent of the given text.

    Args:
        text: The user's input text.
        context: Optional conversation context.

    Returns:
        A dictionary containing the detected intent and confidence.
        Example: {"intent": "ver_catalogo", "confidence": 0.9}
    """
    if not text:
        return {"intent": None, "confidence": 0.0}

    text_lower = text.lower().strip()

    # --- Simple Keyword Matching (Placeholder Logic) ---

    # Catalog Intents
    if any(keyword in text_lower for keyword in ["catalogo", "catálogo", "productos", "comprar", "ver productos"]):
         return {"intent": "ver_catalogo", "confidence": 0.95}

    # Claim Intents
    if any(keyword in text_lower for keyword in ["reclamo", "queja", "reportar", "problema"]):
        return {"intent": "iniciar_reclamo", "confidence": 0.9}

    # Greeting Intents
    if text_lower in ["hola", "buen dia", "buenas", "que tal", "hello", "hi"]:
        return {"intent": "saludo", "confidence": 0.9}

    # Menu Intents
    if text_lower in ["menu", "menú", "opciones", "inicio", "volver"]:
        return {"intent": "menu_principal", "confidence": 0.95}

    # Default / Unknown
    return {"intent": None, "confidence": 0.0}

def load_intents_from_json(filepath: str) -> List[Dict[str, Any]]:
    """Loads intent definitions from a JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get("intents", [])
    except FileNotFoundError:
        logger.error(f"Intent definition file not found: {filepath}")
        return []
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON from: {filepath}")
        return []

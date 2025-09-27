import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

def get_or_create_pyme_user(phone_number: str, profile_name: str, pyme_id: int) -> Dict[str, Any]:
    """
    Retrieves or creates a user associated with a PYME.
    This is a placeholder implementation.
    """
    logger.info(f"Attempting to get or create user for phone: {phone_number}")
    # In a real implementation, this would interact with the database.
    return {"id": 1, "name": profile_name, "phone": phone_number, "pyme_id": pyme_id}

def log_pyme_interaction(pyme_id: int, session_id: str, message: str, response: str, source: str):
    """Logs an interaction for a PYME."""
    logger.info(
        f"PYME_INTERACTION: pyme_id={pyme_id}, session_id={session_id}, "
        f"message='{message[:100]}...', response='{response[:100]}...', source='{source}'"
    )

def add_preference(key: str, value: Any, context: Dict[str, Any]) -> Dict[str, Any]:
    """Adds a preference to the user's context."""
    if not isinstance(context, dict):
        context = {}
    if "preferences" not in context:
        context["preferences"] = {}
    if key not in context["preferences"]:
        context["preferences"][key] = []
    context["preferences"][key].append(value)
    return context

def get_static_pyme_data(pyme_id: int) -> Dict[str, Any]:
    """
    Loads static data for a PYME from JSON files.
    This is a simplified version.
    """
    return {
        "nombre_pyme_cache": f"PYME {pyme_id}",
        "rubro_slug": "generico",
    }
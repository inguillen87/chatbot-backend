from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class BaseCatalogProcessor(ABC):
    """
    Abstract base class for industry-specific catalog processors.
    """

    def __init__(self, rubro_nombre: str = "generico"):
        self.rubro_nombre = rubro_nombre

    @abstractmethod
    def get_extraction_prompt(self, filename: str) -> tuple[str, str]:
        """
        Returns the (system_prompt, user_prompt) tuple for LLM-based extraction.
        """
        pass

    @abstractmethod
    def normalizar_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalizes the raw JSON items returned by the LLM or other parsers
        into a standard format for the database.
        """
        pass

    def infer_unidad_por_caja(self, descripcion: str) -> Optional[int]:
        """
        Helper to infer units per case from description (common in many industries).
        """
        import re
        if not descripcion:
            return None

        # Case: "6 x 750ml" -> 6
        match = re.search(r"(\d{1,3})\s*[xX]\s*\d", descripcion)
        if match:
            return int(match.group(1))

        # Case: "Caja x 6" or "x 12 units" -> 6 or 12
        match = re.search(r"[xX]\s*(\d{1,3})\b", descripcion)
        if match:
            return int(match.group(1))

        # Case: Starts with number "6 botellas" -> 6
        match = re.search(r"^\s*(\d{1,3})\b", descripcion)
        if match:
            return int(match.group(1))

        return None

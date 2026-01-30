from .bodega import BodegaCatalogProcessor
from .generic import GenericCatalogProcessor
from .base import BaseCatalogProcessor

def get_processor_for_rubro(rubro_nombre: str) -> BaseCatalogProcessor:
    """
    Factory function to retrieve the appropriate catalog processor based on the industry (rubro).

    Args:
        rubro_nombre (str): The name of the industry/category (e.g., 'bodega', 'ferreteria').

    Returns:
        BaseCatalogProcessor: An instance of the specific processor or the generic one.
    """
    rubro_norm = rubro_nombre.lower().strip()

    # Industry-specific mappings
    if rubro_norm in ["bodega", "vinoteca", "vinos"]:
        return BodegaCatalogProcessor(rubro_nombre)

    # Future: Add more rubros here (e.g., "corralon" -> CorralonCatalogProcessor)
    # if rubro_norm in ["corralon", "materiales", "construccion"]:
    #     return CorralonCatalogProcessor(rubro_nombre)

    # Default fallback
    return GenericCatalogProcessor(rubro_nombre)

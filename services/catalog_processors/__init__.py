from .base import BaseCatalogProcessor
from .bodega import BodegaCatalogProcessor
from .generic import GenericCatalogProcessor
from .factory import get_processor_for_rubro

__all__ = [
    "BaseCatalogProcessor",
    "BodegaCatalogProcessor",
    "GenericCatalogProcessor",
    "get_processor_for_rubro",
]

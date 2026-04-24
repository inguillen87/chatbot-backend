from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

@dataclass
class CatalogItemData:
    """
    Canonical model for a catalog item, used during processing.
    """
    nombre: str
    precio: Optional[float] = None
    moneda: str = "ARS"
    sku: Optional[str] = None
    stock: Optional[float] = None
    unidad_base: Optional[str] = None # e.g. "botella", "bolsa"
    contenido_paquete: Optional[str] = None # e.g. "caja x6"
    categoria: Optional[str] = None
    atributos: Dict[str, Any] = field(default_factory=dict)

    # Metadata for persistence
    original_text: Optional[str] = None

@dataclass
class ProcessResult:
    items: List[CatalogItemData]
    confidence: float # 0.0 to 1.0
    warnings: List[str]
    raw_text: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

class CatalogProcessor(ABC):
    """
    Abstract base class for industry-specific catalog processors.
    """

    def __init__(self, profile_config: Dict[str, Any] = None):
        self.profile = profile_config or {}

    @property
    @abstractmethod
    def slug(self) -> str:
        pass

    @abstractmethod
    def process(self, file_path: str, mime_type: str, **kwargs) -> ProcessResult:
        """
        Process a file and return extracted items.
        """
        pass

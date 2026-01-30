from typing import Dict, Optional, Type
from .base import CatalogProcessor
from .processors.generic import GenericProcessor
from .processors.bodega import BodegaProcessor
from .processors.corralon import CorralonProcessor
from .processors.indumentaria import IndumentariaProcessor
from .processors.ortopedia import OrtopediaProcessor
from .config import load_profile

class CatalogProcessorRegistry:
    def __init__(self):
        self._processors: Dict[str, Type[CatalogProcessor]] = {}
        self._instances: Dict[str, CatalogProcessor] = {}

        # Register defaults
        self.register(GenericProcessor)
        self.register(BodegaProcessor)
        self.register(CorralonProcessor)
        self.register(IndumentariaProcessor)
        self.register(OrtopediaProcessor)

    def register(self, processor_cls: Type[CatalogProcessor]):
        # Instantiate a temporary object to get the slug if it's a property,
        # or better, expect the class to have a SLUG constant.
        # Let's assume the instance provides the slug or pass it.
        # Ideally, we instantiate on demand with config.
        # For simplicity, we assume we map by a known key or the class has a SLUG attribute.
        # Let's check our base class. It has an abstract property `slug`.
        # We can instantiate it with empty config to check slug, or make SLUG a class attr.
        # Let's verify standard:
        try:
            # Hacky but works if __init__ doesn't do heavy lifting
            temp = processor_cls(profile_config={})
            self._processors[temp.slug] = processor_cls
        except Exception as e:
            print(f"Error registering processor {processor_cls}: {e}")

    def get_processor(self, rubro_slug: str) -> CatalogProcessor:
        """
        Returns a configured instance of the appropriate processor.
        """
        # Normalize slug
        target_slug = rubro_slug.lower().strip()

        # Determine which processor class to use
        # If specific processor exists for this rubro, use it.
        # Otherwise, check if we have a generic mapped one?
        # For now: strict match or generic.

        # Map common aliases if needed, or rely on client sending correct slug
        processor_cls = self._processors.get(target_slug, self._processors.get("generic"))

        if not processor_cls:
            # Fallback if even generic is missing (should not happen)
            from .processors.generic import GenericProcessor
            processor_cls = GenericProcessor

        # Load configuration profile
        profile = load_profile(target_slug)

        # Return new instance with fresh config
        return processor_cls(profile_config=profile)

# Singleton instance
registry = CatalogProcessorRegistry()

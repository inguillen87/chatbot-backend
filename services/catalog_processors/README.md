# Catalog Processors
This package contains the logic for parsing and normalizing catalog data for different industries (rubros).

## Architecture
The system uses a Strategy pattern where `BaseCatalogProcessor` defines the interface, and specific classes implement the logic for different business types.

### Adding a New Processor
To add support for a new industry (e.g., "Ferretería" or "Ortopedia"):

1. **Create a new file** (e.g., `services/catalog_processors/ortopedia.py`).
2. **Inherit from `BaseCatalogProcessor`**.
3. **Implement `get_extraction_prompt`**: Define the specific system and user prompts for the LLM. This allows you to teach the AI about specific columns (e.g., "Talle", "Material", "Peso").
4. **Implement `normalizar_items`**: Map the extracted JSON fields to the standard database schema. You can add industry-specific normalization logic here.
5. **Update the Factory**: Add your new processor to the mapping in `services/catalog_processors/factory.py`.

### Example
```python
# services/catalog_processors/ortopedia.py
from .base import BaseCatalogProcessor

class OrtopediaCatalogProcessor(BaseCatalogProcessor):
    def get_extraction_prompt(self, filename: str):
        return "System Prompt...", "User Prompt looking for specific orthopedics fields..."

    def normalizar_items(self, items):
        # ... logic to normalize ...
        return normalized_items
```

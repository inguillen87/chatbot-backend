import os
from typing import Dict, Any
from .orchestrator import CatalogOrchestrator

class CatalogPipeline:
    def __init__(self):
        self.orchestrator = CatalogOrchestrator()

    def process_upload_preview(self, upload_id: int, file_path: str, mime_type: str, rubro_slug: str) -> Dict[str, Any]:
        """
        Reads file from disk and processes it using the orchestrator.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            content = f.read()

        # Determine mode based on file type or other logic?
        # For now let orchestrator decide (auto)
        result = self.orchestrator.process(content, filename, mode="auto")

        # Transform result to match what route expects ('items' list vs 'columns'/'rows')
        # The route expects 'items' for preview_data.
        # But wait, the user wants 'columns' and 'rows' in the new contract.
        # The existing route code does:
        # upload.preview_data = extraction_result.get('items', [])

        # If I change the contract, I must update the route too.
        # The Orchestrator returns {columns, rows, ...}.
        # I should adapt it here or update the route.
        # Updating the route is better to support the new UI features (column mapping).

        # However, to be safe and compatible with the CURRENT route code shown above:
        # The route commits by iterating 'items'.
        # 'items' seems to be a list of dicts: [{sku, title, price...}]

        # My extractors return 'rows' as list of dicts (normalized).
        # I can just map 'rows' to 'items' in the return here.

        return {
            "items": result.get("rows", []),
            "columns": result.get("columns", []),
            "warnings": result.get("warnings", []),
            "confidence": result.get("confidence", 0.0),
            "metadata": result.get("metadata", {})
        }

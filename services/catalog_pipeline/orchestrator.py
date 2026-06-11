from typing import Dict, Any
from .extractors.xlsx_extractor import XLSXExtractor
from .extractors.docling_extractor import DoclingExtractor
from .extractors.pdf_text_extractor import PDFTextExtractor
from .extractors.llm_vision_extractor import LLMVisionExtractor

class CatalogOrchestrator:
    def process(self, file_content: bytes, filename: str, mode: str = "auto") -> Dict[str, Any]:
        """
        Orchestrates the extraction process.
        Returns dict with keys: columns, rows, confidence, warnings, metadata, engine
        """
        filename_lower = filename.lower()
        result = {}
        engine = "unknown"

        # 1. Deterministic (Excel/CSV)
        if filename_lower.endswith('.xlsx') or filename_lower.endswith('.xls') or filename_lower.endswith('.csv'):
            engine = "xlsx_deterministic"
            result = XLSXExtractor().extract(file_content, filename)

        # 2. PDF Handling
        elif filename_lower.endswith('.pdf'):
            if mode == "vision":
                 engine = "llm_vision"
                 result = LLMVisionExtractor().extract(file_content, filename)
            else:
                engine = "docling"
                result = DoclingExtractor().extract(file_content, filename)

                if len(result.get("rows", [])) > 0:
                    result["warnings"] = result.get("warnings", []) + ["Docling extraction enabled"]
                else:
                    docling_warnings = result.get("warnings", [])
                    result = {}

                # Auto or deterministic
                if not result:
                    engine = "pdf_text"
                    result = PDFTextExtractor().extract(file_content, filename)
                    result["warnings"] = docling_warnings + result.get("warnings", [])

                # If Auto and failed (empty or few rows), fallback to Vision
                if mode == "auto":
                    # Heuristic: if 0 rows detected.
                    if len(result.get('rows', [])) == 0:
                        engine = "llm_vision_fallback"
                        result = LLMVisionExtractor().extract(file_content, filename)
                        result['warnings'] = result.get('warnings', []) + ["Fallback to AI Vision triggered"]

        # 3. Images
        elif filename_lower.endswith(('.png', '.jpg', '.jpeg')):
            engine = "llm_vision"
            result = LLMVisionExtractor().extract(file_content, filename)
        elif filename_lower.endswith(('.docx', '.pptx', '.html', '.htm', '.md', '.txt')):
            engine = "docling"
            result = DoclingExtractor().extract(file_content, filename)
        else:
             result = {
                 "columns": [],
                 "rows": [],
                 "confidence": 0.0,
                 "warnings": ["Formato no soportado"],
                 "metadata": {}
             }

        result['engine'] = engine
        return result

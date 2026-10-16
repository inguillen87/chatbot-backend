import fitz  # PyMuPDF
import io
import pandas as pd
from .base import BaseExtractor
from typing import Dict, Any, List
from services.vision_fallback_service import analyze_image_structured

class LLMVisionExtractor(BaseExtractor):
    def extract(self, file_content: bytes, filename: str, **kwargs) -> Dict[str, Any]:
        """
        Extracts data using LLM Vision.
        If PDF, converts first few pages to images.
        If Image, uses directly.
        """
        images_bytes = []

        try:
            if filename.lower().endswith('.pdf'):
                doc = fitz.open(stream=file_content, filetype="pdf")
                # Limit to first few pages for preview to save tokens/time
                max_pages = kwargs.get('max_pages', 2)
                for i in range(min(len(doc), max_pages)):
                    page = doc.load_page(i)
                    pix = page.get_pixmap()
                    img_bytes = pix.tobytes("jpeg")
                    images_bytes.append(img_bytes)
            else:
                # Assume image
                images_bytes.append(file_content)

            all_rows = []
            columns = []

            for img_data in images_bytes:
                # Call the Vision Service (gpt-4o)
                # Prompt can be customized
                prompt = (
                    "Extrae la tabla de este catálogo. "
                    "Devuelve JSON con 'columns' (lista de strings) y 'rows' (lista de listas). "
                    "Si hay precios, manten el formato numérico o string limpio."
                )

                result = analyze_image_structured(img_data, prompt)

                if result:
                    if not columns and result.get('columns'):
                        columns = result['columns']

                    if result.get('rows'):
                        # Align rows to columns if needed?
                        # For now assume LLM does a decent job or we just append
                        all_rows.extend(result['rows'])

            if not all_rows:
                 return {
                    "columns": [],
                    "rows": [],
                    "confidence": 0.0,
                    "warnings": ["IA Vision no detectó tablas"],
                    "metadata": {}
                }

            # Construct DataFrame
            # Ensure rows have same length as columns
            if columns:
                normalized_rows = []
                for r in all_rows:
                    if len(r) == len(columns):
                        normalized_rows.append(dict(zip(columns, r)))
                    else:
                        # Fallback: create dict with numbered keys or try best fit
                        # Ideally LLM output is consistent.
                        # If row is list, zip it.
                        normalized_rows.append(dict(zip(columns, r))) # Truncate or pad?

                # Create result
                cols_meta = [{"key": c, "label": c, "type": "string"} for c in columns]

                return {
                    "columns": cols_meta,
                    "rows": normalized_rows,
                    # The provider schema does not return calibrated confidence.
                    "confidence": 0.0,
                    "warnings": ["Confianza no informada por el proveedor; requiere revision humana."],
                    "metadata": {
                        "total_rows": len(normalized_rows),
                        "confidence_source": "not_provided",
                    }
                }

            return {
                "columns": [],
                "rows": [],
                "confidence": 0.0,
                "warnings": ["Datos incompletos de IA"],
                "metadata": {}
            }

        except Exception:
            return {
                "columns": [],
                "rows": [],
                "confidence": 0.0,
                "warnings": ["El proveedor de vision no pudo confirmar la extraccion."],
                "metadata": {},
                "error_code": "vision_extraction_failed",
            }

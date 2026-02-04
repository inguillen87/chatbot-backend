import pdfplumber
import pandas as pd
import io
from .base import BaseExtractor
from typing import Dict, Any, List

class PDFTextExtractor(BaseExtractor):
    def extract(self, file_content: bytes, filename: str, **kwargs) -> Dict[str, Any]:
        tables = []
        warnings = []

        try:
            with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                for i, page in enumerate(pdf.pages):
                    extracted = page.extract_table()
                    if extracted:
                        # First row is header
                        df = pd.DataFrame(extracted[1:], columns=extracted[0])
                        tables.append(df)
                    else:
                        warnings.append(f"No table found on page {i+1}")

            if not tables:
                return {
                    "columns": [],
                    "rows": [],
                    "confidence": 0.0,
                    "warnings": ["No tables detected in PDF (text-mode)"],
                    "metadata": {}
                }

            # Merge tables (assuming similar structure for now, or just take first large one)
            # Simple strategy: concat all
            full_df = pd.concat(tables, ignore_index=True)
            return self.normalize_dataframe(full_df)

        except Exception as e:
            return {
                "columns": [],
                "rows": [],
                "confidence": 0.0,
                "warnings": [f"Error parsing PDF: {str(e)}"],
                "metadata": {},
                "error": str(e)
            }

import pandas as pd
import io
from .base import BaseExtractor
from typing import Dict, Any

class XLSXExtractor(BaseExtractor):
    def extract(self, file_content: bytes, filename: str, **kwargs) -> Dict[str, Any]:
        try:
            # Determine engine/reader based on extension
            if filename.lower().endswith('.csv'):
                df = pd.read_csv(io.BytesIO(file_content))
            else:
                df = pd.read_excel(io.BytesIO(file_content))

            return self.normalize_dataframe(df)
        except Exception as e:
            return {
                "columns": [],
                "rows": [],
                "confidence": 0.0,
                "warnings": [f"Error parsing file: {str(e)}"],
                "metadata": {},
                "error": str(e)
            }

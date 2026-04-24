from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd

class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, file_content: bytes, filename: str, **kwargs) -> Dict[str, Any]:
        """
        Returns a dict conforming to the Preview Contract:
        {
            "columns": [{"key": "...", "label": "..."}],
            "rows": [{...}],
            "confidence": 0.0-1.0,
            "warnings": [],
            "metadata": {}
        }
        """
        pass

    def normalize_dataframe(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Helper to convert DF to preview format"""
        df = df.dropna(how="all").fillna("")
        # Convert columns to string
        df.columns = df.columns.map(str)

        columns = [{"key": col, "label": col, "type": "string"} for col in df.columns]
        rows = df.to_dict(orient="records")

        return {
            "columns": columns,
            "rows": rows,
            "confidence": 1.0, # Deterministic usually means high confidence if parsed
            "warnings": [],
            "metadata": {"total_rows": len(rows)}
        }

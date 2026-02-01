import logging
import pandas as pd
from typing import Dict, Any

logger = logging.getLogger(__name__)

class CatalogPipeline:
    def process_upload_preview(self, upload_id: int, file_path: str, mime_type: str, rubro_slug: str = "generic") -> Dict[str, Any]:
        """
        Processes a file (XLSX, CSV) and returns a list of detected items and warnings.
        For PDF, we might use a mock or heuristic parser for P0.
        """
        items = []
        warnings = []

        try:
            if mime_type in ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel", "text/csv"]:
                df = None
                if mime_type == "text/csv":
                    df = pd.read_csv(file_path)
                else:
                    df = pd.read_excel(file_path)

                # Basic normalization of column names
                df.columns = [c.lower().strip() for c in df.columns]

                for index, row in df.iterrows():
                    # Heuristics for columns
                    title = row.get("nombre") or row.get("titulo") or row.get("title") or row.get("producto") or f"Item {index+1}"
                    price = row.get("precio") or row.get("price") or row.get("valor") or 0
                    sku = row.get("sku") or row.get("codigo") or row.get("id") or f"AUTO-{index}"
                    category = row.get("categoria") or row.get("rubro") or "General"

                    try:
                        price = float(str(price).replace("$", "").replace(",", ""))
                    except:
                        warnings.append(f"Row {index+1}: Invalid price for '{title}'")
                        price = 0.0

                    items.append({
                        "sku": str(sku),
                        "title": str(title),
                        "price": price,
                        "category": str(category),
                        "stock": int(row.get("stock") or 0)
                    })

            elif "pdf" in mime_type:
                # Attempt to use legacy extraction if available, otherwise fallback
                try:
                    # Try to import legacy extractor (if exists)
                    from services.pdf_extractor import extract_table_from_file
                    items = extract_table_from_file(file_path)
                except ImportError:
                    # Fallback if no legacy extractor found (P0 Mock)
                    logger.warning(f"Legacy PDF extractor not found. Using mock for {file_path}")
                    items = [
                        {"sku": "PDF-001", "title": "Producto PDF Detectado 1", "price": 1500.0, "category": "General"},
                        {"sku": "PDF-002", "title": "Producto PDF Detectado 2", "price": 2500.0, "category": "General"}
                    ]
                    warnings.append("PDF extraction is in beta mode (legacy extractor unavailable).")
                except Exception as e:
                    logger.error(f"Legacy PDF extraction failed: {e}")
                    warnings.append("PDF extraction failed.")

            else:
                warnings.append(f"Unsupported file type: {mime_type}")

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            warnings.append(f"Error processing file: {str(e)}")

        return {"items": items, "warnings": warnings}

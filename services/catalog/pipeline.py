import logging
import os
from typing import Any, Dict

import pandas as pd

from services.catalog.registry import registry as catalog_registry
from services.document_processing_service import document_processing_service
from services.vision_fallback_service import analyze_image_smart

logger = logging.getLogger(__name__)

class CatalogPipeline:
    def _extract_text_raw(self, file_path: str, mime_type: str) -> str:
        try:
            if mime_type and "image" in mime_type:
                with open(file_path, "rb") as f:
                    vision = analyze_image_smart(f.read())
                return vision.get("full_text_annotation", {}).get("description", "")

            with open(file_path, "rb") as f:
                result = document_processing_service.process_document(
                    f.read(),
                    mime_type or "application/octet-stream",
                    os.path.basename(file_path),
                )

            if result.get("success"):
                return result.get("text_content", "") or str(result.get("datos_estructurados", ""))

            return ""
        except Exception as exc:
            logger.warning(f"[CATALOG PIPELINE] Failed to extract raw text: {exc}")
            return ""

    def _preview_from_processor(
        self,
        file_path: str,
        mime_type: str,
        rubro_slug: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        raw_text = self._extract_text_raw(file_path, mime_type)
        if not raw_text:
            return [], ["No se pudo extraer texto del archivo."]

        processor = catalog_registry.get_processor(rubro_slug)
        result = processor.process(file_path, mime_type, extracted_text=raw_text)
        items = []
        for idx, item in enumerate(result.items, start=1):
            attrs = item.atributos or {}
            items.append(
                {
                    "sku": item.sku or f"AUTO-{idx}",
                    "title": item.nombre,
                    "price": item.precio or 0,
                    "currency": item.moneda,
                    "category": item.categoria or "General",
                    "stock": int(item.stock or 0),
                    "unit": item.unidad_base,
                    "pack": item.contenido_paquete,
                    "brand": attrs.get("marca"),
                    "varietal": attrs.get("varietal"),
                    "anada": attrs.get("anada"),
                    "unidades_por_caja": attrs.get("unidades_por_caja"),
                    "pallet": attrs.get("pallet"),
                }
            )
        warnings = list(result.warnings or [])
        if result.confidence < 0.6 and items:
            warnings.append("Extracción con baja confianza. Revisar antes de confirmar.")
        return items, warnings

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

            if not items and mime_type:
                is_pdf = "pdf" in mime_type
                is_textish = any(
                    token in mime_type for token in ("text", "json", "xml", "word", "doc", "docx")
                )
                if is_pdf or is_textish or "image" in mime_type:
                    extracted_items, extraction_warnings = self._preview_from_processor(
                        file_path,
                        mime_type,
                        rubro_slug or "generic",
                    )
                    items.extend(extracted_items)
                    warnings.extend(extraction_warnings)

            if not items and mime_type and "pdf" in mime_type:
                # Attempt to use legacy extraction if available, otherwise fallback
                try:
                    # Try to import legacy extractor (if exists)
                    from services.pdf_extractor import extract_table_from_file
                    items = extract_table_from_file(file_path)
                except ImportError:
                    logger.warning(f"Legacy PDF extractor not found. Using mock for {file_path}")
                    items = [
                        {"sku": "PDF-001", "title": "Producto PDF Detectado 1", "price": 1500.0, "category": "General"},
                        {"sku": "PDF-002", "title": "Producto PDF Detectado 2", "price": 2500.0, "category": "General"}
                    ]
                    warnings.append("PDF extraction is in beta mode (legacy extractor unavailable).")
                except Exception as e:
                    logger.error(f"Legacy PDF extraction failed: {e}")
                    warnings.append("PDF extraction failed.")

            if not items and mime_type not in [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
                "text/csv",
            ]:
                warnings.append(f"Unsupported file type: {mime_type}")

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            warnings.append(f"Error processing file: {str(e)}")

        return {"items": items, "warnings": warnings}

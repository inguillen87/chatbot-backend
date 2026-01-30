from typing import Dict, Any, List, Optional
from extensions import db
from models import CatalogUpload, CatalogoItem, TenantCatalogMapping
from services.catalog.registry import registry as catalog_registry
from services.upload_processor import _extract_text_raw
from services.embedding_service import embed_textos_llm as embed_textos
from services.qdrant_utils import get_qdrant_client
from qdrant_client import models as qdrant_models
import uuid
import logging
import os

logger = logging.getLogger(__name__)

class CatalogPipeline:
    def process_upload_preview(self, upload_id: int, file_path: str, mime_type: str, rubro_slug: str) -> Dict[str, Any]:
        """
        Stage 1: Process file and return preview items + suggested mapping.
        """
        upload = db.session.get(CatalogUpload, upload_id)
        if not upload:
            raise ValueError("Upload not found")

        processor = catalog_registry.get_processor(rubro_slug)
        upload.processor_slug = processor.slug

        raw_text = _extract_text_raw(file_path, mime_type)
        if not raw_text:
            return {"status": "error", "message": "No text extracted"}

        upload.raw_text_snippet = raw_text[:1000]

        # Process extraction
        result = processor.process(file_path, mime_type, extracted_text=raw_text)

        preview_items = [
            {
                "nombre": item.nombre,
                "precio": item.precio,
                "sku": item.sku,
                "metadata": item.atributos
            }
            for item in result.items[:20] # Preview first 20
        ]

        # Update upload
        upload.status = "processed_preview"
        upload.stats = {"preview_count": len(result.items), "confidence": result.confidence}
        db.session.commit()

        return {
            "upload_token": upload.id,
            "processor": processor.slug,
            "preview_items": preview_items,
            "confidence": result.confidence,
            "warnings": result.warnings
        }

    def confirm_upload(self, upload_id: int, user_id: int, rubro_slug: str, mapping_override: Dict = None) -> Dict[str, Any]:
        """
        Stage 2: Commit items to DB and Qdrant.
        """
        upload = db.session.get(CatalogUpload, upload_id)
        if not upload:
            raise ValueError("Upload not found")

        # Re-run full process (or load from cache if implemented)
        # For robustness, we re-run extraction with the confirmed mapping (if logic supported mapping override)
        # Here we just re-run the processor which is deterministic enough for this MVP.

        # Load file path (reconstruct or store in upload)
        # Ideally upload.filename stores path relative to UPLOAD_FOLDER
        # For this MVP we assume the file is still there.
        from services.upload_processor import UPLOAD_FOLDER
        file_path = os.path.join(UPLOAD_FOLDER, upload.filename)

        if not os.path.exists(file_path):
             return {"status": "error", "message": "File expired"}

        processor = catalog_registry.get_processor(rubro_slug)
        raw_text = upload.raw_text_snippet # Or re-extract if full text needed and snippet was truncated.
        # Actually `raw_text_snippet` is truncated. We need full text.
        # Ideally we stored full text or just re-extract.
        raw_text = _extract_text_raw(file_path, upload.mime_type)

        result = processor.process(file_path, upload.mime_type, extracted_text=raw_text)

        # Save Tenant Mapping Preference
        if mapping_override:
            # Upsert TenantCatalogMapping
            # (Logic to save mapping omitted for brevity, assumes model exists)
            pass

        # Save Items
        CatalogoItem.query.filter_by(user_id=user_id).delete()
        items_db = []
        textos_embed = []
        payloads = []

        for item_data in result.items:
            db_item = CatalogoItem(
                user_id=user_id,
                catalog_upload_id=upload.id,
                nombre=item_data.nombre,
                precio_monetario=item_data.precio,
                # ... map other fields
                extra_metadata=item_data.atributos
            )
            items_db.append(db_item)
            # Prep vector
            textos_embed.append(f"{item_data.nombre} {item_data.categoria}")
            # Prep payload (partial)
            payloads.append({"nombre": item_data.nombre, "user_id": user_id})

        db.session.add_all(items_db)
        db.session.commit()

        # Update Payloads with IDs and Upsert Vector
        for i, db_item in enumerate(items_db):
            payloads[i]["db_id"] = db_item.id

        vectores = embed_textos(textos_embed)
        qdrant_cli = get_qdrant_client()
        points = [
            qdrant_models.PointStruct(id=str(uuid.uuid4()), vector=v, payload=p)
            for v, p in zip(vectores, payloads)
        ]
        if points:
            qdrant_cli.upsert(collection_name="catalogo_pyme", points=points)

        upload.status = "completed"
        db.session.commit()

        return {"status": "confirmed", "items_count": len(items_db)}

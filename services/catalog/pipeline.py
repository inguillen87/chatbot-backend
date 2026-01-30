from typing import Dict, Any, List, Optional
from extensions import db
from models import CatalogUpload, CatalogoItem, TenantCatalogMapping, ArchivoAdjunto, User
from services.catalog.registry import registry as catalog_registry
from services.upload_processor import _extract_text_raw
from services.embedding_service import embed_textos_llm as embed_textos
from services.qdrant_utils import get_qdrant_client
from qdrant_client import models as qdrant_models
from services.gcs_service import guardar_adjunto_y_thumbnail
from werkzeug.datastructures import FileStorage
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

        # Load file path
        from services.upload_processor import UPLOAD_FOLDER
        file_path = os.path.join(UPLOAD_FOLDER, upload.filename)

        if not os.path.exists(file_path):
             return {"status": "error", "message": "File expired"}

        processor = catalog_registry.get_processor(rubro_slug)
        raw_text = _extract_text_raw(file_path, upload.mime_type)

        result = processor.process(file_path, upload.mime_type, extracted_text=raw_text)

        # 1. Permanently Save File (Cloudinary/GCS/Local)
        try:
            with open(file_path, "rb") as f:
                storage_obj = FileStorage(f, filename=upload.filename, content_type=upload.mime_type)
                storage_result = guardar_adjunto_y_thumbnail(storage_obj)

            if storage_result:
                # Create/Update ArchivoAdjunto
                public_url = storage_result.get("public_url") or storage_result.get("original_url")

                # Check for existing catalog attachment to replace/archive?
                # For now, just add new one. The logic in `routes/catalogo.py` takes the latest.
                adjunto = ArchivoAdjunto(
                    user_id=user_id,
                    filename=storage_result.get("unique_name"),
                    nombre_original=upload.filename,
                    mime=upload.mime_type,
                    tamano=storage_result.get("size"),
                    tipo="catalogo",
                    url=public_url
                )
                db.session.add(adjunto)
                logger.info(f"Catalog file stored permanently: {public_url}")
        except Exception as e:
            logger.error(f"Error storing catalog file permanently: {e}")
            # Non-blocking? Maybe blocking is safer.
            pass

        # Save Tenant Mapping Preference
        if mapping_override:
            # Upsert TenantCatalogMapping
            existing_mapping = TenantCatalogMapping.query.filter_by(tenant_id=upload.tenant_id, rubro_slug=rubro_slug).first()
            if existing_mapping:
                existing_mapping.mapping_json = mapping_override
            else:
                new_mapping = TenantCatalogMapping(
                    tenant_id=upload.tenant_id,
                    rubro_slug=rubro_slug,
                    mapping_json=mapping_override
                )
                db.session.add(new_mapping)

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
                sku=item_data.sku,
                cantidad=str(item_data.stock) if item_data.stock else None,
                unidad=item_data.unidad_base,
                categoria=item_data.categoria,
                extra_metadata=item_data.atributos,
                texto=item_data.original_text
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
            # Determine collection based on rubro/tenant type?
            # For now default to CATALOGO_PYME as passed in previous flow or generic.
            # Ideally we pass collection name too.
            qdrant_cli.upsert(collection_name="catalogo_pyme", points=points)

        upload.status = "completed"
        db.session.commit()

        # Cleanup temp file
        try:
            os.remove(file_path)
        except:
            pass

        return {"status": "confirmed", "items_count": len(items_db)}

import os
import uuid
import logging
import traceback
import shutil
import mimetypes
from flask import Blueprint, request, jsonify, g
from werkzeug.utils import secure_filename
from extensions import db
from models import CatalogoItem, User, Rubro, ArchivoAdjunto, CatalogUpload
from services.embedding_service import embed_textos_llm as embed_textos

from services.vision_fallback_service import analyze_image_smart
from services.document_processing_service import document_processing_service
from .common_utils import limpiar_texto_base, parse_cantidad_flexible, parse_precio_flexible

# Import Registry
from services.catalog.registry import registry as catalog_registry

from services.qdrant_utils import (
    get_qdrant_client,
    verificar_y_crear_coleccion_qdrant,
)
from services.qdrant_search import CATALOGO_PYME, CATALOGO_MUNICIPIO, coleccion_catalogo_para_rubro
from services.logic import es_rubro_publico
from services.gcs_service import upload_to_gcs
from qdrant_client import models as qdrant_models
from typing import List, Dict, Any, Optional

upload_bp = Blueprint("upload_bp", __name__)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".txt"}
UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")
CATALOGO_FOLDER = os.path.join("data", "catalogos")

def extension_valida(nombre_archivo: str) -> bool:
    return os.path.splitext(nombre_archivo)[1].lower() in ALLOWED_EXTENSIONS


def _store_catalog_attachment_record(file_storage, user: User) -> Optional[ArchivoAdjunto]:
    """Upload catalog file to configured storage (R2-first) and persist ArchivoAdjunto."""
    if not file_storage or not user:
        return None

    try:
        upload_meta = upload_to_gcs(file_storage, kind="attachments")
    except Exception:
        logger.exception("Catalog upload to storage failed.")
        return None

    if not upload_meta:
        logger.warning("Catalog upload metadata missing; skipping ArchivoAdjunto creation.")
        return None

    public_url = (
        upload_meta.get("public_url")
        or upload_meta.get("url")
        or upload_meta.get("secure_url")
    )
    unique_name = upload_meta.get("unique_name") or secure_filename(file_storage.filename or "")
    original_name = upload_meta.get("original_name") or file_storage.filename
    mimetype = upload_meta.get("mimetype") or file_storage.mimetype
    size = upload_meta.get("size")

    if not public_url:
        return None

    adjunto = ArchivoAdjunto(
        user_id=user.id,
        filename=unique_name or secure_filename(file_storage.filename or f"catalogo_{uuid.uuid4().hex}.pdf"),
        nombre_original=original_name,
        mime=mimetype,
        tamano=size if isinstance(size, int) else None,
        tipo="catalogo",
        url=public_url,
    )
    db.session.add(adjunto)
    db.session.flush()
    return adjunto


def _extract_text_raw(path_archivo: str, mime_type: str) -> str:
    """Helper to extract raw text for the processor."""
    try:
        if "image" in mime_type:
            with open(path_archivo, "rb") as f:
                vision = analyze_image_smart(f.read())
            return vision.get("full_text_annotation", {}).get("description", "")

        with open(path_archivo, "rb") as f:
            res = document_processing_service.process_document(f.read(), mime_type, os.path.basename(path_archivo))

        # If structured data found, reconstruct text or use resume
        if res.get("success"):
            return res.get("text_content", "") or str(res.get("datos_estructurados", ""))

        return ""
    except Exception as e:
        logger.error(f"Error extracting raw text: {e}")
        return ""

def procesar_y_embedear_catalogo(
    path_archivo: str,
    user_id: int,
    pyme_rubro_nombre: str = "generico",
    coleccion: str = CATALOGO_PYME,
    mime_type_override: Optional[str] = None,
    catalog_upload_id: int = None,
    tenant_id: Optional[int] = None,
) -> int:
    logger.info(f"[UPLOAD_PROC] Iniciando v2 para user_id={user_id}, rubro='{pyme_rubro_nombre}'")

    # 1. Select Processor
    processor = catalog_registry.get_processor(pyme_rubro_nombre)
    logger.info(f"[UPLOAD_PROC] Processor seleccionado: {processor.slug}")

    # 2. Extract Text (Common Step)
    raw_text = _extract_text_raw(path_archivo, mime_type_override)
    if not raw_text:
        logger.warning("[UPLOAD_PROC] No se pudo extraer texto del archivo.")
        return 0

    # 3. Process
    result = processor.process(path_archivo, mime_type_override, extracted_text=raw_text)

    # 4. Update Upload Record
    if catalog_upload_id:
        upload_rec = db.session.get(CatalogUpload, catalog_upload_id)
        if upload_rec:
            upload_rec.status = "completed" if result.items else "processed_no_items"
            upload_rec.stats = {
                "items_count": len(result.items),
                "confidence": result.confidence,
                "warnings": result.warnings
            }
            upload_rec.raw_text_snippet = raw_text[:500]
            db.session.commit()

    # 5. Decision: Structured vs Unstructured (RAG only)
    CONFIDENCE_THRESHOLD = 0.6
    if result.confidence < CONFIDENCE_THRESHOLD:
        logger.warning(f"[UPLOAD_PROC] Confianza baja ({result.confidence}). Guardando solo para búsqueda vectorial (RAG), sin items estructurados.")
        # Logic to index raw text chunks for RAG would go here.
        # For now, we abort structured save.
        return 0

    # 6. Save Structured Items (DB + Vector)
    items_para_db = []

    # Clear old items for this user? Or maybe version them.
    # For now, keep "replace all" logic for simplicity unless user wants history.
    items_query = CatalogoItem.query.filter_by(user_id=user_id)
    if tenant_id:
        items_query = items_query.filter_by(tenant_id=tenant_id)
    items_query.delete()

    for item_data in result.items:
        # Create DB Model
        extra_metadata = item_data.atributos or {}
        precio_por_caja = extra_metadata.get("precio_caja") or extra_metadata.get("precio_por_caja")
        unidad_por_caja = extra_metadata.get("unidades_por_caja") or extra_metadata.get("unidad_por_caja")
        db_item = CatalogoItem(
            user_id=user_id,
            tenant_id=tenant_id,
            # catalog_upload_id removed as it is not in the model
            nombre=item_data.nombre,
            descripcion=str(item_data.atributos) if item_data.atributos else "",
            precio=str(item_data.precio) if item_data.precio else None,
            precio_monetario=item_data.precio,
            moneda=item_data.moneda,
            sku=item_data.sku,
            cantidad=str(item_data.stock) if item_data.stock else None,
            unidad=item_data.unidad_base,
            categoria=item_data.categoria or pyme_rubro_nombre,
            extra_metadata=item_data.atributos,
            marca=extra_metadata.get("marca"),
            precio_por_caja=precio_por_caja,
            unidad_por_caja=unidad_por_caja,
            descripcion_corta=item_data.contenido_paquete,
            texto=item_data.original_text
        )
        items_para_db.append(db_item)

    if items_para_db:
        db.session.add_all(items_para_db)
        db.session.commit() # Commit to get IDs

        # 7. Index in Qdrant
        # Re-map DB items to vector payloads
        textos_embed = []
        payloads = []

        for db_item in items_para_db:
            extra_text = ""
            if isinstance(db_item.extra_metadata, dict):
                extra_text = " ".join(
                    str(value) for value in db_item.extra_metadata.values() if value
                )
            texto = " ".join(
                part
                for part in [
                    db_item.nombre,
                    db_item.descripcion or "",
                    db_item.sku or "",
                    extra_text,
                ]
                if part
            ).strip()
            textos_embed.append(texto)
            precio_float = None
            if db_item.precio_monetario is not None:
                precio_float = float(db_item.precio_monetario)
            else:
                _, precio_float, _ = parse_precio_flexible(db_item.precio)
            categoria_qdrant = (db_item.categoria or "").strip().lower() or None
            stock_val = parse_cantidad_flexible(db_item.cantidad)
            extra_metadata = db_item.extra_metadata or {}
            payloads.append({
                "db_id": db_item.id,
                "nombre": db_item.nombre,
                "precio": db_item.precio_monetario,
                "precio_float": precio_float,
                "precio_str": db_item.precio,
                "categoria_qdrant": categoria_qdrant,
                "descripcion": db_item.descripcion,
                "sku": db_item.sku,
                "marca": db_item.marca,
                "stock": stock_val,
                "unidad": db_item.unidad,
                "moneda": db_item.moneda,
                "precio_por_caja": db_item.precio_por_caja,
                "unidad_por_caja": db_item.unidad_por_caja,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "rubro_slug": pyme_rubro_nombre,
                "texto_original_para_embedding": texto,
                "varietal": extra_metadata.get("varietal"),
                "anada": extra_metadata.get("anada"),
                "presentacion": extra_metadata.get("presentacion_original"),
            })

        vectores = embed_textos(textos_embed)

        # Upsert logic (simplified from previous)
        qdrant_cli = get_qdrant_client()
        points = [
            qdrant_models.PointStruct(id=str(uuid.uuid4()), vector=v, payload=p)
            for v, p in zip(vectores, payloads)
        ]
        qdrant_cli.upsert(collection_name=coleccion, points=points)

    return len(items_para_db)

@upload_bp.route("/subir_catalogo", methods=["POST"])
def subir_catalogo(current_user: Optional[User] = None):
    # Authenticate (Simplified from original for brevity)
    user = current_user or g.get("current_user")
    if not user:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if token:
            user = User.query.filter_by(token=token).first()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    archivo = request.files.get("file")
    if not archivo:
        return jsonify({"error": "No file"}), 400

    # Upload to R2/CDN and persist attachment record for direct catalog access
    catalog_attachment = _store_catalog_attachment_record(archivo, user)

    # Save Temp
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = secure_filename(archivo.filename)
    path = os.path.join(UPLOAD_FOLDER, filename)
    archivo.save(path)
    mime_type = archivo.mimetype

    # Get Tenant ID
    # Assuming user.tenant_id exists or we infer it.
    # Fallback to user.id if no tenant profile (legacy).
    tenant_id = user.tenant_id
    if not tenant_id and user.pyme_id: # Try to resolve
         tenant_id = user.pyme_id # Approximate for now

    # Create CatalogUpload record
    upload_rec = CatalogUpload(
        tenant_id=tenant_id if tenant_id else 1, # Default 1 if missing for safety in demo
        filename=filename,
        mime_type=mime_type,
        processor_slug="pending",
        status="processing"
    )
    db.session.add(upload_rec)
    db.session.commit()

    try:
        rubro_nombre = "generico"
        if user.rubro:
            rubro_nombre = user.rubro.nombre

        # Determinar colección correcta
        coleccion_destino = coleccion_catalogo_para_rubro(rubro_nombre)

        count = procesar_y_embedear_catalogo(
            path,
            user.id,
            pyme_rubro_nombre=rubro_nombre,
            coleccion=coleccion_destino,
            mime_type_override=mime_type,
            catalog_upload_id=upload_rec.id,
            tenant_id=tenant_id,
        )

        return jsonify({
            "message": "Procesado correctamente",
            "items_count": count,
            "upload_id": upload_rec.id,
            "catalog_url": getattr(catalog_attachment, "url", None),
            "catalog_attachment_id": getattr(catalog_attachment, "id", None),
        })

    except Exception as e:
        logger.error(f"Error processing: {e}")
        if upload_rec:
            upload_rec.status = "failed"
            db.session.commit()
        return jsonify({"error": str(e)}), 500

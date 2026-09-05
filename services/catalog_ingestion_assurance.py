"""Truthful storage and retrieval receipts for catalog ingestion.

The import preview is useful before every optional provider is online, but a
catalog must not be described as published or retrieval-ready without explicit
provider acknowledgements.  This module keeps those concepts separate and
returns only non-sensitive operational metadata.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from models import CatalogoItem, TenantProfile, db
from services.catalog_inventory import new_catalog_version
from services.embedding_service import embed_textos_llm
from services.gcs_service import upload_to_gcs
from sqlalchemy.orm.attributes import flag_modified
from utils.upload_limits import UploadFileTooLargeError
from werkzeug.datastructures import FileStorage


CONTRACT_VERSION = "catalog.ingestion_assurance.v1"
SNAPSHOT_CONTRACT_VERSION = "catalog.snapshot_publication.v1"


class CatalogSnapshotPublicationError(RuntimeError):
    """Stable, non-sensitive failure raised before changing the active pointer."""

    def __init__(self, reason_code: str, assurance: dict[str, Any]):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.assurance = assurance


_PUBLIC_METADATA_KEYS = frozenset(
    {
        "anada",
        "confidence_score",
        "gallery_urls",
        "image_status",
        "inventory_source",
        "personalization_options",
        "presentacion_original",
        "quality_issues",
        "review_required",
        "stock_updated_at",
        "varietal",
    }
)


def _public_catalog_metadata(raw_metadata: Any) -> dict[str, Any]:
    """Keep only catalog-display metadata; never publish arbitrary tenant data."""

    if not isinstance(raw_metadata, dict):
        return {}
    public = {
        str(key): value
        for key, value in raw_metadata.items()
        if str(key) in _PUBLIC_METADATA_KEYS
    }
    try:
        # Round-trip removes ORM/custom objects and guarantees the snapshot can
        # be serialized without exposing their string representations.
        encoded = json.dumps(public, ensure_ascii=False, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _stage(
    status: str,
    *,
    provider: str | None = None,
    acknowledged: bool = False,
    reason_code: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "acknowledged": bool(acknowledged),
    }
    if provider:
        payload["provider"] = provider
    if reason_code:
        payload["reason_code"] = reason_code
    payload.update({key: value for key, value in details.items() if value is not None})
    return payload


def pending_stage(*, provider: str | None = None, reason_code: str) -> dict[str, Any]:
    return _stage(
        "pending",
        provider=provider,
        acknowledged=False,
        reason_code=reason_code,
    )


def build_ingestion_assurance(
    *,
    stored: dict[str, Any],
    extracted: dict[str, Any],
    indexed: dict[str, Any] | None = None,
    retrieval_verified: dict[str, Any] | None = None,
) -> dict[str, Any]:
    indexed = indexed or pending_stage(
        provider="qdrant",
        reason_code="catalog_index_not_started",
    )
    retrieval_verified = retrieval_verified or pending_stage(
        provider="qdrant",
        reason_code="catalog_retrieval_not_verified",
    )
    stages = {
        "stored": stored,
        "extracted": extracted,
        "indexed": indexed,
        "retrieval_verified": retrieval_verified,
    }
    stage_statuses = {str(stage.get("status") or "pending") for stage in stages.values()}
    if all(
        stage.get("status") == "verified" and stage.get("acknowledged") is True
        for stage in stages.values()
    ):
        overall_status = "ready"
    elif "failed" in stage_statuses:
        overall_status = "failed"
    elif "degraded" in stage_statuses or any(
        stage.get("status") == "pending" and stage.get("provider")
        for stage in stages.values()
    ):
        overall_status = "degraded"
    else:
        overall_status = "pending"

    return {
        "contract_version": CONTRACT_VERSION,
        "status": overall_status,
        "ready": overall_status == "ready",
        "stages": stages,
    }


def stored_pending(*, reason_code: str = "r2_ack_missing") -> dict[str, Any]:
    return pending_stage(provider="cloudflare_r2", reason_code=reason_code)


def extracted_stage(item_count: int) -> dict[str, Any]:
    count = max(0, int(item_count or 0))
    if not count:
        return _stage(
            "failed",
            acknowledged=False,
            reason_code="catalog_structured_rows_missing",
            item_count=0,
        )
    return _stage("verified", acknowledged=True, item_count=count)


def persist_catalog_source(file_storage) -> dict[str, Any]:
    """Persist one source in R2 and require an explicit upload acknowledgement."""

    original_name = str(getattr(file_storage, "filename", "") or "")
    extension = os.path.splitext(original_name)[1].lower()
    safe_extension = extension if re.fullmatch(r"\.[a-z0-9]{1,10}", extension) else ""
    provider_file = FileStorage(
        stream=file_storage.stream,
        filename=f"catalog-source{safe_extension}",
        content_type=getattr(file_storage, "mimetype", None),
    )
    try:
        result = upload_to_gcs(provider_file, kind="catalogos", require_r2=True)
    except UploadFileTooLargeError:
        return _stage(
            "failed",
            provider="cloudflare_r2",
            acknowledged=False,
            reason_code="catalog_source_too_large",
        )
    except Exception:
        return stored_pending(reason_code="r2_provider_unavailable")
    finally:
        try:
            file_storage.seek(0)
        except Exception:
            pass

    if not isinstance(result, dict):
        return stored_pending(reason_code="r2_provider_unavailable")

    unique_name = str(result.get("unique_name") or "").strip()
    provider_url = str(result.get("public_url") or "").strip()
    if not unique_name or not provider_url:
        return stored_pending(reason_code="r2_ack_missing")

    # A non-reversible reference proves which provider receipt was observed
    # without returning an object URL, original filename, tenant data or secret.
    receipt_ref = hashlib.sha256(unique_name.encode("utf-8")).hexdigest()[:24]
    try:
        size_bytes = max(0, int(result.get("size") or 0))
    except (TypeError, ValueError):
        size_bytes = 0
    return _stage(
        "verified",
        provider="cloudflare_r2",
        acknowledged=True,
        receipt_ref=f"r2:{receipt_ref}",
        size_bytes=size_bytes,
    )


def index_catalog_item_with_ack(
    tenant_id: int,
    item_data: dict[str, Any],
    embedding: list[float] | None,
) -> dict[str, Any]:
    """Index and read back one item; success requires both provider receipts."""

    if not embedding:
        return {
            "indexed": False,
            "retrieval_verified": False,
            "reason_code": "embedding_ack_missing",
        }

    try:
        from services.qdrant_service import (
            index_catalog_item,
            verify_catalog_item_index,
        )

        indexed = bool(index_catalog_item(str(tenant_id), item_data, embedding))
        if not indexed:
            return {
                "indexed": False,
                "retrieval_verified": False,
                "reason_code": "qdrant_index_ack_missing",
            }
        retrieval_verified = bool(verify_catalog_item_index(str(tenant_id), item_data))
    except Exception:
        return {
            "indexed": False,
            "retrieval_verified": False,
            "reason_code": "qdrant_provider_unavailable",
        }

    if not retrieval_verified:
        return {
            "indexed": True,
            "retrieval_verified": False,
            "reason_code": "qdrant_retrieval_ack_missing",
        }
    return {
        "indexed": True,
        "retrieval_verified": True,
        "reason_code": None,
    }


def committed_assurance(
    previous: dict[str, Any],
    *,
    expected_count: int,
    indexed_count: int,
    retrieval_count: int,
) -> dict[str, Any]:
    stages = previous.get("stages") if isinstance(previous, dict) else {}
    stored = stages.get("stored") if isinstance(stages, dict) else None
    extracted = stages.get("extracted") if isinstance(stages, dict) else None
    return build_ingestion_assurance(
        stored=stored if isinstance(stored, dict) else stored_pending(),
        extracted=extracted if isinstance(extracted, dict) else extracted_stage(0),
        indexed=_stage(
            "verified" if indexed_count == expected_count and expected_count > 0 else "degraded",
            provider="qdrant",
            acknowledged=indexed_count == expected_count and expected_count > 0,
            reason_code=None if indexed_count == expected_count and expected_count > 0 else "qdrant_index_ack_missing",
            expected_count=expected_count,
            acknowledged_count=indexed_count,
        ),
        retrieval_verified=_stage(
            "verified" if retrieval_count == expected_count and expected_count > 0 else "degraded",
            provider="qdrant",
            acknowledged=retrieval_count == expected_count and expected_count > 0,
            reason_code=None if retrieval_count == expected_count and expected_count > 0 else "qdrant_retrieval_ack_missing",
            expected_count=expected_count,
            acknowledged_count=retrieval_count,
        ),
    )


def degraded_index_assurance(
    previous: dict[str, Any],
    *,
    expected_count: int,
    indexed_count: int,
    retrieval_count: int,
    reason_code: str,
) -> dict[str, Any]:
    stages = previous.get("stages") if isinstance(previous, dict) else {}
    stored = stages.get("stored") if isinstance(stages, dict) else None
    extracted = stages.get("extracted") if isinstance(stages, dict) else None
    return build_ingestion_assurance(
        stored=stored if isinstance(stored, dict) else stored_pending(),
        extracted=extracted if isinstance(extracted, dict) else extracted_stage(0),
        indexed=_stage(
            "degraded",
            provider="qdrant",
            acknowledged=False,
            reason_code=reason_code,
            expected_count=expected_count,
            acknowledged_count=indexed_count,
        ),
        retrieval_verified=_stage(
            "degraded",
            provider="qdrant",
            acknowledged=False,
            reason_code=reason_code,
            expected_count=expected_count,
            acknowledged_count=retrieval_count,
        ),
    )


def _snapshot_item_payload(
    item: CatalogoItem,
    *,
    tenant_id: int,
    rubro_name: str,
    catalog_version: str,
) -> dict[str, Any]:
    metadata = _public_catalog_metadata(item.extra_metadata)
    return {
        "id": item.id,
        "nombre": item.nombre,
        "descripcion": item.descripcion or "",
        "descripcion_corta": item.descripcion_corta,
        "promocion_info": item.promocion_info,
        "precio": item.precio or 0,
        "stock": item.cantidad,
        "sku": item.sku,
        "marca": item.marca,
        "categoria": item.categoria,
        "moneda": item.moneda,
        "unidad": item.unidad,
        "precio_por_caja": item.precio_por_caja,
        "unidad_por_caja": item.unidad_por_caja,
        "modalidad": item.modalidad,
        "disponible": bool(item.disponible),
        "imagen_url": item.imagen_url,
        "external_url": item.external_url,
        "checkout_type": item.checkout_type,
        "extra_metadata": metadata,
        "user_id": item.user_id,
        "tenant_id": tenant_id,
        "rubro": rubro_name,
        "catalog_version": catalog_version,
    }


def _catalog_snapshot_file(
    tenant_id: int,
    catalog_version: str,
    item_payloads: list[dict[str, Any]],
) -> FileStorage:
    source_payload = {
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "catalog_version": catalog_version,
        "items": item_payloads,
    }
    encoded = json.dumps(
        source_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return FileStorage(
        stream=io.BytesIO(encoded),
        filename="catalog-snapshot.json",
        content_type="application/json",
    )


def publish_tenant_catalog_snapshot(
    tenant_id: int,
    *,
    rubro_name: str | None = None,
) -> dict[str, Any]:
    """Stage a complete catalog snapshot and atomically advance its pointer.

    The caller owns the surrounding SQL transaction.  This function flushes
    candidate SQL changes, requires durable R2 acknowledgement, indexes and
    reads back every Qdrant point under an immutable candidate version, and
    only then stages the active pointer update.  A caller rollback therefore
    preserves both the previous catalog rows and the previous active vector
    version; any provider-side candidate points remain unreachable.
    """

    try:
        scoped_tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        scoped_tenant_id = 0
    if scoped_tenant_id <= 0:
        assurance = build_ingestion_assurance(
            stored=stored_pending(reason_code="catalog_tenant_invalid"),
            extracted=extracted_stage(0),
        )
        raise CatalogSnapshotPublicationError("catalog_tenant_invalid", assurance)

    tenant = (
        TenantProfile.query.filter(
            TenantProfile.id == scoped_tenant_id,
            TenantProfile.is_active.is_(True),
        )
        .with_for_update()
        .first()
    )
    if tenant is None:
        assurance = build_ingestion_assurance(
            stored=stored_pending(reason_code="catalog_tenant_not_found"),
            extracted=extracted_stage(0),
        )
        raise CatalogSnapshotPublicationError("catalog_tenant_not_found", assurance)

    owner = tenant.municipio or tenant.pyme
    owner_id = getattr(owner, "id", None)
    if not owner_id:
        assurance = build_ingestion_assurance(
            stored=stored_pending(reason_code="catalog_owner_missing"),
            extracted=extracted_stage(0),
        )
        raise CatalogSnapshotPublicationError("catalog_owner_missing", assurance)

    db.session.flush()
    items = (
        CatalogoItem.query.filter_by(tenant_id=scoped_tenant_id)
        .order_by(CatalogoItem.id.asc())
        .all()
    )
    if not items:
        assurance = build_ingestion_assurance(
            stored=stored_pending(reason_code="catalog_snapshot_empty"),
            extracted=extracted_stage(0),
        )
        raise CatalogSnapshotPublicationError("catalog_snapshot_empty", assurance)
    if any(int(item.user_id) != int(owner_id) for item in items):
        assurance = build_ingestion_assurance(
            stored=stored_pending(reason_code="catalog_owner_scope_mismatch"),
            extracted=extracted_stage(len(items)),
        )
        raise CatalogSnapshotPublicationError("catalog_owner_scope_mismatch", assurance)

    resolved_rubro = str(rubro_name or "").strip()
    if not resolved_rubro and getattr(owner, "rubro", None):
        resolved_rubro = str(getattr(owner.rubro, "nombre", "") or "").strip()
    resolved_rubro = resolved_rubro or "general"
    candidate_version = (
        f"{new_catalog_version(scoped_tenant_id)}_{uuid.uuid4().hex[:8]}"
    )
    item_payloads = [
        _snapshot_item_payload(
            item,
            tenant_id=scoped_tenant_id,
            rubro_name=resolved_rubro,
            catalog_version=candidate_version,
        )
        for item in items
    ]

    stored = persist_catalog_source(
        _catalog_snapshot_file(scoped_tenant_id, candidate_version, item_payloads)
    )
    assurance = build_ingestion_assurance(
        stored=stored,
        extracted=extracted_stage(len(item_payloads)),
    )
    if stored.get("status") != "verified" or stored.get("acknowledged") is not True:
        reason_code = str(stored.get("reason_code") or "r2_ack_missing")
        raise CatalogSnapshotPublicationError(reason_code, assurance)

    texts = [
        " ".join(
            str(value)
            for value in (
                payload.get("nombre"),
                payload.get("categoria"),
                payload.get("descripcion"),
                payload.get("sku"),
            )
            if value
        ).strip()
        for payload in item_payloads
    ]
    try:
        embeddings = embed_textos_llm(texts)
    except Exception:
        embeddings = []

    indexed_count = 0
    retrieval_count = 0
    failure_reason = "embedding_ack_missing"
    if not isinstance(embeddings, list) or len(embeddings) != len(item_payloads):
        degraded = degraded_index_assurance(
            assurance,
            expected_count=len(item_payloads),
            indexed_count=0,
            retrieval_count=0,
            reason_code=failure_reason,
        )
        raise CatalogSnapshotPublicationError(failure_reason, degraded)

    for payload, embedding in zip(item_payloads, embeddings):
        provider_ack = index_catalog_item_with_ack(
            scoped_tenant_id,
            payload,
            embedding if isinstance(embedding, list) else None,
        )
        if provider_ack.get("indexed") is True:
            indexed_count += 1
        else:
            failure_reason = str(
                provider_ack.get("reason_code") or "qdrant_index_ack_missing"
            )
            degraded = degraded_index_assurance(
                assurance,
                expected_count=len(item_payloads),
                indexed_count=indexed_count,
                retrieval_count=retrieval_count,
                reason_code=failure_reason,
            )
            raise CatalogSnapshotPublicationError(failure_reason, degraded)
        if provider_ack.get("retrieval_verified") is True:
            retrieval_count += 1
        else:
            failure_reason = str(
                provider_ack.get("reason_code") or "qdrant_retrieval_ack_missing"
            )
            degraded = degraded_index_assurance(
                assurance,
                expected_count=len(item_payloads),
                indexed_count=indexed_count,
                retrieval_count=retrieval_count,
                reason_code=failure_reason,
            )
            raise CatalogSnapshotPublicationError(failure_reason, degraded)

    assurance = committed_assurance(
        assurance,
        expected_count=len(item_payloads),
        indexed_count=indexed_count,
        retrieval_count=retrieval_count,
    )
    if not assurance.get("ready"):
        raise CatalogSnapshotPublicationError(
            "catalog_provider_ack_incomplete",
            assurance,
        )

    cfg = dict(tenant.configuracion) if isinstance(tenant.configuracion, dict) else {}
    cfg["catalog_version"] = candidate_version
    cfg["catalog_vector_version"] = candidate_version
    cfg["catalog_snapshot_receipt_ref"] = stored.get("receipt_ref")
    cfg["catalog_snapshot_item_count"] = len(item_payloads)
    cfg["catalog_last_inventory_update_at"] = datetime.now(timezone.utc).isoformat()
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")
    db.session.flush()

    return {
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
        "catalog_version": candidate_version,
        "item_count": len(item_payloads),
        "ingestion_assurance": assurance,
    }

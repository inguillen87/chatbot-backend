"""Truthful storage and retrieval receipts for catalog ingestion.

The import preview is useful before every optional provider is online, but a
catalog must not be described as published or retrieval-ready without explicit
provider acknowledgements.  This module keeps those concepts separate and
returns only non-sensitive operational metadata.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from services.gcs_service import upload_to_gcs
from utils.upload_limits import UploadFileTooLargeError
from werkzeug.datastructures import FileStorage


CONTRACT_VERSION = "catalog.ingestion_assurance.v1"


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

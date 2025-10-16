"""Utilities to inspect the vectorized catalog status for a PyME."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy import func

from extensions import db
from models import ArchivoAdjunto, CatalogoEmbedding, CatalogoItem


def _isoformat_or_none(value: Optional[Any]) -> Optional[str]:
    """Return an ISO8601 representation for ``value`` if possible."""

    if not value:
        return None

    try:
        return value.isoformat()  # type: ignore[return-value]
    except AttributeError:
        return None


@dataclass(frozen=True)
class CatalogVectorSyncStatus:
    """Represents a high-level summary of the catalog vector status."""

    pyme_id: int
    status: str
    reason: Optional[str]
    item_count: int
    embedding_count: int
    needs_sync: bool
    last_catalog_upload_at: Optional[str]
    last_catalog_item_at: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pymeId": self.pyme_id,
            "status": self.status,
            "reason": self.reason,
            "itemCount": self.item_count,
            "embeddingCount": self.embedding_count,
            "needsSync": self.needs_sync,
            "lastCatalogUploadAt": self.last_catalog_upload_at,
            "lastCatalogItemAt": self.last_catalog_item_at,
        }


class CatalogVectorSyncService:
    """Expose helpers to inspect the vector embedding catalogue for a PyME."""

    def get_status(self, pyme_id: int) -> CatalogVectorSyncStatus:
        """Return a summarized status for the given ``pyme_id``."""

        item_count = (
            db.session.query(func.count(CatalogoItem.id))
            .filter(CatalogoItem.user_id == pyme_id)
            .scalar()
            or 0
        )
        embedding_count = (
            db.session.query(func.count(CatalogoEmbedding.id))
            .filter(CatalogoEmbedding.user_id == pyme_id)
            .scalar()
            or 0
        )

        last_item_at = (
            db.session.query(func.max(CatalogoItem.timestamp))
            .filter(CatalogoItem.user_id == pyme_id)
            .scalar()
        )

        last_upload = (
            ArchivoAdjunto.query.filter_by(user_id=pyme_id, tipo="catalogo")
            .order_by(ArchivoAdjunto.fecha.desc())
            .first()
        )

        needs_sync = item_count > 0 and embedding_count < item_count

        if item_count == 0:
            status = "no_items"
            reason = "No se encontraron productos cargados en el catálogo."
        elif embedding_count == 0:
            status = "missing_embeddings"
            reason = "El catálogo aún no tiene embeddings vectoriales generados."
        elif needs_sync:
            status = "partial"
            reason = "Hay más productos cargados que embeddings disponibles."
        else:
            status = "ready"
            reason = None

        return CatalogVectorSyncStatus(
            pyme_id=pyme_id,
            status=status,
            reason=reason,
            item_count=item_count,
            embedding_count=embedding_count,
            needs_sync=needs_sync,
            last_catalog_upload_at=_isoformat_or_none(getattr(last_upload, "fecha", None)),
            last_catalog_item_at=_isoformat_or_none(last_item_at),
        )


catalog_vector_sync_service = CatalogVectorSyncService()


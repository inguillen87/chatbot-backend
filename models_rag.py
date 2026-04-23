from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Boolean
from database import db

class SourceDocument(db.Model):
    """Represents an ingested knowledge document for RAG."""
    __tablename__ = "source_documents"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    source_type = Column(String(50), nullable=False) # 'catalog', 'faq', 'procedure', 'attachment', 'manual'
    source_id = Column(String(100), nullable=False) # e.g. "product_123" or URL

    title = Column(String(255), nullable=True)
    url_or_path = Column(String(500), nullable=True)
    access_level = Column(String(50), default="public") # 'public', 'private', 'internal'

    language = Column(String(10), default="es")
    version = Column(String(50), default="1.0")
    hash = Column(String(64), nullable=True) # Document content hash

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class SourceChunk(db.Model):
    """Represents an indexed chunk of a SourceDocument for vector search."""
    __tablename__ = "source_chunks"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("source_documents.id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)

    content = Column(String, nullable=False) # Text content
    embedding_model = Column(String(100), nullable=False) # e.g. "text-embedding-3-small"

    # We do not store the float vector here, that lives in Qdrant.
    # This table is the relational source of truth linking DB IDs to Qdrant Point IDs.
    qdrant_point_id = Column(String(36), nullable=False, index=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class IngestionJob(db.Model):
    __tablename__ = "ingestion_jobs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    status = Column(String(20), default="pending") # pending, processing, completed, failed
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)

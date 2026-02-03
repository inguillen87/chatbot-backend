import logging
import os
from datetime import datetime
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# Configuración de Colecciones (Solo 2 principales)
COLLECTION_CATALOG = "catalog_items"
COLLECTION_KNOWLEDGE = "knowledge_docs"
EMBEDDING_DIMENSION = 1024 # Standardized dimension

def get_qdrant_client():
    """Singleton getter for Qdrant client."""
    url = os.getenv("QDRANT_URL")
    api_key = os.getenv("QDRANT_API_KEY")
    if not url:
        return None
    return QdrantClient(url=url, api_key=api_key)

def ensure_collections_exist():
    """Ensure standard collections exist with correct config."""
    client = get_qdrant_client()
    if not client:
        return

    collections = {
        COLLECTION_CATALOG: "Catalog items (products, services)",
        COLLECTION_KNOWLEDGE: "Knowledge documents (PDFs, regulations)"
    }

    existing = [c.name for c in client.get_collections().collections]

    for name, desc in collections.items():
        if name not in existing:
            logger.info(f"Creating Qdrant collection: {name} ({desc})")
            client.create_collection(
                collection_name=name,
                vectors_config=qdrant_models.VectorParams(
                    size=EMBEDDING_DIMENSION,
                    distance=qdrant_models.Distance.COSINE
                )
            )
            # Create Payload Indexes for Tenant Isolation & Filters
            client.create_payload_index(name, "tenant_id", qdrant_models.PayloadSchemaType.KEYWORD)
            client.create_payload_index(name, "tenant_type", qdrant_models.PayloadSchemaType.KEYWORD)
            client.create_payload_index(name, "rubro", qdrant_models.PayloadSchemaType.KEYWORD)
            if name == COLLECTION_CATALOG:
                 client.create_payload_index(name, "precio", qdrant_models.PayloadSchemaType.FLOAT)
                 client.create_payload_index(name, "stock", qdrant_models.PayloadSchemaType.INTEGER)

def index_catalog_item(tenant_id: str, item_data: Dict[str, Any], embedding: List[float]):
    """Index a catalog item into the shared catalog collection."""
    client = get_qdrant_client()
    if not client: return False

    point_id = item_data.get("id") # Assuming robust ID or hash
    payload = {
        "tenant_id": str(tenant_id),
        "tenant_type": "pyme", # Default for catalog
        "rubro": item_data.get("rubro", "general"),
        "title": item_data.get("nombre"),
        "description": item_data.get("descripcion"),
        "price": float(item_data.get("precio", 0)),
        "stock": int(item_data.get("stock", 0)),
        "source": "manual",
        "updated_at": datetime.utcnow().isoformat()
    }
    extra_metadata = item_data.get("extra_metadata") or {}
    if isinstance(extra_metadata, dict) and extra_metadata:
        payload["extra_metadata"] = extra_metadata

    client.upsert(
        collection_name=COLLECTION_CATALOG,
        points=[
            qdrant_models.PointStruct(
                id=point_id,
                vector=embedding,
                payload=payload
            )
        ]
    )
    return True

def search_catalog(tenant_id: str, query_vector: List[float], limit: int = 5, filters: Dict = None):
    """Search catalog items scoped to a specific tenant."""
    client = get_qdrant_client()
    if not client: return []

    must_filters = [
        qdrant_models.FieldCondition(key="tenant_id", match=qdrant_models.MatchValue(value=str(tenant_id)))
    ]

    if filters:
        if filters.get("min_price"):
             must_filters.append(qdrant_models.FieldCondition(key="price", range=qdrant_models.Range(gte=filters["min_price"])))
        # Add other dynamic filters here

    results = client.search(
        collection_name=COLLECTION_CATALOG,
        query_vector=query_vector,
        query_filter=qdrant_models.Filter(must=must_filters),
        limit=limit
    )
    return results

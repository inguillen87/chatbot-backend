import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models
from typing import List, Optional, Dict, Any, Iterable, Tuple

from services.common_utils import parse_precio_flexible, parse_cantidad_flexible
from services.qdrant_search import coleccion_catalogo_para_rubro
from services.qdrant_utils import (
    get_qdrant_client as get_qdrant_utils_client,
    verificar_y_crear_coleccion_qdrant,
)

logger = logging.getLogger(__name__)

# Configuración de Colecciones (Solo 2 principales)
COLLECTION_CATALOG = "catalog_items"
COLLECTION_KNOWLEDGE = "knowledge_docs"
EMBEDDING_DIMENSION = 1024  # Standardized dimension

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
                client.create_payload_index(name, "price", qdrant_models.PayloadSchemaType.FLOAT)
                client.create_payload_index(name, "stock", qdrant_models.PayloadSchemaType.INTEGER)


def _extra_metadata_entries(extra_metadata: Dict[str, Any], prefix: str = "extra_metadata") -> Iterable[Tuple[str, Any]]:
    for key, value in extra_metadata.items():
        if value is None:
            continue
        nested_key = f"{prefix}.{key}"
        if isinstance(value, dict):
            yield from _extra_metadata_entries(value, prefix=nested_key)
        else:
            yield nested_key, value


def _payload_schema_for_value(value: Any) -> qdrant_models.PayloadSchemaType:
    if isinstance(value, bool):
        return qdrant_models.PayloadSchemaType.BOOL
    if isinstance(value, int) and not isinstance(value, bool):
        return qdrant_models.PayloadSchemaType.INTEGER
    if isinstance(value, float):
        return qdrant_models.PayloadSchemaType.FLOAT
    if isinstance(value, list):
        return qdrant_models.PayloadSchemaType.KEYWORD
    return qdrant_models.PayloadSchemaType.KEYWORD


@lru_cache(maxsize=1)
def _collection_payload_schema(collection_name: str) -> Dict[str, Any]:
    client = get_qdrant_client()
    if not client:
        return {}
    collection_info = client.get_collection(collection_name=collection_name)
    return collection_info.payload_schema or {}


def _ensure_extra_metadata_indexes(extra_metadata: Dict[str, Any]):
    if not extra_metadata:
        return
    client = get_qdrant_client()
    if not client:
        return
    payload_schema = _collection_payload_schema(COLLECTION_CATALOG)
    for field_key, field_value in _extra_metadata_entries(extra_metadata):
        if field_key in payload_schema:
            continue
        schema_type = _payload_schema_for_value(field_value)
        logger.info("Creating Qdrant payload index for %s (%s)", field_key, schema_type)
        client.create_payload_index(COLLECTION_CATALOG, field_key, schema_type)
        payload_schema[field_key] = schema_type


def _parse_stock_value(raw_value: Any) -> int:
    if raw_value is None:
        return 0

    if isinstance(raw_value, bool):
        return int(raw_value)

    if isinstance(raw_value, (int, float)):
        return int(raw_value)

    value_str = str(raw_value).strip()
    if not value_str:
        return 0

    if re.fullmatch(r"\d{1,3}(\.\d{3})+", value_str):
        return int(value_str.replace(".", ""))

    if re.fullmatch(r"\d{1,3}(,\d{3})+", value_str):
        return int(value_str.replace(",", ""))

    parsed = parse_cantidad_flexible(value_str)
    if parsed is not None:
        return parsed

    digits = re.findall(r"\d+", value_str)
    if digits:
        return int("".join(digits))

    return 0


def index_catalog_item(tenant_id: str, item_data: Dict[str, Any], embedding: List[float]):
    """Index a catalog item into the Qdrant catalog collections."""
    client = get_qdrant_utils_client()
    if not client:
        return False

    rubro = item_data.get("rubro") or "general"
    coleccion = coleccion_catalogo_para_rubro(rubro)
    if not verificar_y_crear_coleccion_qdrant(
        coleccion,
        vector_size=EMBEDDING_DIMENSION,
        create_indexes=True,
    ):
        return False

    point_id = item_data.get("id")
    precio_raw = item_data.get("precio", 0)
    _, precio_float, _ = parse_precio_flexible(precio_raw)
    if precio_float is None:
        logger.warning("Precio inválido para indexar en Qdrant: %s", precio_raw)
        precio_float = 0.0

    nombre = item_data.get("nombre")
    descripcion = item_data.get("descripcion")
    sku = item_data.get("sku")
    extra_metadata = item_data.get("extra_metadata") or {}
    texto_original = " ".join(
        part
        for part in [
            nombre,
            descripcion,
            sku,
            " ".join(
                str(value)
                for value in extra_metadata.values()
                if value and not isinstance(value, (list, dict))
            ),
        ]
        if part
    ).strip()

    categoria = (item_data.get("categoria") or rubro or "").strip().lower() or None
    payload = {
        "db_id": point_id,
        "nombre": nombre,
        "precio": precio_float,
        "precio_float": precio_float,
        "precio_str": str(precio_raw),
        "categoria_qdrant": categoria,
        "descripcion": descripcion,
        "sku": sku,
        "marca": item_data.get("marca"),
        "stock": _parse_stock_value(item_data.get("stock", 0)),
        "unidad": item_data.get("unidad"),
        "moneda": item_data.get("moneda"),
        "precio_por_caja": item_data.get("precio_por_caja"),
        "unidad_por_caja": item_data.get("unidad_por_caja"),
        "user_id": item_data.get("user_id") or item_data.get("owner_id") or tenant_id,
        "tenant_id": item_data.get("tenant_id") or tenant_id,
        "rubro_slug": rubro,
        "texto_original_para_embedding": texto_original,
        "varietal": extra_metadata.get("varietal"),
        "anada": extra_metadata.get("anada"),
        "presentacion": extra_metadata.get("presentacion_original"),
    }

    client.upsert(
        collection_name=coleccion,
        points=[
            qdrant_models.PointStruct(
                id=str(point_id),
                vector=embedding,
                payload=payload,
            )
        ],
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

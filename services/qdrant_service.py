from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime
from functools import lru_cache
from typing import List, Optional, Dict, Any, Iterable, Tuple

from services.common_utils import parse_precio_flexible, parse_cantidad_flexible
from services.qdrant_search import coleccion_catalogo_para_rubro
from services.qdrant_utils import (
    get_qdrant_client as get_qdrant_utils_client,
    verificar_y_crear_coleccion_qdrant,
)
from utils.lazy_module import LazyModule

_qdrant_sdk = LazyModule("qdrant_client")
qdrant_models = LazyModule("qdrant_client.http.models")


def QdrantClient(*args, **kwargs):
    """Compatibility constructor that imports the SDK only on first use."""

    return _qdrant_sdk.QdrantClient(*args, **kwargs)

logger = logging.getLogger(__name__)

# Configuración de Colecciones (Solo 2 principales)
COLLECTION_CATALOG = "catalog_items"
COLLECTION_KNOWLEDGE = "knowledge_docs"
EMBEDDING_DIMENSION = 1024  # Standardized dimension


def _flag_enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _qdrant_network_allowed() -> bool:
    """Keep Qdrant offline in tests unless an integration test opts in."""

    testing = _flag_enabled(os.getenv("TESTING"))
    config_opt_in = False
    from flask import current_app, has_app_context

    if has_app_context():
        testing = testing or _flag_enabled(current_app.config.get("TESTING"))
        config_opt_in = _flag_enabled(
            current_app.config.get("QDRANT_ALLOW_NETWORK_IN_TESTS")
        )

    return (
        not testing
        or config_opt_in
        or _flag_enabled(os.getenv("QDRANT_ALLOW_NETWORK_IN_TESTS"))
    )

def get_qdrant_client():
    """Singleton getter for Qdrant client."""
    if not _qdrant_network_allowed():
        logger.info("Qdrant provider blocked reason=test_network_disabled")
        return None
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


def _normalize_qdrant_point_id(raw_point_id: Any, tenant_id: str) -> Any:
    """Return a Qdrant-compatible point id (unsigned int or UUID string)."""
    if isinstance(raw_point_id, int) and raw_point_id >= 0:
        return raw_point_id

    point_as_text = str(raw_point_id or "").strip()
    if point_as_text.isdigit():
        return int(point_as_text)

    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, f"chatboc:{tenant_id}")
    return str(uuid.uuid5(namespace, point_as_text or "sin-id"))


def _catalog_qdrant_point_id(
    raw_point_id: Any,
    tenant_id: str,
    catalog_version: str | None,
) -> Any:
    """Namespace staged catalog points by tenant and immutable version."""

    normalized_version = str(catalog_version or "").strip()
    if not normalized_version:
        return _normalize_qdrant_point_id(raw_point_id, tenant_id)
    namespace = uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"chatboc:{tenant_id}:catalog:{normalized_version}",
    )
    point_as_text = str(raw_point_id or "sin-id").strip() or "sin-id"
    return str(uuid.uuid5(namespace, point_as_text))


def index_catalog_item(tenant_id: str, item_data: Dict[str, Any], embedding: List[float]):
    """Index a catalog item into the Qdrant catalog collections."""
    try:
        scoped_tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        return False
    if scoped_tenant_id <= 0:
        return False
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
    catalog_version = str(item_data.get("catalog_version") or "").strip() or None
    qdrant_point_id = _catalog_qdrant_point_id(
        point_id,
        str(tenant_id),
        catalog_version,
    )
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
        "tenant_id": scoped_tenant_id,
        "rubro_slug": rubro,
        "texto_original_para_embedding": texto_original,
        "varietal": extra_metadata.get("varietal"),
        "anada": extra_metadata.get("anada"),
        "presentacion": extra_metadata.get("presentacion_original"),
    }
    if catalog_version:
        payload["catalog_version"] = catalog_version

    client.upsert(
        collection_name=coleccion,
        points=[
            qdrant_models.PointStruct(
                id=qdrant_point_id,
                vector=embedding,
                payload=payload,
            )
        ],
    )
    return True


def verify_catalog_item_index(tenant_id: str, item_data: Dict[str, Any]) -> bool:
    """Read back one catalog point and verify its tenant-scoped payload.

    A successful ``upsert`` call alone is not enough evidence that retrieval is
    available.  This check is deliberately fail-closed and does not fall back
    to another tenant, collection or local embedding store.
    """

    client = get_qdrant_utils_client()
    if not client:
        return False

    point_id = item_data.get("id")
    catalog_version = str(item_data.get("catalog_version") or "").strip()
    if point_id in (None, "") or not catalog_version:
        return False
    rubro = item_data.get("rubro") or "general"
    collection_name = coleccion_catalogo_para_rubro(rubro)
    qdrant_point_id = _catalog_qdrant_point_id(
        point_id,
        str(tenant_id),
        catalog_version,
    )
    try:
        records = client.retrieve(
            collection_name=collection_name,
            ids=[qdrant_point_id],
            with_payload=["tenant_id", "db_id", "catalog_version"],
            with_vectors=False,
        )
    except Exception as exc:
        logger.warning(
            "Catalog retrieval verification failed error_type=%s",
            type(exc).__name__,
        )
        return False

    for record in records or []:
        payload = getattr(record, "payload", None)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("tenant_id")) != str(tenant_id):
            continue
        if str(payload.get("db_id")) != str(point_id):
            continue
        if str(payload.get("catalog_version")) != catalog_version:
            continue
        return True
    return False


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

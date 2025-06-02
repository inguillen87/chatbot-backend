# services/qdrant_utils.py
import os
from qdrant_client import QdrantClient, models # models para VectorParams y Distance
import logging
from typing import Optional # <--- AÑADIDO

logger = logging.getLogger(__name__)
qdrant_client_instance: Optional[QdrantClient] = None # Tipado añadido

def get_qdrant_client() -> Optional[QdrantClient]: # Tipado añadido
    global qdrant_client_instance
    if qdrant_client_instance is None:
        url = os.getenv("QDRANT_URL")
        api_key = os.getenv("QDRANT_API_KEY")
        if not url:
            logger.error("[QDRANT UTILS] QDRANT_URL no está configurada.")
            return None
        try:
            logger.info(f"[QDRANT UTILS] Intentando conectar a Qdrant URL: {url}")
            qdrant_client_instance = QdrantClient(url=url, api_key=api_key, timeout=20)
            # Opcional: Verificar conexión real si la librería lo permite fácilmente
            # Ejemplo: client.get_collections() podría lanzar una excepción si no conecta.
            # Esto ya se hace implícitamente en verificar_y_crear_coleccion_qdrant
            logger.info("✅ [QDRANT UTILS] Cliente Qdrant inicializado.")
        except Exception as e:
            logger.error(f"❌ [QDRANT UTILS] Error al conectar/inicializar cliente Qdrant: {e}", exc_info=True)
            qdrant_client_instance = None 
    return qdrant_client_instance

def verificar_y_crear_coleccion_qdrant(collection_name: str, vector_size: int, distance_metric: str = "Cosine") -> bool:
    client = get_qdrant_client()
    if not client:
        logger.error(f"[QDRANT UTILS] No se pudo obtener cliente Qdrant para verificar/crear colección '{collection_name}'.")
        return False
    try:
        collections_response = client.get_collections()
        collection_names = [c.name for c in collections_response.collections]

        if collection_name in collection_names:
            logger.info(f"[QDRANT UTILS] Colección '{collection_name}' ya existe en Qdrant.")
            return True
        else:
            logger.info(f"[QDRANT UTILS] Colección '{collection_name}' no encontrada. Creando...")
            client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(size=vector_size, distance=getattr(models.Distance, distance_metric.upper(), models.Distance.COSINE))
            )
            logger.info(f"✅ [QDRANT UTILS] Colección '{collection_name}' creada exitosamente en Qdrant.")
            return True
    except Exception as e:
        logger.error(f"❌ [QDRANT UTILS] Error al verificar o crear la colección '{collection_name}' en Qdrant: {e}", exc_info=True)
        return False
# services/qdrant_utils.py
import os
from qdrant_client import QdrantClient, models # Añadido models por si se usa en el futuro
import logging

logger = logging.getLogger(__name__)
qdrant_client_instance = None

def get_qdrant_client() -> Optional[QdrantClient]:
    global qdrant_client_instance
    if qdrant_client_instance is None:
        url = os.getenv("QDRANT_URL")
        api_key = os.getenv("QDRANT_API_KEY")
        if not url:
            logger.error("QDRANT_URL no está configurada.")
            return None
        try:
            qdrant_client_instance = QdrantClient(url=url, api_key=api_key, timeout=20) # Timeout para cliente
            # Verificar conexión (opcional, pero útil para diagnóstico inicial)
            # qdrant_client_instance.get_collections() 
            logger.info("✅ Cliente Qdrant inicializado y conectado exitosamente.")
        except Exception as e:
            logger.error(f"❌ Error al conectar/inicializar cliente Qdrant: {e}", exc_info=True)
            qdrant_client_instance = None # Asegurar que no se use una instancia fallida
    return qdrant_client_instance

def verificar_y_crear_coleccion_qdrant(collection_name: str, vector_size: int, distance_metric: str = "Cosine"):
    client = get_qdrant_client()
    if not client:
        logger.error(f"No se pudo obtener cliente Qdrant para verificar/crear colección '{collection_name}'.")
        return False
    try:
        # client.get_collection(collection_name) # Lanza excepción si no existe
        # Mejor usar list_collections y buscar
        collections = client.get_collections().collections
        if any(c.name == collection_name for c in collections):
            logger.info(f"Colección '{collection_name}' ya existe en Qdrant.")
            return True
        else:
            logger.info(f"Colección '{collection_name}' no encontrada. Creando...")
            client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(size=vector_size, distance=getattr(models.Distance, distance_metric.upper(), models.Distance.COSINE))
            )
            logger.info(f"✅ Colección '{collection_name}' creada exitosamente en Qdrant.")
            return True
    except Exception as e:
        logger.error(f"❌ Error al verificar o crear la colección '{collection_name}' en Qdrant: {e}", exc_info=True)
        return False
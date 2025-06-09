# En tu archivo /services/vector_search.py

from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos
# ... otras importaciones

QDRANT_COLLECTION_NAME = "catalogos"

def buscar_item_vectorizado(pregunta: str, user_id: int):
    """
    Busca ítems en el catálogo de un usuario específico usando búsqueda vectorial en Qdrant.

    Args:
        pregunta (str): La consulta del cliente.
        user_id (int): El ID del usuario (pyme) al que pertenece el catálogo.

    Returns:
        list: Una lista de resultados encontrados o None si no hay resultados.
    """
    print(f"🔍 [Vector] Buscando coincidencias para: “{pregunta}” | User: {user_id}")

    try:
        qdrant_client = get_qdrant_client()

        # 1. Generar el vector para la pregunta del cliente
        # Usamos 'search_query' como input_type para la búsqueda
        query_vector = embed_textos([pregunta], input_type='search_query')
        
        if not query_vector:
            print(f"⚠️ [Vector] No se pudo generar el vector para la pregunta.")
            return None

        # 2. Construir el filtro para Qdrant para buscar solo en el catálogo de este usuario
        # ¡Este es el paso más importante que probablemente falta!
        from qdrant_client.http import models
        
        search_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id", # El campo en tus metadatos de Qdrant
                    match=models.(value=user_id)
                )
            ]
        )

        # 3. Realizar la búsqueda en Qdrant
        search_result = qdrant_client.search(
            collection_name=QDRANT_COLLECTION_NAME,
            query_vector=query_vector[0],
            query_filter=search_filter,
            limit=3 # Traer los 3 mejores resultados
        )

        if not search_result:
            print(f"⚠️ [Vector] No se encontraron resultados en Qdrant para el user_id: {user_id}.")
            return None

        print(f"✅ [Vector] Se encontraron {len(search_result)} coincidencias.")
        return search_result

    except Exception as e:
        print(f"❌ [Vector] Error durante la búsqueda vectorial: {e}")
        return None
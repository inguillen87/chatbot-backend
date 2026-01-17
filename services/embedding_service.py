
import logging
from typing import List, Optional
from services.openai_bridge import client as openai_client

logger = logging.getLogger(__name__)

def embed_textos_llm(textos: List[str], input_type: str = "search_document") -> Optional[List[List[float]]]:
    """
    Genera embeddings para una lista de textos utilizando la API del LLM (OpenAI).
    Usa el modelo 'text-embedding-3-large' reducido a 1024 dimensiones para máxima calidad semántica
    manteniendo compatibilidad con la base de datos vectorial existente.

    Args:
        textos: Una lista de strings para generar embeddings.
        input_type: El tipo de input para el embedding (ignorado por OpenAI, mantenido por compatibilidad).

    Returns:
        Una lista de listas de floats, donde cada lista interna es un embedding de 1024 dimensiones.
        Retorna None si ocurre un error.
    """
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("Entrada inválida: se esperaba una lista de strings.")
        return None

    if not openai_client:
        logger.error("Cliente OpenAI no inicializado.")
        return None

    try:
        # Reemplazar saltos de línea para mejor rendimiento (recomendación común)
        textos_limpios = [t.replace("\n", " ") for t in textos]

        response = openai_client.embeddings.create(
            input=textos_limpios,
            model="text-embedding-3-large",
            dimensions=1024
        )

        # Extraer los embeddings en orden
        embeddings = [data.embedding for data in response.data]
        return embeddings

    except Exception as e:
        logger.error(f"Error generando embeddings con OpenAI: {e}", exc_info=True)
        return None

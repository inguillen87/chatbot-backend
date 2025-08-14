import logging
from typing import List, Optional
from services.gemini_bridge import llamar_gemini as llamar_gemini_para_generacion_texto

logger = logging.getLogger(__name__)

def embed_textos_gemini(textos: List[str], input_type: str = "search_document") -> Optional[List[List[float]]]:
    """
    Genera embeddings para una lista de textos utilizando la API de Gemini.

    Args:
        textos: Una lista de strings para generar embeddings.
        input_type: El tipo de input para el embedding (search_document o search_query).

    Returns:
        Una lista de listas de floats, donde cada lista interna es un embedding.
        Retorna None si ocurre un error.
    """
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("Entrada inválida: se esperaba una lista de strings.")
        return None

    # NOTA: Esta es una implementación mock/placeholder. Debería ser reemplazada
    # con una llamada real al servicio de embeddings de Gemini.
    # Los tests deben mockear esta función para devolver valores controlados.
    logger.warning("Usando implementación MOCK de embed_textos_gemini. Devolverá vectores de ceros.")
    # Se devuelve un vector de 1024 para ser consistente con los datos de prueba existentes.
    return [[0.0] * 1024 for _ in textos]

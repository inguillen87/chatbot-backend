from services.qdrant_utils import get_qdrant_client
from services.cohere_ai import embed_textos
import logging

def buscar_catalogo_qdrant(user_id, pregunta, limite=3, score_min=0.25):
    qdrant = get_qdrant_client()
    vector = embed_textos([pregunta])[0]
    resultados = qdrant.search(
        collection_name="catalogos",
        query_vector=vector,
        query_filter={"must": [{"key": "user_id", "match": {"value": user_id}}]},
        limit=limite
    )
    respuestas = [
        (hit.payload['texto'], hit.score)
        for hit in resultados if hit.score > score_min
    ]
    logging.info(f"Qdrant resultados para '{pregunta}': {respuestas}")
    return respuestas

def armar_respuesta_legible(resultados_qdrant):
    if not resultados_qdrant:
        return ""  # Cambié None por cadena vacía
    textos = [texto for texto, score in resultados_qdrant]
    respuesta = "\n".join(textos)
    return respuesta

# services/vector_search.py

import logging
from models import CatalogoEmbedding
from services.cohere_ai import embed_textos

def cos_sim(a, b):
    try:
        return sum(x * y for x, y in zip(a, b)) / (
            (sum(x**2 for x in a) ** 0.5) * (sum(y**2 for y in b) ** 0.5)
        )
    except ZeroDivisionError:
        return 0

def buscar_item_vectorizado(pregunta, user_id):
    try:
        pregunta_embedding = embed_textos([pregunta])[0]
        items = CatalogoEmbedding.query.filter_by(user_id=user_id).all()
        if not items:
            return None

        best_match = max(items, key=lambda item: cos_sim(pregunta_embedding, item.embedding_vector))
        similitud = cos_sim(pregunta_embedding, best_match.embedding_vector)

        if similitud > 0.75:
            return (
                f"Tenemos: {best_match.nombre}. "
                f"{best_match.descripcion}. "
                f"Precio: ${best_match.precio}."
            )
    except Exception as e:
        logging.warning(f"❌ Error en búsqueda vectorizada: {e}")
    return None

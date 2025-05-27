import logging
import numpy as np
from models import CatalogoEmbedding
from services.cohere_ai import embed_textos
from sqlalchemy.orm import load_only

def buscar_item_vectorizado(pregunta: str, user_id: int, umbral: float = 0.75) -> str | None:
    try:
        catalogo = CatalogoEmbedding.query.filter_by(user_id=user_id).options(
            load_only("nombre", "descripcion", "precio", "cantidad", "embedding_vector")
        ).all()

        if not catalogo:
            logging.warning("⚠️ No hay embeddings cargados para este usuario.")
            return None

        textos = [f"{item.nombre}. {item.descripcion}. Precio: {item.precio}. Cantidad: {item.cantidad}." for item in catalogo]
        vectores = np.array([item.embedding_vector for item in catalogo])

        pregunta_vector = embed_textos([pregunta])
        if not pregunta_vector or not vectores.any():
            return None

        pregunta_vector = np.array(pregunta_vector).reshape(1, -1)
        similitudes = np.dot(vectores, pregunta_vector.T).flatten()  # alternativa a cosine_similarity

        idx_mejor = int(np.argmax(similitudes))
        sim_max = float(similitudes[idx_mejor])

        logging.info(f"📊 Similaridad máxima con catálogo: {sim_max:.4f}")

        if sim_max < umbral:
            return None

        mejor_item = catalogo[idx_mejor]
        return f"{mejor_item.nombre} - {mejor_item.descripcion}. Precio: ${mejor_item.precio}. Stock: {mejor_item.cantidad} unidades."

    except Exception as e:
        logging.error(f"❌ Error en búsqueda vectorial: {e}")
        return None

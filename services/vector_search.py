from models import User
import logging
import numpy as np
from models import CatalogoEmbedding
from services.cohere_ai import embed_textos

def buscar_item_vectorizado(pregunta: str, user: User, umbral: float = 0.75) -> str | None:
    try:
        # 🚫 IMPORTANTE: no usar load_only con embedding_vector (puede traer nulls o datos mal formateados)
        catalogo = CatalogoEmbedding.query.filter_by(user_id=user.id).all()

        if not catalogo:
            logging.warning("⚠️ No hay embeddings cargados para este usuario.")
            return None

        # 🧠 Asegurar que todos los embeddings sean arrays numéricos válidos
        vectores = np.array([
            np.array(item.embedding_vector, dtype=np.float32)
            for item in catalogo
            if item.embedding_vector
        ])

        # 💬 Embed de la pregunta con Cohere
        pregunta_vector = embed_textos([pregunta])
        if not pregunta_vector or not vectores.any():
            logging.warning("⚠️ Pregunta no embebida correctamente o vectores vacíos.")
            return None

        pregunta_vector = np.array(pregunta_vector[0], dtype=np.float32).reshape(1, -1)
        similitudes = np.dot(vectores, pregunta_vector.T).flatten()

        idx_mejor = int(np.argmax(similitudes))
        sim_max = float(similitudes[idx_mejor])

        logging.info(f"📊 Similaridad máxima con catálogo: {sim_max:.4f}")

        if sim_max < umbral:
            logging.info("📉 Similaridad insuficiente. No se devuelve respuesta.")
            return None

        mejor_item = catalogo[idx_mejor]
        logging.info(f"✅ Match con producto: {mejor_item.nombre}")
        return f"{mejor_item.nombre} - {mejor_item.descripcion}. Precio: ${mejor_item.precio}. Stock: {mejor_item.cantidad} unidades."

    except Exception as e:
        logging.error(f"❌ Error en búsqueda vectorial: {e}")
        return None

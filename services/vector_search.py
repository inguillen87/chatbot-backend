from models import User
import logging
import numpy as np
from models import CatalogoEmbedding
from services.cohere_ai import embed_textos

def buscar_item_vectorizado(pregunta: str, user: User, umbral: float = 0.6) -> str | None:
    try:
        logging.info(f"🔍 [Vector] Buscando coincidencias para: “{pregunta}” | User: {user.nombre_empresa} (ID {user.id})")

        # Cargar catálogo vectorizado del usuario
        catalogo = CatalogoEmbedding.query.filter_by(user_id=user.id).all()
        if not catalogo:
            logging.warning("⚠️ [Vector] No hay embeddings cargados en DB para este usuario.")
            return None

        # Procesar todos los vectores del catálogo
        vectores = []
        items_validos = []
        for item in catalogo:
            if item.embedding_vector:
                try:
                    vectores.append(np.array(item.embedding_vector, dtype=np.float32))
                    items_validos.append(item)
                except Exception as e:
                    logging.warning(f"⚠️ Vector inválido para item ID {item.id}: {e}")

        if not vectores:
            logging.warning("🚫 [Vector] Todos los vectores estaban vacíos o mal formateados.")
            return None

        vectores = np.array(vectores)
        logging.info(f"📦 [Vector] {len(vectores)} vectores cargados correctamente para comparación.")

        # Vectorizar la pregunta
        pregunta_vector = embed_textos([pregunta])
        if not pregunta_vector:
            logging.warning("⚠️ [Vector] Falló el embed de la pregunta con Cohere.")
            return None

        pregunta_vector = np.array(pregunta_vector[0], dtype=np.float32).reshape(1, -1)
        similitudes = np.dot(vectores, pregunta_vector.T).flatten()

        idx_mejor = int(np.argmax(similitudes))
        sim_max = float(similitudes[idx_mejor])
        logging.info(f"📊 [Vector] Similaridad máxima encontrada: {sim_max:.4f}")

        if sim_max < umbral:
            logging.info(f"📉 [Vector] Similaridad ({sim_max:.2f}) menor al umbral ({umbral}). No se responde.")
            return None

        mejor_item = items_validos[idx_mejor]
        logging.info(f"✅ [Vector] Match con producto ID {mejor_item.id} | Nombre: {mejor_item.nombre}")
        return (
            f"{mejor_item.nombre} - {mejor_item.descripcion}. "
            f"Precio: ${mejor_item.precio}. Stock: {mejor_item.cantidad} unidades."
        )

    except Exception as e:
        logging.error(f"❌ [Vector] Error al buscar coincidencia vectorial: {e}")
        return None

import re
import logging
import numpy as np
from models import CatalogoEmbedding
from services.cohere_ai import embed_textos
from sklearn.metrics.pairwise import cosine_similarity


def detectar_cantidad_generica(texto: str) -> int:
    """
    Detecta un número en el texto del usuario (por ejemplo "quiero 6 cajas")
    y lo devuelve como cantidad. Si no encuentra, devuelve 1.
    """
    match = re.search(r"(\d+)\s*(\w+)?", texto.lower())
    return int(match.group(1)) if match else 1


def buscar_en_catalogo(pregunta_usuario: str, user_id: int, threshold: float = 0.82) -> str | None:
    try:
        productos = CatalogoEmbedding.query.filter_by(user_id=user_id).all()
        if not productos:
            logging.info(f"📭 Sin productos embebidos para user_id={user_id}")
            return None

        vectores = np.array([p.embedding_vector for p in productos])
        pregunta_vector = embed_textos([pregunta_usuario])[0]
        similitudes = cosine_similarity([pregunta_vector], vectores)[0]

        idx = int(np.argmax(similitudes))
        if similitudes[idx] < threshold:
            logging.info(f"📉 Similaridad demasiado baja ({similitudes[idx]:.2f}) para catálogo")
            return None

        prod = productos[idx]
        cantidad = detectar_cantidad_generica(pregunta_usuario)

        try:
            precio_unitario = float(str(prod.precio).replace(",", "."))
            total = cantidad * precio_unitario
            precio_str = f"${precio_unitario:,.2f}"
            total_str = f"${total:,.2f}"
        except Exception:
            precio_str = prod.precio or "No informado"
            total_str = "No calculable"

        return (
            f"📦 {prod.nombre}\n"
            f"📝 {prod.descripcion}\n"
            f"💰 Precio unitario: {precio_str}\n"
            f"🧮 Total por {cantidad} unidad(es): {total_str}"
        )

    except Exception as e:
        logging.warning(f"❌ Error al buscar en catálogo: {e}")
        return None

import logging
from typing import List, Optional
from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos
from qdrant_client.http import models as qdrant_models
from .utils import limpiar_texto_base

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(user_id: Optional[int], pregunta: str, limite: int = 3, score_min: float = 0.30) -> List[qdrant_models.ScoredPoint]:
    qdrant_cli = get_qdrant_client()
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener cliente Qdrant.")
        return []

    try:
        pregunta_limpia = limpiar_texto_base(pregunta.strip())
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] Pregunta para búsqueda vacía después de limpiar.")
            return []
        vector_pregunta_lista = embed_textos([pregunta_limpia], input_type="search_query")
        if not vector_pregunta_lista or not isinstance(vector_pregunta_lista[0], list):
            logger.error(f"[QDRANT SEARCH] No se pudo generar vector para pregunta: '{pregunta_limpia}'")
            return []
        vector_q = vector_pregunta_lista[0]
    except Exception as e_embed:
        logger.error(f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
        return []

    try:
        search_filter = None
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"

        if user_id is not None:
            search_filter = qdrant_models.Filter(
                must=[qdrant_models.FieldCondition(key="user_id", match=qdrant_models.MatchValue(value=user_id))]
            )
            logger.info(f"[QDRANT SEARCH] Buscando en catálogo PRIVADO para {id_log}, pregunta '{pregunta_limpia}'")
        else:
            logger.info(f"[QDRANT SEARCH] Buscando en catálogo GENERAL para {id_log}, pregunta '{pregunta_limpia}'")
            # Si tuvieras un campo catálogo_público, acá le podés meter ese filtro

        resultados = qdrant_cli.search(
            collection_name="catalogos",
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min
        )
        logger.info(f"[QDRANT SEARCH] Pregunta: '{pregunta}', Hits: {len(resultados)}, Scores: {[r.score for r in resultados[:3]]}")
        return resultados

    except Exception as e_qdrant:
        id_log = f"user_id {user_id}" if user_id is not None else "ANONIMO"
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para {id_log}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []

def armar_respuesta_legible(resultados_qdrant: List[qdrant_models.ScoredPoint], max_items: int = 5) -> str:
    if not resultados_qdrant:
        logger.info("[QDRANT FORMAT] No hay resultados Qdrant para formatear.")
        return "No encontré productos para mostrar en este momento."

    lines = []
    for idx, hit in enumerate(resultados_qdrant[:max_items], 1):
        p = getattr(hit, "payload", None) or {}
        nombre = p.get("nombre") or p.get("title") or "Producto sin nombre"
        sku = p.get("sku") or ""
        categoria = p.get("categoria_qdrant") or p.get("categoria") or ""
        moneda = p.get("moneda", "")
        precio = p.get("precio_str") or p.get("precio") or p.get("precio_unitario") or ""
        if not precio and p.get("precio_float") is not None:
            precio = f"{p.get('precio_float'):,.2f}"
        unidad = p.get("unidad") or p.get("presentacion") or ""
        descripcion = p.get("descripcion") or p.get("descripcion_corta") or ""

        partes = [f"{idx}. {nombre}"]
        if sku and sku.lower() not in nombre.lower():
            partes[-1] += f" (SKU: {sku})"
        if categoria:
            partes.append(f"Categoría: {categoria}")
        if precio:
            partes.append(f"Precio: {moneda} {precio}".strip())
        if unidad:
            partes.append(f"Presentación: {unidad}")
        if descripcion and descripcion.lower() not in nombre.lower():
            desc_limpia = descripcion.strip().replace("\n", " ")
            if len(desc_limpia) > 100:
                desc_limpia = desc_limpia[:100] + "..."
            partes.append(f"Descripción: {desc_limpia}")

        lines.append(" · ".join(partes))

    return "Según nuestro catálogo, esto podría interesarte:\n\n" + "\n".join(lines)

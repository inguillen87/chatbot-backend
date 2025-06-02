# services/qdrant_search.py
import logging
from typing import List, Optional 
from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos 
from qdrant_client.http import models as qdrant_models 
from .utils import limpiar_texto_base 

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(user_id: int, pregunta: str, limite: int = 3, score_min: float = 0.65) -> List[qdrant_models.ScoredPoint]:
    qdrant_cli = get_qdrant_client() 
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener cliente Qdrant.")
        return []
    vector_q: Optional[List[float]] = None
    try:
        pregunta_limpia = limpiar_texto_base(pregunta.strip())
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] Pregunta para búsqueda vacía después de limpiar.")
            return []
        vector_pregunta_lista = embed_textos([pregunta_limpia], input_type="search_query")
        if not vector_pregunta_lista or not vector_pregunta_lista[0] or not isinstance(vector_pregunta_lista[0], list):
            logger.error(f"[QDRANT SEARCH] No se pudo generar vector para pregunta: '{pregunta_limpia}'")
            return []
        vector_q = vector_pregunta_lista[0]
    except Exception as e_embed:
        logger.error(f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
        return []
    if vector_q is None: 
        logger.error(f"[QDRANT SEARCH] Vector de pregunta es None para '{pregunta_limpia}'.")
        return []
    try:
        logger.info(f"[QDRANT SEARCH] Buscando para user_id {user_id}, pregunta '{pregunta_limpia}', límite={limite}, umbral={score_min}")
        search_filter = qdrant_models.Filter( 
            must=[qdrant_models.FieldCondition(key="user_id", match=qdrant_models.MatchValue(value=user_id))]
        )
        resultados = qdrant_cli.search(
            collection_name="catalogos", query_vector=vector_q, query_filter=search_filter,
            limit=limite, score_threshold=score_min 
        )
        logger.info(f"[QDRANT SEARCH] Búsqueda para user_id {user_id}, pregunta '{pregunta_limpia}': {len(resultados)} hits con score >= {score_min}.")
        return resultados
    except Exception as e_qdrant:
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []

def armar_respuesta_legible(resultados_qdrant: List[qdrant_models.ScoredPoint]) -> str:
    if not resultados_qdrant: logger.info("[QDRANT FORMAT] No hay resultados Qdrant para formatear."); return "" 
    contexto_items = []; logger.info(f"[QDRANT FORMAT] Formateando {len(resultados_qdrant)} resultados.")
    for hit in resultados_qdrant:
        if not hasattr(hit, 'payload') or not isinstance(hit.payload, dict): continue
        p = hit.payload; n = p.get('nombre','P'); pr_s = p.get('precio_str',''); d = p.get('descripcion',''); c = p.get('categoria_qdrant',''); u = p.get('unidad',''); m = p.get('moneda','')
        parts = [f"Nombre: {n}"];_=[parts.append(f"Categoría: {c}") if c else None];
        if pr_s: parts.append(f"Precio: {m} {pr_s}".strip() if m else pr_s)
        elif p.get("precio_float") is not None: parts.append(f"Precio: {m} {p.get('precio_float'):.2f}".strip() if m else f"{p.get('precio_float'):.2f}")
        else: parts.append("Precio: Consultar")
        if u: parts.append(f"Presentación: {u}")
        dl=limpiar_texto_base(d);nl=limpiar_texto_base(n)
        if dl and dl!=nl: parts.append(f"Descripción: {(d[:100]+'...') if len(d)>100 else d}")
        contexto_items.append("- " + "\n  - ".join(parts)) 
    if not contexto_items: logger.info("[QDRANT FORMAT] Ningún ítem formateado."); return "" 
    return "Según nuestro catálogo, esto podría interesarte:\n" + "\n\n".join(contexto_items)
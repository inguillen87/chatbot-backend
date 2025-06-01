# services/qdrant_search.py
from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos 
import logging
# IMPORTANTE: Asegurar que qdrant_models esté disponible para Filter, FieldCondition, etc.
from qdrant_client.http import models as qdrant_models # Ajusta esta línea según tu versión de qdrant-client

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(user_id: int, pregunta: str, limite: int = 3, score_min: float = 0.68) -> list:
    qdrant_client = get_qdrant_client() # Nombre de variable cambiado para claridad
    if not qdrant_client:
        logger.error("[QDRANT SEARCH] No se pudo obtener el cliente de Qdrant.")
        return []

    try:
        pregunta_limpia = pregunta.strip() # limpiar_texto_base podría ser útil aquí también
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] La pregunta para búsqueda semántica está vacía.")
            return []

        vector_pregunta_lista = embed_textos([pregunta_limpia]) 
        if not vector_pregunta_lista or not vector_pregunta_lista[0] or not isinstance(vector_pregunta_lista[0], list):
            logger.error(f"[QDRANT SEARCH] No se pudo generar vector para la pregunta: '{pregunta_limpia}'")
            return []
        vector_q = vector_pregunta_lista[0]
    except Exception as e_embed:
        logger.error(f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
        return []

    try:
        logger.info(f"[QDRANT SEARCH] Buscando para user_id {user_id}, pregunta '{pregunta_limpia}', límite={limite}, umbral={score_min}")
        
        search_filter = qdrant_models.Filter( # CORREGIDO: Usa el alias qdrant_models
            must=[
                qdrant_models.FieldCondition( # CORREGIDO
                    key="user_id", 
                    match=qdrant_models.MatchValue(value=user_id) # CORREGIDO
                )
            ]
        )
        
        resultados = qdrant_client.search(
            collection_name="catalogos",
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min 
        )
        logger.info(f"[QDRANT SEARCH] Búsqueda para user_id {user_id}, pregunta '{pregunta_limpia}': {len(resultados)} hits con score >= {score_min}.")
        return resultados # Devuelve la lista de ScoredPoint
    except NameError as ne: # Específicamente para el error de qdrant_models si la importación falló
        logger.error(f"[QDRANT SEARCH] NameError durante búsqueda en Qdrant (probable problema de importación de qdrant_models): {ne}", exc_info=True)
        return[]
    except Exception as e_qdrant:
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []


def armar_respuesta_legible(resultados_qdrant: list) -> str: # resultados_qdrant es list[ScoredPoint]
    # ... (Tu función armar_respuesta_legible, que ya estaba bastante bien) ...
    # (Asegúrate que use logger.info, logger.debug como en mis ejemplos anteriores si quieres logs aquí)
    if not resultados_qdrant: return "" 
    contexto_items = []
    logger.info(f"[QDRANT FORMAT] Formateando {len(resultados_qdrant)} resultados de Qdrant.")
    for hit_idx, hit in enumerate(resultados_qdrant):
        payload = hit.payload 
        if not isinstance(payload, dict): continue
        nombre = payload.get('nombre', 'Producto'); precio_str = payload.get('precio_str', ''); descripcion = payload.get('descripcion', ''); categoria = payload.get('categoria_qdrant', ''); unidad = payload.get('unidad', ''); moneda = payload.get('moneda', '')
        item_info_parts = [f"Nombre: {nombre}"]
        if categoria: item_info_parts.append(f"Categoría: {categoria}")
        if precio_str: item_info_parts.append(f"Precio: {moneda} {precio_str}".strip())
        else: item_info_parts.append("Precio: Consultar")
        if unidad: item_info_parts.append(f"Presentación/Unidad: {unidad}")
        if descripcion and limpiar_texto_base(descripcion) != limpiar_texto_base(nombre): item_info_parts.append(f"Descripción: {(descripcion[:100] + '...') if len(descripcion) > 100 else descripcion}")
        contexto_items.append("- " + "\n  - ".join(item_info_parts)) 
    if not contexto_items: return "" 
    contexto_final = "Información relevante de nuestro catálogo para tu consulta:\n" + "\n\n".join(contexto_items)
    logger.info(f"[QDRANT FORMAT] Contexto para LLM (parcial): {contexto_final[:300]}...")
    return contexto_final
# services/qdrant_search.py
import logging
from typing import List, Optional 
from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos 
# IMPORTACIÓN CORREGIDA/AÑADIDA:
# Para qdrant-client >= 1.1.0, los modelos suelen estar en qdrant_client.http.models
# Si usas una versión anterior, podría ser directamente from qdrant_client import models as qdrant_models
from qdrant_client.http import models as qdrant_models
# O importar específicamente si solo necesitas algunos:
# from qdrant_client.http.models import Filter, FieldCondition, MatchValue, ScoredPoint

from .utils import limpiar_texto_base 

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(user_id: int, pregunta: str, limite: int = 3, score_min: float = 0.68) -> List[qdrant_models.ScoredPoint]: # Ajusta el tipo de retorno si es necesario
    qdrant_cli = get_qdrant_client() 
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener el cliente de Qdrant.")
        return []

    vector_q: Optional[List[float]] = None
    try:
        pregunta_limpia = limpiar_texto_base(pregunta.strip())
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] La pregunta para búsqueda semántica está vacía después de limpiar.")
            return []

        vector_pregunta_lista = embed_textos([pregunta_limpia], input_type="search_query") # search_query para preguntas
        if not vector_pregunta_lista or not vector_pregunta_lista[0] or not isinstance(vector_pregunta_lista[0], list):
            logger.error(f"[QDRANT SEARCH] No se pudo generar vector para la pregunta: '{pregunta_limpia}'")
            return []
        vector_q = vector_pregunta_lista[0]
    except Exception as e_embed:
        logger.error(f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
        return []
    
    if vector_q is None: # Chequeo adicional
         logger.error(f"[QDRANT SEARCH] El vector de la pregunta es None para '{pregunta_limpia}'. No se puede buscar.")
         return []

    try:
        logger.info(f"[QDRANT SEARCH] Buscando para user_id {user_id}, pregunta '{pregunta_limpia}', límite={limite}, umbral={score_min}")
        
        search_filter = qdrant_models.Filter( 
            must=[
                qdrant_models.FieldCondition(
                    key="user_id", 
                    match=qdrant_models.MatchValue(value=user_id)
                )
            ]
        )
        
        resultados = qdrant_cli.search(
            collection_name="catalogos",
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min 
        )
        logger.info(f"[QDRANT SEARCH] Búsqueda para user_id {user_id}, pregunta '{pregunta_limpia}': {len(resultados)} hits con score >= {score_min}.")
        return resultados
    except Exception as e_qdrant:
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []

def armar_respuesta_legible(resultados_qdrant: List[qdrant_models.ScoredPoint]) -> str:
    # ... (Tu función armar_respuesta_legible como la tenías, ya estaba bastante bien) ...
    # Solo asegúrate de que limpiar_texto_base esté disponible si lo usas aquí también.
    if not resultados_qdrant: 
        logger.info("[QDRANT FORMAT] No hay resultados de Qdrant para formatear.")
        return "" 
    contexto_items = []
    logger.info(f"[QDRANT FORMAT] Formateando {len(resultados_qdrant)} resultados de Qdrant.")
    for hit_idx, hit in enumerate(resultados_qdrant):
        if not hasattr(hit, 'payload') or not isinstance(hit.payload, dict):
            logger.warning(f"[QDRANT FORMAT] Hit {hit_idx} (ID: {getattr(hit, 'id', 'N/A')}) no tiene payload o no es dict.")
            continue
        payload = hit.payload; nombre = payload.get('nombre', 'Producto'); precio_str = payload.get('precio_str', ''); descripcion = payload.get('descripcion', ''); categoria = payload.get('categoria_qdrant', ''); unidad = payload.get('unidad', ''); moneda = payload.get('moneda', '')
        item_info_parts = [f"Nombre: {nombre}"]
        if categoria: item_info_parts.append(f"Categoría: {categoria}")
        if precio_str: precio_display = f"{moneda} {precio_str}".strip() if moneda else precio_str; item_info_parts.append(f"Precio: {precio_display}")
        elif payload.get("precio_float") is not None: precio_display = f"{moneda} {payload.get('precio_float'):.2f}".strip() if moneda else f"{payload.get('precio_float'):.2f}"; item_info_parts.append(f"Precio: {precio_display}")
        else: item_info_parts.append("Precio: Consultar")
        if unidad: item_info_parts.append(f"Presentación/Unidad: {unidad}")
        # Asumimos que limpiar_texto_base está disponible
        desc_limpia_para_comparar = limpiar_texto_base(descripcion); nombre_limpio_para_comparar = limpiar_texto_base(nombre)
        if desc_limpia_para_comparar and desc_limpia_para_comparar != nombre_limpio_para_comparar:
            desc_corta = (descripcion[:100] + '...') if len(descripcion) > 100 else descripcion
            item_info_parts.append(f"Descripción: {desc_corta}")
        contexto_items.append("- " + "\n  - ".join(item_info_parts)) 
    if not contexto_items: logger.info("[QDRANT FORMAT] Ningún ítem de Qdrant formateado."); return "" 
    contexto_final = "Según nuestro catálogo, esto podría interesarte:\n" + "\n\n".join(contexto_items)
    logger.info(f"[QDRANT FORMAT] Contexto para LLM (parcial): {contexto_final[:300]}...")
    return contexto_final
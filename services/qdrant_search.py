# services/qdrant_search.py
from .qdrant_utils import get_qdrant_client
from .cohere_ai import embed_textos 
import logging
# IMPORTACIÓN CORREGIDA/AÑADIDA:
from qdrant_client.http import models as qdrant_models # Para Filter, FieldCondition, etc.
# O si tu versión de qdrant-client es más antigua y esto no funciona, podría ser:
# from qdrant_client import models as qdrant_models
# O importar específicamente:
# from qdrant_client.http.models import Filter, FieldCondition, MatchValue (si es una versión muy nueva)
# Revisa la documentación de tu versión de qdrant-client para la importación correcta de estos.

logger = logging.getLogger(__name__)

# Asumo que tienes limpiar_texto_base en utils.py, aunque no se usa directamente aquí,
# armar_respuesta_legible podría beneficiarse de ello.
from .utils import limpiar_texto_base

def buscar_catalogo_qdrant(user_id: int, pregunta: str, limite: int = 3, score_min: float = 0.68) -> list: # Devuelve lista de ScoredPoint
    qdrant_cli = get_qdrant_client() # Renombrado para evitar colisión con el módulo importado
    if not qdrant_cli:
        logger.error("[QDRANT SEARCH] No se pudo obtener el cliente de Qdrant.")
        return []

    try:
        pregunta_limpia = limpiar_texto_base(pregunta.strip()) # Limpiar pregunta
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] La pregunta para búsqueda semántica está vacía después de limpiar.")
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
        
        search_filter = qdrant_models.Filter( 
            must=[
                qdrant_models.FieldCondition(
                    key="user_id", 
                    match=qdrant_models.MatchValue(value=user_id)
                )
            ]
        )
        
        # El método search ahora se llama directamente en el cliente
        resultados = qdrant_cli.search(
            collection_name="catalogos",
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min 
        )
        logger.info(f"[QDRANT SEARCH] Búsqueda para user_id {user_id}, pregunta '{pregunta_limpia}': {len(resultados)} hits con score >= {score_min}.")
        return resultados # Devuelve la lista de qdrant_models.ScoredPoint
    except NameError as ne: 
        logger.error(f"[QDRANT SEARCH] NameError en Qdrant (verificar import de qdrant_models): {ne}", exc_info=True)
        return[]
    except Exception as e_qdrant:
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []


def armar_respuesta_legible(resultados_qdrant: list) -> str: # resultados_qdrant es list[qdrant_models.ScoredPoint]
    if not resultados_qdrant: 
        logger.info("[QDRANT FORMAT] No hay resultados de Qdrant para formatear.")
        return "" 
    
    contexto_items = []
    logger.info(f"[QDRANT FORMAT] Formateando {len(resultados_qdrant)} resultados de Qdrant.")

    for hit_idx, hit in enumerate(resultados_qdrant):
        if not hasattr(hit, 'payload') or not isinstance(hit.payload, dict):
            logger.warning(f"[QDRANT FORMAT] Hit {hit_idx} (ID: {getattr(hit, 'id', 'N/A')}) no tiene payload o no es dict.")
            continue
            
        payload = hit.payload
        nombre = payload.get('nombre', 'Producto') 
        precio_str = payload.get('precio_str', '') 
        descripcion = payload.get('descripcion', '')
        categoria = payload.get('categoria_qdrant', '') 
        unidad = payload.get('unidad', '')
        moneda = payload.get('moneda', '') # Mostrar solo si está presente
        
        item_info_parts = [f"Nombre: {nombre}"]
        if categoria: item_info_parts.append(f"Categoría: {categoria}")
        
        if precio_str:
            precio_display = f"{moneda} {precio_str}".strip() if moneda else precio_str
            item_info_parts.append(f"Precio: {precio_display}")
        else: 
            item_info_parts.append("Precio: Consultar")
        
        if unidad: item_info_parts.append(f"Presentación/Unidad: {unidad}")
        
        desc_limpia_para_comparar = limpiar_texto_base(descripcion)
        nombre_limpio_para_comparar = limpiar_texto_base(nombre)
        if desc_limpia_para_comparar and desc_limpia_para_comparar != nombre_limpio_para_comparar:
            desc_corta = (descripcion[:100] + '...') if len(descripcion) > 100 else descripcion
            item_info_parts.append(f"Descripción: {desc_corta}")
        
        contexto_items.append("- " + "\n  - ".join(item_info_parts)) 
    
    if not contexto_items:
        logger.info("[QDRANT FORMAT] Ningún ítem de Qdrant fue formateado válidamente.")
        return "" 
        
    contexto_final = "Según nuestro catálogo, esto podría interesarte:\n" + "\n\n".join(contexto_items) # Cambiado el prefijo
    logger.info(f"[QDRANT FORMAT] Contexto para LLM (parcial): {contexto_final[:300]}...")
    return contexto_final
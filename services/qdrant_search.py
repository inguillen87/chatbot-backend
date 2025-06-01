# services/qdrant_search.py
from .qdrant_utils import get_qdrant_client # Usar . para import relativo si están en el mismo paquete
from .cohere_ai import embed_textos 
import logging

logger = logging.getLogger(__name__)

def buscar_catalogo_qdrant(user_id: int, pregunta: str, limite: int = 3, score_min: float = 0.65) -> list: # Límite 3, score_min 0.65
    """
    Busca en Qdrant y devuelve una lista de ScoredPoint (objetos de Qdrant).
    Ajustado el score_min por defecto.
    """
    qdrant_client = get_qdrant_client()
    if not qdrant_client:
        logger.error("[QDRANT SEARCH] No se pudo obtener el cliente de Qdrant.")
        return []

    try:
        # Limpiar y validar pregunta antes de generar embedding
        pregunta_limpia = pregunta.strip()
        if not pregunta_limpia:
            logger.warning("[QDRANT SEARCH] La pregunta para búsqueda semántica está vacía.")
            return []

        vector_pregunta_lista = embed_textos([pregunta_limpia]) 
        if not vector_pregunta_lista or not vector_pregunta_lista[0]:
            logger.error(f"[QDRANT SEARCH] No se pudo generar vector para la pregunta: '{pregunta_limpia}'")
            return []
        vector_q = vector_pregunta_lista[0] # query_vector
    except Exception as e_embed:
        logger.error(f"[QDRANT SEARCH] Error generando embedding para pregunta '{pregunta_limpia}': {e_embed}", exc_info=True)
        return []

    try:
        logger.info(f"[QDRANT SEARCH] Buscando para user_id {user_id}, pregunta '{pregunta_limpia}', límite={limite}, umbral={score_min}")
        # query_filter ahora es una lista de condiciones, incluso si solo hay una.
        # Asegúrate que 'user_id' en el payload de Qdrant sea numérico si lo comparas como número.
        search_filter = qdrant_models.Filter(
            must=[
                qdrant_models.FieldCondition(
                    key="user_id", # Campo en el payload de Qdrant
                    match=qdrant_models.MatchValue(value=user_id)
                )
            ]
        )
        
        resultados = qdrant_client.search(
            collection_name="catalogos", # Nombre de tu colección
            query_vector=vector_q,
            query_filter=search_filter,
            limit=limite,
            score_threshold=score_min # Solo resultados que superen este umbral
        )
        logger.info(f"[QDRANT SEARCH] Búsqueda para user_id {user_id}, pregunta '{pregunta_limpia}': {len(resultados)} hits con score >= {score_min}.")
        return resultados
    except Exception as e_qdrant:
        # Log detallado del error de Qdrant
        logger.error(f"[QDRANT SEARCH] Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta_limpia}': {e_qdrant}", exc_info=True)
        return []


def armar_respuesta_legible(resultados_qdrant: list) -> str:
    """
    Construye una cadena de texto legible a partir de los resultados de Qdrant.
    """
    if not resultados_qdrant:
        return "" 
    
    contexto_items = []
    logger.info(f"[QDRANT FORMAT] Formateando {len(resultados_qdrant)} resultados de Qdrant para el contexto del LLM.")

    for hit_idx, hit in enumerate(resultados_qdrant): # hit es un qdrant_models.ScoredPoint
        payload = hit.payload 
        if not isinstance(payload, dict): # Chequeo de seguridad
            logger.warning(f"[QDRANT FORMAT] Hit {hit_idx} (ID: {hit.id}, Score: {hit.score:.3f}) tiene un payload inválido o no es un diccionario.")
            continue

        nombre = payload.get('nombre', 'Producto') 
        precio_str = payload.get('precio_str', '') 
        descripcion = payload.get('descripcion', '')
        categoria = payload.get('categoria_qdrant', '') 
        unidad = payload.get('unidad', '')
        moneda = payload.get('moneda', '') # No poner ARS por defecto aquí, que se muestre si está en el payload
        # codigo_articulo = payload.get('codigo_articulo', '') # Lo tenías en google_docai, asegúrate que esté en el payload de qdrant si lo necesitas

        item_info_parts = [f"Nombre: {nombre}"]
        if categoria: item_info_parts.append(f"Categoría: {categoria}")
        
        if precio_str:
            precio_display = f"{moneda} {precio_str}".strip() if moneda else precio_str
            item_info_parts.append(f"Precio: {precio_display}")
        else: # Si no hay precio_str, indicar consulta
            item_info_parts.append("Precio: Consultar")
        
        if unidad: item_info_parts.append(f"Presentación/Unidad: {unidad}")
        
        if descripcion and limpiar_texto_base(descripcion) != limpiar_texto_base(nombre): # Evitar redundancia y usar limpiar_texto_base
            desc_corta = (descripcion[:100] + '...') if len(descripcion) > 100 else descripcion # Acortar descripción larga
            item_info_parts.append(f"Descripción: {desc_corta}")
        
        contexto_items.append("- " + "\n  - ".join(item_info_parts)) 
    
    if not contexto_items:
        logger.info("[QDRANT FORMAT] Ningún ítem de Qdrant fue formateado válidamente para el contexto.")
        return "" 
        
    contexto_final = "Información relevante de nuestro catálogo para tu consulta:\n" + "\n\n".join(contexto_items)
    logger.info(f"[QDRANT FORMAT] Contexto de catálogo formateado para LLM (primeros 300 chars): {contexto_final[:300]}...")
    return contexto_final
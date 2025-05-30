# En tu archivo: services/qdrant_search.py

from services.qdrant_utils import get_qdrant_client
from services.cohere_ai import embed_textos # Asumo que embed_textos sigue aquí
import logging
# Asegúrate de tener limpiar_texto_base si lo usas aquí (actualmente no se usa)
# from .utils import limpiar_texto_base # Si la necesitas para alguna limpieza adicional

def buscar_catalogo_qdrant(user_id: int, pregunta: str, limite: int = 5, score_min: float = 0.70) -> list:
    """
    Busca en Qdrant y devuelve una lista de ScoredPoint (objetos de Qdrant).
    Ajustado el score_min por defecto a 0.70 para mayor relevancia inicial.
    """
    qdrant = get_qdrant_client()
    try:
        vector_pregunta_lista = embed_textos([pregunta]) # embed_textos devuelve una lista de vectores
        if not vector_pregunta_lista or not vector_pregunta_lista[0]:
            logging.error(f"[QDRANT] No se pudo generar vector para la pregunta: '{pregunta}'")
            return []
        vector = vector_pregunta_lista[0]
    except Exception as e_embed:
        logging.error(f"[QDRANT] Error generando embedding para pregunta '{pregunta}': {e_embed}", exc_info=True)
        return []

    try:
        resultados = qdrant.search(
            collection_name="catalogos",
            query_vector=vector,
            query_filter={"must": [{"key": "user_id", "match": {"value": user_id}}]},
            limit=limite,
            score_threshold=score_min
        )
        logging.info(f"[QDRANT] Búsqueda para user_id {user_id}, pregunta '{pregunta}': {len(resultados)} hits con score >= {score_min}.")
        return resultados
    except Exception as e_qdrant:
        logging.error(f"[QDRANT] Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta}': {e_qdrant}", exc_info=True)
        return []


def armar_respuesta_legible(resultados_qdrant: list) -> str:
    """
    Construye una cadena de texto legible a partir de los resultados de Qdrant (lista de ScoredPoint),
    utilizando los campos estructurados del payload.
    """
    if not resultados_qdrant:
        logging.info("[QDRANT] No se encontraron resultados en Qdrant para armar respuesta.")
        return "" 
    
    contexto_items = []
    logging.info(f"[QDRANT] Formateando {len(resultados_qdrant)} resultados de Qdrant para el contexto del LLM.")

    for hit_idx, hit in enumerate(resultados_qdrant): # hit es un ScoredPoint
        payload = hit.payload 
        if not payload:
            logging.warning(f"[QDRANT] Hit {hit_idx} (ID: {hit.id}, Score: {hit.score:.3f}) no tiene payload.")
            continue

        nombre = payload.get('nombre', 'Producto') # Default más corto
        precio_str = payload.get('precio_str', '') 
        descripcion = payload.get('descripcion', '')
        categoria = payload.get('categoria_qdrant', '') # El nombre que usamos en el payload
        unidad = payload.get('unidad', '')
        moneda = payload.get('moneda', 'ARS') # Default ARS si no está
        codigo_articulo = payload.get('codigo_articulo', '')

        # Construir la información del item
        item_info_parts = []
        item_info_parts.append(f"Nombre: {nombre}")
        if codigo_articulo:
            item_info_parts.append(f"Código: {codigo_articulo}")
        if categoria:
            item_info_parts.append(f"Categoría: {categoria}")
        
        if precio_str:
            item_info_parts.append(f"Precio: {precio_str} {moneda if moneda else ''}".strip())
        else:
            item_info_parts.append("Precio: Consultar")
        
        if unidad:
            item_info_parts.append(f"Presentación/Unidad: {unidad}")
        
        # Añadir descripción solo si es diferente del nombre y no muy larga
        # y si es informativa.
        descripcion_limpia = ""
        if descripcion and descripcion.lower() != nombre.lower():
            descripcion_limpia = (descripcion[:120] + '...') if len(descripcion) > 120 else descripcion
            item_info_parts.append(f"Descripción: {descripcion_limpia}")
        
        # Loguear el payload completo para depuración si es necesario
        # logging.debug(f"[QDRANT] Payload del Hit {hit_idx+1}: {payload}")

        contexto_items.append("  " + "\n  ".join(item_info_parts)) # Indentar cada item
    
    if not contexto_items:
        logging.info("[QDRANT] Ningún item de Qdrant fue formateado válidamente.")
        return "" 
        
    contexto_final = "Del catálogo, esto podría ser relevante para tu consulta:\n" + "\n---\n".join(contexto_items)
    logging.info(f"[QDRANT] Contexto de catálogo formateado para LLM (primeros 300 chars): {contexto_final[:300]}...")
    return contexto_final
# En tu archivo: services/qdrant_search.py

from services.qdrant_utils import get_qdrant_client
from services.cohere_ai import embed_textos # Asumo que embed_textos sigue aquí
import logging

def buscar_catalogo_qdrant(user_id, pregunta, limite=5, score_min=0.25): # Aumentado el límite a 5 por defecto
    qdrant = get_qdrant_client()
    # Es importante que el embedding de la pregunta use un modelo compatible
    # con los embeddings de tus documentos en Qdrant.
    # Si embed_textos() es para el modelo 'embed-multilingual-v3.0', está bien.
    try:
        vector_pregunta = embed_textos([pregunta])
        if not vector_pregunta or not vector_pregunta[0]:
            logging.error(f"No se pudo generar el vector de embedding para la pregunta: '{pregunta}'")
            return []
        vector = vector_pregunta[0]
    except Exception as e_embed:
        logging.error(f"Error generando embedding para la pregunta '{pregunta}': {e_embed}", exc_info=True)
        return []

    try:
        resultados = qdrant.search(
            collection_name="catalogos", # Asegúrate que este es el nombre de tu colección
            query_vector=vector,
            query_filter={"must": [{"key": "user_id", "match": {"value": user_id}}]},
            limit=limite,
            score_threshold=score_min # Usar score_threshold en lugar de filtrar después
        )
        # 'resultados' ahora es una lista de qdrant_client.http.models.ScoredPoint
        # No necesitamos filtrar por score aquí si usamos score_threshold.
        logging.info(f"Qdrant resultados (Hits) para user_id {user_id}, pregunta '{pregunta}': {len(resultados)} hits.")
        # Devolvemos los payloads directamente para que armar_respuesta_legible los procese.
        # O podrías extraer aquí lo que necesitas si prefieres.
        return resultados # Devolvemos la lista de ScoredPoint directamente

    except Exception as e_qdrant:
        logging.error(f"Error buscando en Qdrant para user_id {user_id}, pregunta '{pregunta}': {e_qdrant}", exc_info=True)
        return []


def armar_respuesta_legible(resultados_qdrant: list) -> str:
    """
    Construye una cadena de texto legible a partir de los resultados de Qdrant,
    utilizando los campos estructurados del payload.
    Resultados_qdrant es una lista de ScoredPoint.
    """
    if not resultados_qdrant:
        # Devolver una cadena vacía es mejor que un mensaje, porque en logic.py
        # verificamos si contexto_catalogo está vacío para decidir si se añade al prompt.
        return "" 
    
    contexto_items = []
    logging.info(f"Formateando {len(resultados_qdrant)} resultados de Qdrant para el contexto del LLM.")

    for hit_idx, hit in enumerate(resultados_qdrant):
        payload = hit.payload # El payload es un diccionario
        if not payload:
            logging.warning(f"Hit {hit_idx} de Qdrant no tiene payload. ID del punto: {hit.id}")
            continue

        # Extraer campos estructurados del payload con fallbacks
        nombre = payload.get('nombre', 'Producto sin nombre especificado')
        precio_str = payload.get('precio_str', '') # Usar precio_str que debería estar formateado
        # precio_float = payload.get('precio_float') # Podrías usarlo para validaciones o lógica aquí si es necesario
        descripcion = payload.get('descripcion', '')
        categoria = payload.get('categoria_qdrant', '') # El nombre que usamos en el payload
        unidad = payload.get('unidad', '')
        # texto_original = payload.get('texto_original_para_embedding', '') # Para depuración o si es útil

        item_info = f"Item {hit_idx + 1}:\n"
        item_info += f"  Nombre: {nombre}\n"
        if precio_str:
            item_info += f"  Precio: {precio_str}\n"
        else:
            item_info += "  Precio: Consultar\n" # Si no hay precio_str
        
        if categoria:
            item_info += f"  Categoría: {categoria}\n"
        if unidad:
            item_info += f"  Presentación/Unidad: {unidad}\n"
        if descripcion and descripcion != nombre: # Evitar descripción redundante si es igual al nombre
            # Limitar la longitud de la descripción para no hacer el contexto demasiado largo
            descripcion_corta = (descripcion[:150] + '...') if len(descripcion) > 150 else descripcion
            item_info += f"  Descripción: {descripcion_corta}\n"
        
        contexto_items.append(item_info.strip())
    
    if not contexto_items:
        return "" # Si después de procesar, ningún item fue válido
        
    # Unir los items con un separador claro
    contexto_final = "Basado en tu consulta, encontré esto en nuestro catálogo que podría interesarte:\n---\n" + "\n---\n".join(contexto_items)
    logging.info(f"Contexto de catálogo formateado para LLM (primeros 300 chars): {contexto_final[:300]}...")
    return contexto_final
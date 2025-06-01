# services/cohere_ai.py
import os
import logging
# import requests # Ya no se usa requests aquí si usamos el SDK para todo
# from time import sleep # Ya no se usa sleep aquí si usamos el SDK para todo
import cohere

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 

def embed_textos(textos: list[str], input_type: str = "search_document") -> list[list[float]]:
    if not COHERE_API_KEY:
        logger.error("[COHERE EMBED] COHERE_API_KEY no está configurada.")
        return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida (debe ser lista de strings).")
        return []

    all_embeddings = []
    # Modelo actualizado a uno que es bueno para búsqueda y clustering, y multilingüe
    model = "embed-multilingual-v3.0" 
    
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch size: {COHERE_EMBED_BATCH_SIZE}")

    try:
        # Inicializar cliente dentro de la función o globalmente si se manejan excepciones de inicialización
        co_client = cohere.Client(COHERE_API_KEY, timeout=30) # Timeout para el cliente
        
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch_textos = textos[i:i + COHERE_EMBED_BATCH_SIZE]
            logger.info(f"➡️ [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1}/{ (len(textos) -1)//COHERE_EMBED_BATCH_SIZE + 1 } ({len(batch_textos)} textos).")
            
            response = co_client.embed(
                texts=batch_textos,
                model=model,
                input_type=input_type,
                # embedding_types=['float'] # Opcional: asegurar el tipo de embedding
                truncate="END" # Truncar textos largos en lugar de fallar
            )
            
            if response.embeddings: # response.embeddings ya es la lista de vectores
                all_embeddings.extend(response.embeddings)
                logger.info(f"⬅️ [COHERE EMBED] Embeddings recibidos para el batch: {len(response.embeddings)}")
            else:
                logger.error(f"❌ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH_SIZE + 1} no devolvió embeddings.")

            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): 
                sleep(0.2) 

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE EMBED] Error de API Cohere: Status {e_api.http_status} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
    except Exception as e_general:
        logger.error(f"❌ [COHERE EMBED] Error genérico generando embeddings: {e_general}", exc_info=True)
        return [] 

    if len(all_embeddings) != len(textos):
        logger.warning(f"⚠️ [COHERE EMBED] Discrepancia: {len(textos)} textos enviados, {len(all_embeddings)} embeddings recibidos.")

    logger.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)}.")
    return all_embeddings


def get_cohere_response(current_message: str, chat_history_for_api: list, system_prompt_for_api: str, rubro_id: int, user_context: dict) -> str:
    """
    Obtiene una respuesta de chat de la API de Cohere usando el SDK oficial.
    Usa 'preamble' para el system_prompt.
    """
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response para pregunta: '{current_message[:50]}...'")
    logger.debug(f"[COHERE CHAT] Historial para API: {chat_history_for_api}")
    logger.debug(f"[COHERE CHAT] Preamble (System Prompt) (primeros 200 chars): {system_prompt_for_api[:200]}...")


    if not COHERE_API_KEY:
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return ""
    if not current_message or not isinstance(current_message, str):
        logger.error("❌ [COHERE CHAT] Mensaje actual del usuario está vacío o no es string.")
        return ""

    try:
        # Es mejor crear el cliente una vez y reutilizarlo si es posible (ej. en app context o global con lock),
        # pero para simplicidad aquí se crea en cada llamada. Considerar optimizar si hay muchas llamadas.
        co_client = cohere.Client(COHERE_API_KEY, timeout=45) # Timeout para la llamada de chat
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar el cliente de Cohere: {e_client}", exc_info=True)
        return ""

    try:
        # Documentos relevantes del user_context (si los tienes y quieres usarlos con RAG de Cohere)
        # documents_for_rag = []
        # if user_context.get("contexto_catalogo_items"):
        #     for item in user_context["contexto_catalogo_items"]: # Suponiendo que es una lista de dicts
        #         documents_for_rag.append({"title": item.get("nombre",""), "snippet": item.get("descripcion","")})

        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: 'command-r-plus'. Mensaje: '{current_message[:70]}'. Historial: {len(chat_history_for_api)}. Preamble usado.")
        
        response = co_client.chat(
            message=current_message,
            chat_history=chat_history_for_api if chat_history_for_api else None, 
            preamble=system_prompt_for_api if system_prompt_for_api else None,
            model="command-r-plus", # Modelo potente y reciente
            temperature=0.3, # Ligeramente reducido para respuestas más enfocadas y consistentes
            # connectors=[{"id": "web-search"}] # Descomentar si quieres habilitar búsqueda web (RAG)
            # documents=documents_for_rag # Para pasar documentos específicos para RAG
        )

        respuesta_texto = response.text.strip() if response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        logger.debug(f"[COHERE CHAT] Respuesta API COMPLETA: {response}") # Loguear el objeto de respuesta completo para más detalles si es necesario
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {e_api.http_status} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if e_api.http_status == 429: # Rate limit
            return "Disculpa, estamos experimentando mucho tráfico en nuestro asistente IA. Por favor, intenta en unos momentos."
        return "Lo siento, no pude procesar tu solicitud en este momento debido a un problema con el asistente de inteligencia artificial."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta."
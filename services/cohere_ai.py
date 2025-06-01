# services/cohere_ai.py
import os
import logging
import requests
from time import sleep
import cohere # SDK oficial de Cohere

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 # Cohere recomienda hasta 96 para embed-multilingual-v3.0

def embed_textos(textos: list[str], input_type: str = "search_document") -> list[list[float]]:
    """
    Genera embeddings para una lista de textos usando la API de Cohere.
    input_type puede ser "search_document" o "search_query".
    """
    if not COHERE_API_KEY:
        logger.error("[COHERE EMBED] COHERE_API_KEY no está configurada.")
        return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida (debe ser lista de strings).")
        return []

    all_embeddings = []
    model = "embed-multilingual-v3.0" # Modelo recomendado para multilingüe y alta performance
    
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch size: {COHERE_EMBED_BATCH_SIZE}")

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30) # Timeout para el cliente
        
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch_textos = textos[i:i + COHERE_EMBED_BATCH_SIZE]
            logger.info(f"➡️ [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1}/{ (len(textos) -1)//COHERE_EMBED_BATCH_SIZE + 1 } ({len(batch_textos)} textos).")
            
            response = co_client.embed(
                texts=batch_textos,
                model=model,
                input_type=input_type
            )
            # response.embeddings es una lista de listas de floats
            if response.embeddings:
                all_embeddings.extend(response.embeddings)
                logger.info(f"⬅️ [COHERE EMBED] Embeddings recibidos para el batch: {len(response.embeddings)}")
            else:
                logger.error(f"❌ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH_SIZE + 1} no devolvió embeddings.")

            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): # Evitar sleep innecesario en el último batch
                sleep(0.2) # Pequeña pausa entre batches grandes para no saturar la API

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE EMBED] Error de API Cohere: Status {e_api.http_status} - {e_api.message}", exc_info=False) # exc_info=False para no duplicar info de CohereAPIError
        # Podrías decidir devolver los embeddings parciales o una lista vacía
        # return all_embeddings # o return []
    except Exception as e_general:
        logger.error(f"❌ [COHERE EMBED] Error genérico generando embeddings: {e_general}", exc_info=True)
        return [] # Devolver lista vacía en caso de error no recuperable

    if len(all_embeddings) != len(textos):
        logger.warning(f"⚠️ [COHERE EMBED] Discrepancia: {len(textos)} textos enviados, {len(all_embeddings)} embeddings recibidos.")
        # Podrías decidir qué hacer aquí: ¿lanzar error, devolver parciales?
        # Por ahora, se devuelven los que se pudieron generar.

    logger.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)}.")
    return all_embeddings


def get_cohere_response(messages_for_llm: list, rubro_id: int, user_context: dict) -> str:
    """
    Obtiene una respuesta de chat de la API de Cohere usando el SDK.
    'messages_for_llm' debe ser una lista de diccionarios con 'role' y 'content'.
    El primer mensaje puede ser 'role':'system' para las instrucciones.
    """
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. {len(messages_for_llm)} mensajes en la estructura recibida.")

    if not COHERE_API_KEY: # Re-chequeo por si acaso
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return ""
    if not messages_for_llm:
        logger.warning("[COHERE CHAT] Lista de mensajes para LLM está vacía.")
        return ""

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=45) # Timeout para la llamada de chat
        logger.info("✅ [COHERE CHAT] Cliente de Cohere inicializado.")
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar el cliente de Cohere: {e_client}", exc_info=True)
        return ""

    # Preparar el historial de chat y el mensaje actual según la API de Cohere
    chat_history_api = []
    system_prompt = None
    
    # El primer mensaje es el prompt del sistema
    if messages_for_llm[0]["role"].lower() == "system":
        system_prompt = messages_for_llm[0]["content"]
        # El historial son los mensajes intermedios
        for msg in messages_for_llm[1:-1]:
            api_role = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_api.append({"role": api_role, "message": msg["content"]})
        current_user_message = messages_for_llm[-1]["content"] if messages_for_llm[-1]["role"].lower() == "user" else ""
    else: # Sin prompt de sistema explícito, tomar todo menos el último como historial
        logger.warning("[COHERE CHAT] No se encontró mensaje de 'system' explícito. Usando el primer mensaje del historial (si existe) como system prompt si es necesario, o ninguno.")
        # Si no hay system prompt, puedes optar por no pasar preamble o system_prompt en la llamada a co.chat
        # o construir uno genérico. Por ahora, el sistema de Cohere puede funcionar sin uno explícito
        # pero es mejor tenerlo para guiar al modelo.
        # Esta lógica asume que el prompt del sistema está INCLUIDO en messages_for_llm[0]
        # y el historial es messages_for_llm[1:-1]
        # Si NO hay system prompt, y solo es user/assistant/user, entonces messages_for_llm[0] sería USER.
        # Esto requiere que `messages_for_llm` tenga al menos un mensaje.
        current_user_message = messages_for_llm[-1]["content"] if messages_for_llm[-1]["role"].lower() == "user" else ""
        for msg in messages_for_llm[:-1]: # Todo menos el último es historial
            api_role = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_api.append({"role": api_role, "message": msg["content"]})


    if not current_user_message:
        logger.error("[COHERE CHAT] Mensaje actual del usuario está vacío o el último mensaje no es del usuario. No se llamará a la API.")
        return ""

    try:
        # Modelo 'command-r-plus' es uno de los más nuevos y potentes.
        # 'preamble' es el lugar correcto para las instrucciones generales del sistema.
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: 'command-r-plus'. Mensaje: '{current_user_message[:100]}...'. Historial API: {len(chat_history_api)} mensajes. System Prompt (Preamble) usado.")
        
        response = co_client.chat(
            message=current_user_message,
            chat_history=chat_history_api, 
            preamble=system_prompt if system_prompt else None, # Usar preamble para instrucciones del sistema
            model="command-r-plus", 
            temperature=0.3, # Ligeramente reducido para respuestas más consistentes
            # connectors=[{"id": "web-search"}] # Descomentar si quieres habilitar búsqueda web (RAG)
        )

        respuesta_texto = response.text.strip() if response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): {respuesta_texto[:200]}")
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        # Aquí puedes añadir lógica para reintentos o fallbacks si es un error de cuota o temporal
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {e_api.http_status} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if e_api.http_status == 429: # Rate limit
            return "Disculpa, estamos experimentando mucho tráfico. Por favor, intenta en unos momentos."
        return "Lo siento, no pude procesar tu solicitud en este momento debido a un problema con el asistente IA."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al generar una respuesta."
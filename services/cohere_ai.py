# services/cohere_ai.py
import os
import logging
import cohere # SDK oficial de Cohere
from time import sleep # Para la función embed_textos
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 # Definir esto aquí si embed_textos lo usa

def embed_textos(textos: List[str], input_type: str = "search_document") -> List[List[float]]:
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

    all_embeddings: List[List[float]] = []
    model_embed = "embed-multilingual-v3.0" # Definir variable para modelo de embedding
    
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model_embed}, Tipo input: {input_type}, Batch size: {COHERE_EMBED_BATCH_SIZE}")

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30)
        
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch_textos = textos[i:i + COHERE_EMBED_BATCH_SIZE]
            logger.info(f"➡️ [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1} / { (len(textos) -1)//COHERE_EMBED_BATCH_SIZE + 1 } ({len(batch_textos)} textos).")
            
            response = co_client.embed(
                texts=batch_textos,
                model=model_embed, # Usar la variable model_embed
                input_type=input_type,
                truncate="END" 
            )
            
            if response.embeddings and isinstance(response.embeddings, list):
                valid_batch_embeddings = [emb for emb in response.embeddings if isinstance(emb, list) and all(isinstance(n, (float, int)) for n in emb)]
                all_embeddings.extend(valid_batch_embeddings)
                logger.info(f"⬅️ [COHERE EMBED] Embeddings válidos recibidos para el batch: {len(valid_batch_embeddings)}")
                if len(valid_batch_embeddings) != len(batch_textos):
                    logger.warning(f"⚠️ [COHERE EMBED] Discrepancia en batch: {len(batch_textos)} textos, {len(valid_batch_embeddings)} embeddings válidos.")
            else:
                logger.error(f"❌ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH_SIZE + 1} no devolvió embeddings o no es una lista.")

            # Pequeña pausa entre batches grandes para no saturar la API, solo si hay más batches por venir
            if len(textos) > COHERE_EMBED_BATCH_SIZE and (i + COHERE_EMBED_BATCH_SIZE) < len(textos): 
                sleep(0.3) 

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE EMBED] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
    except Exception as e_general:
        logger.error(f"❌ [COHERE EMBED] Error genérico generando embeddings: {e_general}", exc_info=True)
        return [] # Devolver lista vacía en caso de error no recuperable

    if len(all_embeddings) != len(textos):
        logger.warning(f"⚠️ [COHERE EMBED] Discrepancia final: {len(textos)} textos enviados, {len(all_embeddings)} embeddings generados.")

    logger.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)}.")
    return all_embeddings


def get_cohere_response(message: str, 
                        chat_history: Optional[List[Dict[str, str]]] = None, 
                        preamble: Optional[str] = None, # Este es el system_prompt que viene de logic.py
                        model: str = "command-r-plus",
                        temperature: float = 0.3,
                        # Los siguientes argumentos son pasados desde logic.py pero no usados directamente por co_client.chat
                        # Se mantienen en la firma por si en el futuro quieres usarlos aquí (ej. para logging avanzado o modificar el preamble).
                        rubro_id: Optional[int] = None, 
                        user_context: Optional[Dict[str, Any]] = None 
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{message[:70]}...'")
    
    # Construir el historial para la API de Cohere, incluyendo el preamble (system_prompt)
    # como el primer mensaje con rol "SYSTEM" si está presente.
    chat_history_for_api_call: List[Dict[str, str]] = []
    if preamble and isinstance(preamble, str) and preamble.strip():
        chat_history_for_api_call.append({"role": "SYSTEM", "message": preamble})
        logger.info(f"[COHERE CHAT] Preamble (System Prompt) añadido como primer mensaje al historial para API (primeros 100 chars): {preamble[:100]}...")
    else:
        logger.info("[COHERE CHAT] No se proporcionó Preamble (System Prompt) o estaba vacío.")

    if chat_history: # chat_history ya debe venir en formato [{"role": "USER", "message": "..."}, {"role": "CHATBOT", "message": "..."}]
        chat_history_for_api_call.extend(chat_history)
        logger.info(f"[COHERE CHAT] Historial de conversación previo añadido ({len(chat_history)} mensajes). Total mensajes para API: {len(chat_history_for_api_call)}")
    else:
        logger.info("[COHERE CHAT] No hay historial de conversación previo.")


    if not COHERE_API_KEY:
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return "Error interno: Asistente IA no disponible en este momento (C01)." # Mensaje para el usuario
    if not message or not isinstance(message, str) or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje actual del usuario está vacío o no es string.")
        return "Por favor, escribe una pregunta o consulta más clara." # Mensaje para el usuario

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=60) # Timeout aumentado para llamadas de chat
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar cliente Cohere: {e_client}", exc_info=True)
        return "Error interno: No se pudo inicializar el asistente IA (C02)."

    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: '{model}'. Temperatura: {temperature}. Mensaje a enviar: '{message[:70]}'. Historial completo para API (incl. system prompt): {len(chat_history_for_api_call)} mensajes.")
        
        # LLAMADA CORREGIDA al SDK de Cohere:
        # - El prompt del sistema (preamble) ahora va dentro de chat_history_for_api_call.
        # - No se usa el argumento 'preamble' directamente en co_client.chat().
        response = co_client.chat(
            message=message, # La pregunta actual del usuario
            chat_history=chat_history_for_api_call if chat_history_for_api_call else None, # Puede ser None si es el primer turno y no hay system prompt
            model=model, 
            temperature=temperature,
            # Aquí podrías añadir 'connectors' si usas RAG con búsqueda web, ej:
            # connectors=[{"id": "web-search"}]
        )

        respuesta_texto = response.text.strip() if response and response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        
        # Loguear si hubo documentos citados por el modelo RAG (si usaras conectores)
        # if response and hasattr(response, 'documents') and response.documents:
        #     logger.info(f"[COHERE CHAT] Documentos citados por el modelo: {len(response.documents)}")
        #     # for doc_idx, doc in enumerate(response.documents):
        #     #     logger.debug(f"  Doc {doc_idx+1}: ID: {doc.get('id')}, Snippet: {doc.get('snippet', '')[:100]}...")

        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429: # Rate limit / Overload
            return "Nuestro asistente IA está experimentando una alta demanda. Por favor, intenta nuevamente en unos momentos."
        # Para otros errores de API, dar un mensaje más genérico al usuario.
        return "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01)."
    except TypeError as te: # Capturar específicamente TypeError si la firma de co.chat() cambia
        logger.error(f"❌ [COHERE CHAT] TypeError en la llamada a co_client.chat(): {te}. Verifica los argumentos.", exc_info=True)
        return "Lo siento, hubo un problema técnico con nuestro asistente IA (TE01)."
    except Exception as e_general: # Capturar otros errores inesperados
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02)."
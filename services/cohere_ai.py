# services/cohere_ai.py
import os
import logging
import cohere
from time import sleep # Solo si usas sleep en embed_textos
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
# Define COHERE_EMBED_BATCH_SIZE si no está ya definido globalmente o en config
COHERE_EMBED_BATCH_SIZE = int(os.getenv("COHERE_EMBED_BATCH_SIZE", 90))


def embed_textos(textos: List[str], input_type: str = "search_document") -> List[List[float]]:
    if not COHERE_API_KEY:
        logger.error("[COHERE EMBED] COHERE_API_KEY no está configurada.")
        return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida (debe ser lista de strings).")
        return []

    all_embeddings: List[List[float]] = []
    model = "embed-multilingual-v3.0" 
    
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch size: {COHERE_EMBED_BATCH_SIZE}")

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30) 
        
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch_textos = textos[i:i + COHERE_EMBED_BATCH_SIZE]
            logger.info(f"➡️ [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1}/{ (len(textos) -1)//COHERE_EMBED_BATCH_SIZE + 1 } ({len(batch_textos)} textos).")
            
            response = co_client.embed(
                texts=batch_textos,
                model=model,
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

            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): 
                sleep(0.3) 

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE EMBED] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
    except Exception as e_general:
        logger.error(f"❌ [COHERE EMBED] Error genérico generando embeddings: {e_general}", exc_info=True)
        return [] 

    if len(all_embeddings) != len(textos):
        logger.warning(f"⚠️ [COHERE EMBED] Discrepancia final: {len(textos)} textos enviados, {len(all_embeddings)} embeddings generados.")

    logger.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)}.")
    return all_embeddings


def get_cohere_response(message: str, 
                        chat_history: Optional[List[Dict[str, str]]] = None, 
                        preamble: Optional[str] = None, # Este es el system_prompt que viene de logic.py
                        model: str = "command-r-plus",
                        temperature: float = 0.3,
                        # Estos argumentos se reciben de logic.py pero no se usan directamente en co_client.chat()
                        # Se mantienen por si quieres usarlos para logging más específico o alguna lógica aquí.
                        rubro_id: Optional[int] = None, 
                        user_context: Optional[Dict[str, Any]] = None 
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{message[:70]}...'")
    
    # Construir el historial para la API, incluyendo el preamble (system_prompt) como el primer mensaje.
    chat_history_for_api_call: List[Dict[str, str]] = []
    if preamble and isinstance(preamble, str) and preamble.strip():
        chat_history_for_api_call.append({"role": "SYSTEM", "message": preamble})
        logger.info(f"[COHERE CHAT] Preamble (System Prompt) añadido como primer mensaje al historial para API (primeros 100 chars): {preamble[:100]}...")
    else:
        logger.info("[COHERE CHAT] No se proporcionó Preamble (System Prompt) o estaba vacío.")

    if chat_history: # chat_history ya viene en formato USER/CHATBOT desde logic.py
        chat_history_for_api_call.extend(chat_history)
        logger.info(f"[COHERE CHAT] Historial de conversación previo añadido ({len(chat_history)} mensajes).")
    else:
        logger.info("[COHERE CHAT] No hay historial de conversación previo.")

    if not COHERE_API_KEY:
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return "Error interno: Asistente IA no disponible en este momento (C01)."
    if not message or not isinstance(message, str) or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje actual del usuario está vacío o no es string.")
        return "Por favor, escribe una pregunta o consulta más clara."

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=60) # Timeout más largo para chat
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar cliente Cohere: {e_client}", exc_info=True)
        return "Error interno: No se pudo inicializar el asistente IA (C02)."

    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: '{model}'. Temperatura: {temperature}. Mensaje: '{message[:70]}'. Historial completo para API (incl. system): {len(chat_history_for_api_call)}.")
        
        # LLAMADA CORREGIDA AL SDK DE COHERE:
        # 'preamble' como argumento directo a co_client.chat() no es soportado por todas las versiones/modelos.
        # La forma más compatible es incluir el prompt del sistema como el primer mensaje en 'chat_history'.
        response = co_client.chat(
            message=message, # La pregunta actual del usuario
            chat_history=chat_history_for_api_call if chat_history_for_api_call else None, # El historial que AHORA incluye el SYSTEM prompt
            model=model, 
            temperature=temperature,
            # El argumento 'preamble' se ha eliminado de aquí.
        )

        respuesta_texto = response.text.strip() if response and response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        # logger.debug(f"[COHERE CHAT] Objeto Respuesta API COMPLETA: {response}") # Para depuración muy detallada
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429: # Rate limit / Overload
            return "Nuestro asistente IA está experimentando una alta demanda. Por favor, intenta nuevamente en unos momentos."
        # Para otros errores de API, dar un mensaje más genérico al usuario.
        return "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01)."
    except TypeError as te: # Capturar específicamente el TypeError si persiste por alguna razón
        logger.error(f"❌ [COHERE CHAT] TypeError durante la llamada a co_client.chat(): {te}", exc_info=True)
        return "Lo siento, hubo un problema técnico con el asistente IA (TE01)."
    except Exception as e_general: # Capturar otros errores inesperados
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02)."
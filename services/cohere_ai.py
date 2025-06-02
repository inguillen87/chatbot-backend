# services/cohere_ai.py
import os
import logging
import cohere
from time import sleep
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
# ... (embed_textos como antes, asegúrate que sea la versión funcional) ...
def embed_textos(textos: List[str], input_type: str = "search_document") -> List[List[float]]:
    if not COHERE_API_KEY: logger.error("[COHERE EMBED] COHERE_API_KEY no configurada."); return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos): logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida."); return []
    all_embeddings: List[List[float]] = []; model = "embed-multilingual-v3.0"
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch: {COHERE_EMBED_BATCH_SIZE if 'COHERE_EMBED_BATCH_SIZE' in globals() else 90}")
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30)
        batch_size = COHERE_EMBED_BATCH_SIZE if 'COHERE_EMBED_BATCH_SIZE' in globals() else 90
        for i in range(0, len(textos), batch_size):
            batch = textos[i:i + batch_size]; logger.info(f"➡️ [COHERE EMBED] Batch {i//batch_size + 1} ({len(batch)} textos).")
            response = co_client.embed(texts=batch, model=model, input_type=input_type, truncate="END")
            if response.embeddings and isinstance(response.embeddings, list):
                valid_embs = [e for e in response.embeddings if isinstance(e, list) and all(isinstance(n, (float, int)) for n in e)]
                all_embeddings.extend(valid_embs); logger.info(f"⬅️ [COHERE EMBED] Embeddings válidos batch: {len(valid_embs)}")
                if len(valid_embs) != len(batch): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia batch: {len(batch)} textos, {len(valid_embs)} embeddings.")
            else: logger.error(f"❌ [COHERE EMBED] Batch no devolvió embeddings o no es una lista.")
            if len(textos) > batch_size and i + batch_size < len(textos): sleep(0.3) 
    except cohere.CohereAPIError as e_api: logger.error(f"❌ [COHERE EMBED] API Error: {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
    except Exception as e_general: logger.error(f"❌ [COHERE EMBED] Error genérico embeddings: {e_general}", exc_info=True); return [] 
    if len(all_embeddings) != len(textos): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia final: {len(textos)} textos, {len(all_embeddings)} embeddings.")
    logger.info(f"✅ [COHERE EMBED] Embeddings totales: {len(all_embeddings)}.")
    return all_embeddings

def get_cohere_response(message: str, 
                        chat_history: Optional[List[Dict[str, str]]] = None, 
                        preamble: Optional[str] = None, # Este es el system_prompt
                        model: str = "command-r-plus",
                        temperature: float = 0.3,
                        rubro_id: Optional[int] = None, 
                        user_context: Optional[Dict[str, Any]] = None 
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{message[:70]}...'")
    
    # Construir el historial para la API, incluyendo el preamble como mensaje SYSTEM
    chat_history_for_api_call: List[Dict[str, str]] = []
    if preamble and isinstance(preamble, str) and preamble.strip():
        chat_history_for_api_call.append({"role": "SYSTEM", "message": preamble})
        logger.info(f"[COHERE CHAT] Preamble (System Prompt) añadido como primer mensaje al historial para API (primeros 100 chars): {preamble[:100]}...")
    
    if chat_history: # chat_history ya debería tener roles USER/CHATBOT
        chat_history_for_api_call.extend(chat_history)
        logger.info(f"[COHERE CHAT] Historial de conversación previo añadido ({len(chat_history)} mensajes).")
    else:
        logger.info("[COHERE CHAT] No hay historial de conversación previo.")

    if not COHERE_API_KEY: # ... (resto de la función como te la pasé en la respuesta @‶gANVneHZLGZv...)
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return "Error interno: Asistente IA no disponible en este momento (C01)."
    if not message or not isinstance(message, str) or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje actual del usuario está vacío o no es string.")
        return "Por favor, escribe una pregunta o consulta más clara."
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=60)
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar cliente Cohere: {e_client}", exc_info=True)
        return "Error interno: No se pudo inicializar el asistente IA (C02)."
    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: '{model}'. Temperatura: {temperature}. Mensaje: '{message[:70]}'. Historial completo para API (incl. system): {len(chat_history_for_api_call)}.")
        response = co_client.chat(
            message=message,
            chat_history=chat_history_for_api_call if chat_history_for_api_call else None, 
            model=model, 
            temperature=temperature,
        )
        respuesta_texto = response.text.strip() if response and response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        return respuesta_texto
    except cohere.CohereAPIError as e_api: # ... (manejo de errores como antes)
            logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
            if hasattr(e_api, 'http_status') and e_api.http_status == 429: return "Nuestro asistente IA está experimentando una alta demanda. Por favor, intenta nuevamente en unos momentos."
            return "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01)."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02)."
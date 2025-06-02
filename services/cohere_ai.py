# services/cohere_ai.py
import os
import logging
import cohere
from time import sleep
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 # Asegúrate que esta variable esté definida

def embed_textos(textos: List[str], input_type: str = "search_document") -> List[List[float]]:
    # ... (Tu función embed_textos como la tenías, ya estaba funcionando bien en los logs)
    if not COHERE_API_KEY: logger.error("[COHERE EMBED] COHERE_API_KEY no configurada."); return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos): logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida."); return []
    all_embeddings: List[List[float]] = []; model_embed = "embed-multilingual-v3.0"
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model_embed}, Tipo input: {input_type}, Batch: {COHERE_EMBED_BATCH_SIZE}")
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30)
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch = textos[i:i + COHERE_EMBED_BATCH_SIZE]; logger.info(f"➡️ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH_SIZE + 1} ({len(batch)} textos).")
            response = co_client.embed(texts=batch, model=model_embed, input_type=input_type, truncate="END")
            if response.embeddings and isinstance(response.embeddings, list):
                valid_embs = [e for e in response.embeddings if isinstance(e, list) and all(isinstance(n, (float, int)) for n in e)]
                all_embeddings.extend(valid_embs); logger.info(f"⬅️ [COHERE EMBED] Embeddings válidos batch: {len(valid_embs)}")
                if len(valid_embs) != len(batch): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia batch: {len(batch)} textos, {len(valid_embs)} embeddings.")
            else: logger.error(f"❌ [COHERE EMBED] Batch no devolvió embeddings o no es una lista.")
            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): sleep(0.3) 
    except cohere.CohereAPIError as e_api: logger.error(f"❌ [COHERE EMBED] API Error: {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
    except Exception as e_general: logger.error(f"❌ [COHERE EMBED] Error genérico embeddings: {e_general}", exc_info=True); return [] 
    if len(all_embeddings) != len(textos): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia final: {len(textos)} textos, {len(all_embeddings)} embeddings.")
    logger.info(f"✅ [COHERE EMBED] Embeddings totales: {len(all_embeddings)}.")
    return all_embeddings


def get_cohere_response(message: str, 
                        chat_history: Optional[List[Dict[str, str]]] = None, # Historial USER/CHATBOT
                        preamble: Optional[str] = None, # Este es el system_prompt de logic.py
                        model: str = "command-r-plus",
                        temperature: float = 0.3,
                        # Estos se mantienen por si los usas para logging o lógica futura aquí
                        rubro_id: Optional[int] = None, 
                        user_context: Optional[Dict[str, Any]] = None 
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{message[:70]}...'")
    
    # Construir el historial para la API, el system_prompt (preamble) va PRIMERO
    chat_history_for_api_call: List[Dict[str, str]] = []
    if preamble and isinstance(preamble, str) and preamble.strip():
        chat_history_for_api_call.append({"role": "SYSTEM", "message": preamble})
        logger.info(f"[COHERE CHAT] System prompt (preamble) añadido como primer mensaje al historial para API.")
    else:
        logger.info("[COHERE CHAT] No se proporcionó preamble (System Prompt) o estaba vacío.")

    if chat_history: # chat_history ya tiene roles USER/CHATBOT
        chat_history_for_api_call.extend(chat_history)
        logger.info(f"[COHERE CHAT] Historial de conversación previo añadido ({len(chat_history)} mensajes).")
    
    logger.info(f"[COHERE CHAT] Total mensajes para API (incluyendo SYSTEM si existe): {len(chat_history_for_api_call)}")


    if not COHERE_API_KEY:
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no configurada.")
        return "Error interno: Asistente IA no disponible (C01)."
    if not message or not isinstance(message, str) or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje de usuario vacío o inválido.")
        return "Por favor, escribe una pregunta más clara."

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=60)
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error inicializando cliente Cohere: {e_client}", exc_info=True)
        return "Error interno: No se pudo inicializar asistente IA (C02)."

    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: '{model}'. Temperatura: {temperature}. Mensaje: '{message[:70]}'. Historial efectivo (para co.chat): {len(chat_history_for_api_call)}.")
        
        # LLAMADA A co_client.chat() CORREGIDA:
        # NO se usa el argumento 'preamble' aquí.
        # El prompt del sistema ya está en 'chat_history_for_api_call' si fue provisto.
        response = co_client.chat(
            message=message, # La pregunta actual del usuario
            chat_history=chat_history_for_api_call if chat_history_for_api_call else None, 
            model=model, 
            temperature=temperature,
        )

        respuesta_texto = response.text.strip() if response and response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429:
            return "Nuestro asistente IA está con alta demanda. Intenta en unos momentos."
        return "Lo siento, no pude procesar tu solicitud con el asistente IA (E01)."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico en llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado generando una respuesta (E02)."
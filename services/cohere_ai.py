# services/cohere_ai.py
import os
import logging
import cohere
from time import sleep
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 

def embed_textos(textos: List[str], input_type: str = "search_document") -> List[List[float]]:
    # ... (Tu función embed_textos como la tenías en la respuesta @‶gANVneHZLGZk...")
    # (Asegúrate de que sea la versión completa y funcional)
    if not COHERE_API_KEY: logger.error("[COHERE EMBED] COHERE_API_KEY no configurada."); return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos): logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida."); return []
    all_embeddings: List[List[float]] = []; model = "embed-multilingual-v3.0"
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch: {COHERE_EMBED_BATCH_SIZE}")
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30)
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch = textos[i:i + COHERE_EMBED_BATCH_SIZE]; logger.info(f"➡️ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH_SIZE + 1} ({len(batch)} textos).")
            response = co_client.embed(texts=batch, model=model, input_type=input_type, truncate="END")
            if response.embeddings and isinstance(response.embeddings, list):
                valid_embs = [e for e in response.embeddings if isinstance(e, list) and all(isinstance(n, float) for n in e)]
                all_embeddings.extend(valid_embs); logger.info(f"⬅️ [COHERE EMBED] Embeddings válidos batch: {len(valid_embs)}")
                if len(valid_embs) != len(batch): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia batch: {len(batch)} textos, {len(valid_embs)} embeddings.")
            else: logger.error(f"❌ [COHERE EMBED] Batch no devolvió embeddings.")
            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): sleep(0.3) 
    except cohere.CohereAPIError as e: logger.error(f"❌ [COHERE EMBED] API Error: {e.message}", exc_info=False)
    except Exception as e: logger.error(f"❌ [COHERE EMBED] Error genérico: {e}", exc_info=True); return [] 
    if len(all_embeddings) != len(textos): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia final: {len(textos)} textos, {len(all_embeddings)} embeddings.")
    logger.info(f"✅ [COHERE EMBED] Embeddings totales: {len(all_embeddings)}.")
    return all_embeddings


def get_cohere_response(message: str, 
                        chat_history: Optional[List[Dict[str, str]]] = None, 
                        system_prompt: Optional[str] = None, # Renombrado para claridad, es el prompt del sistema
                        model: str = "command-r-plus",
                        temperature: float = 0.3,
                        # Los siguientes no se usan directamente en la llamada a co.chat pero se reciben
                        rubro_id: Optional[int] = None, 
                        user_context: Optional[Dict[str, Any]] = None 
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{message[:70]}...'")
    
    final_chat_history_for_api: List[Dict[str, str]] = []
    if system_prompt and isinstance(system_prompt, str) and system_prompt.strip():
        # El SDK de Cohere (especialmente para command-r y command-r-plus)
        # puede tomar el prompt del sistema como el primer mensaje en chat_history
        # o a través del parámetro 'preamble' (revisar documentación del SDK para la versión exacta).
        # La forma más compatible es incluirlo en el historial.
        final_chat_history_for_api.append({"role": "SYSTEM", "message": system_prompt})
        logger.info(f"[COHERE CHAT] System prompt añadido al historial (primeros 100 chars): {system_prompt[:100]}...")
    else:
        logger.info("[COHERE CHAT] No se proporcionó system_prompt o estaba vacío.")

    if chat_history: # chat_history ya viene en formato USER/CHATBOT
        final_chat_history_for_api.extend(chat_history)
        logger.info(f"[COHERE CHAT] Historial de conversación añadido ({len(chat_history)} mensajes).")
    else:
        logger.info("[COHERE CHAT] No hay historial de conversación previo.")


    if not COHERE_API_KEY:
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
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: '{model}'. Temperatura: {temperature}. Mensaje: '{message[:70]}'. Historial completo para API (incl. system): {len(final_chat_history_for_api)}.")
        
        response = co_client.chat(
            message=message,
            chat_history=final_chat_history_for_api if final_chat_history_for_api else None, # Pasar el historial que puede incluir el SYSTEM prompt
            # preamble=None, # Ya no usamos preamble, se incluye en chat_history como SYSTEM
            model=model, 
            temperature=temperature,
        )

        respuesta_texto = response.text.strip() if response and response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429:
            return "Nuestro asistente IA está experimentando una alta demanda. Por favor, intenta nuevamente en unos momentos."
        return "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01)."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02)."
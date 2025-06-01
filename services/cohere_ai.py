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
    # ... (Tu función embed_textos como la tenías en la respuesta @‶gANVneHZLGZh...")
    if not COHERE_API_KEY: logger.error("[COHERE EMBED] COHERE_API_KEY no configurada."); return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos): logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida."); return []
    all_embeddings: List[List[float]] = []
    model = "embed-multilingual-v3.0"; logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch size: {COHERE_EMBED_BATCH_SIZE}")
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30)
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch_textos = textos[i:i + COHERE_EMBED_BATCH_SIZE]; logger.info(f"➡️ [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1}/{ (len(textos) -1)//COHERE_EMBED_BATCH_SIZE + 1 } ({len(batch_textos)} textos).")
            response = co_client.embed(texts=batch_textos, model=model, input_type=input_type, truncate="END")
            if response.embeddings and isinstance(response.embeddings, list):
                valid_batch_embeddings = [emb for emb in response.embeddings if isinstance(emb, list) and all(isinstance(n, float) for n in emb)]; all_embeddings.extend(valid_batch_embeddings)
                logger.info(f"⬅️ [COHERE EMBED] Embeddings válidos recibidos para el batch: {len(valid_batch_embeddings)}")
                if len(valid_batch_embeddings) != len(batch_textos): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia en batch: {len(batch_textos)} textos, {len(valid_batch_embeddings)} embeddings válidos.")
            else: logger.error(f"❌ [COHERE EMBED] Batch no devolvió embeddings o no es una lista.")
            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): sleep(0.3) 
    except cohere.CohereAPIError as e_api: logger.error(f"❌ [COHERE EMBED] Error API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
    except Exception as e_general: logger.error(f"❌ [COHERE EMBED] Error genérico embeddings: {e_general}", exc_info=True); return [] 
    if len(all_embeddings) != len(textos): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia final: {len(textos)} textos, {len(all_embeddings)} embeddings.")
    logger.info(f"✅ [COHERE EMBED] Embeddings totales: {len(all_embeddings)}.")
    return all_embeddings

def get_cohere_response(message: str, # Pregunta actual del usuario
                        chat_history: Optional[List[Dict[str, str]]] = None, # Historial: [{'role': 'USER'/'CHATBOT', 'message': '...'}]
                        preamble: Optional[str] = None, # Prompt del sistema
                        # rubro_id y user_context se pasan desde logic.py pero no se usan directamente aquí.
                        # Podrías usarlos para logging avanzado o si quieres modificar el preamble dinámicamente aquí.
                        rubro_id: Optional[int] = None, 
                        user_context: Optional[Dict[str, Any]] = None
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{message[:70]}...'")
    if chat_history: logger.info(f"[COHERE CHAT] Historial para API: {len(chat_history)} mensajes.")
    if preamble: logger.info(f"[COHERE CHAT] Preamble (System Prompt) (primeros 100 chars): {preamble[:100]}...")

    if not COHERE_API_KEY:
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return "Error interno: Asistente IA no disponible en este momento (C01)."
    if not message or not isinstance(message, str) or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje actual del usuario está vacío o no es string.")
        return "Por favor, escribe una pregunta o consulta."

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=60) 
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar cliente Cohere: {e_client}", exc_info=True)
        return "Error interno: No se pudo inicializar el asistente IA (C02)."

    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: 'command-r-plus'. Mensaje: '{message[:70]}'. Historial: {len(chat_history) if chat_history else 0}. Preamble usado: {'Sí' if preamble else 'No'}")
        
        response = co_client.chat(
            message=message,
            chat_history=chat_history if chat_history else [], 
            preamble=preamble if preamble else None, # Usar el argumento preamble
            model="command-r-plus", 
            temperature=0.3,
        )

        respuesta_texto = response.text.strip() if response and response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        # logger.debug(f"[COHERE CHAT] Objeto Respuesta API COMPLETA: {response}") 
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429:
            return "Nuestro asistente IA está experimentando una alta demanda. Por favor, intenta nuevamente en unos momentos."
        # Para otros errores de API, dar un mensaje más genérico al usuario.
        return "Lo siento, no pude procesar tu solicitud en este momento con el asistente IA (E01)."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta (E02)."
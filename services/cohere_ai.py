# services/cohere_ai.py
import os
import logging
import cohere # SDK oficial de Cohere
from time import sleep # Importar sleep para el reintento

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 

# --- Tu función embed_textos (se mantiene igual que la última versión que te di) ---
def embed_textos(textos: list[str], input_type: str = "search_document") -> list[list[float]]:
    # ... (Pega aquí tu función embed_textos completa y ya funcional)
    # La versión que te di en la respuesta con timestamp @‶gANVneHZLGY... estaba bien.
    # Por ejemplo:
    if not COHERE_API_KEY:
        logger.error("[COHERE EMBED] COHERE_API_KEY no está configurada.")
        return []
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("❌ [COHERE EMBED] Lista de textos vacía o inválida.")
        return []
    all_embeddings = []
    model = "embed-multilingual-v3.0"
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model}, Tipo input: {input_type}, Batch size: {COHERE_EMBED_BATCH_SIZE}")
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=30)
        for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
            batch_textos = textos[i:i + COHERE_EMBED_BATCH_SIZE]
            logger.info(f"➡️ [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1}/{ (len(textos) -1)//COHERE_EMBED_BATCH_SIZE + 1 } ({len(batch_textos)} textos).")
            response = co_client.embed(texts=batch_textos, model=model, input_type=input_type, truncate="END")
            if response.embeddings:
                all_embeddings.extend(response.embeddings)
                logger.info(f"⬅️ [COHERE EMBED] Embeddings recibidos para el batch: {len(response.embeddings)}")
            else: logger.error(f"❌ [COHERE EMBED] Batch no devolvió embeddings.")
            if len(textos) > COHERE_EMBED_BATCH_SIZE and i + COHERE_EMBED_BATCH_SIZE < len(textos): sleep(0.2) 
    except cohere.CohereAPIError as e_api: logger.error(f"❌ [COHERE EMBED] Error API Cohere: {e_api.message}", exc_info=False)
    except Exception as e_general: logger.error(f"❌ [COHERE EMBED] Error genérico embeddings: {e_general}", exc_info=True); return [] 
    if len(all_embeddings) != len(textos): logger.warning(f"⚠️ [COHERE EMBED] Discrepancia: {len(textos)} textos, {len(all_embeddings)} embeddings.")
    logger.info(f"✅ [COHERE EMBED] Embeddings totales: {len(all_embeddings)}.")
    return all_embeddings
# --- Fin de embed_textos ---


def get_cohere_response(current_message: str, 
                        chat_history_for_api: list, # Lista de dicts {"role": "USER/CHATBOT", "message": "..."}
                        system_prompt_for_api: str, 
                        rubro_id: int, # No se usa directamente aquí, pero se pasa desde logic.py
                        user_context: dict # No se usa directamente aquí, pero se pasa desde logic.py
                        ) -> str:
    """
    Obtiene una respuesta de chat de la API de Cohere.
    Usa el parámetro 'preamble' para el system_prompt.
    """
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{current_message[:70]}...'")
    logger.info(f"[COHERE CHAT] Historial para API: {len(chat_history_for_api)} mensajes.")
    logger.info(f"[COHERE CHAT] Preamble (System Prompt) (primeros 100 chars): {system_prompt_for_api[:100]}...")

    if not COHERE_API_KEY:
        logger.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return "Error: Clave API de Cohere no configurada."
    if not current_message or not isinstance(current_message, str):
        logger.error("❌ [COHERE CHAT] Mensaje actual del usuario está vacío o no es string.")
        return "Error: Mensaje de usuario inválido."

    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=45)
    except Exception as e_client:
        logger.error(f"❌ [COHERE CHAT] Error al inicializar cliente Cohere: {e_client}", exc_info=True)
        return "Error: No se pudo inicializar el asistente IA."

    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: 'command-r-plus'. Mensaje: '{current_message[:70]}'. Historial: {len(chat_history_for_api)}. Preamble usado.")
        
        # La llamada correcta al SDK de Cohere usando 'preamble' para el prompt del sistema
        response = co_client.chat(
            message=current_message,
            chat_history=chat_history_for_api if chat_history_for_api else None, 
            preamble=system_prompt_for_api if system_prompt_for_api else None, # <--- USO DE PREAMBLE
            model="command-r-plus", 
            temperature=0.3,
            # connectors=[{"id": "web-search"}] # Si necesitas búsqueda web
        )

        respuesta_texto = response.text.strip() if response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        # logger.debug(f"[COHERE CHAT] Objeto Respuesta API COMPLETA: {response}") # Descomentar para depuración muy detallada
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429:
            return "Disculpa, nuestro asistente IA está experimentando mucho tráfico. Por favor, intenta en unos momentos."
        return "Lo siento, no pude procesar tu solicitud en este momento debido a un problema con el asistente de inteligencia artificial."
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta."
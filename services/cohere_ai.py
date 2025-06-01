# services/cohere_ai.py
import os
import logging
import cohere
from time import sleep

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90 

def embed_textos(textos: list[str], input_type: str = "search_document") -> list[list[float]]:
    # ... (Tu función embed_textos como la tenías, ya estaba bien)
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


def get_cohere_response(current_message: str, 
                        chat_history_for_api: list, # Lista de dicts {"role": "USER/CHATBOT", "message": "..."}
                        system_prompt_for_api: str, # Este es el prompt del sistema
                        rubro_id: int, 
                        user_context: dict
                        ) -> str:
    logger.info(f"➡️ [COHERE CHAT] Iniciando get_cohere_response. Pregunta: '{current_message[:70]}...'")
    logger.debug(f"[COHERE CHAT] Historial para API (antes de añadir system prompt): {chat_history_for_api}")
    logger.debug(f"[COHERE CHAT] System Prompt (primeros 200 chars): {system_prompt_for_api[:200]}...")

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

    # La forma más compatible de usar el system prompt es como 'preamble' o primer mensaje del historial
    # con rol 'SYSTEM'. El SDK de Cohere ha cambiado esto.
    # Si `preamble` da error, la alternativa es:
    formatted_chat_history = []
    if system_prompt_for_api:
        # Algunas versiones/modelos prefieren 'SYSTEM' en chat_history, otras 'SYSTEM' para el parámetro `preamble`
        # y otras más nuevas `system_message`.
        # Vamos a probar con `preamble` y si falla, intentamos con `chat_history` modificado.
        # Por ahora, la lógica de `logic.py` ya arma `mensajes_completos_cohere`
        # que incluye el system prompt como el primer mensaje.
        # Así que la llamada desde logic.py DEBE ser:
        # respuesta_obtenida_llm = get_cohere_response(
        #     mensajes_completos_cohere=mensajes_completos_cohere, <--- CAMBIO AQUÍ
        #     rubro_id=rubro_id_final, 
        #     user_context=user_profile_context
        # )
        # Y esta función get_cohere_response DEBE ser:
        # def get_cohere_response(mensajes_completos_cohere: list, rubro_id: int, user_context: dict) -> str:
        #   current_user_message = mensajes_completos_cohere[-1]["message"]
        #   chat_history_for_api = []
        #   system_prompt = mensajes_completos_cohere[0]["content"]
        #   for msg in mensajes_completos_cohere[1:-1]:
        #        #... convertir a formato USER/CHATBOT
        #
        # Para simplificar y arreglar el error de 'preamble', modificaremos esta función para que
        # acepte los mensajes como una lista única donde el primero es el sistema.
        # PERO LA LLAMADA DESDE logic.py YA LO HACE ASÍ: mensajes_completos_cohere.
        # El error `TypeError: get_cohere_response() got an unexpected keyword argument 'current_message'`
        # INDICA QUE LA DEFINICIÓN DE get_cohere_response EN EL CÓDIGO QUE ESTÁ CORRIENDO EN RENDER
        # NO ES ESTA.

        # VAMOS A USAR LA FIRMA QUE logic.py ESTÁ USANDO AHORA Y AJUSTAR LA LLAMADA A co.chat()
        pass # La lógica de `logic.py` ya prepara `mensajes_completos_cohere`

    try:
        logger.info(f"➡️ [COHERE CHAT CALL] Modelo: 'command-r-plus'. Mensaje: '{current_message[:70]}'. Historial: {len(chat_history_for_api)}. System Prompt usado (vía preamble).")
        
        # Si 'preamble' da error, la alternativa es inyectar el system_prompt_for_api
        # como el primer mensaje en chat_history_for_api con rol "SYSTEM"
        # y NO usar el parámetro 'preamble'.
        
        # Intentemos sin 'preamble' y con 'system_prompt_for_api' como primer mensaje si está presente
        final_chat_history = []
        if system_prompt_for_api:
            final_chat_history.append({"role": "SYSTEM", "message": system_prompt_for_api})
        final_chat_history.extend(chat_history_for_api) # Añadir el historial de user/chatbot

        response = co_client.chat(
            message=current_message,
            chat_history=final_chat_history if final_chat_history else None, 
            # preamble=system_prompt_for_api, # <--- ESTA LÍNEA CAUSABA EL TypeError
            model="command-r-plus", 
            temperature=0.3,
        )

        respuesta_texto = response.text.strip() if response.text else ""
        logger.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): '{respuesta_texto[:200]}'")
        # logger.debug(f"[COHERE CHAT] Objeto Respuesta API COMPLETA: {response}")
        return respuesta_texto

    except cohere.CohereAPIError as e_api:
        logger.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {getattr(e_api, 'http_status', 'N/A')} - {e_api.message}. Tipo: {e_api.__class__.__name__}", exc_info=False)
        if hasattr(e_api, 'http_status') and e_api.http_status == 429:
            return "Disculpa, nuestro asistente IA está experimentando mucho tráfico. Por favor, intenta en unos momentos."
        return "Lo siento, no pude procesar tu solicitud en este momento debido a un problema con el asistente de inteligencia artificial." # Mensaje para el usuario
    except Exception as e_general:
        logger.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e_general}", exc_info=True)
        return "Lo siento, tuve un problema inesperado al intentar generar una respuesta." # Mensaje para el usuario
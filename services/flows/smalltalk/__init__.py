from services.gemini_bridge import llamar_gemini
import logging

logger = logging.getLogger(__name__)

def handle(msg: str, meta: dict) -> dict:
    """
    Handles general conversation or queries that don't match any specific intent
    by passing them to the main LLM.
    """
    logger.info(f"Executing Smalltalk/Fallback flow for message: '{msg}'")

    owner_user = meta.get('user_obj')
    viewer_user = meta.get('viewer_user_obj')
    chat_db_context = meta.get('chat_db_context')

    if not owner_user or not chat_db_context:
        logger.error("[smalltalk.handle] Critical context missing (owner_user or chat_db_context). Cannot call LLM.")
        return {
            "type": "error",
            "message_body": "Lo siento, hubo un problema interno y no puedo procesar tu solicitud en este momento."
        }

    try:
        llm_response_payload = llamar_gemini(
            pregunta=msg,
            owner_user=owner_user,
            viewer_user=viewer_user,
            chat_session_context=chat_db_context,
        )

        llm_response_payload["fuente"] = "llm_fallback"

        logger.info(f"LLM Fallback response: {llm_response_payload}")
        return llm_response_payload

    except Exception as e:
        logger.error(f"Error calling LLM in smalltalk.handle: {e}", exc_info=True)
        return {
            "type": "error",
            "message_body": "Lo siento, estoy teniendo dificultades para conectarme. Por favor, intenta de nuevo en unos momentos."
        }

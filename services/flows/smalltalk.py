from services.gemini_bridge import llamar_gemini
import logging

logger = logging.getLogger(__name__)

def handle(msg: str, meta: dict) -> dict:
    """
    Handles general conversation or queries that don't match any specific intent
    by passing them to the main LLM.
    """
    logger.info(f"Executing Smalltalk/Fallback flow for message: '{msg}'")

    # The 'meta' object should contain all the necessary context for the LLM call.
    # Based on AGENTS.md and responder_municipio, this includes:
    # - owner_user
    # - viewer_user
    # - chat_db_context
    # - etc.
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
        # Pass the user's message and the full context to the LLM.
        # The LLM is expected to handle general conversation, answer questions,
        # or ask for clarification if it thinks a specific tool could be used.
        llm_response_payload = llamar_gemini(
            pregunta=msg,
            owner_user=owner_user,
            viewer_user=viewer_user,
            chat_session_context=chat_db_context,
            # Force a general conversation state if needed, or let the LLM decide
        )

        # The response from llamar_gemini should already be in the correct payload format.
        # We might just need to add the source.
        llm_response_payload["fuente"] = "llm_fallback"

        logger.info(f"LLM Fallback response: {llm_response_payload}")
        return llm_response_payload

    except Exception as e:
        logger.error(f"Error calling LLM in smalltalk.handle: {e}", exc_info=True)
        return {
            "type": "error",
            "message_body": "Lo siento, estoy teniendo dificultades para conectarme. Por favor, intenta de nuevo en unos momentos."
        }

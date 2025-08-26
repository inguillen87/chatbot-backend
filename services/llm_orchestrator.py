import logging
from services.openai_bridge import llamar_openai
from services.cohere_bridge import llamar_cohere
from services.gemini_bridge import llamar_gemini

logger = logging.getLogger(__name__)

def llamar_llm_con_fallback(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str):
    """
    Tries to call LLMs in a specific priority order (OpenAI -> Cohere -> Gemini),
    with fallbacks in case of errors.

    Returns a tuple of (response_dict, context_dict), where context_dict is currently unused.
    """
    # Priority 1: OpenAI
    try:
        logger.info("Attempting LLM call with provider: OpenAI")
        return llamar_openai(app, mensaje_usuario, usuario, historial, chat_session_id)
    except Exception as e_openai:
        logger.warning(f"OpenAI call failed with error: {e_openai}. Falling back to Cohere.")

    # Priority 2: Cohere
    try:
        logger.info("Attempting LLM call with provider: Cohere")
        return llamar_cohere(app, mensaje_usuario, usuario, historial, chat_session_id)
    except Exception as e_cohere:
        logger.warning(f"Cohere call failed with error: {e_cohere}. Falling back to Gemini.")

    # Priority 3: Gemini
    try:
        logger.info("Attempting LLM call with provider: Gemini")
        # The Gemini bridge function already returns the (response, context) tuple.
        return llamar_gemini(app, mensaje_usuario, usuario, historial, chat_session_id=chat_session_id)
    except Exception as e_gemini:
        logger.error(f"All LLM providers failed. Final error from Gemini: {e_gemini}", exc_info=True)
        # Final fallback response if all LLMs fail
        error_response = {
            "message_body": "Lo siento, nuestros asistentes de IA no están disponibles en este momento. Por favor, intenta de nuevo más tarde o contacta a un representante humano.",
            "accion_backend": "error_fatal_llm",
            "datos_estructura": {},
            "pedir_info": None,
            "botones": []
        }
        return error_response, {}

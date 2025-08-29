import logging
from services.openai_bridge import llamar_openai
from services.cohere_bridge import llamar_cohere

logger = logging.getLogger(__name__)

def llamar_llm_con_fallback(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str):
    """
    Try LLM providers in priority order (currently OpenAI then Cohere).
    Falls back to the next provider on failure and returns the first successful
    response. If all providers fail, a generic error response is returned.

    Returns
    -------
    tuple
        `(response_dict, context_dict)` where `context_dict` is currently unused.
    """

    providers = [
        ("OpenAI", llamar_openai),
        ("Cohere", llamar_cohere),
    ]

    last_error = None
    for name, func in providers:
        try:
            logger.info(f"Attempting LLM call with provider: {name}")
            return func(app, mensaje_usuario, usuario, historial, chat_session_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            last_error = exc
            logger.warning(f"{name} call failed with error: {exc}")

    logger.error(
        "All LLM providers failed. Last error: %s", last_error, exc_info=True
    )
    error_response = {
        "message_body": (
            "Lo siento, nuestros asistentes de IA no están disponibles en este momento. "
            "Por favor, intenta de nuevo más tarde o contacta a un representante humano."
        ),
        "accion_backend": "error_fatal_llm",
        "datos_estructura": {},
        "pedir_info": None,
        "botones": [],
    }
    return error_response, {}

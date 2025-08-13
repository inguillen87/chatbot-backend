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
        # Prepare the arguments for ahe `llamar_gemini` function correctly.
        # It expects 'mensaje_usuario', 'usuario' (a dict), and 'historial' (a list).

        # The user to pass to the LLM is the one viewing the chat (the end-user)
        usuario_actual = viewer_user or owner_user
        usuario_dict = {
            "nombre": usuario_actual.nombre,
            "email": usuario_actual.email,
            "id": usuario_actual.id,
            "tipo_entidad": "municipio" # Assuming this flow is only for municipios for now
        }

        # Extract history from the context
        historial = chat_db_context.context_data.get('mensajes_previos_gemini_formato', [])

        llm_response_payload = llamar_gemini(
            mensaje_usuario=msg,
            usuario=usuario_dict,
            historial=historial
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

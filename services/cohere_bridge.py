import os
import cohere
import logging
import json

from services.chatbot_prompts import get_system_prompt

logger = logging.getLogger(__name__)

# It's a good practice to have the client instantiated once and reused if possible,
# but for simplicity in this stateless function, we'll instantiate it on each call.
# The API key is loaded automatically from the environment variable COHERE_API_KEY.
try:
    co = cohere.Client()
except Exception as e:
    logger.error(f"Failed to initialize Cohere client: {e}")
    co = None

try:
    co_v2 = cohere.ClientV2()
except Exception as e:
    logger.warning(f"Failed to initialize Cohere ClientV2: {e}")
    co_v2 = None


def _cohere_chat_model() -> str:
    return os.getenv("COHERE_CHAT_MODEL", "command-a-03-2025")


def _extract_response_text(response) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    message = getattr(response, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        joined = "".join(parts).strip()
        if joined:
            return joined
    if isinstance(content, str):
        return content.strip()

    return ""


def _parse_llm_json(raw_response_text: str) -> dict:
    raw_response_text = raw_response_text.strip()
    if raw_response_text.startswith("```json"):
        raw_response_text = raw_response_text[len("```json"):].strip()
    if raw_response_text.endswith("```"):
        raw_response_text = raw_response_text[:-len("```")].strip()
    parsed_response = json.loads(raw_response_text)
    parsed_response.setdefault('message_body', parsed_response.get('respuesta_usuario', ''))
    parsed_response.setdefault('accion_backend', 'responder_directamente')
    if not isinstance(parsed_response.get('datos_estructura'), dict):
        parsed_response['datos_estructura'] = {}
    if not isinstance(parsed_response.get('botones'), list):
        parsed_response['botones'] = []
    parsed_response.setdefault('pedir_info', None)
    return parsed_response

def llamar_cohere(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str) -> tuple[dict, dict]:
    """
    Calls the Cohere API and formats the response to be compatible with the application's structure.
    """
    if not co and not co_v2:
        raise ConnectionError("Cohere client is not initialized. Check API key.")

    # 1. Format the history for Cohere's chat endpoint
    chat_history = []
    for item in historial:
        role = item.get("role")
        text = item.get("parts", [{}])[0].get("text", "")
        if role == "user":
            chat_history.append({"role": "USER", "message": text})
        elif role == "model":
            chat_history.append({"role": "CHATBOT", "message": text})

    # 2. Construct the prompt for Cohere
    # We will use a simplified prompt for now. The full system prompt might need adaptation.
    # For Cohere, the "preamble" is similar to a system prompt.
    # The message from the user
    message = ""
    instruccion_canal = None
    try:
        message_data = json.loads(mensaje_usuario)
        message = message_data.get("texto", str(message_data))
        instruccion_canal = message_data.get("instruccion_canal")
    except (json.JSONDecodeError, TypeError):
        message = str(mensaje_usuario)

    preamble = get_system_prompt(usuario)
    if instruccion_canal:
        preamble += f"\n\nCONTEXTO DEL CANAL: {instruccion_canal}"

    logger.info(f"Sending to Cohere. Message: {message[:100]}...")

    try:
        model = _cohere_chat_model()
        if co_v2 and os.getenv("COHERE_CHAT_API_VERSION", "v2").lower() == "v2":
            messages = [{"role": "system", "content": preamble}]
            for item in chat_history:
                role = "user" if item.get("role") == "USER" else "assistant"
                messages.append({"role": role, "content": item.get("message", "")})
            messages.append({"role": "user", "content": message})
            response = co_v2.chat(
                model=model,
                messages=messages,
                temperature=0.3,
            )
        else:
            response = co.chat(
                message=message,
                chat_history=chat_history,
                preamble=preamble,
                model=model,
                temperature=0.3,
            )

        raw_response_text = _extract_response_text(response)
        logger.info(f"Response from Cohere (raw): {raw_response_text}")

        parsed_response = _parse_llm_json(raw_response_text)

        return parsed_response, {"model_used": model}

    except Exception as e:
        logger.error(f"Error calling Cohere API: {e}", exc_info=True)
        # To ensure fallback, we re-raise the exception.
        raise

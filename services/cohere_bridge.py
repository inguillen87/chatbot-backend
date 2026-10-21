import os
import logging
import json

from services.chatbot_prompts import get_system_prompt
from services.llm_provider_network_policy import require_llm_provider_network
from utils.lazy_module import LazyModule

cohere = LazyModule("cohere")

logger = logging.getLogger(__name__)

co = None
co_v2 = None
_COHERE_CLIENTS_INITIALIZED = False


def _get_cohere_clients(app=None):
    """Initialize Cohere lazily, after the shared test-network gate."""

    global co, co_v2, _COHERE_CLIENTS_INITIALIZED
    require_llm_provider_network("cohere", app)
    if _COHERE_CLIENTS_INITIALIZED:
        return co, co_v2

    try:
        co = cohere.Client()
    except Exception as exc:
        logger.warning(
            "LLM provider client initialization failed provider=cohere api=v1 error_type=%s",
            type(exc).__name__,
        )
        co = None

    try:
        co_v2 = cohere.ClientV2()
    except Exception as exc:
        logger.warning(
            "LLM provider client initialization failed provider=cohere api=v2 error_type=%s",
            type(exc).__name__,
        )
        co_v2 = None

    _COHERE_CLIENTS_INITIALIZED = True
    return co, co_v2


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
    cohere_v1, cohere_v2 = _get_cohere_clients(app)
    if not cohere_v1 and not cohere_v2:
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

    logger.info("LLM provider request started provider=cohere")

    try:
        model = _cohere_chat_model()
        if cohere_v2 and os.getenv("COHERE_CHAT_API_VERSION", "v2").lower() == "v2":
            messages = [{"role": "system", "content": preamble}]
            for item in chat_history:
                role = "user" if item.get("role") == "USER" else "assistant"
                messages.append({"role": role, "content": item.get("message", "")})
            messages.append({"role": "user", "content": message})
            response = cohere_v2.chat(
                model=model,
                messages=messages,
                temperature=0.3,
            )
        else:
            response = cohere_v1.chat(
                message=message,
                chat_history=chat_history,
                preamble=preamble,
                model=model,
                temperature=0.3,
            )

        raw_response_text = _extract_response_text(response)
        logger.info("LLM provider response received provider=cohere")

        parsed_response = _parse_llm_json(raw_response_text)

        return parsed_response, {"model_used": model}

    except Exception as exc:
        logger.error(
            "LLM provider request failed provider=cohere error_type=%s",
            type(exc).__name__,
        )
        # To ensure fallback, we re-raise the exception.
        raise

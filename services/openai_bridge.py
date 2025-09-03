import os
import openai
import logging
import json
import httpx
from typing import List, Dict

try:
    import tiktoken
    _TOKEN_ENCODER = tiktoken.encoding_for_model("gpt-4o-mini")
except Exception:  # pragma: no cover - optional dependency
    _TOKEN_ENCODER = None

logger = logging.getLogger(__name__)

# The API key is loaded automatically from the environment variable OPENAI_API_KEY.
try:
    # Use a custom HTTP client that ignores system proxy settings. Without
    # this, environments with `http_proxy`/`https_proxy` variables can cause
    # `openai.OpenAI` to raise `TypeError: Client.__init__() got an unexpected
    # keyword argument 'proxies'` during initialization.
    http_client = httpx.Client(proxy=None, trust_env=False)
    client = openai.OpenAI(http_client=http_client)
except Exception as e:
    logger.error(f"Failed to initialize OpenAI client: {e}")
    client = None

def llamar_openai(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str) -> tuple[dict, dict]:
    """
    Calls the OpenAI API and formats the response to be compatible with the application's structure.
    """
    if not client:
        raise ConnectionError("OpenAI client is not initialized. Check API key.")

    # 1. Format the history for OpenAI's chat endpoint
    # The system prompt goes first.
    from services.chatbot_prompts import JULES_SYSTEM_PROMPT
    messages = [{"role": "system", "content": JULES_SYSTEM_PROMPT}]

    for item in historial:
        role = item.get("role")
        # OpenAI uses 'assistant' for the model's role
        if role == "model":
            role = "assistant"
        if role not in {"assistant", "user", "system"}:
            continue
        text = item.get("parts", [{}])[0].get("text", "")
        if not text:
            continue
        messages.append({"role": role, "content": text})

    # The current user message
    message = ""
    try:
        message_data = json.loads(mensaje_usuario)
        message = message_data.get("texto", str(message_data))
    except (json.JSONDecodeError, TypeError):
        message = str(mensaje_usuario)

    messages.append({"role": "user", "content": message})

    def _estimate_tokens(text: str) -> int:
        if _TOKEN_ENCODER:
            return len(_TOKEN_ENCODER.encode(text))
        return len(text.split())

    def _prune_messages(msgs: List[Dict[str, str]], limit: int = 4000) -> List[Dict[str, str]]:
        total = 0
        pruned: List[Dict[str, str]] = []
        for m in reversed(msgs):
            total += _estimate_tokens(m.get("content", ""))
            if total > limit:
                break
            pruned.append(m)
        return list(reversed(pruned))

    messages = _prune_messages(messages)

    logger.info(f"Sending to OpenAI. Message: {message[:100]}...")

    try:
        # 3. Make the API call
        response = client.chat.completions.create(
            model="gpt-4o-mini", # A good, cost-effective default model
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"}, # Request JSON output
        )

        raw_response_text = response.choices[0].message.content.strip()
        logger.info(f"Response from OpenAI (raw): {raw_response_text}")

        # 4. Parse the response
        parsed_response = json.loads(raw_response_text)

        # Ensure the response has the keys our application expects
        parsed_response.setdefault('message_body', parsed_response.get('respuesta_usuario', ''))
        parsed_response.setdefault('accion_backend', 'responder_directamente')

        return parsed_response, {}

    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}", exc_info=True)
        # Re-raise the exception to trigger the fallback mechanism.
        raise

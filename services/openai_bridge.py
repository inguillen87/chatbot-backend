import os
import openai
import logging
import json
import httpx
from typing import List, Dict

from services.chatbot_prompts import get_system_prompt

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

def llamar_openai(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str, model: str = "gpt-4o-mini") -> tuple[dict, dict]:
    """
    Calls the OpenAI API and formats the response to be compatible with the application's structure.
    Allows specifying the model (default: gpt-4o-mini).
    """
    if not client:
        raise ConnectionError("OpenAI client is not initialized. Check API key.")

    # 1. Format the history for OpenAI's chat endpoint
    # The system prompt goes first.
    system_prompt = get_system_prompt(usuario)
    messages = [{"role": "system", "content": system_prompt}]

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

        # Check for channel-specific instructions (e.g. voice mode constraints)
        instruccion_canal = message_data.get("instruccion_canal")
        if instruccion_canal and messages and messages[0].get("role") == "system":
            messages[0]["content"] += f"\n\nCONTEXTO DEL CANAL: {instruccion_canal}"

    except (json.JSONDecodeError, TypeError):
        message = str(mensaje_usuario)

    messages.append({"role": "user", "content": message})

    # SAFETY CHECK: Ensure "JSON" is in the system prompt if we request json_object
    if messages and messages[0]["role"] == "system":
        content = messages[0]["content"] or ""
        if "JSON" not in content and "json" not in content:
            logger.warning("System prompt missing 'JSON' keyword. Appending safety instruction.")
            messages[0]["content"] = content + "\n\nIMPORTANTE: Tu respuesta DEBE ser un objeto JSON válido."

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

    total_prompt_tokens = sum(_estimate_tokens(m.get("content", "")) for m in messages)
    logger.info(
        f"Sending to OpenAI. Message: {message[:100]}... Estimated prompt tokens: {total_prompt_tokens}"
    )

    try:
        # 3. Make the API call
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"}, # Request JSON output
        )

        raw_response_text = response.choices[0].message.content.strip()
        logger.info(f"Response from OpenAI (raw): {raw_response_text}")

        # 4. Parse the response
        parsed_response = json.loads(raw_response_text)

        if not isinstance(parsed_response, dict):
            # logger.warning(f"OpenAI returned non-dict response: {parsed_response}")
            parsed_response = {
                "message_body": str(parsed_response),
                "accion_backend": "responder_directamente",
                "respuesta_usuario": str(parsed_response)
            }

        usage_dict = None
        if getattr(response, "usage", None):
            usage = response.usage
            logger.info(
                f"OpenAI usage - prompt: {usage.prompt_tokens}, completion: {usage.completion_tokens}, total: {usage.total_tokens}"
            )
            try:
                usage_dict = usage.to_dict()
            except Exception:
                # Fallback to simple dict conversion if to_dict isn't available
                usage_dict = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }

        # Ensure the response has the keys our application expects
        parsed_response.setdefault('message_body', parsed_response.get('respuesta_usuario', ''))
        parsed_response.setdefault('accion_backend', 'responder_directamente')

        return parsed_response, {"usage": usage_dict, "prompt_tokens_estimate": total_prompt_tokens}

    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}", exc_info=True)
        # Re-raise the exception to trigger the fallback mechanism.
        raise

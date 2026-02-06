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

def generate_analytics_report(stats: dict, tenant_type: str = "pyme") -> dict:
    """
    Generates a consultancy report based on analytics stats using GPT-4.
    """
    if not client:
        # Fallback if OpenAI not configured
        return {
            "summary": "AI Consultant is offline (Check API Key).",
            "opportunities": [],
            "threats": [],
            "tone": "System"
        }

    try:
        # Construct specific system prompt for the analyst persona
        system_prompt = (
            "You are a Senior Business Analyst & Data Consultant. "
            f"Analyze the provided JSON statistics for a {('local government (Municipio)' if tenant_type == 'municipio' else 'small business (PyME)')}. "
            "Output a JSON object with keys: "
            "'summary' (Executive summary of performance, max 50 words), "
            "'opportunities' (List of 3 specific growth/efficiency opportunities), "
            "'threats' (List of 3 potential risks or negative trends), "
            "'tone' (Must be 'Professional' or 'Consultative'). "
            "Be specific, citing numbers from the data. "
            "Reply strictly in JSON."
        )

        user_message = f"Here is the data for the selected period: {json.dumps(stats, default=str)}"

        logger.info(f"Generating AI Report for {tenant_type}...")

        response = client.chat.completions.create(
            model="gpt-4o", # Use GPT-4 as requested for 'Senior' analysis
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        )

        raw_response_text = response.choices[0].message.content.strip()
        logger.info(f"AI Report Response: {raw_response_text}")

        parsed_response = json.loads(raw_response_text)
        return parsed_response

    except Exception as e:
        logger.error(f"Error generating AI report: {e}", exc_info=True)
        return {
            "summary": "Could not generate report due to an error.",
            "opportunities": [],
            "threats": [],
            "tone": "Error"
        }

def analyze_sentiment(texts: List[str]) -> dict:
    """
    Analyzes a list of texts (open-ended survey answers) to return a sentiment score and keywords.
    """
    if not client or not texts:
        return {
            "sentiment_score": 0.0,
            "keywords": []
        }

    try:
        # Limit input to avoid token limits
        sample = texts[:50] # Analyze max 50 recent answers
        combined_text = "\n".join([f"- {t}" for t in sample])

        system_prompt = (
            "You are a Sentiment Analysis AI. "
            "Analyze the following list of user opinions. "
            "Return a JSON object with: "
            "'sentiment_score' (float between -1.0 for negative and 1.0 for positive), "
            "'keywords' (list of top 5 recurring topics/words as objects {word: str, count: int}). "
            "Reply strictly in JSON."
        )

        logger.info("Analyzing sentiment for survey answers...")

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": combined_text}
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        raw_response_text = response.choices[0].message.content.strip()
        parsed = json.loads(raw_response_text)

        return parsed

    except Exception as e:
        logger.error(f"Error analyzing sentiment: {e}", exc_info=True)
        return {
            "sentiment_score": 0.0,
            "keywords": []
        }

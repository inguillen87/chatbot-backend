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

def llamar_cohere(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str) -> tuple[dict, dict]:
    """
    Calls the Cohere API and formats the response to be compatible with the application's structure.
    """
    if not co:
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
    try:
        message_data = json.loads(mensaje_usuario)
        message = message_data.get("texto", str(message_data))
    except (json.JSONDecodeError, TypeError):
        message = str(mensaje_usuario)

    logger.info(f"Sending to Cohere. Message: {message[:100]}...")

    try:
        # 3. Make the API call
        response = co.chat(
            message=message,
            chat_history=chat_history,
            preamble=get_system_prompt(usuario),
            model="command-r",  # A good default model
            temperature=0.3,
        )

        raw_response_text = response.text.strip()
        logger.info(f"Response from Cohere (raw): {raw_response_text}")

        # 4. Parse the response
        # This assumes Cohere returns a JSON string similar to OpenAI's.
        # This might need significant adjustment based on actual Cohere output.
        if raw_response_text.startswith("```json"):
            raw_response_text = raw_response_text[len("```json"):].strip()
        if raw_response_text.endswith("```"):
            raw_response_text = raw_response_text[:-len("```")].strip()

        parsed_response = json.loads(raw_response_text)

        # Ensure the response has the keys our application expects
        parsed_response.setdefault('message_body', parsed_response.get('respuesta_usuario', ''))
        parsed_response.setdefault('accion_backend', 'responder_directamente')

        return parsed_response, {}

    except Exception as e:
        logger.error(f"Error calling Cohere API: {e}", exc_info=True)
        # To ensure fallback, we re-raise the exception.
        raise

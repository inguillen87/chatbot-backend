import os
import json
import logging
import httpx
import openai

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MAP_MODEL", "gpt-4.1-mini")


def reverse_geocode_llm(lat: float, lon: float) -> dict | None:
    """Return a formatted address for coordinates using OpenAI.

    The function sends a prompt to an OpenAI model requesting a human
    readable address in Spanish for the provided latitude and longitude.
    It expects the model to return a JSON object with a
    ``formatted_address`` field. If anything goes wrong ``None`` is
    returned.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not configured")
        return None
    try:
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = openai.OpenAI(api_key=api_key, http_client=http_client)
        prompt = (
            "Convierte las coordenadas en una dirección humana. "
            f"Latitud: {lat}, Longitud: {lon}. "
            "Responde solamente en JSON con el campo 'formatted_address'."
        )
        schema = {
            "name": "address_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "formatted_address": {"type": "string"}
                },
                "required": ["formatted_address"],
                "additionalProperties": False,
            },
        }
        response = client.responses.create(
            model=DEFAULT_MODEL,
            input=prompt,
            response_format={"type": "json_schema", "json_schema": schema},
        )
        text = response.output[0].content[0].text
        data = json.loads(text)
        return data
    except Exception as e:
        logger.error(f"Error reverse geocoding with OpenAI: {e}", exc_info=True)
        return None

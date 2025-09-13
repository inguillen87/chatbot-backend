import os
import json
import logging
import httpx
import openai

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MAP_MODEL", "gpt-4.1-mini")


def geocodificar_inversa_llm(latitud: float, longitud: float) -> dict | None:
    """Obtiene una dirección formateada para coordenadas usando OpenAI.

    La función envía un mensaje a un modelo de OpenAI solicitando una
    dirección humana en español para la latitud y longitud provistas.
    Se espera que el modelo retorne un objeto JSON con el campo
    ``formatted_address``. Si ocurre un error se devuelve ``None``.
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
            f"Latitud: {latitud}, Longitud: {longitud}. "
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
        logger.error(
            f"Error al geocodificar inversamente con OpenAI: {e}",
            exc_info=True,
        )
        return None

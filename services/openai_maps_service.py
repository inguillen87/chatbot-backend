import os
import logging
from openai import OpenAI

from .geo_service import reverse_geocode

logger = logging.getLogger(__name__)


def geocodificar_inversa_llm(lat: float, lon: float, direccion_raw: str | None = None) -> dict:
    """Realiza geocodificación inversa y opcionalmente normaliza el texto con LLM."""
    try:
        data = reverse_geocode(float(lat), float(lon))
    except Exception as e:
        logger.error("Error en reverse_geocode: %s", e, exc_info=True)
        data = {}

    texto = data.get("display") or direccion_raw or f"{lat}, {lon}"

    client = OpenAI()
    model = os.getenv("GEO_MODEL", "gpt-4o-mini")
    try:
        try:
            ans = client.responses.create(
                model=model,
                input=f"Normaliza esta dirección a una línea: {texto}",
            )
            out = getattr(ans, "output_text", "")
        except AttributeError:
            ans = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"Normaliza esta dirección a una línea: {texto}"}],
            )
            out = ans.choices[0].message.content
    except Exception as e:
        logger.error("Error normalizando dirección con OpenAI: %s", e, exc_info=True)
        out = texto

    data["display"] = (out or texto).strip()
    return data

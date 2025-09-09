import os
import logging
from openai import OpenAI

from .geo_service import reverse_geocode

logger = logging.getLogger(__name__)


def geocodificar_inversa_llm(lat, lon, direccion_raw=None):
    """Realiza geocodificación inversa y normaliza el texto con LLM."""
    try:
        data = reverse_geocode(float(lat), float(lon))
    except Exception as e:
        logger.error(f"Error en reverse_geocode: {e}", exc_info=True)
        data = {}
    texto = data.get("display") or direccion_raw or f"{lat}, {lon}"
    client = OpenAI()
    model = os.getenv("OPENAI_GEO_MODEL", "gpt-4o-mini")
    try:
        if hasattr(client, "responses"):
            ans = client.responses.create(
                model=model,
                input=f"Normaliza esta dirección a una línea: {texto}",
            )
            out = getattr(ans, "output_text", "")
        else:
            ans = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"Normaliza esta dirección a una línea: {texto}"}],
            )
            out = ans.choices[0].message.content
    except Exception as e:
        logger.error(f"Error normalizando dirección con OpenAI: {e}", exc_info=True)
        out = texto
    data["display"] = (out or texto).strip()
    return data

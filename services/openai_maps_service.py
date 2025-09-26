import os
import json
import logging
from typing import Iterable, Mapping

import httpx
import openai

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MAP_MODEL", "gpt-4.1-mini")
CHAT_FALLBACK_MODEL = os.environ.get("OPENAI_MAP_CHAT_MODEL", "gpt-4o-mini")


def _solicitar_json_a_openai(
    *,
    api_key: str,
    prompt: str,
    schema: dict,
    system_message: str,
):
    """Pide a OpenAI que devuelva JSON válido, con fallback para SDK antiguos."""

    messages = [
        {
            "role": "system",
            "content": system_message
            + " Respondé únicamente con JSON válido que cumpla el esquema.",
        },
        {"role": "user", "content": prompt},
    ]

    def _parse_completion(payload):
        try:
            # SDK <= 0.28 devuelve dicts, el >=1 objetos con atributos.
            return payload["choices"][0]["message"]["content"]
        except (TypeError, KeyError):
            return payload.choices[0].message.content

    client_ctor = getattr(openai, "OpenAI", None)
    if client_ctor is not None:
        try:
            with httpx.Client(proxy=None, trust_env=False) as http_client:
                client = client_ctor(api_key=api_key, http_client=http_client)

                responses_api = getattr(client, "responses", None)
                if responses_api is not None and hasattr(responses_api, "create"):
                    try:
                        response = responses_api.create(
                            model=DEFAULT_MODEL,
                            input=[
                                {"role": "system", "content": system_message},
                                {"role": "user", "content": prompt},
                            ],
                            response_format={"type": "json_schema", "json_schema": schema},
                        )
                        text = response.output[0].content[0].text
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        logger.warning(
                            "Respuesta JSON inválida del endpoint responses: %s",
                            exc,
                            exc_info=True,
                        )
                    except Exception as exc:  # pragma: no cover - SDK/network specifics
                        logger.warning(
                            "OpenAI responses API falló, intentando fallback: %s",
                            exc,
                            exc_info=True,
                        )
                else:
                    logger.debug(
                        "Instancia OpenAI sin soporte para responses.create; se intentará chat.completions."
                    )

                chat_api = getattr(client, "chat", None)
                completions_api = getattr(chat_api, "completions", None) if chat_api else None
                if completions_api is not None and hasattr(completions_api, "create"):
                    try:
                        completion = completions_api.create(
                            model=CHAT_FALLBACK_MODEL,
                            temperature=0,
                            messages=messages,
                            response_format={"type": "json_schema", "json_schema": schema},
                        )
                        text = _parse_completion(completion)
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        logger.error(
                            "Respuesta JSON inválida del chat fallback (client): %s",
                            exc,
                            exc_info=True,
                        )
                    except Exception as exc:  # pragma: no cover - SDK/network specifics
                        logger.error(
                            "Error al solicitar JSON vía client.chat.completions: %s",
                            exc,
                            exc_info=True,
                        )
        except AttributeError:
            # Instalada una versión previa del SDK sin soporte para OpenAI client moderno
            logger.debug(
                "El cliente OpenAI no expone la interfaz moderna; se usará ChatCompletion legado.",
                exc_info=True,
            )

    chat_completion_cls = getattr(openai, "ChatCompletion", None)
    if chat_completion_cls is not None and hasattr(chat_completion_cls, "create"):
        try:
            openai.api_key = api_key
            completion = chat_completion_cls.create(
                model=CHAT_FALLBACK_MODEL,
                temperature=0,
                messages=messages,
            )
            text = _parse_completion(completion)
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error(
                "Respuesta JSON inválida del fallback ChatCompletion legado: %s",
                exc,
                exc_info=True,
            )
        except Exception as exc:  # pragma: no cover - network/SDK failures
            logger.error(
                "Error al solicitar JSON a OpenAI mediante ChatCompletion legado: %s",
                exc,
                exc_info=True,
            )

    return None


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

    schema = {
        "name": "address_schema",
        "schema": {
            "type": "object",
            "properties": {"formatted_address": {"type": "string"}},
            "required": ["formatted_address"],
            "additionalProperties": False,
        },
    }
    prompt = (
        "Convierte las coordenadas en una dirección humana. "
        f"Latitud: {latitud}, Longitud: {longitud}."
    )
    system_message = (
        "Sos un asistente geográfico. Generá una descripción breve en español "
        "para las coordenadas proporcionadas."
    )

    return _solicitar_json_a_openai(
        api_key=api_key,
        prompt=prompt,
        schema=schema,
        system_message=system_message,
    )


def geocodificar_texto_llm(
    direccion: str,
    puntos_referencia: Iterable[Mapping[str, object]] | None = None,
    localidad_predeterminada: str | None = None,
) -> dict | None:
    """Genera coordenadas aproximadas para una dirección usando OpenAI.

    El modelo recibe la dirección cruda y una lista opcional de puntos de
    referencia (por ejemplo cámaras simuladas) y debe devolver un JSON con
    campos ``lat`` y ``lon`` en grados decimales. Se incluye un campo opcional
    ``confidence`` (0-1) y ``matched_reference`` con una breve explicación.
    """

    direccion = (direccion or "").strip()
    if not direccion:
        return None

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not configured for forward geocoding")
        return None

    anchors = []
    for punto in puntos_referencia or []:
        try:
            nombre = str(punto.get("nombre") or punto.get("id") or "")
            lat = float(punto.get("lat"))
            lon = float(punto.get("lon"))
        except (TypeError, ValueError):
            continue
        referencia = punto.get("ubicacion_referencia") or ""
        ciudad = punto.get("ciudad") or punto.get("municipio") or ""
        alias = ", ".join(punto.get("alias", []) or [])
        anchors.append(
            f"- {nombre} ({lat:.5f}, {lon:.5f}) cerca de {referencia} {ciudad} {alias}".strip()
        )

    contexto_camaras = "\n".join(anchors) if anchors else "- sin referencias explícitas"
    localidad_txt = f" en {localidad_predeterminada}" if localidad_predeterminada else ""

    prompt = (
        "Sos un asistente SIG que devuelve puntos para simular cámaras de estacionamiento. "
        "Entregá coordenadas realistas dentro de Mendoza y sus municipios. "
        "Preferí ubicarte cerca de los puntos de referencia listados si el texto coincide. "
        "La respuesta debe ser solo JSON válido.\n"
        f"Dirección del usuario: {direccion}.\n"
        f"Municipio preferido{localidad_txt}.\n"
        "Puntos de referencia conocidos:\n"
        f"{contexto_camaras}"
    )

    schema = {
        "name": "parking_forward_geocode",
        "schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "confidence": {
                    "type": "number",
                    "description": "Confianza entre 0 y 1"
                },
                "matched_reference": {"type": "string"},
                "normalized_query": {"type": "string"},
            },
            "required": ["lat", "lon"],
            "additionalProperties": False,
        },
    }

    system_message = (
        "Sos un asistente SIG experto en Mendoza y sus municipios. "
        "Debés devolver únicamente JSON con latitud, longitud y metadatos."
    )

    return _solicitar_json_a_openai(
        api_key=api_key,
        prompt=prompt,
        schema=schema,
        system_message=system_message,
    )

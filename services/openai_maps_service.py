import os
import json
import logging
from typing import Iterable, Mapping

from services.openai_model_defaults import (
    DEFAULT_OPENAI_TERRA_MODEL,
    chat_completion_compatibility_options,
    resolve_openai_model,
)
from services.llm_provider_network_policy import llm_provider_network_allowed
from utils.lazy_module import LazyModule

openai = LazyModule("openai")
httpx = LazyModule("httpx")

logger = logging.getLogger(__name__)

DEFAULT_MODEL = DEFAULT_OPENAI_TERRA_MODEL
CHAT_FALLBACK_MODEL = DEFAULT_OPENAI_TERRA_MODEL
# The module-level ChatCompletion API belongs to SDK 0.x and cannot express the
# GPT-5.6 reasoning contract reliably. Keep this model pinned only for that
# explicit legacy compatibility branch.
LEGACY_CHAT_FALLBACK_MODEL = "gpt-4o-mini"


def _solicitar_json_a_openai(
    *,
    api_key: str,
    prompt: str,
    schema: dict,
    system_message: str,
):
    """Ask OpenAI for schema-constrained JSON with capability-only fallbacks."""

    if not llm_provider_network_allowed("openai"):
        logger.info(
            "LLM provider request skipped provider=openai "
            "capability=geocoding reason=test_network_disabled"
        )
        return None

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

    def _parse_response(payload):
        direct = getattr(payload, "output_text", None)
        if direct:
            return direct
        return payload.output[0].content[0].text

    client_ctor = getattr(openai, "OpenAI", None)
    if client_ctor is not None:
        try:
            with httpx.Client(proxy=None, trust_env=False) as http_client:
                client = client_ctor(api_key=api_key, http_client=http_client)

                responses_api = getattr(client, "responses", None)
                if responses_api is not None and hasattr(responses_api, "create"):
                    model = resolve_openai_model("OPENAI_MAP_MODEL", DEFAULT_MODEL)
                    try:
                        response = responses_api.create(
                            model=model,
                            input=[
                                {"role": "system", "content": system_message},
                                {"role": "user", "content": prompt},
                            ],
                            store=False,
                            text={
                                "format": {
                                    "type": "json_schema",
                                    "name": schema.get("name") or "map_result",
                                    "schema": schema.get("schema") or {},
                                    "strict": True,
                                }
                            },
                        )
                        text = _parse_response(response)
                        return json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning(
                            "OpenAI map Responses output was invalid JSON model=%s",
                            model,
                        )
                        return None
                    except Exception as exc:  # pragma: no cover - SDK/network specifics
                        logger.warning(
                            "OpenAI map Responses request failed model=%s error_type=%s fallback_suppressed=true",
                            model,
                            type(exc).__name__,
                        )
                        return None
                else:
                    logger.debug(
                        "Instancia OpenAI sin soporte para responses.create; se intentará chat.completions."
                    )

                chat_api = getattr(client, "chat", None)
                completions_api = getattr(chat_api, "completions", None) if chat_api else None
                if completions_api is not None and hasattr(completions_api, "create"):
                    model = resolve_openai_model(
                        "OPENAI_MAP_CHAT_MODEL",
                        CHAT_FALLBACK_MODEL,
                    )
                    try:
                        request_kwargs = {
                            "model": model,
                            "messages": messages,
                            "response_format": {
                                "type": "json_schema",
                                "json_schema": schema,
                            },
                        }
                        request_kwargs.update(
                            chat_completion_compatibility_options(model)
                        )
                        completion = completions_api.create(**request_kwargs)
                        text = _parse_completion(completion)
                        return json.loads(text)
                    except json.JSONDecodeError:
                        logger.error(
                            "OpenAI map Chat output was invalid JSON model=%s",
                            model,
                        )
                        return None
                    except Exception as exc:  # pragma: no cover - SDK/network specifics
                        logger.error(
                            "OpenAI map Chat request failed model=%s error_type=%s fallback_suppressed=true",
                            model,
                            type(exc).__name__,
                        )
                        return None
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
                model=LEGACY_CHAT_FALLBACK_MODEL,
                temperature=0,
                messages=messages,
            )
            text = _parse_completion(completion)
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error(
                "Legacy OpenAI map ChatCompletion output was invalid JSON",
            )
        except Exception as exc:  # pragma: no cover - network/SDK failures
            logger.error(
                "Legacy OpenAI map ChatCompletion failed error_type=%s",
                type(exc).__name__,
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

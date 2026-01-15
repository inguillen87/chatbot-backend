import os
import openai
import logging
import json
import httpx
from typing import List, Dict, Optional, Any

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

def llamar_openai(app, mensaje_usuario: str, usuario: dict, historial: list, chat_session_id: str) -> tuple[dict, dict]:
    """
    Calls the OpenAI API and formats the response to be compatible with the application's structure.
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

    total_prompt_tokens = sum(_estimate_tokens(m.get("content", "")) for m in messages)
    logger.info(
        f"Sending to OpenAI. Message: {message[:100]}... Estimated prompt tokens: {total_prompt_tokens}"
    )

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
        # Re-raise the exception to trigger the fallback mechanism
        raise

def detect_intent_municipio(texto_usuario: str) -> Optional[str]:
    """
    Uses OpenAI to classify the user's intent into a predefined set of municipal categories.
    """
    if not client:
        logger.warning("OpenAI client not initialized; skipping LLM intent detection.")
        return None

    prompt = f"""
    Eres un clasificador de intenciones para un chatbot municipal.
    Analiza el siguiente texto y determina la intención más probable.

    Categorías posibles:
    - iniciar_reclamo: Reportar un problema (basura, luces, baches, ruidos, etc).
    - consultar_estado_reclamo: Preguntar por el estado de un ticket existente.
    - ver_catalogo: Interés en comprar productos, ver el mercado, economía social.
    - enviar_sugerencia: Ideas o propuestas generales.
    - licencia_de_conducir: Preguntas sobre carnet, licencia, turnos de licencia.
    - solicitar_turnos: Turnos generales (no licencia).
    - pago_de_tasas_vigentes: Impuestos, deudas, pagos.
    - buscar_estacionamiento: Estacionamiento medido o lugares para aparcar.
    - agenda_y_noticias: Eventos culturales, noticias.
    - veterinaria_bromatologia: Mascotas, castraciones, bromatología.
    - obras: Consultas sobre obras públicas.
    - contactos_utiles: Teléfonos de emergencia, policía, etc.
    - punto_limpio: Reciclaje, botellas, puntos verdes.
    - saludo: Saludos simples sin otra intención.
    - desconocido: No encaja claramente en ninguna anterior.

    Texto del usuario: "{texto_usuario}"

    Responde SOLO con un JSON válido: {{"intent": "nombre_categoria_detectada", "confidence": 0.0_to_1.0}}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        confidence = data.get("confidence", 0)
        if confidence > 0.7:
            return data.get("intent")
        return None
    except Exception as e:
        logger.error(f"Error in detect_intent_municipio: {e}")
        return None

def extraer_datos_reclamo_llm(texto: str, campos_interes: List[str] = None) -> Dict[str, Any]:
    """
    Extracts structured data (name, phone, address, description) from unstructured text.
    """
    if not client:
        return {}

    if not campos_interes:
        campos_interes = ["nombre", "telefono", "email", "direccion", "descripcion", "categoria"]

    prompt = f"""
    Extrae la siguiente información del texto del usuario para un reclamo municipal.
    Campos buscados: {', '.join(campos_interes)}.

    Texto: "{texto}"

    Si un dato no está presente, usa null. Normaliza los teléfonos a formato numérico si es posible.
    Responde SOLO JSON.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Error in extraer_datos_reclamo_llm: {e}")
        return {}

def generar_respuesta_municipio_llm(contexto: str, pregunta: str) -> str:
    """
    Generates a helpful response based on context (e.g., FAQ docs) and the user's question.
    """
    if not client:
        return "Lo siento, no puedo procesar tu consulta en este momento."

    prompt = f"""
    Eres el asistente virtual de un municipio. Responde a la pregunta del vecino basándote en el siguiente contexto (si es relevante) o usando tu conocimiento general de forma cortés y servicial.

    Contexto:
    {contexto}

    Pregunta del vecino: "{pregunta}"

    Respuesta (breve, clara y amigable):
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error in generar_respuesta_municipio_llm: {e}")
        return "Hubo un error al generar la respuesta."

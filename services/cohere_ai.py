# services/cohere_ai.py
import os
import logging
import cohere
from functools import wraps
from time import sleep
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH_SIZE = 90
DEFAULT_MODEL = "command-r-plus"
DEFAULT_TEMPERATURE = 0.3

co_client = None
if COHERE_API_KEY:
    try:
        co_client = cohere.Client(COHERE_API_KEY, timeout=60)
        logger.info("[COHERE_CLIENT] Cliente de Cohere inicializado exitosamente.")
    except Exception as e:
        logger.critical(f"[COHERE_CLIENT] CRÍTICO: No se pudo inicializar el cliente de Cohere. Error: {e}")
else:
    logger.warning("[COHERE_CLIENT] ADVERTENCIA: La variable de entorno COHERE_API_KEY no está configurada.")


def cohere_api_call(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not co_client:
            logger.error(f"❌ [COHERE_CLIENT] Intento de llamar a '{func.__name__}' pero el cliente no está disponible.")
            if "embed" in func.__name__:
                return []
            return None
        try:
            return func(*args, **kwargs)
        except cohere.errors.CohereAPIError as e:
            logger.error(f"❌ [COHERE_CLIENT] Error de API en '{func.__name__}': Status {getattr(e, 'http_status', 'N/A')} - {e.message}", exc_info=False)
            return None
        except Exception as e:
            logger.error(f"❌ [COHERE_CLIENT] Error inesperado en '{func.__name__}': {e}", exc_info=True)
            return None
    return wrapper


@cohere_api_call
def robust_embed(textos: List[str], input_type: str = "search_document") -> Optional[List[List[float]]]:
    if not textos or not isinstance(textos, list) or not all(isinstance(t, str) for t in textos):
        logger.error("❌ [COHERE EMBED] Entrada inválida: se esperaba una lista de strings.")
        return []

    all_embeddings: List[List[float]] = []
    model_embed = "embed-multilingual-v3.0"
    logger.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Modelo: {model_embed}, Tipo: {input_type}")

    for i in range(0, len(textos), COHERE_EMBED_BATCH_SIZE):
        batch = textos[i:i + COHERE_EMBED_BATCH_SIZE]
        logger.info(f"➡️  [COHERE EMBED] Procesando batch {i//COHERE_EMBED_BATCH_SIZE + 1}...")
        response = co_client.embed(texts=batch, model=model_embed, input_type=input_type, truncate="END")

        if response.embeddings:
            all_embeddings.extend(response.embeddings)
        else:
            logger.warning(f"⚠️ [COHERE EMBED] El batch {i//COHERE_EMBED_BATCH_SIZE + 1} no devolvió embeddings.")

        if len(textos) > COHERE_EMBED_BATCH_SIZE:
            sleep(0.3)

    logger.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)}.")
    return all_embeddings


@cohere_api_call
def robust_chat(message: str, preamble: Optional[str] = None, chat_history: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
    if not message or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje del usuario está vacío.")
        return "Por favor, realiza una consulta."

    chat_history_for_api_call: List[Dict[str, str]] = []
    if preamble:
        chat_history_for_api_call.append({"role": "SYSTEM", "message": preamble})
    if chat_history:
        history_corregido = [
            {"role": item["role"].upper().replace("ASSISTANT", "CHATBOT"), "message": item["content"]}
            for item in chat_history
        ]
        chat_history_for_api_call.extend(history_corregido)

    logger.info(
        f"➡️ [COHERE CHAT CALL] Modelo: '{DEFAULT_MODEL}'. Mensaje: '{message[:70]}...'. Historial: {len(chat_history_for_api_call)} msgs."
    )

    response = co_client.chat(
        message=message,
        chat_history=chat_history_for_api_call,
        model=DEFAULT_MODEL,
        temperature=DEFAULT_TEMPERATURE,
    )

    return response.text.strip() if response else None


def get_cohere_response(
    message: str,
    chat_history: Optional[List[Dict[str, str]]] = None,
    preamble: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    **_: Any,
) -> Optional[str]:
    """Mantiene compatibilidad con la API anterior usando ``robust_chat``."""
    return robust_chat(message=message, preamble=preamble, chat_history=chat_history)

# Retrocompatibilidad con funciones antiguas
embed_textos = robust_embed


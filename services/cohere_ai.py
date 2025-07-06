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


import json # Añadido para Opción 2

@cohere_api_call
def robust_chat(
    message: str,
    preamble: Optional[str] = None,
    chat_history: Optional[List[Dict[str, str]]] = None,
    context_summary: Optional[Dict[str, Any]] = None # Nuevo parámetro
) -> Optional[str]:
    if not message or not message.strip():
        logger.error("❌ [COHERE CHAT] Mensaje del usuario está vacío.")
        return "Por favor, realiza una consulta."

    chat_history_for_api_call: List[Dict[str, str]] = []
    if preamble: # Preamble general del sistema
        chat_history_for_api_call.append({"role": "SYSTEM", "message": preamble})

    if context_summary: # Insertar el resumen del contexto como un mensaje del sistema
        context_lines = ["Resumen del Contexto Actual de la Conversación (información recordada):"]
        for key, value in context_summary.items():
            # Evitar mostrar listas o dicts muy largos o vacíos directamente
            if isinstance(value, list):
                if value: # Si la lista no está vacía
                    # Mostrar solo los primeros N elementos para brevedad si es una lista larga
                    display_list = [str(v) for v in value[:3]] # Mostrar hasta 3 elementos
                    if len(value) > 3:
                        display_list.append("...")
                    context_lines.append(f"- {key.replace('_', ' ').capitalize()}: {', '.join(display_list)}")
                # Si la lista está vacía, podríamos optar por no mostrarla o indicarlo
                # else: context_lines.append(f"- {key.replace('_', ' ').capitalize()}: (vacío)")
            elif isinstance(value, dict):
                if value: # Si el dict no está vacío
                    # Formatear dict de forma legible y breve
                    dict_str = ", ".join([f"{k_sub}: {v_sub}" for k_sub, v_sub in list(value.items())[:2]]) # Mostrar hasta 2 pares
                    if len(value) > 2:
                        dict_str += ", ..."
                    context_lines.append(f"- {key.replace('_', ' ').capitalize()}: {{{dict_str}}}")
                # else: context_lines.append(f"- {key.replace('_', ' ').capitalize()}: {{}}")
            elif value is not None and str(value).strip(): # Para otros tipos, si no son None o string vacío
                context_lines.append(f"- {key.replace('_', ' ').capitalize()}: {value}")

        if len(context_lines) > 1: # Si hay algo más que el título "Resumen del Contexto Actual..."
             # Asegurar que el mensaje del sistema no sea excesivamente largo
            system_context_message = "\n".join(context_lines)
            MAX_SYSTEM_CONTEXT_LENGTH = 1000 # Ajustable
            if len(system_context_message) > MAX_SYSTEM_CONTEXT_LENGTH:
                system_context_message = system_context_message[:MAX_SYSTEM_CONTEXT_LENGTH] + "\n...(contexto truncado)"
            chat_history_for_api_call.append({"role": "SYSTEM", "message": system_context_message})
            logger.debug(f"[COHERE CHAT] Contexto del sistema añadido: {system_context_message}")


    if chat_history: # Historial de mensajes de usuario/chatbot
        history_corregido = [
            # Asegurarse que el rol sea USER o CHATBOT según la API de Cohere
            {"role": "USER" if item["role"].upper() == "USER" else "CHATBOT", "message": item["content"]}
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
    model: str = DEFAULT_MODEL, # model y temperature no son usados por robust_chat actualmente
    temperature: float = DEFAULT_TEMPERATURE, # podrían pasarse si robust_chat los aceptara
    **kwargs: Any, # Cambiado de _kwargs a kwargs para claridad
) -> Optional[str]:
    """Mantiene compatibilidad con la API anterior usando ``robust_chat``.
    Ahora también pasa kwargs (como context_summary) a robust_chat.
    """
    # Extraer context_summary de kwargs si está presente
    context_summary_data = kwargs.get("context_summary")

    # Aquí, model y temperature no se pasan explícitamente a robust_chat
    # porque robust_chat ya usa DEFAULT_MODEL y DEFAULT_TEMPERATURE internamente.
    # Si quisiéramos que robust_chat fuera más configurable, necesitaría aceptar model y temperature.
    return robust_chat(
        message=message,
        preamble=preamble,
        chat_history=chat_history,
        context_summary=context_summary_data # Pasar el context_summary
    )

# Retrocompatibilidad con funciones antiguas
embed_textos = robust_embed


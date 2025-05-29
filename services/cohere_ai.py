# services/cohere_ai.py

import os
import logging
import requests # Para embed_textos
from time import sleep # Para embed_textos
import cohere

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH = 50 # Para la función embed_textos

# --- Tu función embed_textos (se mantiene como la tienes) ---
def embed_textos(textos: list[str]) -> list[list[float]]:
    if not COHERE_API_KEY:
        logging.error("[COHERE EMBED] COHERE_API_KEY no está configurada.")
        return []
    url = "https://api.cohere.ai/v1/embed"
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }
    model = "embed-multilingual-v3.0"
    input_type = "search_document"
    all_embeddings = []
    if not textos or not isinstance(textos, list):
        logging.error("❌ [COHERE EMBED] Lista de textos vacía o inválida.")
        return []
    logging.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Batch size: {COHERE_EMBED_BATCH}")
    for i in range(0, len(textos), COHERE_EMBED_BATCH):
        batch = textos[i:i+COHERE_EMBED_BATCH]
        payload = {"texts": batch, "model": model, "input_type": input_type }
        logging.info(f"➡️ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH + 1}: {len(batch)} textos.")
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            logging.info(f"⬅️ [COHERE EMBED] Status: {response.status_code}")
            response.raise_for_status()
            embeddings_data = response.json().get("embeddings", [])
            logging.info(f"⬅️ [COHERE EMBED] Vectores devueltos en batch: {len(embeddings_data)}")
            if not embeddings_data: logging.error("❌ [COHERE EMBED] Batch sin embeddings.")
            all_embeddings.extend(embeddings_data)
            if len(textos) > COHERE_EMBED_BATCH : sleep(0.5)
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ [COHERE EMBED] Error de red en batch {i//COHERE_EMBED_BATCH + 1}: {e}")
        except Exception as e:
            logging.error(f"❌ [COHERE EMBED] Error genérico en batch {i//COHERE_EMBED_BATCH + 1}: {e}")
    logging.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)} / {len(textos)}")
    return all_embeddings
# --- Fin de embed_textos ---


def get_cohere_response(messages_for_llm: list, rubro_id: int, user_context: dict) -> str:
    """
    Obtiene una respuesta de chat de la API de Cohere.
    Las instrucciones del sistema se incluyen como el primer mensaje en chat_history.
    """
    logging.info(f"➡️ [COHERE CHAT ATTEMPT] Iniciando get_cohere_response. {len(messages_for_llm)} mensajes recibidos.")

    local_cohere_api_key = os.getenv("COHERE_API_KEY")
    if not local_cohere_api_key:
        logging.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada.")
        return ""

    try:
        co_client = cohere.Client(local_cohere_api_key)
        logging.info("✅ [COHERE CHAT] Cliente de Cohere inicializado/verificado para esta llamada.")
    except Exception as e:
        logging.error(f"❌ [COHERE CHAT] Error al inicializar el cliente de Cohere: {e}", exc_info=True)
        return ""

    chat_history_for_api = []
    current_user_message = ""
    system_instructions_content = ""

    if not messages_for_llm:
        logging.warning("[COHERE CHAT] Lista de mensajes para LLM está vacía.")
        return ""

    # Procesar messages_for_llm para separar instrucciones del sistema, historial y mensaje actual
    if messages_for_llm[0]["role"] == "system":
        system_instructions_content = messages_for_llm[0]["content"]
        # El historial "real" son los mensajes entre el sistema y el último del usuario
        for msg in messages_for_llm[1:-1]:
            role_cohere = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_for_api.append({"role": role_cohere, "message": msg["content"]})
        if messages_for_llm[-1]["role"] == "user":
            current_user_message = messages_for_llm[-1]["content"]
        else:
            logging.error("[COHERE CHAT] El último mensaje procesado (después del sistema) no es del usuario.")
            return ""
    else: # No hay mensaje "system", todo es historial y pregunta actual
        logging.info("[COHERE CHAT] No se encontró mensaje de 'system'. Todo es historial y pregunta.")
        for msg in messages_for_llm[:-1]:
            role_cohere = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_for_api.append({"role": role_cohere, "message": msg["content"]})
        if messages_for_llm[-1]["role"] == "user":
            current_user_message = messages_for_llm[-1]["content"]
        else:
            logging.error("[COHERE CHAT] El último mensaje procesado (sin sistema) no es del usuario.")
            return ""

    if not current_user_message:
        logging.warning("[COHERE CHAT] Mensaje actual del usuario está vacío. No se llamará a la API.")
        return ""

    # Integrar las instrucciones del sistema en el chat_history si existen
    # Algunos modelos esperan el rol "SYSTEM", otros "USER" para las instrucciones.
    # Prueba con "SYSTEM" primero. Si da error o no funciona bien, prueba con "USER".
    final_chat_history_for_api = []
    if system_instructions_content:
        final_chat_history_for_api.append({"role": "SYSTEM", "message": system_instructions_content})
        # Alternativamente, si "SYSTEM" no es bien interpretado por tu modelo/versión:
        # final_chat_history_for_api.append({"role": "USER", "message": f"Instrucciones importantes: {system_instructions_content}\n\nAhora empieza la conversación:"})
    
    final_chat_history_for_api.extend(chat_history_for_api) # Añadir el historial de usuario/chatbot

    try:
        logging.info(f"➡️ [COHERE CHAT CALL] Usando modelo 'command-r'. Mensaje: '{current_user_message}'. Historial para API (incluye sistema si se añadió): {len(final_chat_history_for_api)} mensajes.")
        
        # Llamada a co.chat SIN el parámetro 'preamble'
        response = co_client.chat(
            message=current_user_message,
            chat_history=final_chat_history_for_api, # Aquí ya van las instrucciones del sistema (si las hubo)
            model="command-r", # Asegúrate que este es el modelo correcto y disponible
            temperature=0.3
            # Otros parámetros como 'connectors', 'documents' pueden ir aquí si los necesitas
        )

        respuesta_texto = response.text
        logging.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): {respuesta_texto[:200]}")
        return respuesta_texto

    except cohere.CohereAPIError as e: # Errores específicos de la API
        logging.error(f"❌ [COHERE CHAT] Error de API Cohere: Status {e.http_status} - {e.message}", exc_info=True)
        # Puedes añadir manejo específico para ciertos códigos de error, ej. rate limits (429)
    except Exception as e: # Otros errores (red, etc.)
        logging.error(f"❌ [COHERE CHAT] Error genérico durante la llamada a Cohere: {e}", exc_info=True)

    return ""
# services/cohere_ai.py

import os
import logging
import requests # Lo necesitas para embed_textos
from time import sleep # Lo necesitas para embed_textos
import cohere

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH = 50

# NO inicialices el cliente 'co' globalmente aquí si sospechamos problemas.
# Lo haremos dentro de la función get_cohere_response para asegurar que se intenta con cada llamada.

def embed_textos(textos: list[str]) -> list[list[float]]:
    # ... (tu función embed_textos se mantiene igual que antes, ya que funciona para embeddings) ...
    # Solo asegúrate de que COHERE_API_KEY se esté cargando bien para esta función también.
    # Para brevedad, no la repito aquí, pero debe estar presente en tu archivo.
    # TU CÓDIGO DE embed_textos VA AQUÍ
    # (El que me pasaste antes y que usa requests.post para el endpoint /v1/embed)
    if not COHERE_API_KEY: # Chequeo adicional
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


def get_cohere_response(messages_for_llm: list, rubro_id: int, user_context: dict) -> str:
    logging.info(f"➡️ [COHERE CHAT ATTEMPT] Iniciando get_cohere_response. {len(messages_for_llm)} mensajes recibidos.")

    local_cohere_api_key = os.getenv("COHERE_API_KEY")
    if not local_cohere_api_key:
        logging.error("❌ [COHERE CHAT] COHERE_API_KEY no está configurada en el entorno.")
        return ""

    try:
        # Intenta inicializar el cliente aquí para cada llamada (para depuración)
        # En producción normal, un cliente global es mejor, pero esto ayuda a aislar.
        co_client = cohere.Client(local_cohere_api_key)
        logging.info("✅ [COHERE CHAT] Cliente de Cohere reinicializado para esta llamada.")
    except Exception as e:
        logging.error(f"❌ [COHERE CHAT] Error al inicializar el cliente de Cohere: {e}", exc_info=True)
        return ""

    preamble_text = None
    chat_history_for_cohere = []
    current_user_message = ""

    if not messages_for_llm:
        logging.warning("[COHERE CHAT] Lista de mensajes para LLM está vacía.")
        return ""

    if messages_for_llm[0]["role"] == "system":
        preamble_text = messages_for_llm[0]["content"]
        for msg in messages_for_llm[1:-1]:
            role_cohere = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_for_cohere.append({"role": role_cohere, "message": msg["content"]})
        if messages_for_llm[-1]["role"] == "user":
            current_user_message = messages_for_llm[-1]["content"]
        else:
            logging.warning("[COHERE CHAT] El último mensaje en messages_for_llm no es del usuario.")
            return ""
    else:
        logging.info("[COHERE CHAT] No se encontró mensaje de sistema. Usando mensajes directamente.")
        for msg in messages_for_llm[:-1]:
            role_cohere = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_for_cohere.append({"role": role_cohere, "message": msg["content"]})
        if messages_for_llm[-1]["role"] == "user":
            current_user_message = messages_for_llm[-1]["content"]
        else:
            logging.warning("[COHERE CHAT] El último mensaje no es del usuario y no hay system prompt.")
            return ""

    if not current_user_message:
        logging.warning("[COHERE CHAT] Mensaje actual del usuario está vacío.")
        return ""

    try:
        logging.info(f"➡️ [COHERE CHAT CALL] Mensaje: '{current_user_message}'. Historial: {len(chat_history_for_cohere)} mensajes. Preamble: '{preamble_text[:100] if preamble_text else 'None'}...'")

        # Construir los argumentos para co.chat()
        chat_args = {
            "message": current_user_message,
            "chat_history": chat_history_for_cohere,
            # "model": "command-r", # Puedes especificar el modelo aquí
            "temperature": 0.3
        }
        # Solo añadir preamble si existe, para evitar pasar preamble=None si eso causa el TypeError
        if preamble_text:
            chat_args["preamble"] = preamble_text

        response = co_client.chat(**chat_args) # Usar co_client instanciado aquí

        respuesta_texto = response.text
        logging.info(f"⬅️ [COHERE CHAT] Respuesta API (primeros 200 chars): {respuesta_texto[:200]}")
        return respuesta_texto

    except TypeError as te:
        logging.error(f"❌ [COHERE CHAT] TypeError al llamar a co.chat(): {te}. Argumentos enviados: {chat_args}", exc_info=True)
        logging.error("    Esto usualmente indica una incompatibilidad con la versión de la librería Cohere o el modelo. Intenta actualizar la librería 'cohere'.")
        if 'preamble' in str(te):
             logging.error("    El error específico es sobre 'preamble'. Si actualizar la librería no funciona, se deberá modificar cómo se envían las instrucciones del sistema.")
    except cohere.CohereAPIError as e:
        logging.error(f"❌ [COHERE CHAT] Error de API: Status {e.http_status} - {e.message}", exc_info=True)
    except Exception as e:
        logging.error(f"❌ [COHERE CHAT] Error genérico: {e}", exc_info=True)

    return ""
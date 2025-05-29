# services/cohere_ai.py

import os
import logging
import requests
from time import sleep
import cohere # Importar la librería oficial de Cohere

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH = 50  # Para la función embed_textos

# Inicializar el cliente de Cohere una vez cuando se carga el módulo
try:
    if not COHERE_API_KEY:
        logging.error("La variable de entorno COHERE_API_KEY no está configurada.")
        co = None
    else:
        co = cohere.Client(COHERE_API_KEY)
        logging.info("Cliente de Cohere inicializado correctamente.")
except Exception as e:
    logging.error(f"Error al inicializar el cliente de Cohere. Error: {e}")
    co = None

def embed_textos(textos: list[str]) -> list[list[float]]:
    """
    Devuelve embeddings de Cohere para una lista de textos.
    Hace el request en batches si hay muchos textos.
    (Esta es tu función original, la mantenemos tal cual la tenías)
    """
    if not co: # Chequeo adicional por si la inicialización del cliente falló
        logging.error("Cliente de Cohere no disponible para embed_textos.")
        return []
        
    url = "https://api.cohere.ai/v1/embed" # Endpoint de Embeddings
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }
    model = "embed-multilingual-v3.0" # Modelo de embeddings
    input_type = "search_document" # Tipo de input para embeddings

    all_embeddings = []

    if not textos or not isinstance(textos, list):
        logging.error("❌ [COHERE EMBED] Lista de textos vacía o inválida.")
        return []

    logging.info(f"➡️ [COHERE EMBED] Solicitando embeddings para {len(textos)} textos. Batch size: {COHERE_EMBED_BATCH}")

    for i in range(0, len(textos), COHERE_EMBED_BATCH):
        batch = textos[i:i+COHERE_EMBED_BATCH]
        payload = {
            "texts": batch,
            "model": model,
            "input_type": input_type
        }
        logging.info(f"➡️ [COHERE EMBED] Batch {i//COHERE_EMBED_BATCH + 1}: {len(batch)} textos.")
        try:
            # Para embed_textos, sigues usando requests directamente como en tu código original.
            # Si quisieras usar co.embed() de la librería, la lógica cambiaría un poco.
            # Por ahora, mantengo tu implementación original para embed_textos.
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            logging.info(f"⬅️ [COHERE EMBED] Status: {response.status_code}")
            response.raise_for_status()
            embeddings_data = response.json().get("embeddings", [])
            logging.info(f"⬅️ [COHERE EMBED] Vectores devueltos en batch: {len(embeddings_data)}")
            if not embeddings_data:
                logging.error("❌ [COHERE EMBED] Batch sin embeddings.")
            all_embeddings.extend(embeddings_data)
            if len(textos) > COHERE_EMBED_BATCH : # Solo hacer sleep si hay más de un batch
                 sleep(0.5) 
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ [COHERE EMBED] Error de red en batch {i//COHERE_EMBED_BATCH + 1}: {e}")
        except Exception as e:
            logging.error(f"❌ [COHERE EMBED] Error genérico en batch {i//COHERE_EMBED_BATCH + 1}: {e}")
            # Considera si quieres reintentar o devolver los embeddings parciales. Por ahora, continúa.

    logging.info(f"✅ [COHERE EMBED] Embeddings totales generados: {len(all_embeddings)} / {len(textos)}")
    return all_embeddings


def get_cohere_response(messages_for_llm: list, rubro_id: int, user_context: dict) -> str:
    """
    Obtiene una respuesta de chat de la API de Cohere usando la librería oficial.

    Args:
        messages_for_llm: Lista de mensajes. El primer mensaje PUEDE ser el del sistema.
        rubro_id: ID del rubro.
        user_context: Diccionario con contexto del usuario/PYME.

    Returns:
        La respuesta textual del modelo de chat o una cadena vacía en caso de error.
    """
    if co is None:
        logging.error("El cliente de Cohere no está inicializado. No se puede procesar la solicitud de chat.")
        return ""

    preamble = None 
    chat_history_for_cohere = []
    current_user_message = ""

    if not messages_for_llm:
        logging.warning("[COHERE CHAT] Lista de mensajes para LLM está vacía.")
        return ""

    if messages_for_llm[0]["role"] == "system":
        preamble = messages_for_llm[0]["content"]
        for msg in messages_for_llm[1:-1]: 
            role_cohere = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_for_cohere.append({"role": role_cohere, "message": msg["content"]})
        if messages_for_llm[-1]["role"] == "user":
             current_user_message = messages_for_llm[-1]["content"]
        else:
            logging.warning("[COHERE CHAT] El último mensaje en messages_for_llm no es del usuario.")
            return ""
    else: 
        logging.info("[COHERE CHAT] No se encontró mensaje de sistema. Usando mensajes directamente para historial y pregunta.")
        for msg in messages_for_llm[:-1]:
            role_cohere = "USER" if msg["role"].lower() == "user" else "CHATBOT"
            chat_history_for_cohere.append({"role": role_cohere, "message": msg["content"]})
        if messages_for_llm[-1]["role"] == "user":
            current_user_message = messages_for_llm[-1]["content"]
        else:
            logging.warning("[COHERE CHAT] El último mensaje no es del usuario y no hay system prompt.")
            return ""

    if not current_user_message:
        logging.warning("[COHERE CHAT] No se pudo extraer el mensaje actual del usuario para Cohere.")
        return ""

    try:
        logging.info(f"➡️ [COHERE CHAT] Enviando a API. Preamble: '{preamble[:100] if preamble else 'N/A'}...'. Historial: {len(chat_history_for_cohere)} msgs. Actual: '{current_user_message}'")
        
        response = co.chat(
            message=current_user_message,
            chat_history=chat_history_for_cohere,
            preamble=preamble, 
            model="command-r", # Revisa que este sea el modelo que quieres usar
            temperature=0.3  
        )
        
        respuesta_texto = response.text
        logging.info(f"⬅️ [COHERE CHAT] Respuesta de API (primeros 200 chars): {respuesta_texto[:200]}")
        return respuesta_texto

    except cohere.CohereAPIError as e: 
        logging.error(f"❌ [COHERE CHAT] Error de API: {e.http_status} - {e.message}", exc_info=True)
    except Exception as e:
        logging.error(f"❌ [COHERE CHAT] Error genérico: {e}", exc_info=True)
    
    return ""
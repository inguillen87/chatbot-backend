import requests
import os
import logging

COHERE_API_KEY = os.getenv("COHERE_API_KEY")

def get_cohere_response(messages: list[dict], rubro_id=None) -> str:
    if not COHERE_API_KEY:
        logging.warning("⚠️ COHERE_API_KEY no está configurada.")
        return "Lo siento, no puedo responder en este momento."

    if not isinstance(messages, list) or not messages:
        logging.warning("⚠️ Lista de mensajes vacía o inválida.")
        return "No se recibió ningún mensaje válido para procesar."

    # Extraer el último mensaje del usuario
    last_user_message = next((m["content"] for m in reversed(messages) if m.get("role") == "user" and "content" in m), "Hola")

    url = "https://api.cohere.ai/v1/chat"
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "message": last_user_message,
        "model": "command-r-plus",  # o "command-r" si da error
        "temperature": 0.3,
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            respuesta = response.json().get("text", "")
            logging.info(f"💬 Respuesta Cohere: {respuesta}")
            return respuesta or "Lo siento, no tengo una respuesta clara para eso."
        elif response.status_code == 401:
            logging.error("🔒 Error 401: No autorizado. Verificá tu API Key de Cohere.")
            return "No tengo autorización para responder. Por favor, contactá al administrador."
        elif response.status_code == 429:
            logging.warning("⏳ Error 429: Límite de uso alcanzado.")
            return "Se alcanzó el límite de consultas por ahora. Intentá más tarde."
        else:
            logging.warning(f"⚠️ Error Cohere {response.status_code}: {response.text}")
            return "Lo siento, no puedo responder en este momento."

    except Exception as e:
        logging.error(f"❌ Excepción al consultar Cohere: {e}")
        return "Lo siento, ocurrió un error al responder."

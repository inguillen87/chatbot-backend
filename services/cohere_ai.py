import requests
import os
import logging

COHERE_API_KEY = os.getenv("COHERE_API_KEY")

def get_cohere_response(messages: list[dict], rubro_id=None, user_context=None) -> str:
    if not COHERE_API_KEY:
        logging.warning("⚠️ COHERE_API_KEY no está configurada.")
        return "Lo siento, no puedo responder en este momento."

    if not isinstance(messages, list) or not messages:
        logging.warning("⚠️ Lista de mensajes vacía o inválida.")
        return "No se recibió ningún mensaje válido para procesar."

    # Extraer contexto del usuario
    nombre_empresa = user_context.get("nombre_empresa", "tu empresa")
    rubro_nombre = user_context.get("rubro_nombre", "general")
    plan = user_context.get("plan", "demo")

    # Prompt para control total del tono
    system_prompt = (
        f"Sos **Chatboc**, el asistente virtual profesional de la empresa '{nombre_empresa}', especializada en el rubro '{rubro_nombre}'. "
        f"Tu tarea es responder dudas de clientes como si fueras parte del equipo humano, usando un tono cálido, directo y profesional. "
        f"Nunca digas que sos una IA, un modelo de lenguaje, ni digas frases como 'esta respuesta fue generada automáticamente'. "
        f"Si no sabés algo, invitá al usuario a comunicarse con el equipo real. "
        f"Plan del cliente: {plan}. Siempre respondé en español neutro, sin tecnicismos, y usando ejemplos si es útil."
    )

    # Estructura del historial de chat
    chat_history = [{"role": "system", "message": system_prompt}] + [
        {"role": m["role"], "message": m["content"]}
        for m in messages
        if m.get("role") and m.get("content")
    ]

    # Último mensaje válido del usuario
    last_user_message = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user" and "content" in m),
        "Hola"
    )

    # Configuración de la API de Cohere
    url = "https://api.cohere.ai/v1/chat"
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "message": last_user_message,
        "model": "command-r-plus",
        "temperature": 0.3,
        "chat_history": chat_history,
        "prompt_truncation": "auto"
    }

    try:
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 200:
            respuesta = response.json().get("text", "").strip()

            # Filtros de seguridad (idioma, longitud, utilidad)
            if any(w in respuesta.lower() for w in ["the", "you can", "hospital", "insurance", "thank you"]):
                raise ValueError("Respuesta en inglés detectada.")
            if len(respuesta.split()) < 3:
                raise ValueError("Respuesta demasiado corta.")
            if "lo siento" in respuesta.lower() and "podés" not in respuesta.lower():
                raise ValueError("Respuesta tipo disculpa sin acción.")

            logging.info(f"💬 Respuesta Cohere: {respuesta}")
            return respuesta or "Lo siento, no tengo una respuesta clara para eso."

        elif response.status_code == 401:
            logging.error("🔒 Error 401: API Key inválida.")
            return "No tengo autorización para responder."

        elif response.status_code == 429:
            logging.warning("⏳ Límite de uso alcanzado.")
            return "Se alcanzó el límite de consultas. Intentá más tarde."

        else:
            logging.warning(f"⚠️ Error Cohere {response.status_code}: {response.text}")
            return "Lo siento, no puedo responder en este momento."

    except Exception as e:
        logging.error(f"❌ Excepción al consultar Cohere: {e}")
        return "Lo siento, ocurrió un error inesperado al responder."

def embed_textos(textos: list[str]) -> list[list[float]]:
    url = "https://api.cohere.ai/v1/embed"
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
    "texts": textos,
    "model": "embed-multilingual-v3.0",
    "input_type": "search_document"
}



    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json().get("embeddings", [])
    except Exception as e:
        logging.error(f"❌ Error al obtener embeddings: {e}")
        return []

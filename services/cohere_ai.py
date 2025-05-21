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

    # Contexto del usuario y empresa
    nombre_empresa = user_context.get("nombre_empresa", "la empresa")
    rubro_nombre = user_context.get("rubro_nombre", "general")
    plan = user_context.get("plan", "demo")

    system_prompt = (
        f"Sos Chatboc, el asistente virtual oficial de la empresa '{nombre_empresa}', que trabaja en el rubro '{rubro_nombre}'. "
        f"Respondé como si fueras parte real del equipo. Usá lenguaje natural, directo y en español. "
        f"Nunca digas que sos una IA ni que esta respuesta fue generada automáticamente. "
        f"Si no sabés algo, pedí más detalles o indicá que se puede consultar al equipo humano. "
        f"Plan actual del cliente: {plan}."
    )

    # Insertar mensaje system al inicio del chat
    chat_history = [{"role": "system", "message": system_prompt}] + [
        {"role": m["role"], "message": m["content"]}
        for m in messages
        if m.get("role") and m.get("content")
    ]

    last_user_message = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user" and "content" in m),
        "Hola"
    )

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

            if any(w in respuesta.lower() for w in ["the", "you can", "hospital", "insurance", "thank you"]):
                raise ValueError("Respuesta en inglés detectada.")
            if len(respuesta.split()) < 3:
                raise ValueError("Respuesta demasiado corta.")
            if "lo siento" in respuesta.lower() and "podés" not in respuesta.lower():
                raise ValueError("Respuesta tipo disculpa sin valor.")

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

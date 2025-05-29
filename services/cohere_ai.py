import os
import logging
import requests

COHERE_API_KEY = os.getenv("COHERE_API_KEY")

def embed_textos(textos: list[str]) -> list[list[float]]:
    """
    Devuelve embeddings de Cohere para una lista de textos.
    """
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
    logging.info(f"➡️ [COHERE] Solicitando embeddings para: {textos[:3]}")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=25)
        logging.info(f"⬅️ [COHERE] Status: {response.status_code}, Body: {response.text[:200]}")
        response.raise_for_status()
        embeddings = response.json().get("embeddings", [])
        logging.info(f"⬅️ [COHERE] Vectores devueltos: {len(embeddings)}")
        return embeddings
    except Exception as e:
        logging.error(f"❌ [COHERE] Error al obtener embeddings: {e}")
        return []

def get_cohere_chat_response(messages: list[dict], user_context=None) -> str:
    """
    Usa Cohere Chat para mantener conversación.
    """
    if not COHERE_API_KEY:
        logging.error("⚠️ COHERE_API_KEY no está configurada.")
        return "No puedo responder en este momento."

    if not messages or not isinstance(messages, list):
        logging.error("⚠️ Mensajes inválidos.")
        return "No se recibió ningún mensaje válido."

    nombre_empresa = user_context.get("nombre_empresa", "tu empresa") if user_context else "tu empresa"
    rubro_nombre = user_context.get("rubro_nombre", "general") if user_context else "general"
    plan = user_context.get("plan", "demo") if user_context else "demo"

    system_prompt = (
        f"Sos Chatboc, el asistente virtual profesional de '{nombre_empresa}' (rubro: '{rubro_nombre}'). "
        f"Respondé como si fueras del equipo humano, cálido, profesional y directo. Nunca digas que sos una IA."
        f" Plan del cliente: {plan}. Siempre respondé en español neutro."
    )

    chat_history = [{"role": "system", "message": system_prompt}] + [
        {"role": m["role"], "message": m["content"]}
        for m in messages if m.get("role") and m.get("content")
    ]
    last_user_message = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user" and "content" in m), "Hola"
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
    logging.info(f"➡️ [COHERE_CHAT] Mensaje: {last_user_message}")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        logging.info(f"⬅️ [COHERE_CHAT] Status: {response.status_code}, Body: {response.text[:200]}")
        if response.status_code == 200:
            respuesta = response.json().get("text", "").strip()
            if not respuesta or len(respuesta) < 3:
                raise ValueError("Respuesta vacía o muy corta de Cohere.")
            return respuesta
        elif response.status_code == 401:
            return "No tengo autorización para responder."
        elif response.status_code == 429:
            return "Se alcanzó el límite de consultas. Intentá más tarde."
        else:
            return "Lo siento, no puedo responder en este momento."
    except Exception as e:
        logging.error(f"❌ [COHERE_CHAT] Error: {e}")
        return "Ocurrió un error inesperado al responder."


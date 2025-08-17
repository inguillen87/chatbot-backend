import json
import logging

logger = logging.getLogger(__name__)

def build_interactive_response(options: list,
                               body_text: str,
                               channel: str,
                               message_type: str = 'text',
                               original_bot_response: dict = None,
                               **kwargs) -> dict:
    if original_bot_response is None:
        original_bot_response = {}

    # Use options_list from the bot response, fallback to the passed options
    options = original_bot_response.get('options_list', []) or options or []

    # Ensure options is a flat list of dictionaries
    if options and isinstance(options[0], list):
        options = [item for sublist in options for item in sublist]

    if channel == "whatsapp":
        # Per user request, always format as text to ensure options are always visible
        final_body = body_text or ""
        if options:
            options_text_parts = []
            for i, o in enumerate(options):
                if isinstance(o, dict) and o.get("texto"):
                    options_text_parts.append(f"*{i+1}*. {o.get('texto')}")

            if options_text_parts:
                final_body += "\n\n" + "\n".join(options_text_parts)
                final_body += "\n\n*➡️ Responde con el número de la opción que necesites.*"

        return {
            "type": "text",
            "text": {"body": final_body.strip()},
            "contexto_actualizado": original_bot_response.get("contexto_actualizado")
        }

    elif channel == "web":
        web_response = {
            "respuesta": body_text,
            "botones": [],
            "fuente": original_bot_response.get("fuente"),
            "contexto_actualizado": original_bot_response.get("contexto_actualizado"),
            "ticket_id": original_bot_response.get("ticket_id"),
            "es_publico": original_bot_response.get("es_publico"),
            "preguntas_usadas": original_bot_response.get("preguntas_usadas"),
            "limite_preguntas": original_bot_response.get("limite_preguntas"),
            "interpretacion_adjunto": original_bot_response.get("interpretacion_adjunto"),
            "estado_respuesta": original_bot_response.get("estado_respuesta"),
            "adjuntos": original_bot_response.get("adjuntos", []),
            "audio_url": original_bot_response.get("audio_url")
        }

        if message_type == 'interactive_menu' and original_bot_response.get("data"):
            web_response["menu"] = original_bot_response["data"]
        elif options:
            formatted_botones = []
            for o in options:
                btn = None
                if isinstance(o, str):
                    btn = {"texto": o, "action_id": o}
                elif isinstance(o, dict) and o.get("texto"):
                    action_id = o.get("id", o.get("action_id", o.get("texto")))
                    btn = {"texto": o.get("texto"), "action_id": action_id}
                    if o.get("type") == "url" and o.get("url"):
                        btn["url"] = o["url"]

                if btn:
                    if message_type == 'quick_replies':
                        btn["type"] = "quick_reply"
                    formatted_botones.append(btn)
            web_response["botones"] = formatted_botones
        return web_response
    else:
        logger.error(f"Canal desconocido: {channel}. No se pudo formatear la respuesta.")
        return {"error": f"Canal no soportado: {channel}"}

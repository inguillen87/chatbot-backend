import json
import logging

logger = logging.getLogger(__name__)

def build_interactive_response(options: list,
                               body_text: str,
                               channel: str,
                               message_type: str = 'text', # e.g. 'text', 'interactive_buttons', 'interactive_list'
                               original_bot_response: dict = None, # The full dict from responder_pyme/municipio
                               header_text: str = None,
                               footer_text: str = None,
                               audio_url: str = None,
                               ) -> dict:
    if original_bot_response is None:
        original_bot_response = {}

    # Prioritize options from original_bot_response if available
    if original_bot_response.get('botones'):
        options = original_bot_response['botones']
    elif original_bot_response.get('options_list'):
        options = original_bot_response['options_list']

    # Ensure options is a list and handle nesting
    if options is None:
        options = []
    if options and isinstance(options[0], list):
        # Flatten the list if it's nested (e.g., [[...]])
        options = [item for sublist in options for item in sublist]

    logger.debug(
        "build_interactive_response called | channel=%s | message_type=%s | num_options=%d | audio_url=%s",
        channel,
        message_type,
        len(options) if options else 0,
        audio_url,
    )

    # Track options sent so numeric replies can be mapped later
    context_update = original_bot_response.get("contexto_actualizado")
    if channel == "whatsapp" and options:
        # Store the raw option objects so we can resolve numeric responses
        context_update = (context_update or {}).copy()
        context_update["last_options_sent"] = options


    if channel == "whatsapp":
        num_options = len(options)
        final_message_type = message_type

        # 1. Decide the final message type
        if final_message_type != 'text':
            if 1 <= num_options <= 3:
                final_message_type = 'interactive_buttons'
            elif 4 <= num_options <= 10:
                final_message_type = 'interactive_list'
            else: # Fallback for 0 or > 10 options
                final_message_type = 'text'

        logger.debug(
            "WhatsApp flow | original_type=%s | final_type=%s | num_options=%d",
            message_type,
            final_message_type,
            num_options,
        )

        # 2. Build the payload based on the final message type
        payload = {"contexto_actualizado": context_update if context_update else None}
        if audio_url:
            payload["audio"] = {"link": audio_url}

        if final_message_type == 'text':
            final_body = body_text
            if options:
                # Logic for categorized menus
                categorias = original_bot_response.get("categorias")
                if categorias:
                    lines = []
                    counter = 1
                    for categoria in categorias:
                        titulo = categoria.get("titulo")
                        if titulo:
                            lines.append(f"*{titulo}*")
                        for boton in categoria.get("botones", []):
                            lines.append(f"*{counter}*. {boton.get('texto', '')}")
                            counter += 1
                    options_text = "\n\n" + "\n".join(lines)
                else: # Logic for simple menus
                    options_text = "\n\n" + "\n".join(
                        [f"*{i+1}*. {o.get('texto', '')}" for i, o in enumerate(options)]
                    )
                options_text += "\n\nResponde con el número de la opción que necesites."
                final_body += options_text

            payload.update({"type": "text", "text": {"body": final_body}})
            return payload

        # 3. Build interactive messages (Buttons or List)
        interactive_data = {
            "body": {"text": body_text},
            "action": {}
        }
        if header_text:
            interactive_data["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive_data["footer"] = {"text": footer_text}

        if final_message_type == 'interactive_buttons':
            interactive_data["type"] = "button"
            interactive_data["action"]["buttons"] = [
                {"type": "reply", "reply": {"id": o.get("id", o.get("action_id", str(i+1))), "title": o.get("texto", "")[:20]}}
                for i, o in enumerate(options)
            ]
        elif final_message_type == 'interactive_list':
            interactive_data["type"] = "list"
            interactive_data["action"]["button"] = original_bot_response.get("interactive_list_button_text", "Ver opciones")
            interactive_data["action"]["sections"] = [{
                "title": original_bot_response.get("interactive_list_section_title", "Opciones"),
                "rows": [
                    {
                        "id": o.get("id", o.get("action_id", str(i+1))),
                        "title": o.get("texto", "")[:24],
                        "description": o.get("description", "")[:72]
                    }
                    for i, o in enumerate(options)
                ]
            }]

        payload.update({"type": "interactive", "interactive": interactive_data})
        logger.info(f"build_interactive_response: returning interactive payload: {payload}")
        return payload

    elif channel == "web":
        web_response = {
            "respuesta": body_text, # "respuesta" is the key often used for web body
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
            web_response["botones"] = [] # Ensure buttons are not processed separately
        elif message_type in ['interactive_buttons', 'interactive_list', 'quick_replies'] and options:
            formatted_botones = []
            for o in options:
                btn = None
                if isinstance(o, str):
                    # Handle the case where an option is a simple string.
                    btn = {"texto": o, "action_id": o}
                elif isinstance(o, dict):
                    # It's a dictionary, process it.
                    btn_text = o.get("texto")
                    if not btn_text:
                        logger.warning(f"Button object is missing 'texto' key: {o}")
                        continue

                    # Use action_id for web. If type is 'url', default action_id to 'open_url_action' unless specified otherwise.
                    action_id = o.get("id", o.get("action", btn_text))
                    if o.get("type") == "url":
                        action_id = o.get("action_id", "open_url_action")

                    btn = {"texto": btn_text, "action_id": action_id}

                    if o.get("type") == "url" and o.get("url"):
                        btn["url"] = o["url"]
                else:
                    logger.warning(f"Unsupported type in options list: {type(o)}. Skipping.")
                    continue

                if message_type == 'quick_replies' and btn:
                    btn["type"] = "quick_reply"

                if btn:
                    formatted_botones.append(btn)
            web_response["botones"] = formatted_botones

        # Clean None values from web_response for cleaner JSON, if desired
        # web_response_cleaned = {k: v for k, v in web_response.items() if v is not None}
        # return web_response_cleaned
        return web_response
    else:
        logger.error(f"Canal desconocido: {channel}. No se pudo formatear la respuesta.")
        return {"error": f"Canal no soportado: {channel}"}

if __name__ == '__main__':
    sample_options_short = [
        {"id": "reclamo_basura_123", "texto": "🗑️ Basura"},
        {"id": "reclamo_luminaria_456", "texto": "💡 Luminaria"},
    ]
    sample_options_long = [
        {"id": "tramite_a", "texto": "Trámite A", "description": "Descripción del trámite A"},
        {"id": "tramite_b", "texto": "Trámite B"},
        {"id": "tramite_c", "texto": "Trámite C con un texto bastante largo para el título"},
    ]
    url_option = [{"id": "web_url", "texto": "Visitar Web", "type": "url", "url": "https://example.com"}]


    print("--- WhatsApp Interactive Output (Buttons) ---")
    whatsapp_buttons = build_interactive_response(options=sample_options_short, body_text="Elige una categoría de reclamo:", channel="whatsapp", message_type='interactive_buttons', header_text="Reclamos", footer_text="Selecciona una opción")
    print(json.dumps(whatsapp_buttons, indent=2, ensure_ascii=False))
    # Expected: {"type": "interactive", "interactive": {"type": "button", ...}}

    print("\n--- WhatsApp Interactive Output (List) ---")
    whatsapp_list = build_interactive_response(options=sample_options_long, body_text="Selecciona un trámite:", channel="whatsapp", message_type='interactive_list', header_text="Trámites Municipales", footer_text="Elige de la lista")
    print(json.dumps(whatsapp_list, indent=2, ensure_ascii=False))
    # Expected: {"type": "interactive", "interactive": {"type": "list", ...}}

    print("\n--- WhatsApp Text Output ---")
    whatsapp_text = build_interactive_response(options=[], body_text="Este es un mensaje de texto simple.", channel="whatsapp", message_type='text')
    print(json.dumps(whatsapp_text, indent=2, ensure_ascii=False))
    # Expected: {"type": "text", "text": {"body": "..."}}

    print("\n--- Web Response (con opciones) ---")
    web_response_options = build_interactive_response(
        options=sample_options_short + url_option,
        body_text="Elige una opción para la web:",
        channel="web",
        message_type='interactive_buttons',
        original_bot_response={"fuente": "test_web_main_example"}
    )
    print(json.dumps(web_response_options, indent=2, ensure_ascii=False))
    # Expected: {"respuesta": "...", "botones": [{"texto": ..., "action_id": ...}, {"texto": ..., "action_id": ..., "url": ...}]}

    print("\n--- Web Response (text only) ---")
    web_response_text = build_interactive_response(
        options=[],
        body_text="Texto simple para web.",
        channel="web",
        message_type='text',
        original_bot_response={"fuente": "test_web_text_example"}
    )
    print(json.dumps(web_response_text, indent=2, ensure_ascii=False))
    # Expected: {"respuesta": "...", "botones": [], ...}

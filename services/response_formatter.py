import json
import logging
import os

logger = logging.getLogger(__name__)

# When Twilio hasn't approved interactive templates yet we fall back to
# rendering every WhatsApp menu as plain text.  The environment variable
# allows re‑enabling interactive components without touching the code.
# MODIFIED: Default to TRUE to satisfy user request for text-based menus.
WHATSAPP_FORCE_TEXT = os.getenv("WHATSAPP_FORCE_TEXT", "true").lower() != "false"

def render_audio_text(message: str, options: list | None = None, categorias: list | None = None) -> str:
    """Builds a plain text version of a menu suitable for TTS.

    Parameters
    ----------
    message: str
        The main text body.
    options: list | None
        Flat list of option dictionaries with a ``texto`` key.
    categorias: list | None
        Structured categories as returned by the greeting handler. Each
        category contains ``titulo`` and a ``botones`` list.

    Returns
    -------
    str
        Text with numbered options ready for speech synthesis.
    """
    lines = [message.strip()] if message else []
    counter = 1

    if categorias:
        for categoria in categorias:
            titulo = categoria.get("titulo")
            if titulo:
                lines.append(titulo)
            for boton in categoria.get("botones", []):
                lines.append(f"{counter}. {boton.get('texto', '')}")
                counter += 1
    elif options:
        for opt in options:
            lines.append(f"{counter}. {opt.get('texto', '')}")
            counter += 1

    return "\n".join(lines)

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
    elif (
        not options
        and message_type == "interactive_menu"
        and original_bot_response.get("data", {}).get("items")
    ):
        # Convert generic menu items into the standard options structure
        items = original_bot_response.get("data", {}).get("items", [])
        options = [
            {
                "texto": item.get("label") or item.get("title") or item.get("texto", ""),
                "id": item.get("key") or item.get("id") or item.get("n") or str(i),
                "action_id": item.get("key") or item.get("id") or item.get("n") or str(i),
                "description": item.get("description", ""),
            }
            for i, item in enumerate(items, 1)
        ]

    # Ensure options is a list and handle nesting
    if options is None:
        options = []
    if options and isinstance(options[0], list):
        # Flatten the list if it's nested (e.g., [[...]])
        options = [item for sublist in options for item in sublist]

    # Propagate any image url provided by the bot so the caller can attach it
    image_url = original_bot_response.get("image_url")

    logger.debug(
        "build_interactive_response called | channel=%s | message_type=%s | num_options=%d | audio_url=%s",
        channel,
        message_type,
        len(options) if options else 0,
        audio_url,
    )

    # Prepare context update placeholder; final options (including navigation
    # buttons) will be attached later once we know the message type and the
    # number of options we can send.
    context_update = original_bot_response.get("contexto_actualizado")
    if channel == "whatsapp":
        context_update = (context_update or {}).copy()

    if channel == "whatsapp":
        original_type = message_type
        num_options = len(options)

        # If interactive templates aren't yet approved we force plain text
        # responses so the user still sees every option in the menu.
        if WHATSAPP_FORCE_TEXT:
            message_type = 'text'
        else:
            # Decide message type based on options, unless it's forced to 'text'
            is_greeting_menu = original_bot_response.get("fuente") == "greeting_handler_categorized_v2"

            if is_greeting_menu:
                message_type = 'text'
            elif message_type != 'text':
                if 1 <= num_options <= 3:
                    message_type = 'interactive_buttons'
                elif 4 <= num_options <= 10:
                    message_type = 'interactive_list'
                else:
                    # Fallback for 0 or >10 options
                    message_type = 'text'

        logger.debug(
            "WhatsApp flow | original_type=%s | final_type=%s | num_options=%d",
            original_type,
            message_type,
            num_options,
        )

        # After appending navigation buttons track the final options so the
        # webhook can map numeric replies back to actions (for interactive
        # messages). When falling back to plain text we will set this field
        # later after filtering out URL-only entries that shouldn't be
        # associated with numeric replies.
        if options and message_type != 'text':
            context_update["last_options_sent"] = options

        # Si el tipo de mensaje es 'text', siempre formatear como texto.
        if message_type == 'text':
            final_body = body_text

            nav_buttons = [
                {"texto": "Menú", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]
            existing_ids = {
                str(o.get("action_id") or o.get("id") or o.get("texto"))
                for o in options
            }
            for btn in nav_buttons:
                if str(btn["action_id"]) not in existing_ids and btn["texto"] not in existing_ids:
                    options.append(btn)
                    existing_ids.add(str(btn["action_id"]))

            # Separate options that are simple URLs from those that require a
            # numeric reply. URL-only options should be displayed inline and
            # excluded from the context mapping so replying with "1" doesn't
            # mistakenly reference them.
            url_options = []
            actionable_options = []
            for o in options:
                if o.get("url") and not (o.get("action_id") or o.get("id")):
                    url_options.append(o)
                else:
                    actionable_options.append(o)

            if url_options:
                url_lines = [
                    f"{o.get('texto', '')}: {o.get('url', '')}" for o in url_options
                ]
                final_body += "\n\n" + "\n".join(url_lines)

            if actionable_options:
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
                else:
                    options_text = "\n\n" + "\n".join(
                        [f"*{i+1}*. {o.get('texto', '')}" for i, o in enumerate(actionable_options)]
                    )
                final_body += options_text

                # Update context with only the actionable options so numeric
                # replies map correctly.
                context_update["last_options_sent"] = actionable_options
            else:
                logger.debug(
                    "build_interactive_response: no actionable options; sending plain text"
                )

            payload = {
                "type": "text",
                "text": {"body": final_body},
                "contexto_actualizado": context_update if context_update else None,
            }
            if image_url:
                payload["image_url"] = image_url
            if audio_url:
                payload["audio"] = {"link": audio_url}
            return payload

        # This part handles interactive messages
        is_interactive = message_type in ['interactive_buttons', 'interactive_list']
        if not is_interactive:
             # Should not happen due to logic above, but as a safeguard
            return {"type": "text", "text": {"body": body_text}}

        interactive_data = {
            "body": {"text": body_text},
            "action": {}
        }

        if image_url:
            interactive_data["header"] = {"type": "image", "image": {"link": image_url}}
        elif header_text:
            interactive_data["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive_data["footer"] = {"text": footer_text}

        # Automatically decide between button and list based on number of options
        if message_type == 'interactive_buttons':
            interactive_data["type"] = "button"

            reply_buttons = []
            url_texts = []
            body_text_to_update = interactive_data["body"]["text"]

            for i, o in enumerate(options):
                if o.get("type") == "url" and o.get("url"):
                    # For URL options, add them to the text body
                    url_texts.append(f"➡️ {o.get('texto', 'Ver más')}: {o.get('url')}")
                else:
                    # For other options, create a standard reply button
                    reply_buttons.append(
                        {"type": "reply", "reply": {"id": o.get("id", o.get("action_id", str(i))), "title": o.get("texto", "")[:20]}}
                    )

            if url_texts:
                body_text_to_update += "\n\n" + "\n".join(url_texts)

            if not reply_buttons:
                # If there are no reply buttons left (e.g., it was only a URL option),
                # we must fall back to a text message.
                payload = {
                    "type": "text",
                    "text": {"body": body_text_to_update},
                    "contexto_actualizado": context_update if context_update else None,
                }
                if image_url:
                    payload["image_url"] = image_url
                if audio_url:
                    payload["audio"] = {"link": audio_url}
                logger.info(f"build_interactive_response: falling back to TEXT payload because only URL options were present.")
                return payload
            else:
                # Otherwise, send the interactive message with the reply buttons
                interactive_data["body"]["text"] = body_text_to_update
                interactive_data["action"]["buttons"] = reply_buttons
        elif message_type == 'interactive_list':
            interactive_data["type"] = "list"
            interactive_data["action"]["button"] = original_bot_response.get("interactive_list_button_text", "Ver opciones")
            interactive_data["action"]["sections"] = [{
                "title": original_bot_response.get("interactive_list_section_title", "Opciones"),
                "rows": [
                    {
                        "id": o.get("id", o.get("action_id", str(i))),
                        "title": o.get("texto", "")[:24],
                        "description": f"{o.get('url', '')}\n{o.get('description', '')}".strip()[:72]
                    }
                    for i, o in enumerate(options)
                ]
            }]

        # This is the start of the corrected block
        payload = {
            "type": "interactive",
            "interactive": interactive_data,
            "contexto_actualizado": context_update if context_update else None
        }

        # Clean None values from header/footer before returning
        if "header" in interactive_data and not interactive_data["header"]:
            del interactive_data["header"]
        if "footer" in interactive_data and not interactive_data["footer"]:
            del interactive_data["footer"]

        logger.info(f"build_interactive_response: returning interactive payload: {interactive_data}")

        if audio_url:
            payload["audio"] = {"link": audio_url}
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

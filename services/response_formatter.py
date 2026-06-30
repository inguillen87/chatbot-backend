import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# When Twilio hasn't approved interactive templates yet we fall back to
# rendering every WhatsApp menu as plain text.  The environment variable
# allows re‑enabling interactive components without touching the code.
# MODIFIED: Default to TRUE to satisfy user request for text-based menus.
WHATSAPP_FORCE_TEXT = os.getenv("WHATSAPP_FORCE_TEXT", "true").lower() != "false"

MOJIBAKE_REPLACEMENTS = {
    "Ã¡": "á",
    "Ã©": "é",
    "Ã­": "í",
    "Ã³": "ó",
    "Ãº": "ú",
    "Ã±": "ñ",
    "Ã": "Á",
    "Ã‰": "É",
    "Ã": "Í",
    "Ã“": "Ó",
    "Ãš": "Ú",
    "Ã‘": "Ñ",
    "Â¿": "¿",
    "Â¡": "¡",
    "â€“": "-",
    "â€”": "-",
    "â€¦": "...",
    "â€œ": '"',
    "â€": '"',
    "â€˜": "'",
    "â€™": "'",
    "â€¢": "•",
    "âœ…": "✅",
    "âŒ": "❌",
    "ðŸ—‘ï¸": "🗑️",
    "ðŸ’¡": "💡",
    "ðŸ“°": "📰",
    "ðŸ—ºï¸": "🗺️",
    "âž¡ï¸": "➡️",
}


def repair_common_mojibake(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    for broken, fixed in MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(broken, fixed)
    return text


def _repair_text(value) -> str:
    return repair_common_mojibake(value)


def _as_option_dicts(options: list | None) -> list[dict]:
    if not options:
        return []
    return [item for item in options if isinstance(item, dict)]


def _clean_text(value, fallback: str = "") -> str:
    text = _repair_text(value).strip()
    return text or fallback


def _truncate_label(value, limit: int, fallback: str) -> str:
    text = _clean_text(value, fallback)
    text = text[:limit].strip()
    return text or fallback[:limit]


def _option_id(option: dict, fallback: str | int) -> str:
    value = option.get("id") or option.get("action_id") or fallback
    return _clean_text(value, str(fallback))[:200]


def _navigation_key(option: dict) -> str:
    action_value = _clean_text(
        option.get("action_id") or option.get("id") or option.get("action")
    ).casefold()
    text_value = _clean_text(
        option.get("texto") or option.get("label") or option.get("title")
    ).casefold()
    combined = f"{action_value} {text_value}"

    if "cancel" in combined or "cancelar" in combined:
        return "cancelar"
    if re.search(r"\bmenu\b|\bmenú\b", combined):
        return "menu_principal"
    return action_value or text_value


def _url_description(option: dict) -> str:
    text = f"{_clean_text(option.get('url'))}\n{_clean_text(option.get('description'))}".strip()
    return text[:72]


def _whatsapp_options_fallback_text(options: list[dict], *, include_urls: bool = True) -> str:
    lines: list[str] = []
    for index, option in enumerate(options[:10], start=1):
        label = _clean_text(option.get("texto") or option.get("label") or option.get("title"), f"Opcion {index}")
        if not label:
            continue
        url = _clean_text(option.get("url")) if include_urls else ""
        if url:
            lines.append(f"{index}. {label}: {url}")
        else:
            lines.append(f"{index}. {label}")
    if not lines:
        return ""
    return "\n\nOpciones:\n" + "\n".join(lines)


def render_audio_text(
    message: str,
    options: list | None = None,
    categorias: list | None = None,
    datos: dict | None = None,
    accion: str | None = None,
) -> str:
    """Builds a plain text version of a response tailored for text-to-speech.

    Besides enumerating menu options, this helper extracts key information from
    ``datos`` so the generated audio provides a concise summary of reclamos o
    sugerencias. The resulting text avoids visual cues and relies on short,
    descriptive sentences that are easier to follow when using a screen reader
    or an audio player.
    """

    def _clean(value: str | None) -> str:
        if value is None:
            return ""
        return _repair_text(value).strip()

    def _append_if_valid(container: list[str], text: str | None) -> None:
        cleaned = _clean(text)
        if cleaned:
            container.append(cleaned)

    lines: list[str] = []
    _append_if_valid(lines, message)

    summary_lines: list[str] = []
    if isinstance(datos, dict):
        # Map of possible keys to human friendly labels. Several keys share the
        # same label so we group them together.
        summary_mapping: list[tuple[tuple[str, ...], str]] = [
            (("categoria",), "Categoría"),
            (("descripcion",), "Descripción"),
            (("ubicacion", "direccion"), "Ubicación"),
            (("distrito", "barrio"), "Distrito"),
            (("nombre_usuario_detectado", "nombre"), "Nombre de contacto"),
            (("dni",), "Documento"),
            (("telefono_detectado", "telefono"), "Teléfono"),
            (("email_detectado", "email"), "Correo"),
        ]
        seen_labels: set[str] = set()
        for keys, label in summary_mapping:
            for key in keys:
                value = _clean(datos.get(key))
                if value:
                    entry = f"{label}: {value}"
                    if entry not in seen_labels:
                        summary_lines.append(entry)
                        seen_labels.add(entry)
                    break

    if summary_lines:
        if accion in {"crear_reclamo", "iniciar_reclamo"}:
            header = "Resumen del reclamo:"
        elif accion == "hacer_sugerencia":
            header = "Resumen de la sugerencia:"
        else:
            header = "Resumen de la gestión:"
        lines.append(header)
        lines.extend(summary_lines)

    counter = 1
    options_present = False

    def _render_option(text: str | None) -> None:
        nonlocal counter, options_present
        cleaned = _clean(text)
        if cleaned:
            if not options_present:
                lines.append("Opciones disponibles:")
                options_present = True
            lines.append(f"Opción {counter}: {cleaned}")
            counter += 1

    if categorias:
        for categoria in categorias:
            titulo = _clean(categoria.get("titulo"))
            if titulo:
                if not options_present:
                    lines.append("Opciones disponibles:")
                    options_present = True
                lines.append(f"{titulo}:")
            for boton in categoria.get("botones", []):
                _render_option(boton.get("texto"))
    elif options:
        for opt in options:
            _render_option(opt.get("texto"))

    if options_present:
        lines.append(
            "Respondé con el número de la opción que prefieras. Si necesitás "
            "escucharlo otra vez, pedilo."
        )

    return "\n".join(lines).strip()

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
    options = _as_option_dicts(options)

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

        force_interactive = bool(
            original_bot_response.get("_force_whatsapp_interactive")
        )
        force_text_override = original_bot_response.get("_force_whatsapp_text")
        bypass_force_text = force_interactive

        if force_text_override is not None:
            # Explicit overrides use truthiness to mirror environment parsing.
            if bool(force_text_override):
                message_type = 'text'
                bypass_force_text = False
            else:
                bypass_force_text = True

        # If interactive templates aren't yet approved we force plain text
        # responses so the user still sees every option in the menu.
        if WHATSAPP_FORCE_TEXT and not bypass_force_text:
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
            final_body = _repair_text(body_text).strip()

            nav_buttons = [
                {"texto": "Menú", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]
            existing_ids = {_navigation_key(o) for o in options}
            for btn in nav_buttons:
                nav_key = _navigation_key(btn)
                if nav_key not in existing_ids:
                    options.append(btn)
                    existing_ids.add(nav_key)

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
                    f"{_clean_text(o.get('texto'), 'Abrir enlace')}: {_clean_text(o.get('url'))}"
                    for o in url_options
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
                            if not isinstance(boton, dict):
                                continue
                            lines.append(f"*{counter}*. {_clean_text(boton.get('texto'), f'Opcion {counter}')}")
                            counter += 1
                    options_text = "\n\n" + "\n".join(lines)
                else:
                    options_text = "\n\n" + "\n".join(
                        [
                            f"*{i+1}*. {_clean_text(o.get('texto'), f'Opcion {i + 1}')}"
                            for i, o in enumerate(actionable_options)
                        ]
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
            return {"type": "text", "text": {"body": _repair_text(body_text).strip()}}

        interactive_data = {
            "body": {"text": _repair_text(body_text).strip()},
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
                        {
                            "type": "reply",
                            "reply": {
                                "id": _option_id(o, i),
                                "title": _truncate_label(o.get("texto"), 20, f"Opcion {i + 1}"),
                            },
                        }
                    )

            if url_texts:
                normalized_url_texts = []
                for o in options:
                    if o.get("type") == "url" and o.get("url"):
                        normalized_url_texts.append(
                            f"{_clean_text(o.get('texto'), 'Ver mas')}: {_clean_text(o.get('url'))}"
                        )
                if normalized_url_texts:
                    url_texts = normalized_url_texts

            if url_texts:
                body_text_to_update += "\n\n" + "\n".join(url_texts)

            button_fallback_text = _whatsapp_options_fallback_text(
                [
                    option
                    for option in options
                    if not (option.get("type") == "url" and option.get("url"))
                ],
                include_urls=False,
            )
            if button_fallback_text:
                body_text_to_update += button_fallback_text

            if not reply_buttons:
                # If there are no reply buttons left (e.g., it was only a URL option),
                # we must fall back to a text message.
                payload = {
                    "type": "text",
                    "text": {"body": _repair_text(body_text_to_update).strip()},
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
                interactive_data["body"]["text"] = _repair_text(body_text_to_update).strip()
                interactive_data["action"]["buttons"] = reply_buttons
        elif message_type == 'interactive_list':
            interactive_data["type"] = "list"
            interactive_data["action"]["button"] = _truncate_label(
                original_bot_response.get("interactive_list_button_text"),
                20,
                "Ver opciones",
            )

            sections_override = original_bot_response.get("interactive_list_sections")
            sections_payload = []
            if isinstance(sections_override, list):
                for section in sections_override:
                    if not isinstance(section, dict):
                        continue
                    title_value = section.get("title") or original_bot_response.get("interactive_list_section_title", "Opciones")
                    rows_value = section.get("rows") or []
                    rows_payload = []
                    for idx, row in enumerate(rows_value):
                        if not isinstance(row, dict):
                            continue
                        row_id = _option_id(row, idx)
                        row_title = _clean_text(row.get("title") or row.get("texto"))
                        if not row_title:
                            continue
                        row_desc = _clean_text(row.get("description"))
                        rows_payload.append({
                            "id": row_id,
                            "title": _truncate_label(row_title, 24, f"Opcion {idx + 1}"),
                            "description": row_desc[:72],
                        })
                    if rows_payload:
                        sections_payload.append({
                            "title": _truncate_label(title_value, 24, "Opciones"),
                            "rows": rows_payload,
                        })

            if not sections_payload:
                sections_payload = [{
                    "title": _truncate_label(
                        original_bot_response.get("interactive_list_section_title"),
                        24,
                        "Opciones",
                    ),
                    "rows": [
                        {
                            "id": _option_id(o, i),
                            "title": _truncate_label(o.get("texto"), 24, f"Opcion {i + 1}"),
                            "description": _url_description(o),
                        }
                        for i, o in enumerate(options)
                    ]
                }]

            interactive_data["action"]["sections"] = sections_payload
            list_fallback_text = _whatsapp_options_fallback_text(options, include_urls=True)
            if list_fallback_text:
                interactive_data["body"]["text"] = (
                    _repair_text(interactive_data["body"]["text"]).strip()
                    + list_fallback_text
                ).strip()

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
            "respuesta": _repair_text(body_text).strip(), # "respuesta" is the key often used for web body
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
                    btn_text = _clean_text(o)
                    btn = {"texto": btn_text, "action_id": btn_text}
                elif isinstance(o, dict):
                    # It's a dictionary, process it.
                    btn_text = _clean_text(o.get("texto"))
                    if not btn_text:
                        logger.warning(f"Button object is missing 'texto' key: {o}")
                        continue

                    # Use action_id for web. If type is 'url', default action_id to 'open_url_action' unless specified otherwise.
                    action_id = _clean_text(o.get("id", o.get("action", btn_text)), btn_text)
                    if o.get("type") == "url":
                        action_id = _clean_text(o.get("action_id"), "open_url_action")

                    btn = {"texto": btn_text, "action_id": action_id}

                    if o.get("type") == "url" and o.get("url"):
                        btn["url"] = _clean_text(o["url"])
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

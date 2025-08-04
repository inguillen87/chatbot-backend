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
                               # recipient_id: str = None # Removed: To be handled by the sending service
                               ) -> dict:
    if original_bot_response is None:
        original_bot_response = {}

    if channel == "whatsapp":
        # This function will now return the content part of the WhatsApp message.
        # The sending service (e.g., WhatsAppService) will add "messaging_product", "to".

        if message_type == 'interactive_buttons' and options:
            if not (1 <= len(options) <= 3):
                logger.warning(f"WhatsApp 'button' type requires 1-3 options, got {len(options)}. Truncating or consider 'list'.")

            interactive_payload_content = {
                "type": "button",
                "body": {"text": body_text}
            }
            if header_text:
                interactive_payload_content["header"] = {"type": "text", "text": header_text}
            if footer_text:
                interactive_payload_content["footer"] = {"text": footer_text}

            interactive_payload_content["action"] = {
                "buttons": [
                    {"type": "reply", "reply": {"id": str(o.get("id", o["texto"]))[:200], "title": o["texto"][:20]}}
                    for o in options[:3] # Max 3 buttons
                ]
            }
            return {
                "type": "interactive",
                "interactive": interactive_payload_content
            }

        elif message_type == 'interactive_list' and options:
            if not (1 <= len(options) <= 10):
                logger.warning(f"WhatsApp 'list' type requires 1-10 options per section, got {len(options)}. Truncating.")

            list_button_text = original_bot_response.get("interactive_list_button_text", "Ver opciones")[:20]
            section_title = original_bot_response.get("interactive_list_section_title", "Opciones disponibles")[:24]


            interactive_payload_content = {
                "type": "list",
                "body": {"text": body_text}
            }
            if header_text:
                interactive_payload_content["header"] = {"type": "text", "text": header_text}
            if footer_text:
                interactive_payload_content["footer"] = {"text": footer_text}

            interactive_payload_content["action"] = {
                "button": list_button_text,
                "sections": [
                    {
                        "title": section_title,
                        "rows": [
                            {"id": str(o.get("id", o["texto"]))[:200],
                             "title": o["texto"][:24],
                             "description": str(o.get("description", ""))[:72] if o.get("description") else ""}
                            for o in options[:10] # Max 10 rows
                        ]
                    }
                ]
            }
            # Ensure description is not present if empty, as WhatsApp API might reject empty string for description
            for row in interactive_payload_content["action"]["sections"][0]["rows"]:
                if not row["description"]:
                    del row["description"]

            return {
                "type": "interactive",
                "interactive": interactive_payload_content
            }

        elif message_type == 'text' or not options: # Simple text message
            return {
                "type": "text",
                "text": {"body": body_text}
            }
        else: # Fallback or unsupported message_type for WhatsApp by this formatter
            logger.warning(f"Unsupported message_type '{message_type}' or missing options for WhatsApp interactive. Sending plain text.")
            return {
                "type": "text",
                "text": {"body": body_text}
            }

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
            "adjuntos": original_bot_response.get("adjuntos", [])
        }

        if message_type in ['interactive_buttons', 'interactive_list', 'quick_replies'] and options:
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

        # HOTFIX: Aplana los botones en el texto de respuesta para el cliente web
        # que parece no poder renderizarlos.
        if web_response.get("botones"):
            button_texts = [f"➡️ {btn.get('texto', '')}" for btn in web_response["botones"]]

            # Añadir los textos de los botones a la respuesta principal
            if body_text:
                web_response["respuesta"] = f"{body_text}\n\n{'\n'.join(button_texts)}"
            else:
                web_response["respuesta"] = '\n'.join(button_texts)

            # Vaciar el array de botones para que el frontend no lo procese
            web_response["botones"] = []

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

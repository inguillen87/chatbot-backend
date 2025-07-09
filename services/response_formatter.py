import json
import logging

logger = logging.getLogger(__name__)

def build_interactive_response(options: list,
                               body_text: str,
                               channel: str,
                               message_type: str = 'text',
                               original_bot_response: dict = None,
                               header_text: str = None,
                               footer_text: str = None
                               ) -> dict:
    if original_bot_response is None:
        original_bot_response = {}

    if channel == "whatsapp":
        interactive_payload = None

        if message_type == 'interactive_buttons' and options:
            if not (1 <= len(options) <= 3):
                logger.warning(f"WhatsApp 'button' type requires 1-3 options, got {len(options)}. Consider using 'list' or reducing options.")

            interactive_payload = {
                "type": "button",
                "body": {"text": body_text}
            }
            if header_text:
                interactive_payload["header"] = {"type": "text", "text": header_text}
            if footer_text:
                interactive_payload["footer"] = {"text": footer_text}

            interactive_payload["action"] = {
                "buttons": [
                    {"type": "reply", "reply": {"id": str(o.get("id", o["texto"])), "title": o["texto"][:20]}}
                    for o in options[:3]
                ]
            }

        elif message_type == 'interactive_list' and options:
            if not (1 <= len(options) <= 10):
                logger.warning(f"WhatsApp 'list' type requires 1-10 options, got {len(options)}. Consider reducing options.")

            interactive_payload = {
                "type": "list",
                "body": {"text": body_text}
            }
            if header_text:
                interactive_payload["header"] = {"type": "text", "text": header_text}
            if footer_text:
                interactive_payload["footer"] = {"text": footer_text}

            interactive_payload["action"] = {
                "button": "Ver opciones"[:20],
                "sections": [
                    {
                        "rows": [
                            {"id": str(o.get("id", o["texto"])), "title": o["texto"][:24], "description": o.get("description", "")[:72]}
                            for o in options[:10]
                        ]
                    }
                ]
            }
            if not header_text and len(interactive_payload["action"]["sections"]) == 1:
                 interactive_payload["action"]["sections"][0]["title"] = "Opciones disponibles"[:24]

        if interactive_payload:
            # This is the structure for the 'interactive' field of the main WhatsApp message object
            return {
                "main_body": body_text,
                "interactive_object": interactive_payload
            }
        else: # Simple text message
            return {
                "main_body": body_text,
                "interactive_object": None
            }

    elif channel == "web":
        web_response = original_bot_response.copy()
        web_response["respuesta"] = body_text
        if message_type in ['interactive_buttons', 'interactive_list'] and options:
            formatted_botones = []
            for o in options:
                btn = {"texto": o["texto"], "action": o.get("action", o.get("id", o["texto"]))}
                if o.get("type") == "url" and o.get("url"): # Handle URL type for web buttons
                    btn["url"] = o["url"]
                    # Action could be a special value like 'open_url' or frontend handles based on presence of 'url'
                    btn["action"] = o.get("action", "url") # Keep original action or default to 'url'
                formatted_botones.append(btn)
            web_response["botones"] = formatted_botones
        elif "botones" not in web_response: # Ensure 'botones' key exists even if empty
             web_response["botones"] = []
        return web_response
    else:
        logger.error(f"Canal desconocido: {channel}. No se pudo formatear la respuesta.")
        return {"error": f"Canal no soportado: {channel}"}

# Example Usage (for testing purposes, can be removed later)
if __name__ == '__main__':
    sample_options_short = [
        {"id": "reclamo_basura_123", "texto": "🗑️ Basura"},
        {"id": "reclamo_luminaria_456", "texto": "💡 Luminaria"},
    ]
    sample_options_long = [
        {"id": "tramite_a", "texto": "Trámite A", "description": "Descripción del trámite A"},
        {"id": "tramite_b", "texto": "Trámite B"},
        {"id": "tramite_c", "texto": "Trámite C con un texto bastante largo para el título"},
        {"id": "tramite_d", "texto": "Trámite D"},
        {"id": "tramite_e", "texto": "Trámite E"},
        {"id": "tramite_f", "texto": "Trámite F"},
        {"id": "tramite_g", "texto": "Trámite G"},
        {"id": "tramite_h", "texto": "Trámite H"},
        {"id": "tramite_i", "texto": "Trámite I"},
        {"id": "tramite_j", "texto": "Trámite J"},
        {"id": "tramite_k", "texto": "Trámite K (este no aparecerá en lista de 10)"},
    ]

    print("--- WhatsApp Interactive Output (Buttons) ---")
    whatsapp_buttons = build_interactive_response(sample_options_short, "Elige una categoría de reclamo:", "whatsapp", message_type='interactive_buttons', header_text="Reclamos", footer_text="Selecciona una opción")
    print(json.dumps(whatsapp_buttons, indent=2, ensure_ascii=False))

    print("\n--- WhatsApp Interactive Output (List) ---")
    whatsapp_list = build_interactive_response(sample_options_long, "Selecciona un trámite:", "whatsapp", message_type='interactive_list', header_text="Trámites Municipales", footer_text="Elige de la lista")
    print(json.dumps(whatsapp_list, indent=2, ensure_ascii=False))

    print("\n--- WhatsApp Text Output ---")
    whatsapp_text = build_interactive_response([], "Este es un mensaje de texto simple.", "whatsapp", message_type='text')
    print(json.dumps(whatsapp_text, indent=2, ensure_ascii=False))

    print("\n--- Web Response (con opciones) ---")
    web_response_options = build_interactive_response(sample_options_short, "Elige una opción para la web:", "web", message_type='interactive_buttons', original_bot_response={"fuente": "test_web"})
    print(json.dumps(web_response_options, indent=2, ensure_ascii=False))

import os
from flask import Blueprint, request, jsonify, current_app
from werkzeug.datastructures import ImmutableMultiDict
from services.auth import get_current_user_or_pyme
from services.chat_orchestrator import ChatOrchestrator
from services.context_manager import get_context, set_context
from services.logging_config import get_logger
from services.pymes import responder_pyme_v4
from services.utils import (
    extract_from_request,
    get_session_id,
    normalize_phone_number,
)
from services.whatsapp_service import send_whatsapp_message, format_pyme_menu_for_whatsapp
from services.pyme_menu import get_pyme_menu_payload
from scheduler import scheduler
from datetime import datetime, timedelta

logger = get_logger(__name__)
whatsapp_bp = Blueprint("whatsapp_webhook", __name__)

def should_trigger_welcome(message_body: str, chat_context: dict) -> bool:
    """Determines if a welcome message should be triggered."""
    normalized_body = message_body.lower().strip()
    is_greeting = normalized_body in ("hola", "buenas", "buen dia", "menu", "menú", "inicio")
    is_first_interaction = not chat_context.get("historial_chat")
    return is_greeting or is_first_interaction

def handle_welcome_message(pyme_id: int, normalized_phone: str, profile_name: str, pyme_context: dict):
    """Handles sending the welcome message and delayed menu for PYME users."""

    # Initial Greeting
    welcome_text = f"¡Hola {profile_name}! Soy WinRey, el asistente virtual de {pyme_context.get('nombre_pyme_cache', 'nuestro negocio')}."
    send_whatsapp_message(to=normalized_phone, body=welcome_text)

    # Schedule Delayed Menu
    @scheduler.task('date', run_date=datetime.now() + timedelta(seconds=5))
    def send_delayed_menu():
        with current_app.app_context():
            menu_payload = get_pyme_menu_payload(pyme_context)
            formatted_menu = format_pyme_menu_for_whatsapp(menu_payload)
            send_whatsapp_message(
                to=normalized_phone,
                body=formatted_menu["body"],
                options=formatted_menu["options"]
            )
            logger.info(f"Sent delayed menu to {normalized_phone}")

    logger.info(f"Scheduled delayed menu for {normalized_phone}")
    return jsonify({"status": "welcome message sent"})


@whatsapp_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Main endpoint for handling incoming WhatsApp messages."""
    data = request.form
    if not data:
        return jsonify({"error": "No data received"}), 400

    (
        message_body,
        from_phone,
        profile_name,
        media_url,
        location,
    ) = extract_from_request(data)

    normalized_phone = normalize_phone_number(from_phone)
    if not normalized_phone:
        return jsonify({"error": "Invalid phone number"}), 400

    pyme_id = 1 # Placeholder for single PYME setup
    session_id = get_session_id(normalized_phone, pyme_id)
    chat_context = get_context(session_id)

    tipo_chat = "pyme" # Hardcoded for now

    if tipo_chat == "pyme":
        pyme_context = chat_context.get("contexto_pyme_v2", {})
        if not pyme_context:
            from services.herramientas_pyme import get_static_pyme_data
            pyme_context = get_static_pyme_data(pyme_id)
            pyme_context["nombre_cliente"] = profile_name
            chat_context["contexto_pyme_v2"] = pyme_context


        if should_trigger_welcome(message_body, chat_context):
            return handle_welcome_message(pyme_id, normalized_phone, profile_name, pyme_context)

        response_data = current_app.loop.run_until_complete(
            responder_pyme_v4(
                pyme_id=pyme_id,
                message=message_body,
                normalized_phone=normalized_phone,
                profile_name=profile_name,
                chat_context_data=chat_context,
                media_url=media_url,
                location=location,
            )
        )
    else:
        return jsonify({"status": "ok", "reply": "Municipio logic not implemented"}), 200

    set_context(session_id, response_data.get("contexto_actualizado", {}))

    send_whatsapp_message(
        to=normalized_phone,
        body=response_data.get("message_body"),
        options=response_data.get("options_list"),
        media_url=response_data.get("audio_url")
    )

    return jsonify({"status": "ok"}), 200
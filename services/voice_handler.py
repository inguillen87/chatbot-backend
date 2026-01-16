import logging
import re
from flask import current_app, g
from models import WhatsappNumero, User, ChatSessionContext
from extensions import db
from utils.db_utils import safe_flag_modified
from sqlalchemy.orm import joinedload
from twilio.rest import Client
import os

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

def initiate_outbound_call(to_number, from_number):
    """
    Triggers an outbound call to the user using Twilio.
    The call will connect to the /voice/welcome webhook.
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error("Twilio credentials missing for voice call.")
        return False

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # Ensure URL is absolute. In production, this must be the public HTTPS URL.
    # For dev, we assume current_app.config['APP_BASE_URL'] is set (e.g. ngrok)
    base_url = current_app.config.get("APP_BASE_URL") or current_app.config.get("BACKEND_URL")
    if not base_url:
        logger.error("APP_BASE_URL or BACKEND_URL not set. Cannot trigger voice call.")
        return False

    url = f"{base_url.rstrip('/')}/voice/welcome"

    try:
        call = client.calls.create(
            to=to_number,
            from_=from_number,
            url=url
        )
        logger.info(f"Outbound call initiated SID: {call.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
        return False

def handle_voice_interaction(user_speech, user_phone, bot_phone, call_sid):
    """
    Core logic to process voice input using the existing chatbot infrastructure.
    """
    try:
        # 1. Resolve Users (Tenant & End User) similar to WhatsApp Webhook
        # Note: In outbound calls, 'To' is the user, 'From' is the bot.
        # But we need to find the WhatsappNumero config to know which Tenant matches the 'From' (bot) number.

        # We need to clean numbers to match DB format usually
        # user_phone might be '+549...' or '+54...'
        # bot_phone might be 'whatsapp:+...' (unlikely for voice) or just '+...'

        # Cleanup
        user_phone_clean = user_phone.replace("whatsapp:", "").strip()
        bot_phone_clean = bot_phone.replace("whatsapp:", "").strip()

        # Find the Tenant (Owner) via the Bot's phone number
        # We search in WhatsappNumero where numero_whatsapp matches bot_phone_clean
        # (This assumes the voice number is the same as the WA number, or configured similarly)
        whatsapp_mapping = WhatsappNumero.query.options(
             joinedload(WhatsappNumero.user).joinedload(User.rubro)
        ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone_clean.replace('+','').replace(' ','')}%")).first()

        if not whatsapp_mapping:
             # Fallback: Try to match without country code or partial
             # This is tricky. Let's assume for MVP we find it.
             logger.warning(f"Could not find tenant for bot phone {bot_phone_clean}")
             return "Lo siento, hubo un error de configuración."

        client_user = whatsapp_mapping.user

        # Find End User (The Caller)
        from services.pymes import get_or_create_user_by_phone
        end_user = get_or_create_user_by_phone(user_phone_clean, client_user)

        # 2. Load/Create Chat Session
        # We use the SAME session ID format as WhatsApp so the bot "remembers" the text conversation context!
        empresa_id = client_user.id
        chat_session_id = f"whatsapp_{empresa_id}_{user_phone_clean}"

        session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
        if not session_context:
            session_context = ChatSessionContext(
                chat_session_id=chat_session_id,
                user_id=empresa_id,
                context_data={}
            )
            db.session.add(session_context)
            db.session.commit()

        # 3. Call Responder Logic
        # We use channel='voice' to instruct the bot to be brief and text-only

        from services.municipio_responder import responder_chatboc

        response_dict = responder_chatboc(
            pregunta=user_speech,
            owner_user=client_user,
            current_user=end_user,
            rubro_obj=client_user.rubro,
            chat_db_context=session_context,
            chat_session_uuid=chat_session_id,
            channel="voice"
        )

        # 4. Process Response for Voice
        message_body = response_dict.get('message_body', "No tengo respuesta.")

        # Clean up text for speech (remove Markdown, URLs, etc.)
        speech_text = _clean_text_for_speech(message_body)

        # Handling Options: If there are buttons, we list them.
        options = response_dict.get('options_list', [])
        if options:
            speech_text += " Puedes decir: "
            option_texts = [opt.get('texto', '') for opt in options[:3]] # Limit to 3 options for sanity
            speech_text += ", o ".join(option_texts)

        # Save context
        safe_flag_modified(session_context, "context_data")
        db.session.commit()

        return speech_text

    except Exception as e:
        logger.error(f"Error in handle_voice_interaction: {e}", exc_info=True)
        return "Hubo un error al procesar tu solicitud."

def _clean_text_for_speech(text):
    """
    Removes Markdown, URLs, and formatting to make text suitable for TTS.
    """
    if not text: return ""

    # Remove URLs
    text = re.sub(r'http\S+', '', text)

    # Remove Markdown bold/italic
    text = text.replace('*', '').replace('_', '')

    # Remove excessive newlines
    text = text.replace('\n', ' ')

    # Remove specific button instructions often found in bot text
    text = text.replace('Hacé click en', 'Selecciona')

    return text.strip()

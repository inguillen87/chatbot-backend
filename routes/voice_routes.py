from flask import Blueprint, request, current_app, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.request_validator import RequestValidator
from models import WhatsappNumero, ChatSessionContext, User
from extensions import db
from services.voice_handler import handle_voice_interaction
from utils.db_utils import ensure_chat_session_context_schema
from sqlalchemy.orm import joinedload
import os

voice_bp = Blueprint('voice', __name__)

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

@voice_bp.route('/voice/welcome', methods=['POST'])
def voice_welcome():
    """
    Endpoint for the initial call greeting.
    Twilio requests this URL when the call starts.
    """
    response = VoiceResponse()

    # Check if we should record the call (optional)
    # response.record(max_length=30)

    # Look up user to personalize greeting
    to_number = request.form.get("To")
    from_number = request.form.get("From") # Bot number in outbound call

    # For outbound calls initiated by the bot, 'To' is the user and 'From' is the bot.
    # We need to reverse logic slightly if it was an inbound call.
    # Assuming outbound for "Solicitar Llamada" feature.

    user_name = "Vecino"
    # Attempt to find the user name from the session context if possible,
    # but we might not have the session ID easily here without looking it up.

    # Simple greeting
    gather = Gather(
        input='speech',
        action='/voice/process',
        language='es-AR',
        speechTimeout='auto'
    )
    gather.say(f"Hola, soy el asistente virtual. ¿En qué puedo ayudarte hoy?", language="es-AR")
    response.append(gather)

    # Loop if no input
    response.say("No te escuché. ¿Podrías repetirlo?", language="es-AR")
    response.redirect('/voice/welcome')

    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/process', methods=['POST'])
def voice_process():
    """
    Endpoint that processes speech input and returns the bot's response.
    """
    # 1. Validate Request
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(request.url, request.form, request.headers.get('X-Twilio-Signature', '')):
           return "Forbidden", 403

    user_speech = request.form.get('SpeechResult')
    confidence = float(request.form.get('Confidence', 0.0))

    to_number = request.form.get("To") # User phone in outbound
    from_number = request.form.get("From") # Bot phone in outbound
    call_sid = request.form.get("CallSid")

    response = VoiceResponse()

    if not user_speech or confidence < 0.5:
        gather = Gather(input='speech', action='/voice/process', language='es-AR')
        gather.say("Lo siento, no te entendí bien. ¿Podrías repetirlo?", language="es-AR")
        response.append(gather)
        return Response(str(response), mimetype='text/xml')

    # 2. Call the Logic Handler
    bot_response_text = handle_voice_interaction(
        user_speech=user_speech,
        user_phone=to_number,
        bot_phone=from_number,
        call_sid=call_sid
    )

    # 3. Construct TwiML Response
    gather = Gather(input='speech', action='/voice/process', language='es-AR')
    gather.say(bot_response_text, language="es-AR")
    response.append(gather)

    # 4. Handle End of Conversation (optional detection)
    # If bot_response_text indicates goodbye, we could use response.hangup()

    return Response(str(response), mimetype='text/xml')

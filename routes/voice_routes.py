from flask import Blueprint, request, current_app, Response
from twilio.twiml.voice_response import VoiceResponse, Gather, Play
from twilio.request_validator import RequestValidator
from models import WhatsappNumero, ChatSessionContext, User
from extensions import db
from services.voice_handler import handle_voice_interaction
from services.tts_orchestrator import generar_audio
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

    # 1. Validate Request
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(request.url, request.form, request.headers.get('X-Twilio-Signature', '')):
           return "Forbidden", 403

    # Look up user to personalize greeting
    # Note: For outbound calls initiated via API, 'To' is the user, 'From' is the bot (Twilio number).
    # For inbound calls, 'From' is the user, 'To' is the bot.
    # We can detect direction if needed, but for 'Solicitar llamada', it is outbound.

    user_phone = request.form.get("To", "").replace("whatsapp:", "").strip()
    bot_phone = request.form.get("From", "").replace("whatsapp:", "").strip()
    direction = request.form.get("Direction", "outbound-api")

    if direction == "inbound":
        user_phone, bot_phone = bot_phone, user_phone

    user_name = "Vecino"
    tenant_name = "tu municipio"
    assistant_name = "el asistente virtual"

    # Attempt to resolve context
    try:
        # Find Tenant via Bot Phone
        whatsapp_mapping = WhatsappNumero.query.options(
             joinedload(WhatsappNumero.user).joinedload(User.rubro)
        ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone.replace('+','').replace(' ','')}%")).first()

        client_user = whatsapp_mapping.user if whatsapp_mapping else None

        if client_user:
            # Find End User
            from services.pymes import get_or_create_user_by_phone
            end_user = get_or_create_user_by_phone(user_phone, client_user)
            if end_user and end_user.name and end_user.name != "Vecino/a":
                user_name = end_user.name

            # Resolve Tenant/Bot Name
            tenant_profile = (
                getattr(client_user, "tenant", None)
                or getattr(client_user, "tenant_profile", None)
                or getattr(client_user, "tenant_profile_municipio", None)
            )
            if tenant_profile and tenant_profile.configuracion:
                config = tenant_profile.configuracion
                tenant_name = (
                    config.get("nombre_municipio")
                    or config.get("nombre")
                    or getattr(client_user, "nombre_empresa", None)
                    or tenant_name
                )
                assistant_name = config.get("assistant_name") or config.get("bot_name") or assistant_name
            elif client_user.nombre_empresa:
                tenant_name = client_user.nombre_empresa

    except Exception as e:
        current_app.logger.error(f"[VOICE_WELCOME] Error resolving context: {e}")

    greeting_text = f"Hola {user_name}, soy {assistant_name} de {tenant_name}. ¿En qué puedo ayudarte hoy?"

    # Try premium TTS
    audio_url = None
    try:
        audio_url = generar_audio(greeting_text)
    except Exception as e:
        current_app.logger.error(f"[VOICE_WELCOME] TTS failed: {e}")

    gather = Gather(
        input='speech',
        action='/voice/process',
        language='es-AR',
        speechTimeout='auto',
        timeout=5
    )

    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(greeting_text, language="es-AR")

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
    result = handle_voice_interaction(
        user_speech=user_speech,
        user_phone=to_number,
        bot_phone=from_number,
        call_sid=call_sid
    )

    if isinstance(result, dict):
        bot_response_text = result.get("text")
        audio_url = result.get("audio_url")
    else:
        bot_response_text = str(result)
        audio_url = None

    # 3. Construct TwiML Response
    gather = Gather(input='speech', action='/voice/process', language='es-AR')

    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(bot_response_text, language="es-AR")

    response.append(gather)

    # 4. Handle End of Conversation (optional detection)
    # If bot_response_text indicates goodbye, we could use response.hangup()

    return Response(str(response), mimetype='text/xml')

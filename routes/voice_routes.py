from flask import Blueprint, request, current_app, Response, url_for
from twilio.twiml.voice_response import VoiceResponse, Gather, Play, Connect, Dial
from twilio.request_validator import RequestValidator
from models import WhatsappNumero, ChatSessionContext, User
from extensions import db, sock
from services.voice_handler import handle_voice_interaction, handle_call_status
from services.tts_orchestrator import generar_audio
from utils.db_utils import ensure_chat_session_context_schema
from sqlalchemy.orm import joinedload
import os
import json
import base64
import logging
from services.voice_stream_service import VoiceStreamService

voice_bp = Blueprint('voice', __name__)

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
logger = logging.getLogger(__name__)

@voice_bp.route('/voice/fallback', methods=['POST'])
def voice_fallback():
    """
    Fallback endpoint for Twilio errors.
    """
    response = VoiceResponse()
    response.say("Lo siento, ha ocurrido un error técnico. Por favor intenta más tarde.", language="es-AR")
    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/welcome', methods=['POST'])
def voice_welcome():
    """
    Endpoint for the initial call greeting (Legacy TwiML).
    Retained for backward compatibility or simple flows if needed.
    """
    response = VoiceResponse()

    # 1. Validate Request
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(request.url, request.form, request.headers.get('X-Twilio-Signature', '')):
           return "Forbidden", 403

    user_phone = request.form.get("To", "").replace("whatsapp:", "").strip()
    bot_phone = request.form.get("From", "").replace("whatsapp:", "").strip()
    direction = request.form.get("Direction", "outbound-api")

    if direction == "inbound":
        user_phone, bot_phone = bot_phone, user_phone

    user_name = "Vecino"
    tenant_name = "tu municipio"
    assistant_name = "el asistente virtual"

    try:
        whatsapp_mapping = WhatsappNumero.query.options(
             joinedload(WhatsappNumero.user).joinedload(User.rubro)
        ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone.replace('+','').replace(' ','')}%")).first()

        client_user = whatsapp_mapping.user if whatsapp_mapping else None

        if client_user:
            from services.pymes import get_or_create_user_by_phone
            end_user = get_or_create_user_by_phone(user_phone, client_user)
            if end_user and end_user.name and end_user.name != "Vecino/a":
                user_name = end_user.name

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

    audio_url = None
    try:
        audio_url = generar_audio(greeting_text)
    except Exception as e:
        current_app.logger.error(f"[VOICE_WELCOME] TTS failed: {e}")

    gather = Gather(
        input='speech dtmf',
        num_digits=1,
        action=url_for('voice.voice_process', _external=True),
        language='es-AR',
        speechTimeout='auto',
        timeout=5,
        bargeIn=True
    )

    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(greeting_text, language="es-AR")

    response.append(gather)

    response.say("No te escuché. ¿Podrías repetirlo?", language="es-AR")
    response.redirect(url_for('voice.voice_welcome', _external=True))

    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/twilio/voice/inbound', methods=['POST'])
def voice_inbound_stream():
    """
    New Endpoint for Inbound Calls using Twilio Media Streams & OpenAI Realtime.
    Returns TwiML with <Connect><Stream>.
    """
    response = VoiceResponse()

    # 1. Validate Request
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        # Using request.url might be HTTP if behind proxy.
        if not validator.validate(request.url, request.form, request.headers.get('X-Twilio-Signature', '')):
           return "Forbidden", 403

    # Extract call details to pass to the stream (via custom parameters if needed,
    # or just use the callSid in the stream URL params)
    call_sid = request.form.get('CallSid')
    from_number = request.form.get('From')
    to_number = request.form.get('To')
    source_chat_session_id = request.values.get("chat_session_id")

    # We can pass context via query params to the WebSocket URL
    # Assuming the app is running on a domain, we need to construct the wss URL
    # current_app.config['BACKEND_URL'] is usually http(s). We need ws(s).
    backend_url = current_app.config.get("BACKEND_URL", "http://localhost:8080")
    ws_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")

    # Updated path as per requirement
    stream_url = f"{ws_url}/twilio/voice/stream"

    # Add a short greeting to avoid silence before streaming
    response.say("Hola, un segundo que ya te atiendo.", language="es-AR")

    # The greeting will be handled by the AI Stream immediately upon connection.
    connect = Connect()
    stream = connect.stream(url=stream_url)
    # Pass metadata to the stream context
    stream.parameter(name="from_number", value=from_number)
    stream.parameter(name="to_number", value=to_number)
    stream.parameter(name="call_sid", value=call_sid)
    if source_chat_session_id:
        stream.parameter(name="chat_session_id", value=source_chat_session_id)

    response.append(connect)

    # Fallback if stream fails
    response.say("Lo siento, hubo un error de conexión. Por favor intenta más tarde.")

    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/twilio/voice/transfer', methods=['POST'])
def voice_transfer():
    """
    Endpoint that returns TwiML to transfer the call to a human agent.
    Expected to be called via Call Update API.
    """
    target = request.args.get("target") or request.form.get("target")
    response = VoiceResponse()

    if target:
        response.say("Transfiriendo a un representante. Aguarde un momento, por favor.", language="es-AR")
        response.dial(target)
    else:
        response.say("Lo siento, no pude conectar con un representante.", language="es-AR")

    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/process', methods=['POST'])
def voice_process():
    """
    Legacy Endpoint that processes speech input (Gather) and returns TwiML.
    Kept for backward compatibility or non-streaming flows.
    """
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(request.url, request.form, request.headers.get('X-Twilio-Signature', '')):
           return "Forbidden", 403

    user_speech = request.form.get('SpeechResult')
    digits = request.form.get('Digits')
    input_text = user_speech or digits
    confidence = float(request.form.get('Confidence', 0.0))
    to_number = request.form.get("To")
    from_number = request.form.get("From")
    call_sid = request.form.get("CallSid")
    direction = request.form.get("Direction", "outbound-api")

    response = VoiceResponse()

    if not input_text or (user_speech and confidence < 0.5):
        gather = Gather(
            input='speech dtmf',
            num_digits=1,
            action=url_for('voice.voice_process', _external=True),
            language='es-AR',
            bargeIn=True
        )
        gather.say("Lo siento, no te entendí bien. ¿Podrías repetirlo?", language="es-AR")
        response.append(gather)
        return Response(str(response), mimetype='text/xml')

    if direction == "inbound":
        user_phone = from_number
        bot_phone = to_number
    else:
        user_phone = to_number
        bot_phone = from_number

    result = handle_voice_interaction(
        user_speech=input_text,
        user_phone=user_phone,
        bot_phone=bot_phone,
        call_sid=call_sid
    )

    if isinstance(result, dict):
        if result.get("type") == "handoff":
            response.say(result.get("text", "Transfiriendo..."), language="es-AR")
            response.dial(result.get("target"))
            return Response(str(response), mimetype='text/xml')

        bot_response_text = result.get("text")
        audio_url = result.get("audio_url")
    else:
        bot_response_text = str(result)
        audio_url = None

    gather = Gather(
        input='speech dtmf',
        num_digits=1,
        action=url_for('voice.voice_process', _external=True),
        language='es-AR',
        bargeIn=True,
        speechTimeout='auto',
        timeout=5
    )

    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(bot_response_text, language="es-AR")

    response.append(gather)
    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/status', methods=['POST'])
def voice_status():
    """
    Handles call status updates.
    """
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(request.url, request.form, request.headers.get('X-Twilio-Signature', '')):
           return "Forbidden", 403

    call_sid = request.form.get('CallSid')
    call_status = request.form.get('CallStatus')
    to_number = request.form.get("To")
    from_number = request.form.get("From")
    direction = request.form.get("Direction")

    handle_call_status(call_sid, call_status, to_number, from_number, direction)

    return Response(status=200)

# WebSocket Route for Media Streams
# Using flask-sock extension
@sock.route('/twilio/voice/stream')
def voice_stream_socket(ws):
    """
    WebSocket handler for Twilio Media Streams <-> OpenAI Realtime.
    """
    logger.info("New Voice Stream WebSocket connection")
    # Pass the actual application object to the service to allow context creation in threads
    stream_service = VoiceStreamService(ws, app=current_app._get_current_object())
    stream_service.run()

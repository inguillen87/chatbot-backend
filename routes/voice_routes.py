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

from utils.auth_helpers import token_requerido
from services.realtime_session_service import realtime_session_service
from flask import jsonify

voice_bp = Blueprint('voice', __name__)

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER = "+18564858589"
logger = logging.getLogger(__name__)


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _legacy_voice_gather_enabled() -> bool:
    return _is_truthy(
        current_app.config.get("VOICE_LEGACY_GATHER_ENABLED")
        or os.environ.get("VOICE_LEGACY_GATHER_ENABLED")
    )


def _normalize_phone(value: str | None) -> str:
    return str(value or "").replace("whatsapp:", "").strip()


def _configured_chatboc_demo_numbers() -> set[str]:
    raw_numbers = os.environ.get("CHATBOC_DEMO_WHATSAPP_NUMBERS") or ""
    candidates = [part for part in raw_numbers.replace(";", ",").split(",") if part.strip()]
    candidates.append(CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER)
    return {_normalize_phone(candidate) for candidate in candidates if _normalize_phone(candidate)}


def _is_chatboc_demo_voice_number(*numbers: str | None) -> bool:
    configured = _configured_chatboc_demo_numbers()
    return any(_normalize_phone(number) in configured for number in numbers if number)


def _chatboc_demo_voice_max_seconds() -> int:
    raw_value = os.environ.get("CHATBOC_DEMO_VOICE_MAX_SECONDS") or "60"
    try:
        return max(15, int(raw_value))
    except (TypeError, ValueError):
        return 60


def _validate_twilio_request() -> bool:
    if not TWILIO_AUTH_TOKEN:
        return True
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(
        request.url,
        request.form,
        request.headers.get('X-Twilio-Signature', ''),
    )


def _voice_stream_twiml_response() -> Response:
    response = VoiceResponse()

    call_sid = request.form.get('CallSid')
    from_number = request.form.get('From')
    to_number = request.form.get('To')
    source_chat_session_id = request.values.get("chat_session_id")

    backend_url = current_app.config.get("BACKEND_URL", "http://localhost:8080")
    ws_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")
    stream_url = f"{ws_url}/twilio/voice/stream"

    response.pause(length=1)

    connect = Connect()
    stream = connect.stream(url=stream_url)
    stream.parameter(name="from_number", value=from_number)
    stream.parameter(name="to_number", value=to_number)
    stream.parameter(name="call_sid", value=call_sid)
    if _is_chatboc_demo_voice_number(from_number, to_number):
        stream.parameter(name="max_call_seconds", value=str(_chatboc_demo_voice_max_seconds()))
        stream.parameter(name="demo_hub", value="chatboc")
    if source_chat_session_id:
        stream.parameter(name="chat_session_id", value=source_chat_session_id)

    response.append(connect)
    response.say("Lo siento, hubo un error de conexion. Por favor intenta mas tarde.", language="es-AR")

    return Response(str(response), mimetype='text/xml')


@voice_bp.route('/api/realtime/session', methods=['POST'])
@token_requerido
def create_webrtc_session(current_user, owner_user, anon_id):
    """
    Creates an ephemeral WebRTC session token for the browser client.
    Requires widget/panel auth.
    """
    tenant_id = getattr(owner_user, 'tenant_id', None) or getattr(owner_user, 'id', None)
    if not tenant_id:
        return jsonify({"error": "Tenant missing"}), 400

    user_id = getattr(current_user, 'id', None)

    result = realtime_session_service.create_session(tenant_id, user_id, anon_id)

    status_code = result.pop("status_code", 500)
    if status_code != 200:
        return jsonify({"error": result.get("error")}), status_code

    return jsonify(result), 200

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
    Compatibility endpoint for older Twilio webhooks.
    By default, calls are upgraded to OpenAI Realtime through Twilio Media Streams.
    """
    response = VoiceResponse()

    if not _validate_twilio_request():
        return "Forbidden", 403

    if not _legacy_voice_gather_enabled():
        return _voice_stream_twiml_response()

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
    Primary endpoint for inbound calls using Twilio Media Streams & OpenAI Realtime.
    Returns TwiML with <Connect><Stream>.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403

    return _voice_stream_twiml_response()

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

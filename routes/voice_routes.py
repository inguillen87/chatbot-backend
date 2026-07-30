from flask import Blueprint, request, current_app, Response, url_for
from twilio.twiml.voice_response import VoiceResponse, Gather, Play, Connect, Dial
from twilio.request_validator import RequestValidator
from models import ChatSessionContext
from extensions import db, sock
from services.voice_handler import handle_voice_interaction, handle_call_status
from utils.db_utils import ensure_chat_session_context_schema
import os
import json
import base64
import logging
import re
import unicodedata
from services.voice_stream_service import VoiceStreamService
from services.voice_stream_envelope import (
    VoiceStreamEnvelopeError,
    create_voice_stream_envelope,
)
from services.voice_consent_lifecycle import (
    VoiceConsentLifecycleError,
    assert_voice_stream_authorized,
    begin_voice_consent,
    mark_voice_lifecycle_failed,
    record_voice_consent_decision,
    record_voice_consent_missing,
    record_voice_provider_status,
    resolve_authoritative_voice_tenant,
    resolve_voice_consent_policy,
    voice_consent_lifecycle_enabled,
)

from utils.auth_helpers import token_requerido
from services.realtime_session_service import realtime_session_service
from flask import jsonify

voice_bp = Blueprint('voice', __name__)

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER = "+18564858589"
DEFAULT_TWILIO_FALLBACK_VOICE = "Polly.Lupe-Neural"
DEFAULT_TWILIO_FALLBACK_SAY_LANGUAGE = "es-AR"
DEFAULT_TWILIO_GATHER_LANGUAGE = "es-AR"
VOICE_TENANT_ALIASES = {
    "junin": "junin-1",
    "juni-01": "junin-1",
    "juni": "junin-1",
    "club-demo-ar": "junin-1",
    "chatboc-demo": "chatboc-platform",
}
VOICE_VERTICAL_ALIASES = {
    "juni": "municipio",
    "junin": "municipio",
    "gobierno": "municipio",
    "sales": "ventas",
    "empresa": "pyme",
    "empresas": "pyme",
    "school": "educacion",
    "colegio": "educacion",
    "colegios": "educacion",
}
logger = logging.getLogger(__name__)


def _normalize_voice_text(value) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char))


def _canonical_voice_tenant(value: str | None) -> str:
    normalized = _normalize_voice_text(value)
    return VOICE_TENANT_ALIASES.get(normalized, normalized)


def _canonical_voice_vertical(value: str | None) -> str:
    normalized = _normalize_voice_text(value)
    return VOICE_VERTICAL_ALIASES.get(normalized, normalized)


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _legacy_voice_gather_enabled() -> bool:
    return _is_truthy(
        current_app.config.get("VOICE_LEGACY_GATHER_ENABLED")
        or os.environ.get("VOICE_LEGACY_GATHER_ENABLED")
    )


def _voice_config_value(*names: str, default: str | None = None) -> str | None:
    for name in names:
        if not name:
            continue

        value = None
        try:
            value = current_app.config.get(name)
        except RuntimeError:
            value = None

        if value is None:
            value = os.environ.get(name)

        if value is None:
            continue

        text = str(value).strip()
        if text:
            return text

    return default


def _twilio_fallback_voice() -> str:
    return _voice_config_value(
        "TWILIO_FALLBACK_VOICE",
        default=DEFAULT_TWILIO_FALLBACK_VOICE,
    )


def _twilio_fallback_say_language() -> str:
    configured = _voice_config_value(
        "TWILIO_FALLBACK_SAY_LANGUAGE",
        default=DEFAULT_TWILIO_FALLBACK_SAY_LANGUAGE,
    )
    if _normalize_voice_text(configured) in {"es-us", "en-us", "en"}:
        return DEFAULT_TWILIO_FALLBACK_SAY_LANGUAGE
    return configured


def _twilio_gather_language() -> str:
    return _voice_config_value(
        "TWILIO_GATHER_LANGUAGE",
        default=DEFAULT_TWILIO_GATHER_LANGUAGE,
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


def _voice_say(parent, text: str):
    """Centralized fallback speech. Main phone flow uses OpenAI Realtime audio."""
    parent.say(
        text,
        language=_twilio_fallback_say_language(),
        voice=_twilio_fallback_voice(),
    )


def _demo_voice_action_url(endpoint: str = "voice.voice_demo_process", **extra) -> str:
    params = {
        "tenant": _current_voice_tenant(),
        "vertical": _current_voice_vertical(),
        "intent": _normalize_voice_text(request.values.get("intent")),
    }
    params.update(extra)
    return url_for(endpoint, _external=True, **{k: v for k, v in params.items() if v})


def _current_voice_vertical() -> str:
    return _canonical_voice_vertical(request.values.get("vertical") or request.values.get("sector"))


def _current_voice_tenant() -> str:
    return _canonical_voice_tenant(request.values.get("tenant") or request.values.get("tenant_slug"))


def _has_explicit_voice_context() -> bool:
    return bool(
        request.values.get("tenant")
        or request.values.get("tenant_slug")
        or request.values.get("vertical")
        or request.values.get("sector")
    )


def _voice_gather_endpoint_for_current_context() -> str:
    if _has_explicit_voice_context():
        return "voice.voice_process"
    return "voice.voice_demo_process"


def _fallback_prompt_for_current_context() -> str:
    vertical = _current_voice_vertical()
    tenant = _current_voice_tenant()
    if vertical == "municipio" or tenant.startswith("junin"):
        return (
            "La conexion realtime no quedo estable, pero sigo por telefono en espanol. "
            "Soy el asistente telefonico del municipio. Deci o marca 1 para iniciar reclamo, "
            "2 para consultar estado, 3 para tramites, o 4 para hablar con un operador."
        )
    if vertical == "educacion":
        return (
            "La conexion realtime no quedo estable, pero sigo por telefono en espanol. "
            "Soy el asistente del colegio. Deci o marca 1 para admisiones, 2 para certificados, "
            "3 para pagos o cuotas, o 4 para hablar con secretaria."
        )
    if vertical in {"pyme", "ventas"} or tenant == "chatboc-demo":
        return (
            "La conexion realtime no quedo estable, pero sigo por telefono en espanol. "
            "Soy Chatboc.ar. Deci o marca 1 para demos de municipios, 2 para colegios, "
            "3 para empresas y pedidos, o 4 para hablar con ventas."
        )
    return (
        "Hola, soy Chatboc.ar y te atiendo en espanol. "
        "Deci o marca 1 para municipios, 2 para colegios, "
        "3 para empresas y pedidos, o 4 para ventas."
    )


def _append_demo_voice_gather(response: VoiceResponse, prompt: str | None = None) -> None:
    endpoint = _voice_gather_endpoint_for_current_context()
    gather = Gather(
        input="speech dtmf",
        num_digits=1,
        action=_demo_voice_action_url(endpoint),
        language=_twilio_gather_language(),
        speechTimeout="auto",
        timeout=6,
        bargeIn=True,
    )
    _voice_say(
        gather,
        prompt or _fallback_prompt_for_current_context(),
    )
    response.append(gather)


def _demo_voice_reply_for(input_text: str | None) -> str:
    normalized = _normalize_voice_text(input_text)
    vertical = _current_voice_vertical()
    tenant = _current_voice_tenant()
    if vertical == "municipio" or tenant.startswith("junin"):
        if normalized in {"menu", "principal", "volver"}:
            return (
                "Menu del municipio. Deci o marca 1 iniciar reclamo, 2 consultar estado, "
                "3 tramites, o 4 operador."
            )
        if normalized in {"1", "uno"} or any(
            word in normalized for word in ("reclamo", "bache", "luminaria", "arbol", "calle", "agua")
        ):
            return (
                "Perfecto. Puedo iniciar un reclamo municipal. Decime la categoria, la direccion "
                "y una descripcion breve del problema."
            )
        if normalized in {"2", "dos"} or any(word in normalized for word in ("estado", "ticket", "pin")):
            return "Para consultar estado, decime el numero de ticket o el PIN de seguimiento."
        if normalized in {"3", "tres"} or any(word in normalized for word in ("tramite", "turno", "consulta")):
            return "Para tramites, decime que gestion necesitas y te indico el camino correcto."
        if normalized in {"4", "cuatro"} or any(word in normalized for word in ("operador", "persona", "agente")):
            return "Te puedo dejar pedido de contacto con un operador. Decime tu nombre y el motivo."
        return (
            "No llegue a ubicar la opcion municipal. Deci iniciar reclamo, consultar estado, "
            "tramites, u operador."
        )

    if normalized in {"menu", "principal", "volver"}:
        return (
            "Menú principal. Decí o marcá 1 municipios, 2 colegios, "
            "3 empresas y pedidos, o 4 ventas."
        )
    if normalized in {"1", "uno"} or any(
        word in normalized for word in ("municipio", "reclamo", "tramite", "bache", "luminaria", "junin")
    ):
        return (
            "Demo municipios. Puedo hacer un reclamo completo, consultar estado o simular un tramite. "
            "Decime por ejemplo: quiero reclamar una luminaria apagada y la direccion."
        )
    if normalized in {"2", "dos"} or any(
        word in normalized for word in ("colegio", "escuela", "familia", "admis", "cuota", "inasistencia")
    ):
        return (
            "Demo colegios. Puedo registrar admisiones, inasistencias, pagos, certificados o consultas de secretaria. "
            "Decime que necesita la familia o el alumno."
        )
    if normalized in {"3", "tres"} or any(
        word in normalized for word in ("empresa", "pyme", "pedido", "producto", "catalogo", "envio", "stock")
    ):
        return (
            "Demo empresas. Puedo tomar un pedido, consultar productos, cotizar envio o revisar un pedido. "
            "Decime que queres comprar o probar."
        )
    if normalized in {"4", "cuatro"} or any(word in normalized for word in ("venta", "ventas", "asesor", "contratar")):
        return (
            "Perfecto. Para ventas decime tu nombre, rubro o empresa, y que queres automatizar. "
            "Lo dejo registrado para seguimiento comercial."
        )
    return (
        "No llegue a ubicar la opcion. Deci municipios, colegios, empresas o ventas. "
        "Tambien podes contar directamente que queres probar."
    )


def _validate_twilio_request() -> bool:
    """Validate Twilio webhooks and fail closed outside explicit test/dev mode."""

    if current_app.config.get("TESTING") and not current_app.config.get(
        "VOICE_VALIDATE_TWILIO_IN_TESTS"
    ):
        return True

    auth_token = (
        current_app.config.get("TWILIO_AUTH_TOKEN")
        or os.environ.get("TWILIO_AUTH_TOKEN")
        or TWILIO_AUTH_TOKEN
    )
    if not auth_token:
        environment = str(current_app.config.get("ENV") or "").strip().lower()
        allow_unsigned = bool(current_app.config.get("TESTING")) or (
            _is_truthy(current_app.config.get("VOICE_ALLOW_UNSIGNED_TWILIO_WEBHOOKS"))
            and environment not in {"prod", "production"}
        )
        if not allow_unsigned:
            logger.error("Twilio voice webhook rejected reason=auth_token_missing")
        return allow_unsigned
    validator = RequestValidator(str(auth_token))
    return validator.validate(
        request.url,
        request.form,
        request.headers.get('X-Twilio-Signature', ''),
    )


def _voice_control_response(message: str, *, status: int = 200) -> Response:
    """Return bounded TwiML without opening an audio-input processor."""

    response = VoiceResponse()
    _voice_say(response, message)
    response.hangup()
    return Response(str(response), mimetype="text/xml", status=status)


def _resolve_voice_http_tenant():
    return resolve_authoritative_voice_tenant(
        from_number=request.form.get("From"),
        to_number=request.form.get("To"),
        direction=request.form.get("Direction"),
        requested_tenant_slug=_current_voice_tenant(),
        config=current_app.config,
    )


def _voice_consent_action_url(tenant_slug: str) -> str:
    params = {
        "tenant": tenant_slug,
        "vertical": _current_voice_vertical(),
        "intent": _normalize_voice_text(request.values.get("intent")),
        "chat_session_id": request.values.get("chat_session_id"),
    }
    return url_for(
        "voice.voice_consent",
        _external=True,
        **{key: value for key, value in params.items() if value},
    )


def _voice_stream_twiml_response(*, authoritative_tenant=None) -> Response:
    response = VoiceResponse()

    call_sid = request.form.get('CallSid')
    from_number = request.form.get('From')
    to_number = request.form.get('To')
    source_chat_session_id = request.values.get("chat_session_id")

    backend_url = current_app.config.get("BACKEND_URL", "http://localhost:8080")
    ws_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")
    stream_url = f"{ws_url}/twilio/voice/stream"

    if not voice_consent_lifecycle_enabled(current_app.config):
        return _voice_control_response(
            "La atencion por inteligencia artificial no esta habilitada en este momento."
        )

    try:
        tenant_profile = authoritative_tenant or _resolve_voice_http_tenant()
        policy = resolve_voice_consent_policy(tenant_profile)
        if policy.ai_processing != "explicit_per_call":
            raise VoiceConsentLifecycleError("policy_disabled")
        lifecycle = assert_voice_stream_authorized(
            tenant_id=tenant_profile.id,
            call_sid=call_sid,
        )
        if lifecycle.consent_policy_version != policy.version:
            raise VoiceConsentLifecycleError("call_policy_mismatch")
    except VoiceConsentLifecycleError as exc:
        logger.warning("Twilio voice stream refused reason=%s", exc.code)
        return _voice_control_response(
            "Necesitamos tu consentimiento antes de iniciar la atencion inteligente."
        )

    tenant = str(tenant_profile.slug or "").strip()
    vertical = _current_voice_vertical()
    intent = _normalize_voice_text(request.values.get("intent"))
    is_demo = _is_chatboc_demo_voice_number(from_number, to_number)
    max_call_seconds = _chatboc_demo_voice_max_seconds() if is_demo else None

    try:
        envelope = create_voice_stream_envelope(
            call_sid=call_sid,
            from_number=from_number,
            to_number=to_number,
            tenant_slug=tenant,
            vertical=vertical,
            intent=intent,
            chat_session_id=source_chat_session_id,
            demo=is_demo,
            max_call_seconds=max_call_seconds,
            config=current_app.config,
        )
    except VoiceStreamEnvelopeError as exc:
        logger.error(
            "Twilio voice stream unavailable reason=%s",
            exc.code,
        )
        _voice_say(
            response,
            "La atencion inteligente no esta disponible en este momento. "
            "Continuamos con el menu telefonico.",
        )
        response.redirect(_demo_voice_action_url("voice.voice_fallback"))
        return Response(str(response), mimetype='text/xml')

    response.pause(length=1)

    connect = Connect()
    stream = connect.stream(url=stream_url)
    for name in (
        "from_number",
        "to_number",
        "call_sid",
        "tenant_slug",
        "vertical",
        "intent",
        "chat_session_id",
        "demo",
        "max_call_seconds",
        "version",
        "ts",
        "nonce",
        "signature",
    ):
        value = envelope.get(name)
        if value not in (None, ""):
            stream.parameter(name=name, value=value)
    if envelope["tenant_slug"]:
        stream.parameter(name="tenant", value=envelope["tenant_slug"])
    if envelope["vertical"]:
        stream.parameter(name="sector", value=envelope["vertical"])
    if envelope["demo"] == "1":
        stream.parameter(name="demo_hub", value="chatboc")

    response.append(connect)
    response.redirect(_demo_voice_action_url("voice.voice_fallback"))

    return Response(str(response), mimetype='text/xml')


def _voice_consent_entry_response() -> Response:
    """Create an auditable call receipt and ask for consent using DTMF only."""

    if not voice_consent_lifecycle_enabled(current_app.config):
        logger.warning("Twilio voice consent unavailable reason=feature_disabled")
        return _voice_control_response(
            "La atencion telefonica inteligente todavia no esta habilitada para esta organizacion."
        )

    try:
        tenant = _resolve_voice_http_tenant()
        policy = resolve_voice_consent_policy(tenant)
        lifecycle = begin_voice_consent(
            tenant_id=tenant.id,
            call_sid=request.form.get("CallSid"),
            direction=request.form.get("Direction"),
            policy=policy,
        )
    except Exception as exc:
        db.session.rollback()
        reason = exc.code if isinstance(exc, VoiceConsentLifecycleError) else "persistence_unavailable"
        logger.error("Twilio voice consent unavailable reason=%s", reason)
        return _voice_control_response(
            "La atencion inteligente no esta disponible en este momento."
        )

    if policy.ai_processing == "disabled":
        try:
            mark_voice_lifecycle_failed(
                tenant_id=tenant.id,
                call_sid=request.form.get("CallSid"),
                reason_code="policy_disabled",
            )
        except Exception:
            db.session.rollback()
            logger.error("Twilio voice policy-disabled audit unavailable")
        return _voice_control_response(
            "Esta organizacion no tiene habilitado el procesamiento de voz por inteligencia artificial."
        )
    if lifecycle.state in {"completed", "failed"}:
        return _voice_control_response(
            "Esta llamada ya fue finalizada y no puede volver a abrirse."
        )
    if lifecycle.consent_status == "granted":
        return _voice_stream_twiml_response(authoritative_tenant=tenant)
    if lifecycle.consent_status == "declined":
        return _voice_control_response(
            "Respetamos tu decision. No enviaremos el audio a inteligencia artificial ni grabaremos la llamada."
        )

    response = VoiceResponse()
    gather = Gather(
        input="dtmf",
        num_digits=1,
        action=_voice_consent_action_url(str(tenant.slug)),
        method="POST",
        timeout=8,
        action_on_empty_result=True,
    )
    _voice_say(
        gather,
        "Para usar el asistente de voz, necesitamos enviar el audio en tiempo real al proveedor de inteligencia artificial. "
        "Esta funcion no graba la llamada. Marca 1 para aceptar o 2 para rechazar.",
    )
    response.append(gather)
    response.hangup()
    return Response(str(response), mimetype="text/xml")


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

@voice_bp.route('/voice/fallback', methods=['GET', 'POST'])
def voice_fallback():
    """
    Fallback endpoint for Twilio errors.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403
    try:
        tenant = _resolve_voice_http_tenant()
        assert_voice_stream_authorized(
            tenant_id=tenant.id,
            call_sid=request.form.get("CallSid"),
        )
    except VoiceConsentLifecycleError:
        return _voice_control_response(
            "La llamada finalizo sin iniciar procesamiento inteligente."
        )

    response = VoiceResponse()
    _append_demo_voice_gather(response, _fallback_prompt_for_current_context())
    _voice_say(response, "No te escuché. Te mando el menú por WhatsApp y podés volver a llamar cuando quieras.")
    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/demo/process', methods=['POST'])
def voice_demo_process():
    """
    Deterministic phone demo fallback when OpenAI Realtime cannot stay connected.
    The primary path remains /twilio/voice/stream.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403
    try:
        tenant = _resolve_voice_http_tenant()
        assert_voice_stream_authorized(
            tenant_id=tenant.id,
            call_sid=request.form.get("CallSid"),
        )
    except VoiceConsentLifecycleError:
        return _voice_control_response(
            "La llamada finalizo sin iniciar procesamiento inteligente."
        )

    input_text = request.form.get("SpeechResult") or request.form.get("Digits")
    response = VoiceResponse()
    _append_demo_voice_gather(response, _demo_voice_reply_for(input_text))
    _voice_say(response, "Si queres volver al menu principal, deci menu.")
    return Response(str(response), mimetype='text/xml')


@voice_bp.route('/voice/welcome', methods=['POST'])
def voice_welcome():
    """
    Compatibility endpoint for older Twilio webhooks.
    By default, calls are upgraded to OpenAI Realtime through Twilio Media Streams.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403

    if not _legacy_voice_gather_enabled():
        return _voice_consent_entry_response()

    # The legacy speech Gather is intentionally unavailable until it can carry
    # the same durable consent contract. DTMF consent must not be bypassed by a
    # compatibility flag.
    return _voice_control_response(
        "El modo telefonico anterior no esta disponible. Usa el canal de voz seguro."
    )

@voice_bp.route('/twilio/voice/inbound', methods=['POST'])
def voice_inbound_stream():
    """
    Primary inbound endpoint. It asks for durable explicit consent before it
    can return TwiML containing a Media Stream.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403

    return _voice_consent_entry_response()


@voice_bp.route('/twilio/voice', methods=['POST'])
def voice_inbound_stream_alias():
    """
    Compatibility alias for Twilio consoles configured with /twilio/voice.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403

    return _voice_consent_entry_response()


@voice_bp.route('/twilio/voice/consent', methods=['POST'])
def voice_consent():
    """Consume a one-digit decision; raw DTMF is never persisted or logged."""

    if not _validate_twilio_request():
        return "Forbidden", 403
    if not voice_consent_lifecycle_enabled(current_app.config):
        return _voice_control_response(
            "La atencion inteligente no esta habilitada en este momento."
        )

    try:
        tenant = _resolve_voice_http_tenant()
        policy = resolve_voice_consent_policy(tenant)
        if policy.ai_processing != "explicit_per_call":
            raise VoiceConsentLifecycleError("policy_disabled")

        # Translate the transport digit in memory. The ledger only receives a
        # bounded semantic decision code.
        digit = str(request.form.get("Digits") or "").strip()
        if digit == "1":
            record_voice_consent_decision(
                tenant_id=tenant.id,
                call_sid=request.form.get("CallSid"),
                decision="granted",
            )
            return _voice_stream_twiml_response(authoritative_tenant=tenant)
        if digit == "2":
            record_voice_consent_decision(
                tenant_id=tenant.id,
                call_sid=request.form.get("CallSid"),
                decision="declined",
            )
            return _voice_control_response(
                "Respetamos tu decision. No enviaremos el audio a inteligencia artificial ni grabaremos la llamada."
            )

        record_voice_consent_missing(
            tenant_id=tenant.id,
            call_sid=request.form.get("CallSid"),
        )
        return _voice_control_response(
            "No recibimos una autorizacion valida. La atencion inteligente no se inicio."
        )
    except Exception as exc:
        db.session.rollback()
        reason = exc.code if isinstance(exc, VoiceConsentLifecycleError) else "persistence_unavailable"
        logger.error("Twilio voice consent decision refused reason=%s", reason)
        return _voice_control_response(
            "No pudimos confirmar el consentimiento. La atencion inteligente no se inicio."
        )

@voice_bp.route('/twilio/voice/transfer', methods=['POST'])
def voice_transfer():
    """
    Endpoint that returns TwiML to transfer the call to a human agent.
    Expected to be called via Call Update API.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403

    target = request.args.get("target") or request.form.get("target")
    response = VoiceResponse()
    try:
        if not voice_consent_lifecycle_enabled(current_app.config):
            raise VoiceConsentLifecycleError("voice_consent_feature_disabled")
        tenant = _resolve_voice_http_tenant()
        assert_voice_stream_authorized(
            tenant_id=tenant.id,
            call_sid=request.form.get("CallSid"),
        )
        tenant_config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
        configured_target = (
            tenant_config.get("human_handoff_number")
            or tenant_config.get("telefono_atencion")
        )
        requested_target = _normalize_phone(target)
        allowed_target = _normalize_phone(configured_target)
        e164_pattern = re.compile(r"^\+[1-9]\d{7,14}$")
        if (
            not e164_pattern.fullmatch(requested_target)
            or not e164_pattern.fullmatch(allowed_target)
            or requested_target != allowed_target
        ):
            raise VoiceConsentLifecycleError("transfer_target_not_authorized")
    except VoiceConsentLifecycleError as exc:
        logger.warning("Twilio voice transfer refused reason=%s", exc.code)
        _voice_say(response, "La transferencia no esta disponible en este momento.")
        return Response(str(response), mimetype="text/xml")

    if requested_target:
        _voice_say(
            response,
            "Vamos a intentar comunicarte con un representante. Aguarda un momento, por favor.",
        )
        response.dial(requested_target)
    else:
        _voice_say(response, "Lo siento, no pude conectar con un representante.")

    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/process', methods=['POST'])
def voice_process():
    """
    Legacy Endpoint that processes speech input (Gather) and returns TwiML.
    Kept for backward compatibility or non-streaming flows.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403
    if not voice_consent_lifecycle_enabled(current_app.config):
        return _voice_control_response(
            "La atencion inteligente no esta habilitada en este momento."
        )
    try:
        tenant = _resolve_voice_http_tenant()
        assert_voice_stream_authorized(
            tenant_id=tenant.id,
            call_sid=request.form.get("CallSid"),
        )
    except VoiceConsentLifecycleError:
        return _voice_control_response(
            "Necesitamos tu consentimiento antes de procesar audio."
        )

    user_speech = request.form.get('SpeechResult')
    digits = request.form.get('Digits')
    input_text = user_speech or digits
    try:
        confidence = float(request.form.get('Confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    to_number = request.form.get("To")
    from_number = request.form.get("From")
    call_sid = request.form.get("CallSid")
    direction = request.form.get("Direction", "outbound-api")

    response = VoiceResponse()

    if not input_text or (user_speech and confidence < 0.5):
        gather = Gather(
            input='speech dtmf',
            num_digits=1,
            action=_demo_voice_action_url("voice.voice_process"),
            language=_twilio_gather_language(),
            bargeIn=True
        )
        _voice_say(gather, "Lo siento, no te entendí bien. ¿Podrías repetirlo?")
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
            _voice_say(response, result.get("text", "Transfiriendo..."))
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
        action=_demo_voice_action_url("voice.voice_process"),
        language=_twilio_gather_language(),
        bargeIn=True,
        speechTimeout='auto',
        timeout=5
    )

    if audio_url:
        gather.play(audio_url)
    else:
        _voice_say(gather, bot_response_text)

    response.append(gather)
    return Response(str(response), mimetype='text/xml')

@voice_bp.route('/voice/status', methods=['POST'])
def voice_status():
    """
    Handles call status updates.
    """
    if not _validate_twilio_request():
        return "Forbidden", 403

    call_sid = request.form.get('CallSid')
    call_status = request.form.get('CallStatus')
    to_number = request.form.get("To")
    from_number = request.form.get("From")
    direction = request.form.get("Direction")

    if voice_consent_lifecycle_enabled(current_app.config):
        try:
            tenant = _resolve_voice_http_tenant()
            policy = resolve_voice_consent_policy(tenant)
            record_voice_provider_status(
                tenant_id=tenant.id,
                call_sid=call_sid,
                direction=direction,
                policy=policy,
                provider_status=call_status,
            )
        except Exception as exc:
            db.session.rollback()
            reason = exc.code if isinstance(exc, VoiceConsentLifecycleError) else "persistence_unavailable"
            logger.error("Twilio voice status refused reason=%s", reason)
            # A non-2xx response asks Twilio to retry rather than silently
            # claiming that a lifecycle update was persisted.
            return Response(status=503)

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

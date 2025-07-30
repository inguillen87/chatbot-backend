import os
import json
from twilio.rest import Client
import logging

logger = logging.getLogger(__name__)


def _get_twilio_client():
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if sid and token:
        return Client(sid, token)
    return None


def enviar_notificacion_sms(numero_destino: str, mensaje: str):
    phone = os.environ.get("TWILIO_PHONE_NUMBER")
    client = _get_twilio_client()
    if not client or not phone:
        print("[NOTIFICACION SMS] Faltan credenciales de Twilio SMS.")
        return
    try:
        message = client.messages.create(
            body=mensaje, from_=phone, to=numero_destino
        )
        print(f"[NOTIFICACION SMS] SMS enviado SID: {message.sid}")
    except Exception as e:
        print(f"[NOTIFICACION SMS] Error al enviar SMS: {e}")


def enviar_notificacion_whatsapp_con_plantilla(
    numero_destino: str, nombre: str, nro_ticket: str, categoria: str
):
    client = _get_twilio_client()
    whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+17432643718")
    content_sid = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")
    if not all([client, whatsapp_number, content_sid]):
        logger.error(
            "[NOTIFICACION WHATSAPP] Faltan credenciales de Twilio WhatsApp (SID/Token/Number/Content_SID)."
        )
        return
    destinatario_whatsapp = f"whatsapp:{numero_destino}"
    try:
        variables_plantilla = {"1": nombre, "2": f"M-{nro_ticket}", "3": categoria}
        message = client.messages.create(
            from_=whatsapp_number,
            to=destinatario_whatsapp,
            content_sid=content_sid,
            content_variables=json.dumps(variables_plantilla),
        )
        logger.info(f"[NOTIFICACION WHATSAPP] Plantilla enviada, SID: {message.sid}")
    except Exception as e:
        logger.error(
            f"[NOTIFICACION WHATSAPP] Error al enviar plantilla: {e}", exc_info=True
        )


def enviar_bienvenida_whatsapp(numero_destino: str, nombre: str):
    """Envía el mensaje de bienvenida con botones usando una plantilla de WhatsApp."""
    client = _get_twilio_client()
    whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
    template_name = os.environ.get("TWILIO_WELCOME_TEMPLATE", "bienvenida")
    if not client or not whatsapp_number:
        logger.error("[WHATSAPP] Faltan credenciales de Twilio para bienvenida.")
        return

    destinatario = f"whatsapp:{numero_destino}"
    template_payload = {
        "name": template_name,
        "language": {"code": "es"},
        "components": [
            {"type": "body", "parameters": [{"type": "text", "text": nombre}]}
        ],
    }

    try:
        message = client.messages.create(
            from_=whatsapp_number,
            to=destinatario,
            template=template_payload,
        )
        logger.info(f"[WHATSAPP] Bienvenida enviada, SID: {message.sid}")
    except Exception as e:
        logger.error(
            f"[WHATSAPP] Error al enviar mensaje de bienvenida: {e}",
            exc_info=True,
        )

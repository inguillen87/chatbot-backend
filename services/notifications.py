import os
import json
from twilio.rest import Client
import logging

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = os.environ.get(
    "TWILIO_WHATSAPP_NUMBER", "whatsapp:+17432643718"
)
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")
TWILIO_WELCOME_TEMPLATE = os.environ.get("TWILIO_WELCOME_TEMPLATE", "bienvenida")


def enviar_notificacion_sms(numero_destino: str, mensaje: str):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        print("[NOTIFICACION SMS] Faltan credenciales de Twilio SMS.")
        return
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            body=mensaje, from_=TWILIO_PHONE_NUMBER, to=numero_destino
        )
        print(f"[NOTIFICACION SMS] SMS enviado SID: {message.sid}")
    except Exception as e:
        print(f"[NOTIFICACION SMS] Error al enviar SMS: {e}")


def enviar_notificacion_whatsapp_con_plantilla(
    numero_destino: str, nombre: str, nro_ticket: str, categoria: str
):
    if not all(
        [
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
            TWILIO_WHATSAPP_NUMBER,
            TWILIO_WHATSAPP_CONTENT_SID,
        ]
    ):
        logger.error(
            "[NOTIFICACION WHATSAPP] Faltan credenciales de Twilio WhatsApp (SID/Token/Number/Content_SID)."
        )
        return
    destinatario_whatsapp = f"whatsapp:{numero_destino}"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        variables_plantilla = {"1": nombre, "2": f"M-{nro_ticket}", "3": categoria}
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=destinatario_whatsapp,
            content_sid=TWILIO_WHATSAPP_CONTENT_SID,
            content_variables=json.dumps(variables_plantilla),
        )
        logger.info(f"[NOTIFICACION WHATSAPP] Plantilla enviada, SID: {message.sid}")
    except Exception as e:
        logger.error(
            f"[NOTIFICACION WHATSAPP] Error al enviar plantilla: {e}", exc_info=True
        )
        try:
            logger.info(f"Intentando enviar mensaje de texto plano como fallback a {destinatario_whatsapp}")
            fallback_message = f"Hola {nombre}, tu reclamo por '{categoria}' ha sido registrado con el número de ticket M-{nro_ticket}."
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=destinatario_whatsapp,
                body=fallback_message,
            )
            logger.info(f"[NOTIFICACION WHATSAPP] Mensaje de fallback enviado con éxito.")
        except Exception as e_fallback:
            logger.error(
                f"[NOTIFICACION WHATSAPP] Error al enviar mensaje de fallback: {e_fallback}", exc_info=True
            )


def enviar_bienvenida_whatsapp(numero_destino: str, nombre: str):
    """Envía el mensaje de bienvenida con botones usando una plantilla de WhatsApp."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
        logger.error("[WHATSAPP] Faltan credenciales de Twilio para bienvenida.")
        return

    destinatario = f"whatsapp:{numero_destino}"
    template_payload = {
        "name": TWILIO_WELCOME_TEMPLATE,
        "language": {"code": "es"},
        "components": [
            {"type": "body", "parameters": [{"type": "text", "text": nombre}]}
        ],
    }

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=destinatario,
            template=template_payload,
        )
        logger.info(f"[WHATSAPP] Bienvenida enviada, SID: {message.sid}")
    except Exception as e:
        logger.error(
            f"[WHATSAPP] Error al enviar mensaje de bienvenida: {e}",
            exc_info=True,
        )

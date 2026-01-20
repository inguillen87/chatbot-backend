import os
from typing import Optional

from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback

TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
MESSAGING_SERVICE_SID = os.environ.get("MESSAGING_SERVICE_SID")


def send_whatsapp_message(
    to_phone: str,
    body: str,
    *,
    media_url: Optional[str] = None,
    from_number: Optional[str] = None,
) -> bool:
    """
    Send a WhatsApp message using a consistent sender and payload format.
    """
    sender = from_number or TWILIO_WHATSAPP_NUMBER
    return enviar_mensaje_whatsapp_con_fallback(
        numero_destino=to_phone,
        cuerpo=body,
        image_url=media_url,
        from_number=sender,
        messaging_service_sid=None if sender else MESSAGING_SERVICE_SID,
    )

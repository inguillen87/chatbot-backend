import os
import logging
from typing import Optional, Union, List

from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")  # ej: +1743...
MESSAGING_SERVICE_SID = os.environ.get("MESSAGING_SERVICE_SID")  # opcional


def _as_whatsapp(number: str) -> str:
    if not number:
        return ""
    n = number.strip()
    if n.startswith("whatsapp:"):
        return n
    return f"whatsapp:{n}"


def _resolve_from(from_number: Optional[str] = None) -> dict:
    """
    Twilio WhatsApp sender: either MessagingServiceSid OR From number.
    """
    if MESSAGING_SERVICE_SID:
        return {"messaging_service_sid": MESSAGING_SERVICE_SID}

    base = from_number or TWILIO_WHATSAPP_NUMBER
    if not base:
        raise ValueError("No sender configured: set MESSAGING_SERVICE_SID or TWILIO_PHONE_NUMBER")

    return {"from_": _as_whatsapp(base)}


def enviar_mensaje_whatsapp(
    to_number: str,
    body: str,
    media_url: Optional[Union[str, List[str]]] = None,
    from_number: Optional[str] = None,
) -> bool:
    """
    Sends WhatsApp message via Twilio. Supports optional media_url (audio/image/pdf/etc).
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error("Twilio credentials missing (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).")
        return False

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    try:
        payload = {
            "to": _as_whatsapp(to_number),
            "body": body or "",
            **_resolve_from(from_number),
        }

        if media_url:
            if isinstance(media_url, list):
                payload["media_url"] = media_url
            else:
                payload["media_url"] = [media_url]

        msg = client.messages.create(**payload)
        logger.info(f"[TWILIO WHATSAPP] OK SID: {msg.sid}")
        return True

    except Exception as e:
        logger.error(f"[TWILIO WHATSAPP] ERROR sending message: {e}", exc_info=True)
        return False


def enviar_mensaje_whatsapp_con_fallback(
    to_number: str,
    message_body: str,
    media_url: Optional[str] = None,
    from_number: Optional[str] = None,
    options_list: Optional[list] = None,
) -> bool:
    """
    Sends message + simple menu fallback appended as text (Twilio interactive is limited).
    """
    body = message_body or ""

    if options_list:
        # options_list can be like: [{"texto":"Menú","action_id":"menu_principal"}, ...]
        # We just show friendly numbered options.
        lines = []
        for i, opt in enumerate(options_list, start=1):
            t = opt.get("texto") if isinstance(opt, dict) else str(opt)
            lines.append(f"{i}. {t}")
        body = f"{body}\n\n" + "\n".join(lines)

    return enviar_mensaje_whatsapp(
        to_number=to_number,
        body=body,
        media_url=media_url,
        from_number=from_number,
    )

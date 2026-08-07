import os
import requests
from twilio.rest import Client

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Debe ser algo como:
# "whatsapp:+14155238886" (sandbox Twilio)
# o "whatsapp:+54..." si tenés WhatsApp aprobado en tu cuenta
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
TWILIO_WHATSAPP_STATUS_CALLBACK_URL = os.environ.get("TWILIO_WHATSAPP_STATUS_CALLBACK_URL")


def _get_twilio_client():
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    print("[TWILIO WHATSAPP] Faltan credenciales de Twilio.")
    return None


def enviar_mensaje_whatsapp(numero, mensaje, api_key):
    """
    CallMeBot legacy (NO recomendado para producción).
    """
    url = f"https://api.callmebot.com/whatsapp.php?phone={numero}&text={mensaje}&apikey={api_key}"
    response = requests.get(url)
    return response.status_code == 200


def _ensure_whatsapp_prefix(value: str) -> str:
    if not value:
        return value
    value = str(value).strip()
    return value if value.startswith("whatsapp:") else f"whatsapp:{value}"


def _build_text_fallback_body(cuerpo: str, botones=None, lista=None) -> str:
    """
    Twilio WhatsApp NO soporta botones/listas dinámicas sin templates aprobados.
    Así que los convertimos a texto plano (sin romper).
    """
    body = (cuerpo or "").strip()

    if botones:
        rendered_buttons = []
        for boton in botones:
            if isinstance(boton, dict):
                label = boton.get("texto") or boton.get("title") or boton.get("label")
                if boton.get("url") and label:
                    rendered_buttons.append(f"{label}: {boton.get('url')}")
                elif label:
                    rendered_buttons.append(label)
                else:
                    rendered_buttons.append(str(boton))
            else:
                rendered_buttons.append(str(boton))
        body += "\n\nOpciones:\n" + "\n".join([f"- {b}" for b in rendered_buttons if b])

    if lista:
        titulo = lista.get("titulo", "Opciones")
        body += f"\n\n{titulo}\n"
        for seccion in lista.get("secciones", []):
            body += f"\n*{seccion.get('title', '')}*\n"
            for row in seccion.get("rows", []):
                body += f"- {row.get('title', '')}\n"

    return body.strip()


def enviar_mensaje_whatsapp_con_fallback(
    numero_destino,
    cuerpo,
    botones=None,
    lista=None,
    image_url=None,
    from_number=None,
    messaging_service_sid=None,
    status_callback=None,
):
    """
    Envía WhatsApp por Twilio.
    - Si hay botones o lista => se convierte automáticamente a texto plano.
    - Soporta from_number o messaging_service_sid (MGxxxx).
    - Soporta imagen (media_url).
    """
    client = _get_twilio_client()
    if not client:
        return False

    # Sanitizar destino
    numero_destino = _ensure_whatsapp_prefix(numero_destino)

    # Body final (texto plano seguro)
    body_final = _build_text_fallback_body(cuerpo, botones=botones, lista=lista)

    message_params = {
        "to": numero_destino,
        "body": body_final,
    }
    callback_url = status_callback or TWILIO_WHATSAPP_STATUS_CALLBACK_URL
    if callback_url:
        message_params["status_callback"] = callback_url

    # Imagen opcional
    if image_url:
        message_params["media_url"] = [image_url]

    # Si from_number viene como MGxxxx, lo tratamos como messaging_service_sid
    if from_number and str(from_number).startswith("MG") and not messaging_service_sid:
        messaging_service_sid = from_number
        from_number = None

    # 1) Si hay messaging_service_sid => NO usar from_
    if messaging_service_sid:
        message_params["messaging_service_sid"] = messaging_service_sid
    else:
        # 2) Caso normal => usar from_number o TWILIO_WHATSAPP_NUMBER
        sender_raw = from_number or TWILIO_WHATSAPP_NUMBER
        if not sender_raw:
            print("[TWILIO WHATSAPP] No sender configured (from_number or TWILIO_WHATSAPP_NUMBER missing).")
            return False

        # Asegurar prefix whatsapp:
        sender = _ensure_whatsapp_prefix(sender_raw)
        message_params["from_"] = sender

    try:
        message = client.messages.create(**message_params)
        print(f"[TWILIO WHATSAPP] Mensaje enviado OK SID: {message.sid}")
        return True
    except Exception as e:
        print(f"[TWILIO WHATSAPP] Error enviando WhatsApp: {e}")
        return False


def enviar_mensaje_whatsapp_con_botones(numero_destino, cuerpo, botones):
    """
    Botones => fallback a texto plano (compatible siempre)
    """
    return enviar_mensaje_whatsapp_con_fallback(numero_destino, cuerpo, botones=botones)


def enviar_mensaje_whatsapp_con_lista(numero_destino, cuerpo, titulo_lista, secciones):
    """
    Lista => fallback a texto plano (compatible siempre)
    """
    lista = {"titulo": titulo_lista, "secciones": secciones}
    return enviar_mensaje_whatsapp_con_fallback(numero_destino, cuerpo, lista=lista)


def enviar_imagen_whatsapp(numero_destino, cuerpo, url_imagen):
    """
    Envía imagen por WhatsApp usando Twilio (media_url).
    """
    return enviar_mensaje_whatsapp_con_fallback(
        numero_destino,
        cuerpo,
        image_url=url_imagen,
    )


# ---------------------------------------------------------------------------
# Twilio Content Templates (ContentSid) — Enterprise WhatsApp Templates
# ---------------------------------------------------------------------------
# These are pre-approved templates created via Twilio Content API.
# They use content_sid + content_variables instead of persistent_action,
# which avoids the 63019 errors that plague interactive messages.
#
# Template SIDs are stored here and in the MessageTemplateRegistry DB.
# To add new templates: create via Content API, submit for approval,
# then register the SID here.
# ---------------------------------------------------------------------------

import json as _json
import logging as _logging

_template_logger = _logging.getLogger(__name__)

# Known approved template SIDs — updated after Twilio/Meta approval
TEMPLATE_SIDS = {
    "chatboc_claim_created_v2": os.environ.get(
        "TWILIO_TEMPLATE_CLAIM_CREATED_SID", "HX1b9c5c594b09ae397dcd1167d69fef35"
    ),
    "chatboc_claim_resolved_v1": os.environ.get(
        "TWILIO_TEMPLATE_CLAIM_RESOLVED_SID", "HX48336a6bc9f9292c85ec40d768f7f591"
    ),
    "chatboc_survey_invite_v1": os.environ.get(
        "TWILIO_TEMPLATE_SURVEY_INVITE_SID", "HX84aa283585a3d6ce8a1b49f57ed2ed76"
    ),
    "chatboc_appointment_confirm_v1": os.environ.get(
        "TWILIO_TEMPLATE_APPOINTMENT_SID", "HXca0422128c618c9ab8202fe26781dc5d"
    ),
    "chatboc_order_update_v1": os.environ.get(
        "TWILIO_TEMPLATE_ORDER_UPDATE_SID", "HXa446274eeeb522090a59f8ae953d0257"
    ),
}


def enviar_template_whatsapp(
    numero_destino: str,
    template_name: str,
    variables: dict | None = None,
    *,
    from_number: str | None = None,
    messaging_service_sid: str | None = None,
    status_callback: str | None = None,
    fallback_body: str | None = None,
) -> bool:
    """Send an approved Twilio Content Template via WhatsApp.

    Uses content_sid + content_variables for proper template delivery.
    Falls back to plain text if the template SID is not found or sending fails.

    Args:
        numero_destino: WhatsApp number (e.g. "+5492364..." or "whatsapp:+5492364...")
        template_name: Key in TEMPLATE_SIDS (e.g. "chatboc_claim_created_v2")
        variables: Dict of template variables {"1": "value1", "2": "value2"}
        from_number: Override sender number
        messaging_service_sid: Override messaging service
        status_callback: Status callback URL
        fallback_body: Plain text fallback if template fails

    Returns:
        True if sent successfully, False otherwise.
    """
    content_sid = TEMPLATE_SIDS.get(template_name)
    if not content_sid:
        _template_logger.warning(
            "[TEMPLATE_WA] Unknown template '%s', falling back to text", template_name
        )
        if fallback_body:
            return enviar_mensaje_whatsapp_con_fallback(numero_destino, fallback_body)
        return False

    client = _get_twilio_client()
    if not client:
        return False

    numero_destino = _ensure_whatsapp_prefix(numero_destino)

    message_params = {
        "to": numero_destino,
        "content_sid": content_sid,
    }

    # Content variables must be JSON string
    if variables:
        message_params["content_variables"] = _json.dumps(variables)

    callback_url = status_callback or TWILIO_WHATSAPP_STATUS_CALLBACK_URL
    if callback_url:
        message_params["status_callback"] = callback_url

    # Sender resolution
    if from_number and str(from_number).startswith("MG") and not messaging_service_sid:
        messaging_service_sid = from_number
        from_number = None

    if messaging_service_sid:
        message_params["messaging_service_sid"] = messaging_service_sid
    else:
        sender_raw = from_number or TWILIO_WHATSAPP_NUMBER
        if not sender_raw:
            _template_logger.error("[TEMPLATE_WA] No sender configured")
            return False
        message_params["from_"] = _ensure_whatsapp_prefix(sender_raw)

    try:
        message = client.messages.create(**message_params)
        _template_logger.info(
            "[TEMPLATE_WA] Template '%s' sent OK SID=%s to=%s",
            template_name, message.sid, numero_destino[:20]
        )
        return True
    except Exception as e:
        _template_logger.error(
            "[TEMPLATE_WA] Failed sending template '%s': %s", template_name, e
        )
        # Fallback to plain text
        if fallback_body:
            _template_logger.info("[TEMPLATE_WA] Falling back to plain text")
            return enviar_mensaje_whatsapp_con_fallback(numero_destino, fallback_body)
        return False

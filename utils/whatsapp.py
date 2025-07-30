import os
import requests
from twilio.rest import Client

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")


def _get_twilio_client():
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    print("[TWILIO WHATSAPP] Faltan credenciales de Twilio.")
    return None


def enviar_mensaje_whatsapp(numero, mensaje, api_key):
    url = (
        f"https://api.callmebot.com/whatsapp.php?phone={numero}&text={mensaje}&apikey={api_key}"
    )
    response = requests.get(url)
    return response.status_code == 200


def enviar_mensaje_whatsapp_con_botones(numero_destino, cuerpo, botones):
    """Envía un mensaje de WhatsApp con hasta 3 botones de respuesta rápida."""
    client = _get_twilio_client()
    if not client or not TWILIO_WHATSAPP_NUMBER:
        return False

    actions = [
        {"id": f"btn_{i+1}", "type": "reply", "title": texto_boton}
        for i, texto_boton in enumerate(botones)
    ]
    try:
        message = client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=f"whatsapp:{numero_destino}",
            body=cuerpo,
            actions=actions,
        )
        print(f"Mensaje interactivo de WhatsApp enviado con SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Error al enviar mensaje interactivo de WhatsApp: {e}")
        return False


def enviar_mensaje_whatsapp_con_lista(numero_destino, cuerpo, titulo_lista, secciones):
    """Envía un mensaje de WhatsApp con una lista interactiva."""
    client = _get_twilio_client()
    if not client or not TWILIO_WHATSAPP_NUMBER:
        return False

    actions = [{"button": titulo_lista, "sections": secciones}]
    try:
        message = client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=f"whatsapp:{numero_destino}",
            body=cuerpo,
            actions=actions,
        )
        print(
            f"Mensaje de lista interactiva de WhatsApp enviado con SID: {message.sid}"
        )
        return True
    except Exception as e:
        print(f"Error al enviar mensaje de lista interactiva de WhatsApp: {e}")
        return False

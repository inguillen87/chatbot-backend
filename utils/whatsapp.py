import requests
from twilio.rest import Client
import os
from twilio.rest import Client
import os

def enviar_mensaje_whatsapp(numero, mensaje, api_key):
    url = f"https://api.callmebot.com/whatsapp.php?phone={numero}&text={mensaje}&apikey={api_key}"
    response = requests.get(url)
    return response.status_code == 200

def enviar_mensaje_whatsapp_con_botones(numero_destino, cuerpo, botones):
    """
    Envía un mensaje de WhatsApp con botones interactivos usando Twilio.

    :param numero_destino: Número de teléfono del destinatario en formato E.164.
    :param cuerpo: El texto principal del mensaje.
    :param botones: Una lista de hasta 3 strings, cada uno para el texto de un botón.
    """
    if not all([os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"), os.environ.get("TWILIO_WHATSAPP_NUMBER")]):
        print("[TWILIO WHATSAPP] Faltan credenciales de Twilio para enviar mensajes interactivos.")
        return False

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

    # Prepara la lista de acciones (botones) para la API de Twilio
    actions = [
        {"id": f"btn_{i+1}", "type": "reply", "title": texto_boton}
        for i, texto_boton in enumerate(botones)
    ]

    try:
        message = client.messages.create(
            from_=f'whatsapp:{os.environ["TWILIO_WHATSAPP_NUMBER"]}',
            to=f'whatsapp:{numero_destino}',
            body=cuerpo,
            actions=actions
        )
        print(f"Mensaje interactivo de WhatsApp enviado con SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Error al enviar mensaje interactivo de WhatsApp: {e}")
        return False

def enviar_mensaje_whatsapp_con_botones(numero_destino, cuerpo, botones):
    """
    Envía un mensaje de WhatsApp con botones interactivos usando Twilio.

    :param numero_destino: Número de teléfono del destinatario en formato E.164.
    :param cuerpo: El texto principal del mensaje.
    :param botones: Una lista de hasta 3 strings, cada uno para el texto de un botón.
    """
    if not all([os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"), os.environ.get("TWILIO_WHATSAPP_NUMBER")]):
        print("[TWILIO WHATSAPP] Faltan credenciales de Twilio para enviar mensajes interactivos.")
        return False

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

    # Prepara la lista de acciones (botones) para la API de Twilio
    actions = [
        {"id": f"btn_{i+1}", "type": "reply", "title": texto_boton}
        for i, texto_boton in enumerate(botones)
    ]

    try:
        message = client.messages.create(
            from_=f'whatsapp:{os.environ["TWILIO_WHATSAPP_NUMBER"]}',
            to=f'whatsapp:{numero_destino}',
            body=cuerpo,
            actions=actions
        )
        print(f"Mensaje interactivo de WhatsApp enviado con SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Error al enviar mensaje interactivo de WhatsApp: {e}")
        return False

def enviar_mensaje_whatsapp_con_lista(numero_destino, cuerpo, titulo_lista, secciones):
    """
    Envía un mensaje de WhatsApp con una lista interactiva usando Twilio.

    :param numero_destino: Número de teléfono del destinatario en formato E.164.
    :param cuerpo: El texto principal del mensaje.
    :param titulo_lista: El título de la lista.
    :param secciones: Una lista de diccionarios, donde cada diccionario representa una sección de la lista.
    """
    if not all([os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"), os.environ.get("TWILIO_WHATSAPP_NUMBER")]):
        print("[TWILIO WHATSAPP] Faltan credenciales de Twilio para enviar mensajes interactivos.")
        return False

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

    # Prepara la lista de acciones (lista) para la API de Twilio
    actions = [
        {
            "button": titulo_lista,
            "sections": secciones
        }
    ]

    try:
        message = client.messages.create(
            from_=f'whatsapp:{os.environ["TWILIO_WHATSAPP_NUMBER"]}',
            to=f'whatsapp:{numero_destino}',
            body=cuerpo,
            actions=actions
        )
        print(f"Mensaje de lista interactiva de WhatsApp enviado con SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Error al enviar mensaje de lista interactiva de WhatsApp: {e}")
        return False

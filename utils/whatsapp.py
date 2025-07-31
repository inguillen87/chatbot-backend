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


def enviar_mensaje_whatsapp_con_fallback(numero_destino, cuerpo, botones=None, lista=None):
    """
    Intenta enviar un mensaje interactivo (botones o lista). Si falla, envía un mensaje de texto plano como fallback.
    """
    client = _get_twilio_client()
    if not client or not TWILIO_WHATSAPP_NUMBER:
        return False

    try:
        if botones:
            # Lógica para enviar con botones
            message = client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=f"whatsapp:{numero_destino}",
                body=cuerpo,
                actions=[{"id": f"btn_{i+1}", "type": "reply", "title": texto_boton} for i, texto_boton in enumerate(botones)],
            )
            print(f"Mensaje interactivo de WhatsApp enviado con SID: {message.sid}")
            return True
        elif lista:
            # Lógica para enviar con lista
            message = client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=f"whatsapp:{numero_destino}",
                body=cuerpo,
                actions=[{"button": lista["titulo"], "sections": lista["secciones"]}],
            )
            print(f"Mensaje de lista interactiva de WhatsApp enviado con SID: {message.sid}")
            return True
        else:
            # Lógica para enviar mensaje simple
            message = client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=f"whatsapp:{numero_destino}",
                body=cuerpo,
            )
            print(f"Mensaje de texto simple de WhatsApp enviado con SID: {message.sid}")
            return True
    except Exception as e:
        print(f"Error al enviar mensaje interactivo de WhatsApp: {e}. Intentando fallback a texto plano.")
        try:
            fallback_body = cuerpo
            if botones:
                fallback_body += "\n\nOpciones:\n" + "\n".join([f"- {b}" for b in botones])
            elif lista:
                fallback_body += f"\n\n{lista['titulo']}\n"
                for seccion in lista['secciones']:
                    fallback_body += f"\n*{seccion['title']}*\n"
                    for row in seccion['rows']:
                        fallback_body += f"- {row['title']}\n"

            message = client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=f"whatsapp:{numero_destino}",
                body=fallback_body,
            )
            print(f"Mensaje de fallback de WhatsApp enviado con SID: {message.sid}")
            return True
        except Exception as e_fallback:
            print(f"Error al enviar mensaje de fallback de WhatsApp: {e_fallback}")
            return False

def enviar_mensaje_whatsapp_con_botones(numero_destino, cuerpo, botones):
    """Envía un mensaje de WhatsApp con hasta 3 botones de respuesta rápida."""
    return enviar_mensaje_whatsapp_con_fallback(numero_destino, cuerpo, botones=botones)


def enviar_mensaje_whatsapp_con_lista(numero_destino, cuerpo, titulo_lista, secciones):
    """Envía un mensaje de WhatsApp con una lista interactiva."""
    lista = {"titulo": titulo_lista, "secciones": secciones}
    return enviar_mensaje_whatsapp_con_fallback(numero_destino, cuerpo, lista=lista)

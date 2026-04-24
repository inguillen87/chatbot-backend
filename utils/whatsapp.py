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

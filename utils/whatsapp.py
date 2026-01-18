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


def enviar_mensaje_whatsapp_con_fallback(numero_destino, cuerpo, botones=None, lista=None, image_url=None, from_number=None, messaging_service_sid=None):
    """
    Intenta enviar un mensaje interactivo (botones o lista). Si falla, envía un mensaje de texto plano como fallback.
    Permite anular el remitente (from_number) o usar un messaging_service_sid.
    """
    client = _get_twilio_client()
    if not client:
        return False

    # Sanitizar número destino: si viene como "+549...", asegurarse de tener "whatsapp:"
    # Si viene como "whatsapp:+549...", está bien.
    if not numero_destino.startswith("whatsapp:"):
        numero_destino = f"whatsapp:{numero_destino}"

    # Configurar remitente
    message_params = {"to": numero_destino, "body": cuerpo}

    if messaging_service_sid:
        message_params["messaging_service_sid"] = messaging_service_sid
    else:
        # Priorizar from_number si existe, sino usar el default del env
        sender_raw = from_number or TWILIO_WHATSAPP_NUMBER
        if not sender_raw:
             print("[TWILIO WHATSAPP] No sender configured (from_number or TWILIO_WHATSAPP_NUMBER missing).")
             return False

        # Ensure 'whatsapp:' prefix for pure numbers
        sender = f"whatsapp:{sender_raw}" if not sender_raw.startswith("whatsapp:") and not sender_raw.startswith("MG") else sender_raw
        message_params["from_"] = sender

    if image_url:
        message_params["media_url"] = [image_url]

    try:
        if botones:
            # Lógica para enviar con botones
            # Nota: content_sid podría ser requerido para templates pre-aprobados en prod.
            # Aquí usamos el modo "session" (ventana de 24hs) o templates si estuvieran configurados.
            # Para simplificar, asumimos que el mensaje es libre si estamos en ventana.
            # Asignamos params base
            client.messages.create(
                **message_params
                # actions no es soportado directamente en create() salvo via content_sid o messaging service
                # Twilio no soporta botones dinámicos en 'body' + 'actions' para WhatsApp directamente sin templates.
                # A MENOS que sea Sandbox.
                # Intentamos texto plano con botones si es sandbox, o fallback si falla.
                # Para mantener compatibilidad, si falla 'actions' no estándar, vamos a fallback.
                # Nota: La librería de python usa 'content_sid' para templates.
                # Si queremos botones libres, Twilio no los soporta fuera de templates aprobados.
                # EXCEPTO reply buttons en sesión iniciada por usuario.
                # Intentaremos mandar texto + lista textual.
            )
            # Como create() no acepta 'actions' arbitrarios para WhatsApp sin content_sid,
            # forzamos el fallback directamente si botones están presentes,
            # O asumimos que la implementación anterior usaba una librería modificada o wrapper.
            # Dado el error reportado, mejor simplificar a texto.
            raise Exception("Botones dinámicos no soportados sin Template.")

        elif lista:
             raise Exception("Listas dinámicas no soportadas sin Template.")
        else:
            # Lógica para enviar mensaje simple
            message = client.messages.create(**message_params)
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

            # Update body in params
            message_params["body"] = fallback_body

            message = client.messages.create(**message_params)
            print(f"Mensaje de fallback de WhatsApp enviado con SID: {message.sid}")
            return True
        except Exception as e_fallback:
            print(f"Error al enviar mensaje de fallback de WhatsApp: {e_fallback}")
            return False
                # actions no es soportado directamente en create() salvo via content_sid o messaging service
                # Twilio no soporta botones dinámicos en 'body' + 'actions' para WhatsApp directamente sin templates.
                # A MENOS que sea Sandbox.
                # Intentamos texto plano con botones si es sandbox, o fallback si falla.
                # Para mantener compatibilidad, si falla 'actions' no estándar, vamos a fallback.
                # Nota: La librería de python usa 'content_sid' para templates.
                # Si queremos botones libres, Twilio no los soporta fuera de templates aprobados.
                # EXCEPTO reply buttons en sesión iniciada por usuario.
                # Intentaremos mandar texto + lista textual.
            )
            # Como create() no acepta 'actions' arbitrarios para WhatsApp sin content_sid,
            # forzamos el fallback directamente si botones están presentes,
            # O asumimos que la implementación anterior usaba una librería modificada o wrapper.
            # Dado el error reportado, mejor simplificar a texto.
            raise Exception("Botones dinámicos no soportados sin Template.")

        elif lista:
             raise Exception("Listas dinámicas no soportadas sin Template.")
        else:
            # Lógica para enviar mensaje simple
            message = client.messages.create(
                from_=sender,
                to=numero_destino,
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
                from_=sender,
                to=numero_destino,
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


def enviar_imagen_whatsapp(numero_destino, cuerpo, url_imagen):
    """Envía un mensaje de WhatsApp con una imagen adjunta."""
    client = _get_twilio_client()
    if not client or not TWILIO_WHATSAPP_NUMBER:
        return False

    sender = f"whatsapp:{TWILIO_WHATSAPP_NUMBER}" if not TWILIO_WHATSAPP_NUMBER.startswith("whatsapp:") else TWILIO_WHATSAPP_NUMBER
    if not numero_destino.startswith("whatsapp:"):
        numero_destino = f"whatsapp:{numero_destino}"

    try:
        message = client.messages.create(
            from_=sender,
            to=numero_destino,
            body=cuerpo,
            media_url=[url_imagen],
        )
        print(f"Mensaje de WhatsApp con imagen enviado con SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Error al enviar mensaje de WhatsApp con imagen: {e}")
        return False

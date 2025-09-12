import re

MUNI_URL = "https://www.juninmendoza.gov.ar/"

def _remove_redundant_urls_from_message(message_body, options_list):
    """
    Removes URLs from the message body if they are already present in the buttons.
    """
    if not message_body or not options_list:
        return message_body

    message_body_str = str(message_body)

    for option in options_list:
        if isinstance(option, dict) and 'url' in option and option['url'] in message_body_str:
            message_body_str = message_body_str.replace(option['url'], '')

    # Clean up common leftover phrases and extra spaces
    message_body_str = re.sub(r'por favor\s+ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body_str, flags=re.IGNORECASE).strip()
    message_body_str = re.sub(r'ingresá\s+al\s+siguiente\s+enlace\s*:?', '', message_body_str, flags=re.IGNORECASE).strip()
    message_body_str = re.sub(r'enlace\s*:?', '', message_body_str, flags=re.IGNORECASE).strip()

    # Replace multiple spaces with a single space and clean up punctuation
    message_body_str = re.sub(r'\s{2,}', ' ', message_body_str).strip()
    message_body_str = message_body_str.replace(' .', '.').strip()
    message_body_str = re.sub(r'[,:]\s*\.', '.', message_body_str)
    if message_body_str == ':':
        message_body_str = ''

    if not message_body_str.strip():
        return message_body

    return message_body_str

def formatear_ticket_respuesta(
    tipo,
    nombre_usuario,
    descripcion,
    categoria,
    id_ticket=None,
    contacto_especializado=None,
    base_chat_url=None,
    dni=None,
    telefono=None,
    email=None,
    consulta_pin=None,
):
    nombre_asesor = None
    titulo_asesor = None
    telefono_asesor = None
    horario_asesor = None
    link_informacion = None
    link_whatsapp = None
    botones = []
    chat_url = None

    if contacto_especializado:
        nombre_asesor = contacto_especializado.get("nombre")
        titulo_asesor = contacto_especializado.get("titulo")
        telefono_asesor = contacto_especializado.get("telefono")
        horario_asesor = contacto_especializado.get("horario")
        link_informacion = contacto_especializado.get("link")
        if nombre_asesor and telefono_asesor:
            # telefono_numerico = ''.join(filter(str.isdigit, str(telefono_asesor)))
            # link_whatsapp = f"https://wa.me/{telefono_numerico}?text=Hola,%20quiero%20hacer%20seguimiento%20de%20mi%20{tipo}%20(ID:{id_ticket})"
            # botones.append({
            #     "texto": f"📱 Contactar a {nombre_asesor}",
            #     "url": str(link_whatsapp),
            #     "type": "url"
            # })
            pass
        if link_informacion:
            botones.append({
                "texto": "🌐 Más información",
                "url": str(link_informacion),
                "type": "url"
            })

    if id_ticket and base_chat_url:
        if base_chat_url.endswith('/'):
            base_chat_url = base_chat_url[:-1]

        ticket_id_numeric = id_ticket.replace('M-', '').replace('S-', '')
        chat_url = f"{base_chat_url}/{ticket_id_numeric}"
        if consulta_pin:
            chat_url += f"?pin={consulta_pin}"
        botones.append({
            "texto": "💬 Ver mi Ticket",
            "url": chat_url,
            "type": "url"
        })

    tipos = {
        "reclamo": "Reclamo",
        "sugerencia": "Sugerencia",
        "tramite": "Trámite",
        "chat": "Chat en vivo"
    }
    texto_tipo = tipos.get(tipo, "Consulta")

    respuesta = f"""✅ *¡{texto_tipo} recibido, {nombre_usuario}!*

📄 *Resumen de tu {texto_tipo}:*
- *N° de Ticket:* `{id_ticket}`
- *Categoría:* {categoria}
- *Descripción:* {descripcion}
"""
    if dni:
        respuesta += f"\n- *DNI:* {dni}"
    if telefono:
        respuesta += f"\n- *Teléfono:* {telefono}"
    if email:
        respuesta += f"\n- *Email:* {email}"
    if consulta_pin:
        respuesta += f"\n- *PIN:* {consulta_pin}"

    if nombre_asesor:
        partes_contacto = [nombre_asesor]
        if titulo_asesor:
            partes_contacto.append(titulo_asesor)
        if telefono_asesor:
            partes_contacto.append(str(telefono_asesor))
        respuesta += "\n📞 *Contacto para seguimiento:* " + " - ".join(partes_contacto)
        if horario_asesor:
            respuesta += f"\n🕒 *Horario de atención:* {horario_asesor}"
        if link_informacion:
            respuesta += f"\n🔗 {link_informacion}"

    if dni or telefono or email:
        respuesta += "\n\n🔎 Si tus datos no son correctos, respondé *Actualizar datos*."

    respuesta += (
        f"\n🌐 Más información municipal: {MUNI_URL}"
    )
    if chat_url:
        respuesta += f"\n💬 Ver mi Ticket: {chat_url}"

    respuesta += "\nTe mantendremos al tanto de las novedades. ¡Gracias por tu colaboración!"

    # Añadir botones estándar si no existen
    existing_texts = {b.get("texto") for b in botones}
    if "🌐 Más información" not in existing_texts:
        botones.append({
            "texto": "🌐 Más información",
            "url": MUNI_URL,
            "type": "url",
        })
    if chat_url and "💬 Ver mi Ticket" not in existing_texts:
        botones.append({
            "texto": "💬 Ver mi Ticket",
            "url": chat_url,
            "type": "url",
        })

    # Limpiar URLs redundantes del cuerpo del mensaje, preservando los enlaces principales
    botones_para_limpieza = [
        b for b in botones if b.get("texto") not in {"💬 Ver mi Ticket", "🌐 Más información"}
    ]
    respuesta_limpia = _remove_redundant_urls_from_message(respuesta, botones_para_limpieza)

    return respuesta_limpia, botones

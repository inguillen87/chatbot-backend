import re

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

    return message_body_str

def formatear_ticket_respuesta(tipo, nombre_usuario, descripcion, categoria, id_ticket=None, contacto_especializado=None, base_chat_url=None, dni=None, consulta_pin=None):
    nombre_asesor = None
    titulo_asesor = None
    telefono_asesor = None
    horario_asesor = None
    link_informacion = None
    link_whatsapp = None
    botones = []

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

    def _resumir_descripcion(texto: str) -> str:
        """Corta prefijos innecesarios de la descripción para un resumen más claro."""
        if not texto:
            return texto
        texto = texto.strip()
        prefijos = [
            r"^tengo\s+un?\s+\w+\s+",
            r"^hay\s+un?\s+\w+\s+",
        ]
        for patron in prefijos:
            nuevo = re.sub(patron, "", texto, flags=re.IGNORECASE)
            if nuevo != texto:
                texto = nuevo
                break
        return texto

    descripcion_resumen = _resumir_descripcion(descripcion)

    respuesta = f"""✅ *¡{texto_tipo} recibido, {nombre_usuario}!*

📄 *Resumen de tu {texto_tipo}:*
- *N° de Ticket:* `{id_ticket}`
- *Categoría:* {categoria}
- *Descripción:* {descripcion_resumen}
"""
    if dni:
        respuesta += f"- *DNI:* `{dni}`\n"
    if consulta_pin:
        respuesta += f"- *PIN de seguimiento:* `{consulta_pin}`\n"

    if nombre_asesor:
        respuesta = respuesta.rstrip("\n") + "\n"
        respuesta += "📞 *Contacto para seguimiento:*\n"
        respuesta += f"- *Nombre:* {nombre_asesor}\n"
        if titulo_asesor:
            respuesta += f"- *Cargo:* {titulo_asesor}\n"
        if telefono_asesor:
            respuesta += f"- *Teléfono:* {telefono_asesor}\n"
        if horario_asesor:
            respuesta += f"- *Horario:* {horario_asesor}\n"

    if link_informacion:
        respuesta += f"🔗 *Más información:* {link_informacion}\n"


    respuesta += """
¡Gracias por tu colaboración!"""

    # Limpiar URLs redundantes del cuerpo del mensaje
    respuesta_limpia = _remove_redundant_urls_from_message(respuesta, botones)

    return respuesta_limpia, botones

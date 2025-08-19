def formatear_ticket_respuesta(tipo, nombre_usuario, descripcion, categoria, id_ticket=None, contacto_especializado=None, base_chat_url=None):
    nombre_asesor = None
    telefono_asesor = None
    horario_asesor = None
    link_informacion = None
    link_whatsapp = None
    botones = []

    if contacto_especializado:
        nombre_asesor = contacto_especializado.get("nombre")
        telefono_asesor = contacto_especializado.get("telefono")
        horario_asesor = contacto_especializado.get("horario")
        link_informacion = contacto_especializado.get("link")
        if nombre_asesor and telefono_asesor:
            telefono_numerico = ''.join(filter(str.isdigit, telefono_asesor))
            link_whatsapp = f"https://wa.me/{telefono_numerico}?text=Hola,%20quiero%20hacer%20seguimiento%20de%20mi%20{tipo}%20(ID:{id_ticket})"
            botones.append({
                "texto": f"📱 Contactar a {nombre_asesor}",
                "url": link_whatsapp,
                "type": "url"
            })
        if link_informacion:
            botones.append({
                "texto": "🌐 Más información",
                "url": link_informacion,
                "type": "url"
            })

    if id_ticket and base_chat_url:
        if base_chat_url.endswith('/'):
            base_chat_url = base_chat_url[:-1]

        ticket_id_numeric = id_ticket.replace('M-', '').replace('S-', '')
        chat_url = f"{base_chat_url}/{ticket_id_numeric}"
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

    if nombre_asesor:
        respuesta += f"""
📞 *Contacto para seguimiento:* {nombre_asesor}"""
        if horario_asesor:
            respuesta += f"\n🕒 *Horario de atención:* {horario_asesor}"

    respuesta += """

Te mantendremos al tanto de las novedades. ¡Gracias por tu colaboración!"""

    return respuesta, botones

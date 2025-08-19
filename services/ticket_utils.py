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
        # The frontend URL for viewing a ticket is /ticket/<nro>, not /chat/<nro>
        # We derive the base URL and append the correct path.
        base_url = base_chat_url.split('/chat')[0] if '/chat' in base_chat_url else base_chat_url
        chat_url = f"{base_url}/ticket/{ticket_id_numeric}"
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

    # --- Nuevo formato de respuesta ---
    respuesta = f"""*¡Tu {texto_tipo} ha sido registrado con éxito!* ✅

Aquí tienes los detalles de tu ticket:
-----------------------------------
- 📝 *Número de Ticket:* `{id_ticket}`
- 🗂️ *Categoría:* {categoria}
- 📋 *Tu descripción:* "{descripcion}"
-----------------------------------

*¿Qué sigue ahora?*
El área correspondiente revisará tu solicitud. Te notificaremos por este medio sobre cualquier actualización.
"""

    # Bloque de contacto mejorado
    if nombre_asesor or telefono_asesor:
        respuesta += "\n*¿Necesitas hacer un seguimiento?*\n"
        if nombre_asesor:
            respuesta += f"- 🏢 *Área:* {nombre_asesor}\n"
        if horario_asesor:
            respuesta += f"- ⏰ *Horario:* {horario_asesor}\n"
        if telefono_asesor:
            respuesta += f"- 📞 *Teléfono:* {telefono_asesor}\n"

    respuesta += "\n\nGracias por ayudarnos a mejorar Junín."

    return respuesta, botones

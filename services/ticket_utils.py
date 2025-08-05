def formatear_ticket_respuesta(tipo, nombre_usuario, descripcion, categoria, id_ticket=None, contacto_especializado=None):
    nombre_asesor = None
    telefono_asesor = None
    link_whatsapp = None
    boton_contacto = None

    if contacto_especializado:
        # No hardcodear el default aquí, se debe manejar en el llamador.
        nombre_asesor = contacto_especializado.get("nombre")
        telefono_asesor = contacto_especializado.get("telefono")
        if nombre_asesor and telefono_asesor:
            # Limpiar y asegurar que el número de teléfono sea solo dígitos
            telefono_numerico = ''.join(filter(str.isdigit, telefono_asesor))
            link_whatsapp = f"https://wa.me/{telefono_numerico}?text=Hola,%20quiero%20hacer%20seguimiento%20de%20mi%20{tipo}%20(ID:{id_ticket})"
            boton_contacto = {
                "texto": f"Contactar a {nombre_asesor}",
                "url": link_whatsapp
            }

    tipos = {
        "reclamo": "Reclamo",
        "sugerencia": "Sugerencia",
        "tramite": "Trámite",
        "chat": "Chat en vivo"
    }
    texto_tipo = tipos.get(tipo, "Consulta")

    respuesta = (
        f"✅ ¡{texto_tipo} recibido, {nombre_usuario}!

"
        f"📄 **Resumen:**
"
    )
    if categoria:
        respuesta += f"  - **Categoría:** {categoria}
"
    if descripcion:
        respuesta += f"  - **Descripción:** {descripcion}
"
    if id_ticket:
        respuesta += f"  - **N° de Ticket:** {id_ticket}
"

    # No mostrar el link en el texto, solo en el botón.
    if nombre_asesor:
        respuesta += (
            f"

**Contacto para seguimiento:**
"
            f"Para seguir el estado de tu ticket, podés hablar directamente con **{nombre_asesor}**."
        )

    respuesta += f"

Te mantendremos al tanto de las novedades. ¡Gracias por tu colaboración!"

    # Devuelve tanto el texto formateado como el botón de contacto si existe
    return respuesta, boton_contacto

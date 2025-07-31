def formatear_ticket_respuesta(tipo, nombre_usuario, descripcion, categoria, id_ticket=None, contacto_especializado=None):
    nombre_asesor = None
    telefono_asesor = None
    link_whatsapp = None

    if contacto_especializado:
        nombre_asesor = contacto_especializado.get("nombre")
        telefono_asesor = contacto_especializado.get("telefono")
        if telefono_asesor:
            link_whatsapp = f"https://wa.me/{telefono_asesor.replace('+', '')}?text=Hola,%20quiero%20hacer%20seguimiento%20de%20mi%20{tipo}%20(ID:{id_ticket})"

    tipos = {
        "reclamo": "reclamo",
        "sugerencia": "sugerencia",
        "tramite": "trámite"
    }
    texto_tipo = tipos.get(tipo, "consulta")

    respuesta = (
        f"✅ {texto_tipo.capitalize()} recibido, {nombre_usuario}!\n\n"
        f"📄 **Resumen:**\n"
        f"  - **Categoría:** {categoria}\n"
        f"  - **Descripción:** {descripcion}\n"
    )
    if id_ticket:
        respuesta += f"  - **N° de Ticket:** {id_ticket}\n"

    if nombre_asesor:
        respuesta += f"\n👤 **Asesor a cargo:** {nombre_asesor}"

    respuesta += f"\n\nTe mantendremos al tanto de las novedades. ¡Gracias por tu colaboración!"
    return respuesta

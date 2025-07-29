def obtener_asesor_categoria(categoria):
    # Ejemplo: podés tener un diccionario
    MAPA_ASESORES = {
        "Luminaria": ("Juan Pérez", "+5492613000001"),
        "Trámites": ("Ana Gómez", "+5492613000002"),
        # Default:
        "default": ("Soporte Municipio", "+5492613168608"),
    }
    return MAPA_ASESORES.get(categoria, MAPA_ASESORES["default"])

def formatear_ticket_respuesta(tipo, nombre_usuario, descripcion, categoria, id_ticket=None):
    nombre_asesor, telefono_asesor = obtener_asesor_categoria(categoria)
    link_whatsapp = f"https://wa.me/{telefono_asesor.replace('+', '')}?text=Hola,%20quiero%20hacer%20seguimiento%20de%20mi%20{tipo}%20(ID:{id_ticket})"

    tipos = {
        "reclamo": "reclamo",
        "sugerencia": "sugerencia",
        "tramite": "trámite"
    }
    texto_tipo = tipos.get(tipo, "consulta")

    respuesta = (
        f"✅ {texto_tipo.capitalize()} recibido, {nombre_usuario}!\n"
        f"• Categoría: {categoria}\n"
        f"• Descripción: {descripcion}\n"
    )
    if id_ticket:
        respuesta += f"• Número de ticket: {id_ticket}\n"
    respuesta += (
        f"\n👉 Para hacer el seguimiento o recibir ayuda personalizada, podés comunicarte con nuestro asesor {nombre_asesor} "
        f"al WhatsApp {telefono_asesor} o haciendo clic aquí: {link_whatsapp}\n"
        f"¡Gracias por comunicarte! Vamos a darle seguimiento a tu {texto_tipo}."
    )
    return respuesta

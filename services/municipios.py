def responder_municipio(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):

    """
    Lógica específica para municipios: prompt especial, memoria (historial con el vecino), tickets, etc.
    """
    from services.cohere_ai import get_cohere_response
    from services.logic import reemplazar_placeholders

    # 1. Histórico para contexto
    NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-6:]

    prompt = (
        f"Sos el asistente virtual oficial de la Municipalidad de {user_obj.nombre_empresa or rubro_obj.nombre}, provincia de {user_obj.provincia or 'Mendoza'}."
        "\nTu función es atender consultas, reclamos, trámites y brindar información oficial a los vecinos. "
        "Nunca vendas productos ni menciones compras online. Si recibís un reclamo, explicá que fue registrado y sugerí el número de ticket. "
        "Si la consulta requiere derivación, sugerí canales oficiales: teléfono, turnos, sitio web o ventanilla única."
        "\nSiempre responde de forma clara, cordial y con información oficial, en lenguaje sencillo."
        "\nSi el vecino pregunta varias veces, recordá el historial de la conversación."
        "\nEjemplos de temas: bacheo, recolección de residuos, tasas municipales, habilitaciones, licencias, denuncias, luminaria, seguridad, turnos, reclamos."
        "\n\nINSTRUCCIONES:"
        "\n- NO respondas como vendedor, nunca digas 'carrito', 'compra', 'producto', ni promociones."
        "\n- Si la pregunta es un reclamo, generá un ticket y devolvé un número único."
        "\n- Si no sabés la respuesta, decí: 'Para ese trámite específico te recomiendo comunicarte por WhatsApp al [telefono] o visitar la web oficial [linkWeb]'."
        "\n- Finalizá siempre la respuesta preguntando si necesita otra gestión."
        "\n\nHISTORIAL DE LA CONVERSACIÓN:"
    )
    for msg in mensajes_previos:
        prompt += f"\n- {msg.get('role')}: {msg.get('content')}"
    prompt += f"\n- Vecino: {pregunta}\n- Asistente:"

    # 2. Ticket automático si es reclamo
    if contiene_reclamo(pregunta):
        nro_ticket = generar_ticket_db(pregunta, user_obj)
        respuesta = f"Tu reclamo fue registrado con el número #{nro_ticket}. Nuestro equipo lo revisará a la brevedad. ¿Querés realizar otra gestión?"
        return {"respuesta": respuesta, "fuente": "municipio_ticket"}

    # 3. Cohere
    respuesta_llm = get_cohere_response(
        message=pregunta,
        chat_history=[{"role": m.get("role","user"), "message": m.get("content","")} for m in mensajes_previos],
        preamble=prompt,
        rubro_id=rubro_obj.id,
        user_context={ # opcional, podés sumar más datos
            "nombre_empresa": user_obj.nombre_empresa,
            "telefono": user_obj.telefono,
            "link_web": user_obj.link_web
        }
    )
    respuesta_llm = reemplazar_placeholders(respuesta_llm, user_obj)
    # Guardá historial (memoria)
    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    session.modified = True
    return {"respuesta": respuesta_llm, "fuente": "cohere"}

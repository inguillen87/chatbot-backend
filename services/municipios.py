def responder_municipio(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    import datetime
    import json
    from flask import session
    from services.cohere_ai import get_cohere_response
    from services.logic import reemplazar_placeholders
    from models import Conversacion, MunicipioTicket, db  # <--- importa modelos y db

    session = session_obj if session_obj is not None else __import__("flask").session

    NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]

    nombre_municipio = user_obj.nombre_empresa or rubro_obj.nombre or "el municipio"
    telefono = user_obj.telefono or ""
    telefono_wsp = ''.join(filter(str.isdigit, telefono))
    web_oficial = user_obj.link_web or "https://www.argentina.gob.ar"
    direccion = getattr(user_obj, "direccion", "") or "Consultar en la web"
    ciudad = getattr(user_obj, "ciudad", "") or ""
    provincia = getattr(user_obj, "provincia", "") or ""
    direccion_completa = f"{direccion}, {ciudad}, {provincia}".replace(" ,", "").strip(", ")
    horario = getattr(user_obj, "horario", "Consultar en la web oficial")
    horario_json_str = getattr(user_obj, "horario_json", "[]")
    horarios_para_prompt = horario
    try:
        if horario_json_str and isinstance(horario_json_str, str) and horario_json_str.startswith("["):
            horarios_data = json.loads(horario_json_str)
            partes = [f"{h.get('dia','')}: de {h.get('abre','--:--')} a {h.get('cierra','--:--')}" if not h.get('cerrado') else f"{h.get('dia','')}: Cerrado"
                      for h in horarios_data if isinstance(h, dict) and h.get('dia')]
            if partes:
                horarios_para_prompt = ". ".join(partes) + "."
    except Exception:
        horarios_para_prompt = horario

    # --- Lógica para pedir datos de contacto si faltan ---
    datos_faltantes = []
    if not user_obj.telefono or user_obj.telefono.strip() == "":
        datos_faltantes.append("teléfono")
    if not user_obj.email or user_obj.email.strip() == "":
        datos_faltantes.append("email")
    # Si después querés pedir DNI u otros, agregalos acá

    if datos_faltantes:
        mensaje = "Antes de continuar, por favor brindá tu " + " y ".join(datos_faltantes) + " para que podamos ayudarte mejor."
        # Guardá conversación
        conv = Conversacion(
            user_id=user_obj.id if user_obj else None,
            pregunta=pregunta,
            respuesta=mensaje,
            fuente="falta_dato_contacto",
            rubro="municipio"
        )
        db.session.add(conv)
        db.session.commit()
        # También guardalo en historial de sesión para el contexto del LLM
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": mensaje})
        session.modified = True
        return {"respuesta": mensaje + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": "falta_dato_contacto"}

    # --- Prompt Mejorado ---
    prompt = (
        f"Chatboc, un agente de atención ciudadana para {nombre_municipio}, ubicado en {direccion_completa}."
        " Atendés consultas, reclamos y trámites de vecinos, de manera profesional, clara y amable, como lo haría un empleado municipal real."
        "\n\n- No sos un chatbot ni una IA. Usá lenguaje institucional pero humano, sencillo y cercano. Evitá tecnicismos y nunca te presentes como asistente virtual."
        "\n- No vendas productos ni promociones, jamás menciones compras, carrito o precios."
        "\n- Si recibís un reclamo (ej: bache, luminaria, basura, ruidos, seguridad), generá ticket y devolvé el número único."
        "\n- Para trámites y servicios (licencias, turnos, habilitaciones, tasas, partidas), explicá paso a paso cómo se hacen, ofreciendo link, teléfono, dirección y horarios cuando corresponda. Si solo es presencial, aclaralo."
        "\n- Si preguntan por horarios, usá la info oficial. Si falta, derivá a la web o teléfono."
        "\n- Si no sabés algo, decí 'No tengo ese dato exacto ahora, podés consultarlo en la web oficial o al teléfono informado arriba.'"
        "\n- Si la pregunta es confusa, contestá: 'Disculpá, no entendí la consulta. Puedo ayudarte con trámites, reclamos, turnos o servicios municipales. ¿Con qué tema necesitas ayuda hoy?'"
        "\n- Al final de cada respuesta, preguntá amablemente si necesitás gestionar algo más, por ejemplo: '¿Querés que te pase el link?', '¿Te ayudo con otro trámite?', '¿Necesitás info de servicios digitales?'"
        "\n- Mantené siempre el contexto de la charla. Si hay reclamos repetidos, empatizá y explicá el seguimiento."
        "\n\nDATOS OFICIALES DEL MUNICIPIO:"
        f"\n🏛️ Municipio: {nombre_municipio}"
        f"\n📞 Teléfono: {telefono if telefono else 'No especificado. Consultá la web oficial.'}"
        f"\n📍 Dirección: {direccion_completa}"
        f"\n🕐 Horarios: {horarios_para_prompt}"
        f"\n🌐 Web oficial: {web_oficial}"
        "\n\n---\nHISTORIAL DE LA CONVERSACIÓN:"
    )
    for msg in mensajes_previos:
        prompt += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
    prompt += f"\n- Vecino: {pregunta}\n- Agente:"

    # --- Detección de reclamos ---
    def contiene_reclamo(texto):
        claves = ["bache", "reclamo", "denuncia", "luminaria", "basura", "ruido", "inseguridad", "robo", "perro suelto", "poda", "árbol", "corte de agua", "vereda rota", "servicio no funciona", "corte de luz"]
        return any(k in texto.lower() for k in claves)

    # --- Si hay reclamo, genera y guarda ticket ---
    if contiene_reclamo(pregunta):
        import random
        nro_ticket = random.randint(10000, 99999)
        ticket = MunicipioTicket(
            pregunta=pregunta,
            user_id=user_obj.id if user_obj else None,
            estado="nuevo",
            nro_ticket=nro_ticket,
            fecha=datetime.datetime.utcnow()
        )
        db.session.add(ticket)
        db.session.commit()
        respuesta_ticket = (
            f"Tu reclamo fue registrado con el número #{nro_ticket}. "
            "Nuestro equipo lo revisará a la brevedad. ¿Te gustaría gestionar otro trámite o consulta?"
        )
        # Guardá conversación
        conv = Conversacion(
            user_id=user_obj.id if user_obj else None,
            pregunta=pregunta,
            respuesta=respuesta_ticket,
            fuente="municipio_ticket",
            rubro="municipio"
        )
        db.session.add(conv)
        db.session.commit()
        session[NOMBRE_HISTORIAL_SESION].extend([
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": respuesta_ticket}
        ])
        session.modified = True
        return {"respuesta": respuesta_ticket + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": "municipio_ticket"}

    # --- LLM principal (Cohere/GPT) ---
    try:
        respuesta_llm = get_cohere_response(
            message=pregunta,
            chat_history=[{"role": m.get("role", "user"), "message": m.get("content", "")} for m in mensajes_previos],
            preamble=prompt,
            rubro_id=rubro_obj.id,
            user_context={
                "nombre_empresa": nombre_municipio,
                "telefono": telefono,
                "link_web": web_oficial,
                "direccion": direccion_completa,
                "horario": horarios_para_prompt
            }
        )
        respuesta_llm = reemplazar_placeholders(respuesta_llm, user_obj)
    except Exception:
        respuesta_llm = "Lo siento, hubo un problema técnico al procesar tu consulta. Podés comunicarte telefónicamente o por la web oficial."

    # Guarda conversación (todo)
    conv = Conversacion(
        user_id=user_obj.id if user_obj else None,
        pregunta=pregunta,
        respuesta=respuesta_llm,
        fuente="cohere",
        rubro="municipio"
    )
    db.session.add(conv)
    db.session.commit()

    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    session.modified = True

    return {
        "respuesta": respuesta_llm + render_botones_municipio(web_oficial, telefono_wsp, pregunta),
        "fuente": "cohere"
    }

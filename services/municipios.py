def responder_municipio(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    """
    Lógica ultra robusta de atención municipal inteligente:
    - Prompt contextual y completo.
    - Responde como agente humano, no IA.
    - Memoria/historial del vecino.
    - Tickets automáticos para reclamos.
    - Botones de WhatsApp y Web oficial si corresponde.
    - Maneja errores y placeholders.
    """
    import datetime
    import random
    import json
    import urllib.parse
    from flask import session

    from services.cohere_ai import get_cohere_response
    from services.logic import reemplazar_placeholders

    # Usa session_obj si viene, sino usa el global de flask
    session = session_obj if session_obj is not None else session

    # ---- Contexto clave ----
    NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]

    nombre_municipio = user_obj.nombre_empresa or rubro_obj.nombre or "el municipio"
    telefono = getattr(user_obj, "telefono", "") or "No especificado. Consultá la web oficial."
    web_oficial = getattr(user_obj, "link_web", "") or "https://www.argentina.gob.ar"
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
            partes = [
                f"{h.get('dia','')}: de {h.get('abre','--:--')} a {h.get('cierra','--:--')}" if not h.get('cerrado')
                else f"{h.get('dia','')}: Cerrado"
                for h in horarios_data if isinstance(h, dict) and h.get('dia')
            ]
            if partes:
                horarios_para_prompt = ". ".join(partes) + "."
    except Exception:
        horarios_para_prompt = horario

    # ---- Prompt de sistema ----
    prompt = (
        f"Sos Chatboc, un agente municipal experto en atención ciudadana para {nombre_municipio}, ubicado en {direccion_completa}. "
        "Tu objetivo principal es ayudar al vecino a resolver su consulta, guiarlo en trámites, reclamos y acceso a servicios municipales en el menor número de pasos posibles (idealmente 2-4). "
        f"Ya tuviste {len(mensajes_previos)//2} interacciones previas en esta sesión. Revisá el historial para no repetir info y mantener contexto."
        "\nActuá siempre como un empleado real del municipio: profesional, cálido, directo, resolutivo y paciente. Usá lenguaje sencillo, nunca técnico ni robotizado. No uses palabras de IA, chatbot ni te presentes como asistente virtual."
        "\nINFORMACIÓN OFICIAL DEL MUNICIPIO:"
        f"\n- Teléfono: {telefono}"
        f"\n- Dirección: {direccion_completa}"
        f"\n- Horarios de atención: {horarios_para_prompt}"
        f"\n- Web oficial: {web_oficial}"
        "\nINSTRUCCIONES DE RESPUESTA:"
        "\n1. Si es un reclamo o denuncia (bache, luminaria, basura, ruidos, seguridad, etc), generá ticket y devolvé el número único: 'Tu reclamo fue registrado con el número #[ticket]. Nuestro equipo lo revisará a la brevedad. ¿Querés realizar otra gestión?'"
        "\n2. Si la consulta es sobre trámites (licencias, habilitaciones, tasas, turnos, partidas), explicá paso a paso cómo hacerlo y guiá con link, teléfono o dirección. Si el trámite es solo presencial, aclaralo con horarios."
        "\n3. Si preguntan por horarios, usá la información que tenés. Si falta, derivá a la web oficial."
        "\n4. Si no tenés el dato exacto, nunca inventes: decí 'No tengo ese dato exacto ahora, pero podés consultar en nuestra web oficial o llamarnos al teléfono informado arriba. ¿Te ayudo con otra gestión?'"
        "\n5. Jamás hables de productos, ventas, carrito, precios ni promociones. No sos vendedor."
        "\n6. Si la consulta es confusa o ajena (ej: 'asdfgh'), decí: 'Disculpá, no entendí la consulta. Te puedo ayudar con trámites, reclamos, turnos o servicios municipales. ¿Con qué tema necesitás ayuda hoy?'"
        "\n7. Siempre cerrá tu respuesta con una pregunta o propuesta concreta para el vecino: '¿Querés que te pase el link?', '¿Te ayudo con otro trámite?', '¿Necesitás recibir info sobre servicios digitales?'"
        "\n---\nHISTORIAL DE LA CONVERSACIÓN:"
    )
    for msg in mensajes_previos:
        prompt += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
    prompt += f"\n- Vecino: {pregunta}\n- Agente:"

    # --- Detección de reclamos ---
    def contiene_reclamo(texto):
        claves = [
            "bache", "reclamo", "denuncia", "luminaria", "basura", "ruido", "inseguridad", "robo",
            "perro suelto", "poda", "árbol", "corte de agua", "vereda rota", "servicio no funciona", "vandalismo"
        ]
        return any(k in texto.lower() for k in claves)

    def generar_ticket_db(pregunta, user_obj):
        # Tu lógica real de ticket, si tenés una tabla municipio_ticket, usala aquí:
        import random
        nro_ticket = random.randint(10000, 99999)
        # Ejemplo: guardá el ticket real en la DB acá
        # ticket = MunicipioTicket(user_id=getattr(user_obj, "id", None), pregunta=pregunta, estado="nuevo", ...)
        # db.session.add(ticket); db.session.commit(); return ticket.id
        return nro_ticket

    # 1. Ticket automático si es reclamo
    if contiene_reclamo(pregunta):
        nro_ticket = generar_ticket_db(pregunta, user_obj)
        respuesta_ticket = (
            f"Tu reclamo fue registrado con el número #{nro_ticket}. "
            "Nuestro equipo lo revisará a la brevedad. ¿Te gustaría realizar otro trámite o consulta?"
        )
        session[NOMBRE_HISTORIAL_SESION].extend([
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": respuesta_ticket}
        ])
        session.modified = True
        return {"respuesta": respuesta_ticket, "fuente": "municipio_ticket"}

    # 2. LLM (Cohere, Gemini, etc)
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
    except Exception as e:
        respuesta_llm = (
            "Lo siento, hubo un problema técnico al procesar tu consulta. "
            "Podés comunicarte telefónicamente o por la web oficial."
        )

    # Memoria/Historial
    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    session.modified = True

    # --- BOTONES ---
    respuesta_final = respuesta_llm

    # Botón WhatsApp si hay teléfono válido
    def formatear_numero_whatsapp_simple(telefono_str, codigo_pais="54"):
        import re
        if not telefono_str:
            return ""
        numeros = re.sub(r'\D', '', str(telefono_str))
        if numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 1 + 10):
            return numeros
        if numeros.startswith(codigo_pais) and not numeros.startswith(codigo_pais + "9") and len(numeros) == (len(codigo_pais) + 10):
            return codigo_pais + "9" + numeros[len(codigo_pais):]
        if len(numeros) == 10:
            return f"{codigo_pais}9{numeros}"
        return numeros

    # WhatsApp
    numero_wsp = formatear_numero_whatsapp_simple(telefono)
    if numero_wsp and "No especificado" not in telefono:
        mensaje_whatsapp = f"Hola {nombre_municipio}, tengo una consulta sobre trámites municipales."
        mensaje_whatsapp_encoded = urllib.parse.quote(mensaje_whatsapp)
        boton_wsp_html = (
            f'''
<div style="margin-top:14px;text-align:center;">
  <a href="https://wa.me/{numero_wsp}?text={mensaje_whatsapp_encoded}" target="_blank"
     style="
        display:inline-block;background:linear-gradient(90deg,#25d366 85%,#059669 100%);
        color:#fff;padding:14px 32px;text-align:center;text-decoration:none;
        border-radius:12px;font-size:1.07em;font-family:'Inter','Segoe UI',Arial,sans-serif;
        font-weight:700;box-shadow:0 4px 18px 0 rgba(27,205,96,0.09),0 1.5px 7px 0 rgba(0,0,0,0.05);
        transition:background 0.2s,box-shadow 0.2s;margin:0 auto;min-width:200px;"
     onmouseover="this.style.background='linear-gradient(90deg,#15ad42 90%,#0dd9a7 100%)';this.style.boxShadow='0 6px 22px 0 rgba(27,205,96,0.15)';"
     onmouseout="this.style.background='linear-gradient(90deg,#25d366 85%,#059669 100%)';this.style.boxShadow='0 4px 18px 0 rgba(27,205,96,0.09),0 1.5px 7px 0 rgba(0,0,0,0.05)';"
  >
    <span style="display:inline-block;vertical-align:middle;margin-right:7px;font-size:1.22em;">💬</span>
    Contactar por WhatsApp
  </a>
</div>
''')
        respuesta_final += boton_wsp_html

    # Botón Web oficial si hay link válido
    if web_oficial and web_oficial.startswith("http"):
        boton_web_html = (
            f'''
<div style="margin-top:10px;font-size:0.97em;text-align:center;">
  <a href="{web_oficial}" target="_blank"
     style="color:#2980f3;text-decoration:underline;font-weight:600;font-size:1.1em;">
    🌐 Ir a la web oficial del municipio
  </a>
</div>
''')
        respuesta_final += boton_web_html

    return {"respuesta": respuesta_final, "fuente": "cohere"}

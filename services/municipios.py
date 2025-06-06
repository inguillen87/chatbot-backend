import datetime
import json
import random
import logging
import re
from flask import session as flask_session
from models import Conversacion, MunicipioTicket, TicketComentario, db
from services.cohere_ai import get_cohere_response
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro  # <--- AGREGADO
from services.procesar_ticket_entidad import procesar_ticket_entidad

NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"
PALABRAS_CLAVE_HUMANO = [
    "representante", "humano", "persona", "agente", "encargado",
    "no quiero un bot", "atención real", "alguien de verdad", "quiero hablar con", "hablar con", "operador"
]

def render_botones_municipio(web_oficial, telefono_wsp, pregunta):
    estilo_btn = (
        "display:inline-block;background:#2980f3;color:#fff;padding:6px 14px;"
        "text-decoration:none;border-radius:5px;font-size:0.95em;font-weight:500;margin:0 2px;"
    )
    botones = ""
    if web_oficial:
        botones += f'<a href="{web_oficial}" target="_blank" style="{estilo_btn}">🌐 Web</a>'
    if telefono_wsp and len(str(telefono_wsp)) >= 9:
        from urllib.parse import quote
        msg = quote(f"Hola, tengo una consulta sobre: '{pregunta}'")
        botones += f'<a href="https://wa.me/{telefono_wsp}?text={msg}" target="_blank" style="{estilo_btn.replace("#2980f3","#25d366")}">💬 WhatsApp</a>'
    if botones:
        return f'<div style="margin-top:7px;text-align:center;">{botones}</div>'
    return ""

def detectar_palabra_humano(texto):
    texto = texto.lower()
    return any(palabra in texto for palabra in PALABRAS_CLAVE_HUMANO)

def guardar_conversacion(user_id, pregunta, respuesta, fuente, rubro):
    conv = Conversacion(
        user_id=user_id,
        pregunta=pregunta,
        respuesta=respuesta,
        fuente=fuente,
        rubro=rubro
    )
    db.session.add(conv)
    db.session.commit()

def guardar_ticket(pregunta, user_id, estado="nuevo"):
    nro_ticket = random.randint(10000, 99999)
    ticket = MunicipioTicket(
        pregunta=pregunta,
        user_id=user_id,
        estado=estado,
        nro_ticket=nro_ticket,
        fecha=datetime.datetime.utcnow()
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket

def guardar_comentario(ticket_id, user_id, comentario):
    comentario_obj = TicketComentario(
        ticket_id=ticket_id,
        user_id=user_id,
        comentario=comentario,
        fecha=datetime.datetime.utcnow()
    )
    db.session.add(comentario_obj)
    db.session.commit()

def buscar_ticket_activo(user_id):
    return MunicipioTicket.query.filter(
        MunicipioTicket.user_id == user_id,
        MunicipioTicket.estado.in_(["nuevo", "en curso"])
    ).order_by(MunicipioTicket.fecha.desc()).first()

def buscar_ticket_por_nro(nro_ticket, user_id=None):
    q = MunicipioTicket.query.filter_by(nro_ticket=int(nro_ticket))
    if user_id:
        q = q.filter_by(user_id=user_id)
    return q.first()

def get_attr(obj, attr, default=""):
    try:
        return getattr(obj, attr, default) if obj else default
    except Exception:
        return default
    
def get_datos_municipio(user_obj, rubro_obj):
    nombre_municipio = get_attr(user_obj, "nombre_empresa") or get_attr(rubro_obj, "nombre") or "el municipio"
    telefono = get_attr(user_obj, "telefono")
    telefono_wsp = ''.join(filter(str.isdigit, telefono)) if telefono else ""
    web_oficial = get_attr(user_obj, "link_web") or "https://www.argentina.gob.ar"
    direccion = get_attr(user_obj, "direccion") or "Consultar en la web"
    ciudad = get_attr(user_obj, "ciudad") or ""
    provincia = get_attr(user_obj, "provincia") or ""
    direccion_completa = f"{direccion}, {ciudad}, {provincia}".replace(" ,", "").strip(", ")
    horario = get_attr(user_obj, "horario") or "Consultar en la web oficial"
    horario_json_str = get_attr(user_obj, "horario_json") or "[]"
    horarios_para_prompt = horario
    try:
        if horario_json_str and isinstance(horario_json_str, str) and horario_json_str.startswith("["):
            horarios_data = json.loads(horario_json_str)
            partes = [
                f"{h.get('dia','')}: de {h.get('abre','--:--')} a {h.get('cierra','--:--')}" if not h.get('cerrado') else f"{h.get('dia','')}: Cerrado"
                for h in horarios_data if isinstance(h, dict) and h.get('dia')]
            if partes:
                horarios_para_prompt = ". ".join(partes) + "."
    except Exception:
        pass
    return nombre_municipio, telefono, telefono_wsp, web_oficial, direccion_completa, horarios_para_prompt

def es_reclamo_municipal(pregunta):
    palabras_reclamo = [
        "basura", "residuos", "luminaria", "luz", "foco", "bache", "calle", "agua",
        "riego", "árbol", "arbol", "semáforo", "semáforo", "pozo", "acumulación"
    ]
    return any(p in pregunta.lower() for p in palabras_reclamo)

def datos_faltantes_usuario(user_obj):
    faltantes = []
    if not get_attr(user_obj, "telefono") or not get_attr(user_obj, "telefono").strip():
        faltantes.append("teléfono")
    if not get_attr(user_obj, "email") or not get_attr(user_obj, "email").strip():
        faltantes.append(r"email")
    return faltantes

def datos_faltantes_para_ticket(user_obj):
    faltan = []
    if not get_attr(user_obj, "direccion") or not get_attr(user_obj, "direccion").strip():
        faltan.append("dirección")
    if not get_attr(user_obj, "telefono") or not get_attr(user_obj, "telefono").strip():
        faltan.append("teléfono")
    if not get_attr(user_obj, "email") or not get_attr(user_obj, "email").strip():
        faltan.append("email")
    return faltan


def responder_municipio(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    fuente = "desconocida"
    session = session_obj if session_obj is not None else flask_session
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]
    user_id = get_attr(user_obj, "id", None)
    nombre_municipio, telefono, telefono_wsp, web_oficial, direccion_completa, horarios_para_prompt = get_datos_municipio(user_obj, rubro_obj)

    # 1. Derivación a humano SIEMPRE va primero
    if detectar_palabra_humano(pregunta):
        ticket = buscar_ticket_activo(user_id)
        if not ticket:
            ticket = guardar_ticket(pregunta, user_id, estado="derivado")
        else:
            ticket.estado = "derivado"
            db.session.commit()
        guardar_comentario(ticket.id, user_id, pregunta)
        mensaje = (
            "Te estamos derivando a un representante municipal.<br>"
            "<strong>Vas a recibir la respuesta directamente en este chat cuando el agente te responda.</strong><br>"
            "Si no recibís respuesta en unos minutos, podés escribirnos por WhatsApp."
        )
        fuente = "derivar_humano"
        guardar_conversacion(user_id, pregunta, mensaje, fuente, "municipio")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": mensaje})
        session.modified = True
        return {"respuesta": mensaje + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": fuente}

    # 2. Lógica de tickets
    res_ticket = procesar_ticket_entidad(pregunta, user_obj)
    if res_ticket:
        guardar_conversacion(user_id, pregunta, res_ticket["respuesta"], res_ticket["fuente"], "municipio")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": res_ticket["respuesta"]})
        session.modified = True
        return {"respuesta": res_ticket["respuesta"], "fuente": res_ticket["fuente"]}

    # ... resto igual
    # 3. Datos faltantes, estado de ticket, Cohere, etc.

    # (todo igual que antes)


    # 2. Datos de contacto faltantes
    faltantes = datos_faltantes_para_ticket(user_obj)
    if faltantes:
        mensaje = "Para registrar tu reclamo necesito tu " + " y ".join(faltantes) + ". Por favor, completá esos datos y volvé a consultar."
        fuente = "falta_dato_contacto"
        guardar_conversacion(user_id, pregunta, mensaje, fuente, "municipio")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": mensaje})
        session.modified = True
        return {"respuesta": mensaje + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": fuente}

    # 3. Consulta estado de ticket
    ticket_match = re.search(r"(ticket|reclamo)[\s#]*([0-9]{4,7})", pregunta, re.IGNORECASE)
    if ticket_match:
        nro = ticket_match.group(2)
        ticket = buscar_ticket_por_nro(nro)
        fuente = "consulta_estado_ticket"
        if ticket:
            msg_estado = f"El ticket #{nro} tiene estado: '{ticket.estado}'."
            if ticket.comentarios.count() > 0:
                ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                msg_estado += f" Último comentario: {ult_com.comentario}"
            else:
                msg_estado += " Actualmente no hay comentarios adicionales."
            return {"respuesta": msg_estado, "fuente": fuente}
        else:
            return {"respuesta": f"No encontramos un ticket con el número {nro}. Revisá que esté bien escrito.", "fuente": fuente}

    # 4. Reclamo municipal típico
    if es_reclamo_municipal(pregunta):
        ticket = buscar_ticket_activo(user_id)
        if not ticket:
            ticket = guardar_ticket(pregunta, user_id, estado="nuevo")
        guardar_comentario(ticket.id, user_id, pregunta)
        respuesta = (
            f"Listo, tu reclamo fue registrado con el número {ticket.nro_ticket}. "
            "Vas a recibir novedades cuando el equipo municipal lo procese. "
            "¿Querés agregar algún comentario o detalle más?"
        )
        fuente = "registro_reclamo"
        guardar_conversacion(user_id, pregunta, respuesta, fuente, "municipio")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": respuesta})
        session.modified = True
        return {"respuesta": respuesta, "fuente": fuente}
    
    # === ACÁ VA EL BLOQUE NUEVO ===
    from services.webinfo import obtener_info_web  # Asegurate de tenerlo arriba

    info_web = obtener_info_web(user_id, web_oficial)
    prompt_extra = ""
    if info_web and not info_web.get("error"):
         prompt_extra = "\n\nInformación extra obtenida automáticamente del sitio web:"
    if info_web.get('emails'):
        mails = ', '.join(info_web['emails'])
        if mails:
            prompt_extra += f"\n- Emails encontrados: {mails}"
    if info_web.get('telefonos'):
        tels = ', '.join(info_web['telefonos'])
        if tels:
            prompt_extra += f"\n- Teléfonos encontrados: {tels}"
    if info_web.get('direcciones'):
        dirs = ', '.join(info_web['direcciones'])
        if dirs:
            prompt_extra += f"\n- Direcciones mencionadas: {dirs}"
    if info_web.get('noticias'):
        noticias = '; '.join(info_web['noticias'][:5])  # Solo las 5 más recientes o relevantes
        if noticias:
            prompt_extra += f"\n- Noticias destacadas: {noticias}"
    if info_web.get('scrap_fecha'):
        prompt_extra += f"\n(Datos extraídos automáticamente el {info_web['scrap_fecha'][:10]})"


    # 5. Consulta a Cohere
    prompt_municipio = (
    f"Chatboc, un agente de atención ciudadana para el municipio de {nombre_municipio}, ubicado en {direccion_completa}. "
    f"Tu función es resolver consultas, reclamos y trámites de vecinos de forma amable, clara y eficiente, como un agente municipal real. "
    "Tu objetivo es responder con información precisa y llevar al vecino a la acción concreta que necesita (registrar un reclamo, iniciar un trámite, consultar estado, etc.), en el menor número de intercambios posible (idealmente 2-3). "
    f"Ya tuviste {len(mensajes_previos)} intercambios en esta sesión, usá el historial de conversación para no repetir datos y mantener coherencia. "
    "Siempre que la consulta corresponda a un reclamo (luminaria, árbol caído, basura, semáforo, etc.), ofrece iniciar el trámite/ticket automáticamente y da el número de seguimiento si aplica. "
    "No te presentes como asistente virtual ni como bot ni digas que sos inteligencia artificial. Eres un agente municipal real. "
    "Si el vecino pregunta algo confuso, poco claro, irrelevante o fuera de competencia municipal, responde amable pero firme y reorienta la consulta a temas de servicios municipales o trámites. "
    "Nunca inventes información: si no disponés del dato, orienta al vecino a consultar la web oficial, WhatsApp, o llamar al teléfono de contacto."
    "\n\nINFORMACIÓN DEL MUNICIPIO PARA RESPONDER:"
    f"\n- Teléfono: {telefono}"
    f"\n- Dirección: {direccion_completa}"
    f"\n- Horario de atención: {horarios_para_prompt}"
    f"\n- Web oficial: {web_oficial}"
    "\n---"
)
    prompt_llm = prompt_municipio
    for msg in mensajes_previos:
        prompt_llm += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
    prompt_llm += f"\n- Vecino: {pregunta}\n- Agente:"

    try:
        rubro_id = getattr(rubro_obj, "id", 5)
        respuesta_llm = get_cohere_response(
            message=pregunta,
            chat_history=[{"role": m.get("role", "user"), "message": m.get("content", "")} for m in mensajes_previos],
            preamble=prompt_llm,
            rubro_id=rubro_id,
            user_context={
                "nombre_empresa": nombre_municipio,
                "telefono": telefono,
                "link_web": web_oficial,
                "direccion": direccion_completa,
                "horario": horarios_para_prompt
            }
        )

        respuesta_llm = reemplazar_placeholders(respuesta_llm, user_obj)
        if not respuesta_llm or "no puedo responder" in respuesta_llm.lower():
            raise Exception("La IA no devolvió respuesta válida.")
        fuente = "cohere"  # <- si llegó hasta acá, la respuesta vino de Cohere
    except Exception as e:
        logging.exception("Error en LLM municipio: %s", e)
        ticket = buscar_ticket_activo(user_id)
        if not ticket:
            ticket = guardar_ticket(pregunta, user_id, estado="derivado")
        else:
            ticket.estado = "derivado"
            db.session.commit()
        guardar_comentario(ticket.id, user_id, pregunta)
        mensaje = (
            "Te estamos derivando a un representante municipal.<br>"
            "<strong>Vas a recibir la respuesta directamente en este chat cuando el agente te responda.</strong><br>"
            "Si no recibís respuesta en unos minutos, podés escribirnos por WhatsApp."
        )
        fuente = "derivar_humano_auto"
        guardar_conversacion(user_id, pregunta, mensaje, fuente, "municipio")
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": mensaje})
        session.modified = True
        return {"respuesta": mensaje + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": fuente}

    # 6. Fallback sugerencias
    if not respuesta_llm:
        sugs = sugerencias_por_rubro("municipios")
        respuesta_llm = "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s)
        fuente = "sugerencia_sistema"

    guardar_conversacion(user_id, pregunta, respuesta_llm, fuente, "municipio")
    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_llm}
    ])
    session.modified = True

    return {
        "respuesta": respuesta_llm,
        "fuente": fuente
    }

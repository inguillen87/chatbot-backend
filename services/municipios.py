import datetime
import json
import random
from flask import session as flask_session
from models import Conversacion, MunicipioTicket, TicketComentario, db
from services.cohere_ai import get_cohere_response
from services.logic import reemplazar_placeholders

NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"
PALABRAS_CLAVE_HUMANO = [
    "representante", "humano", "persona", "agente", "encargado",
    "no quiero un bot", "atención real", "alguien de verdad", "quiero hablar con", "hablar con", "operador"
]

def render_botones_municipio(web_oficial, telefono_wsp, pregunta):
    botones = ""
    if web_oficial:
        botones += f'''
<div style="margin-top: 14px; text-align: center;">
  <a href="{web_oficial}" target="_blank" style="
        display: inline-block;
        background: #2980f3;
        color: #fff;
        padding: 13px 30px;
        text-decoration: none;
        border-radius: 10px;
        font-size: 1.08em;
        font-weight: 700;
        margin: 0 6px;">
    🌐 Web Oficial
  </a>
</div>
'''
    if telefono_wsp and len(str(telefono_wsp)) >= 9:
        from urllib.parse import quote
        msg = quote(f"Hola, tengo una consulta sobre: '{pregunta}'")
        botones += f'''
<div style="margin-top: 10px; text-align: center;">
  <a href="https://wa.me/{telefono_wsp}?text={msg}" target="_blank" style="
        display: inline-block;
        background: #25d366;
        color: #fff;
        padding: 13px 30px;
        text-decoration: none;
        border-radius: 10px;
        font-size: 1.08em;
        font-weight: 700;
        margin: 0 6px;">
    💬 WhatsApp
  </a>
</div>
'''
    return botones

def detectar_palabra_humano(texto):
    texto = texto.lower()
    return any(palabra in texto for palabra in PALABRAS_CLAVE_HUMANO)

def contiene_reclamo(texto):
    claves = [
        "bache", "reclamo", "denuncia", "luminaria", "basura", "ruido", "inseguridad", "robo", "perro suelto",
        "poda", "árbol", "corte de agua", "vereda rota", "servicio no funciona", "corte de luz"
    ]
    return any(k in texto.lower() for k in claves)

def responder_municipio(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]

    # 1. DATOS MUNICIPIO
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

    # 2. DERIVAR A HUMANO
    if detectar_palabra_humano(pregunta):
        # Busca ticket abierto o lo crea
        ticket = MunicipioTicket.query.filter(
            MunicipioTicket.user_id == user_obj.id,
            MunicipioTicket.estado.in_(["nuevo", "en curso"])
        ).order_by(MunicipioTicket.fecha.desc()).first()
        if not ticket:
            nro_ticket = random.randint(10000, 99999)
            ticket = MunicipioTicket(
                pregunta=pregunta,
                user_id=user_obj.id if user_obj else None,
                estado="derivado",
                nro_ticket=nro_ticket,
                fecha=datetime.datetime.utcnow()
            )
            db.session.add(ticket)
            db.session.commit()
        else:
            ticket.estado = "derivado"
            db.session.commit()
        # Guarda comentario para el admin (visible en panel)
        comentario = TicketComentario(
            ticket_id=ticket.id,
            user_id=user_obj.id,
            comentario=pregunta,
            fecha=datetime.datetime.utcnow()
        )
        db.session.add(comentario)
        db.session.commit()
        mensaje = (
            "Te estamos derivando a un representante municipal.<br>"
            "<strong>Vas a recibir la respuesta directamente en este chat cuando el agente te responda.</strong><br>"
            "Si no recibís respuesta en unos minutos, podés escribirnos por WhatsApp."
        )
        conv = Conversacion(
            user_id=user_obj.id if user_obj else None,
            pregunta=pregunta,
            respuesta=mensaje,
            fuente="derivar_humano",
            rubro="municipio"
        )
        db.session.add(conv)
        db.session.commit()
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": mensaje})
        session.modified = True
        return {"respuesta": mensaje + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": "derivar_humano"}

    # 3. PEDIR DATOS DE CONTACTO SI FALTAN
    datos_faltantes = []
    if not user_obj.telefono or user_obj.telefono.strip() == "":
        datos_faltantes.append("teléfono")
    if not user_obj.email or user_obj.email.strip() == "":
        datos_faltantes.append("email")
    if datos_faltantes:
        mensaje = "Antes de continuar, por favor brindá tu " + " y ".join(datos_faltantes) + " para que podamos ayudarte mejor."
        conv = Conversacion(
            user_id=user_obj.id if user_obj else None,
            pregunta=pregunta,
            respuesta=mensaje,
            fuente="falta_dato_contacto",
            rubro="municipio"
        )
        db.session.add(conv)
        db.session.commit()
        session[NOMBRE_HISTORIAL_SESION].append({"role": "assistant", "content": mensaje})
        session.modified = True
        return {"respuesta": mensaje + render_botones_municipio(web_oficial, telefono_wsp, pregunta), "fuente": "falta_dato_contacto"}

    # 4. DETECTAR Y GUARDAR RECLAMO / TICKET
    if contiene_reclamo(pregunta):
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

    # 5. CONSULTAR ESTADO DE TICKET
    import re
    ticket_match = re.search(r"(ticket|reclamo)[\s#]*([0-9]{4,7})", pregunta, re.IGNORECASE)
    if ticket_match:
        nro = ticket_match.group(2)
        ticket = MunicipioTicket.query.filter_by(nro_ticket=int(nro)).first()
        if ticket:
            msg_estado = f"El ticket #{nro} tiene estado: '{ticket.estado}'."
            if ticket.comentarios.count() > 0:
                ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                msg_estado += f" Último comentario: {ult_com.comentario}"
            else:
                msg_estado += " Actualmente no hay comentarios adicionales."
            return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
        else:
            return {"respuesta": f"No encontramos un ticket con el número {nro}. Revisá que esté bien escrito.", "fuente": "consulta_estado_ticket"}

    # 6. PROMPT FINAL (ROBUSTO)
    prompt = (
        f"Sos Chatboc, un agente de atención ciudadana de {nombre_municipio}, ubicado en {direccion_completa}. "
        "Respondés de manera amable, profesional, clara y directa como un agente municipal real. "
        "No te presentás como IA ni como bot. No vendés ni promocionás nada. "
        "Si recibís un reclamo, ofrecés generar ticket. Para trámites, das instrucciones claras. "
        f"\n\nDATOS MUNICIPIO: {nombre_municipio} - Teléfono: {telefono} - Dirección: {direccion_completa} - Horario: {horarios_para_prompt} - Web: {web_oficial}."
        "\nSiempre preguntá al final si necesita ayuda con algo más."
        "\n---\nHistorial de la conversación (extracto):"
    )
    for msg in mensajes_previos:
        prompt += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
    prompt += f"\n- Vecino: {pregunta}\n- Agente:"

    # 7. LLAMAR A IA (COHERE/OPENAI/GEMINI)
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
# services/municipios.py

from models import MunicipioTicket

def buscar_estado_ticket(nro_ticket, user_id=None):
    # Buscar por número de ticket, opcionalmente filtrar por usuario
    q = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket)
    if user_id:
        q = q.filter_by(user_id=user_id)
    ticket = q.first()
    if not ticket:
        return None
    return {
        "estado": ticket.estado,
        "pregunta": ticket.pregunta,
        "fecha": ticket.fecha,
        "nro_ticket": ticket.nro_ticket,
    }

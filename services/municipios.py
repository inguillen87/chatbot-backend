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

def render_botones_accion(acciones):
    """
    Renderiza botones de acción dinámicos para el chat.
    Ejemplo de acciones: [{"texto": "Crear Ticket", "payload": "crear_ticket"}, ...]
    """
    estilo_btn = (
        "display:inline-block;background:#5a67d8;color:#fff;padding:8px 16px;"
        "text-decoration:none;border-radius:5px;font-size:0.95em;font-weight:500;margin:4px;"
        "border:none;cursor:pointer;"
    )
    botones_html = ""
    for accion in acciones:
        # Usaremos un postback a través de JavaScript para mantener la conversación en el mismo chat
        payload = accion.get("payload", accion.get("texto"))
        botones_html += f'<button style="{estilo_btn}" onclick="enviarMensajeAsistente(\'{payload}\')">{accion.get("texto")}</button>'

    if botones_html:
        return f'<div style="margin-top:10px;text-align:center;">{botones_html}</div>'
    return ""

def solicitar_datos_faltantes(user_obj):
    """
    Verifica si faltan datos del usuario y genera un mensaje para solicitarlos.
    """
    faltantes = []
    # Ampliamos para pedir también el nombre
    if not get_attr(user_obj, "nombre_completo"):
        faltantes.append("nombre completo")
    if not get_attr(user_obj, "email"):
        faltantes.append("email")
    if not get_attr(user_obj, "telefono"):
        faltantes.append("teléfono de contacto")

    if faltantes:
        mensaje = (
            f"Para poder ayudarte mejor y registrar tus consultas, necesito algunos datos. "
            f"Por favor, ¿podrías indicarme tu {' y '.join(faltantes)}?<br>"
            "Puedes escribirlos directamente aquí."
        )
        return {"respuesta": mensaje, "estado_sesion": "pidiendo_datos"}
    return None

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
    
    # Inicializar sesión si no existe
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    session.setdefault(NOMBRE_ESTADO_SESION, "inicio")
    session.setdefault(NOMBRE_TICKET_ID_SESION, None)

    mensajes_previos = session[NOMBRE_HISTORIAL_SESION][-8:]
    user_id = get_attr(user_obj, "id", None)
    estado_actual = session[NOMBRE_ESTADO_SESION]

    nombre_municipio, telefono, _, web_oficial, _, _ = get_datos_municipio(user_obj, rubro_obj)

    # --- INICIO DEL FLUJO CONVERSACIONAL ---

    # 1. Derivación a humano (prioridad máxima)
    if detectar_palabra_humano(pregunta):
        # (La lógica de derivación a humano que ya tienes es correcta, la mantenemos)
        # ...
        return {"respuesta": mensaje_derivacion, "fuente": "derivar_humano"}

    # 2. Gestión de estado: ¿Estamos esperando datos del usuario?
    if estado_actual == "pidiendo_datos":
        # Aquí iría la lógica para procesar los datos que el usuario envía.
        # Por simplicidad, asumiremos que los actualiza en su perfil y vuelve a consultar.
        # Una mejora futura sería parsear la respuesta para extraer nombre, email, etc.
        session[NOMBRE_ESTADO_SESION] = "inicio" # Reseteamos el estado
        session.modified = True
        # Forzamos una nueva verificación de datos después de la posible actualización
        faltan_datos_msg = solicitar_datos_faltantes(user_obj)
        if faltan_datos_msg:
             return {"respuesta": "Gracias. Aún necesito más información: " + faltan_datos_msg["respuesta"], "fuente": "falta_dato_contacto"}
        else:
             return {"respuesta": "¡Perfecto! Ya tengo tus datos. Ahora sí, ¿en qué te puedo ayudar?", "fuente": "datos_completados"}


    # 3. Gestión de estado: ¿Estamos trabajando sobre un ticket abierto?
    ticket_activo_id = session.get(NOMBRE_TICKET_ID_SESION)
    if ticket_activo_id:
        ticket = MunicipioTicket.query.get(ticket_activo_id)
        if ticket and ticket.estado in ["nuevo", "en curso"]:
            # Si el usuario dice que no o algo similar, cerramos el ticket
            if any(palabra in pregunta.lower() for palabra in ["no", "nada mas", "gracias", "listo"]):
                ticket.estado = "resuelto"
                db.session.commit()
                session[NOMBRE_TICKET_ID_SESION] = None
                session[NOMBRE_ESTADO_SESION] = "inicio"
                session.modified = True
                respuesta = (
                    "¡Entendido! Doy tu reclamo por finalizado. Si necesitas algo más, no dudes en consultar.<br>"
                    "¿Te resultó útil mi ayuda?"
                )
                botones = render_botones_accion([
                    {"texto": "👍 Sí, mucho", "payload": "valoracion_positiva"},
                    {"texto": "👎 Podría mejorar", "payload": "valoracion_negativa"}
                ])
                return {"respuesta": respuesta + botones, "fuente": "cierre_ticket"}
            else:
                # Cualquier otra cosa es un comentario adicional al ticket
                guardar_comentario(ticket.id, user_id, pregunta)
                respuesta = "He añadido tu comentario al ticket. ¿Hay algo más que quieras agregar?"
                botones = render_botones_accion([
                    {"texto": "No, eso es todo", "payload": "no"}
                ])
                return {"respuesta": respuesta + botones, "fuente": "agrega_comentario_ticket"}

    # 4. Verificación de datos del usuario ANTES de cualquier acción
    faltan_datos_msg = solicitar_datos_faltantes(user_obj)
    if faltan_datos_msg:
        session[NOMBRE_ESTADO_SESION] = faltan_datos_msg["estado_sesion"]
        session.modified = True
        return {"respuesta": faltan_datos_msg["respuesta"], "fuente": "falta_dato_contacto"}

    # 5. Lógica de tickets (búsqueda y creación)
    res_ticket = procesar_ticket_entidad(pregunta, user_obj) # Asumo que esta función crea o busca tickets
    if res_ticket:
        # Si se crea un ticket nuevo, lo guardamos en sesión
        if res_ticket.get("ticket_id"):
             session[NOMBRE_TICKET_ID_SESION] = res_ticket["ticket_id"]
             session[NOMBRE_ESTADO_SESION] = "gestionando_ticket"
             session.modified = True
        
        # Añadir botones de acción si procede
        if res_ticket.get("accion") == "ticket_creado":
            res_ticket["respuesta"] += "<br>¿Quieres agregar más detalles, como fotos o una descripción más larga?"
            botones = render_botones_accion([
                {"texto": "Sí, agregar detalles", "payload": "si"},
                {"texto": "No, está bien así", "payload": "no"}
            ])
            res_ticket["respuesta"] += botones

        guardar_conversacion(user_id, pregunta, res_ticket["respuesta"], res_ticket["fuente"], "municipio")
        return res_ticket

    # 6. Flujo General con IA (Cohere)
    # (Tu lógica de consulta a Cohere y fallback se mantiene aquí, es el último recurso)
    # ...
    # Simplemente asegúrate de guardar el historial en sesión como ya lo haces.
    # ...
    
    # Al final, si ninguna otra lógica se activó, podrías ofrecer botones genéricos.
    respuesta_final = "Hola, soy tu asistente municipal. ¿Cómo puedo ayudarte hoy?" # Mensaje por defecto
    fuente_final = "saludo_inicial"
    
    # Ejemplo de lógica para obtener respuesta de Cohere
    try:
        # ... tu código para llamar a get_cohere_response ...
        respuesta_llm = "..." # La respuesta que obtienes de Cohere
        if respuesta_llm:
            respuesta_final = respuesta_llm
            fuente_final = "cohere"
    except Exception as e:
        # ... tu manejo de errores ...
        respuesta_final = "En este momento no puedo procesar tu consulta, por favor intenta más tarde."
        fuente_final = "error_llm"

    botones = render_botones_accion([
        {"texto": "Hacer un reclamo", "payload": "Quiero hacer un reclamo de alumbrado"},
        {"texto": "Consultar estado de ticket", "payload": "Cuál es el estado de mi ticket 12345"},
        {"texto": "Horarios de atención", "payload": "Me decís los horarios de atención?"}
    ])
    
    # Guardar en conversación y sesión
    guardar_conversacion(user_id, pregunta, respuesta_final, fuente_final, "municipio")
    session[NOMBRE_HISTORIAL_SESION].extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_final}
    ])
    session.modified = True

    return {"respuesta": respuesta_final + botones, "fuente": fuente_final}
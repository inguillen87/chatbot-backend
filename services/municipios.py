import datetime
import json
import random
import logging
import re
from flask import session as flask_session

# Importaciones de la base de datos y modelos
from extensions import db
from models import User, Conversacion, MunicipioTicket, TicketComentario

# Importaciones de servicios (asegúrate que las rutas sean correctas)
from services.cohere_ai import get_cohere_response
from services.utils_placeholders import reemplazar_placeholders

# --- CONFIGURACIÓN Y CONSTANTES ---
logger = logging.getLogger(__name__)

# Constantes para la máquina de estados en la sesión
NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"
NOMBRE_ESTADO_SESION = "estado_chat_municipio"
NOMBRE_TICKET_ID_SESION = "ticket_id_activo_municipio"
NOMBRE_CONTEXTO_RECLAMO_SESION = "contexto_reclamo_pendiente"


# --- FUNCIONES AUXILIARES ---

def get_attr(obj, attr, default=""):
    """Obtiene un atributo de un objeto de forma segura."""
    return getattr(obj, attr, default) if obj else default

def render_botones_accion(acciones):
    """Renderiza botones de acción dinámicos con estilos mejorados."""
    if not acciones:
        return ""
    
    estilo_btn_base = "display:inline-block;padding:10px 18px;text-decoration:none;border-radius:8px;font-size:0.98em;font-weight:600;margin:5px 4px;border:1px solid transparent;cursor:pointer;transition:all 0.2s ease;"
    estilos = {
        "primario": f"background:#4A55A2;color:#FFF;{estilo_btn_base}",
        "secundario": f"background:transparent;color:#4A55A2;border-color:#4A55A2;{estilo_btn_base}"
    }
    
    botones_html = ""
    for accion in acciones:
        payload = accion.get("payload", accion.get("texto"))
        tipo_estilo = accion.get("tipo", "primario")
        payload_escapado = payload.replace("'", "\\'")
        botones_html += f'<button style="{estilos.get(tipo_estilo, estilos["primario"])}" onclick="enviarMensajeAsistente(\'{payload_escapado}\')">{accion.get("texto")}</button>'

    if botones_html:
        return f'<div style="margin-top:15px;padding-top:10px;border-top:1px solid #e2e8f0;text-align:center;">{botones_html}</div>'
    return ""

def guardar_conversacion_db(user_id, pregunta, respuesta, fuente, rubro="municipio"):
    """Guarda la interacción en la tabla Conversacion de la base de datos."""
    try:
        if user_id:
            conv = Conversacion(user_id=user_id, pregunta=pregunta, respuesta=respuesta, fuente=fuente, rubro=rubro, timestamp=datetime.datetime.now())
            db.session.add(conv)
            db.session.commit()
    except Exception as e:
        logger.error(f"Error al guardar conversación en DB para user {user_id}: {e}")
        db.session.rollback()

def solicitar_datos_faltantes(user_obj):
    """Verifica si faltan datos del usuario en su perfil y genera un mensaje para solicitarlos."""
    faltantes = []
    if not get_attr(user_obj, "name"):
        faltantes.append("nombre y apellido")
    if not get_attr(user_obj, "email"):
        faltantes.append("email")
    if not get_attr(user_obj, "telefono"):
        faltantes.append("teléfono")

    if faltantes:
        mensaje = (
            f"¡Hola! Para poder asistirte de la mejor manera, necesito que completemos tu perfil. "
            f"Por favor, ¿podrías indicarme tu **{'**, **'.join(faltantes)}**?<br><br>"
            "Puedes escribirlos aquí mismo. Tus datos son confidenciales y se usarán solo para gestiones municipales."
        )
        return {"respuesta": mensaje, "estado_sesion": "pidiendo_datos_iniciales"}
    return None

def detectar_intencion_reclamo(pregunta):
    """Detecta la intención y el posible tema de un reclamo para iniciar el flujo conversacional."""
    temas = {
        "alumbrado público": ["luminaria", "luz quemada", "foco", "poste sin luz", "calle oscura"],
        "estado de calles": ["bache", "pozo", "calle rota", "asfalto roto", "hundimiento", "vereda rota"],
        "recolección de residuos": ["basura", "residuo", "contenedor lleno", "no pasa el basurero", "mugre"],
        "arbolado público": ["árbol caído", "arbol caido", "rama peligrosa", "poda de arbol", "raíces levantando vereda"],
        "agua y cloacas": ["pérdida de agua", "caño roto", "cloaca tapada", "agua servida", "falta de agua"],
        "seguridad ciudadana": ["inseguridad", "vandalismo", "actividad sospechosa", "alarma comunitaria"],
        "control de plagas": ["plaga", "roedores", "ratas", "desinfección", "fumigación"]
    }
    texto = pregunta.lower()
    for tema, keywords in temas.items():
        if any(kw in texto for kw in keywords):
            return tema
    return None

def crear_y_guardar_ticket(user_id, pregunta_inicial, contexto, detalles_adicionales):
    """Crea, guarda en DB y devuelve un objeto MunicipioTicket (VERSIÓN CORREGIDA)."""
    try:
        # Generamos un Nro de Ticket como INTEGER para que coincida con el modelo de la DB
        nro_ticket_int = random.randint(10000, 99999)
        # Podríamos verificar si ya existe para asegurar unicidad, aunque es poco probable.
        
        descripcion_completa = f"Consulta Original: {pregunta_inicial}\n\nDetalles Adicionales: {detalles_adicionales}"
        
        ticket = MunicipioTicket(
            user_id=user_id,
            pregunta=descripcion_completa, # Usamos el campo 'pregunta' que es de tipo Texto
            nro_ticket=nro_ticket_int,      # Guardamos el número entero
            estado="nuevo",
            fecha=datetime.datetime.now()
        )
        db.session.add(ticket)
        db.session.commit()
        logger.info(f"Ticket {ticket.nro_ticket} creado exitosamente para user {user_id}.")
        return ticket
    except Exception as e:
        logger.error(f"Error CRÍTICO al guardar ticket en DB para user {user_id}: {e}")
        db.session.rollback()
        return None
# --- FUNCIÓN PRINCIPAL: AGENTE MUNICIPAL INTELIGENTE ---

def responder_municipio(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    """
    Gestiona la conversación con el ciudadano a través de una máquina de estados,
    manejando solicitudes de datos, creación y seguimiento de tickets, y consultas generales con IA.
    """
    session = session_obj if session_obj is not None else flask_session
    user_id = get_attr(user_obj, "id")

    # Inicialización y persistencia de la sesión
    session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    session.setdefault(NOMBRE_ESTADO_SESION, "inicio")
    session.setdefault(NOMBRE_TICKET_ID_SESION, None)
    session.setdefault(NOMBRE_CONTEXTO_RECLAMO_SESION, None)

    estado_actual = session[NOMBRE_ESTADO_SESION]
    respuesta_final = ""
    fuente = "desconocida"
    botones = ""
    
    # --- MÁQUINA DE ESTADOS CONVERSACIONAL ---

    if estado_actual == "pidiendo_datos_iniciales":
        # En un futuro, aquí se procesarían los datos enviados por el usuario para actualizar su perfil.
        # Por ahora, se asume que lo hace y se resetea el flujo.
        session[NOMBRE_ESTADO_SESION] = "inicio"
        respuesta_final = "¡Muchas gracias por completar tus datos! Ahora sí, ¿en qué puedo ayudarte?"
        fuente = "datos_completados"
        
    elif estado_actual == "gestionando_ticket_activo":
        ticket_id = session.get(NOMBRE_TICKET_ID_SESION)
        ticket = db.session.get(MunicipioTicket, ticket_id) if ticket_id else None
        
        if not ticket:
            session[NOMBRE_ESTADO_SESION] = "inicio"
            session[NOMBRE_TICKET_ID_SESION] = None
            return responder_municipio(pregunta, user_obj, rubro_obj, session_obj, **kwargs)

        if any(w in pregunta.lower() for w in ["no", "nada mas", "listo", "gracias", "cerrar", "finalizar"]):
            ticket.estado = "resuelto_ciudadano"
            db.session.commit()
            respuesta_final = f"Entendido. Doy por finalizado el seguimiento del ticket **#{ticket.nro_ticket}**. Si necesitas algo más en el futuro, no dudes en consultarme."
            fuente = "cierre_ticket_usuario"
            session[NOMBRE_ESTADO_SESION] = "inicio"
            session[NOMBRE_TICKET_ID_SESION] = None
        else:
            comentario = TicketComentario(ticket_id=ticket.id, user_id=user_id, comentario=pregunta, fecha=datetime.datetime.now(), tipo_ticket='municipio')
            db.session.add(comentario)
            db.session.commit()
            respuesta_final = f"He añadido tu comentario al ticket **#{ticket.nro_ticket}**. El equipo a cargo lo revisará. ¿Deseas agregar algo más o podemos finalizar el seguimiento por ahora?"
            fuente = "agrega_comentario_ticket"
        
        botones = render_botones_accion([
            {"texto": "Finalizar seguimiento", "payload": "no, eso es todo", "tipo": "primario"},
            {"texto": "Hacer otra consulta", "payload": "quiero hacer otra consulta", "tipo": "secundario"}
        ])

    elif estado_actual == "confirmando_reclamo":
        contexto_reclamo = session.get(NOMBRE_CONTEXTO_RECLAMO_SESION)
        if any(w in pregunta.lower() for w in ["si", "sí", "correcto", "afirmativo", "iniciar reclamo"]):
            respuesta_final = f"Perfecto. Para poder registrar tu reclamo sobre **{contexto_reclamo}**, por favor, indícame la **dirección exacta** (calle y altura, esquina, o puntos de referencia claros) y cualquier otro detalle que consideres importante."
            session[NOMBRE_ESTADO_SESION] = "pidiendo_datos_reclamo"
            fuente = "solicita_detalles_reclamo"
        else:
            respuesta_final = "Entendido. No hay problema. Entonces, ¿en qué otra cosa puedo ayudarte?"
            session[NOMBRE_ESTADO_SESION] = "inicio"
            session[NOMBRE_CONTEXTO_RECLAMO_SESION] = None
            fuente = "cancela_creacion_reclamo"

    elif estado_actual == "pidiendo_datos_reclamo":
        contexto = session.get(NOMBRE_CONTEXTO_RECLAMO_SESION, "Varios")
        pregunta_original = next((msg['content'] for msg in reversed(session[NOMBRE_HISTORIAL_SESION]) if msg['role'] == 'user'), pregunta)
        ticket = crear_y_guardar_ticket(user_id, pregunta_original, contexto, pregunta)
        
        if ticket:
            respuesta_final = (
                f"¡Gracias! Tu reclamo ha sido generado con éxito.\n\n"
                f"**N° de Ticket: {ticket.nro_ticket}**\n\n"
                "Ya derivamos tu solicitud al área correspondiente. Puedes usar ese número para consultas futuras. "
                "Ahora estoy atento a este ticket. Si quieres añadir más información (como fotos o videos), puedes indicármelo. ¿Hay algo más que desees agregar por ahora?"
            )
            session[NOMBRE_TICKET_ID_SESION] = ticket.id
            session[NOMBRE_ESTADO_SESION] = "gestionando_ticket_activo"
            fuente = "ticket_creado_exitosamente"
        else:
            respuesta_final = "Lo lamento, hubo un problema técnico y no pude generar tu ticket. Por favor, intenta nuevamente en unos minutos o contacta al municipio por otros medios."
            session[NOMBRE_ESTADO_SESION] = "inicio"
            fuente = "error_creando_ticket"
        
        session[NOMBRE_CONTEXTO_RECLAMO_SESION] = None

    else:  # ESTADO "inicio"
        datos_faltantes = solicitar_datos_faltantes(user_obj)
        if datos_faltantes:
            session[NOMBRE_ESTADO_SESION] = datos_faltantes["estado_sesion"]
            respuesta_final = datos_faltantes["respuesta"]
            fuente = "solicitud_datos_perfil"
        else:
            tema_reclamo = detectar_intencion_reclamo(pregunta)
            if tema_reclamo:
                respuesta_final = f"Detecté que tu consulta podría ser un reclamo sobre **{tema_reclamo}**. ¿Es correcto y deseas que inicie la gestión de un ticket?"
                session[NOMBRE_ESTADO_SESION] = "confirmando_reclamo"
                session[NOMBRE_CONTEXTO_RECLAMO_SESION] = tema_reclamo
                fuente = "confirmacion_intencion_reclamo"
                botones = render_botones_accion([
                    {"texto": "Sí, iniciar reclamo", "payload": "si", "tipo": "primario"},
                    {"texto": "No, es otra cosa", "payload": "no", "tipo": "secundario"}
                ])
            else:
                # --- AQUÍ VA LA CONSULTA A LA INTELIGENCIA ARTIFICIAL (COHERE) ---
                fuente = "cohere_ia"
                try:
                    # Lógica para construir un prompt de alta calidad para Cohere
                    nombre_municipio = get_attr(user_obj, "nombre_empresa", "este municipio")
                    prompt_sistema = (
                        f"Sos un agente de atención ciudadana experto para {nombre_municipio}. Tu identidad es la de un empleado municipal real, amable, eficiente y muy resolutivo. No te presentes como IA o chatbot. "
                        "Tu objetivo es ayudar a los vecinos con sus consultas sobre trámites, servicios, horarios e información general de forma clara y concisa. Si no sabes algo, derívalo a la web oficial o al teléfono de contacto. "
                        "No debes gestionar reclamos directamente, esa es una función de otra parte del sistema. Si el usuario insiste en un reclamo, indícale amablemente: 'Para iniciar un reclamo, por favor sé más específico, por ejemplo: `quiero reclamar por un bache en la calle X`.' "
                        "Siempre finaliza tu respuesta con una pregunta que invite a la acción, como '¿Puedo ayudarte con algo más?'."
                    )
                    
                    historial_para_api = [{"role": "USER" if msg.get("role") == "user" else "CHATBOT", "message": msg.get("content","")} for msg in session.get(NOMBRE_HISTORIAL_SESION, [])[-8:]]

                    respuesta_final = get_cohere_response(
                        message=pregunta,
                        chat_history=historial_para_api,
                        preamble=prompt_sistema,
                        rubro_id=get_attr(rubro_obj, "id"),
                        user_context={"nombre_empresa": nombre_municipio}
                    )
                    if not respuesta_final or len(respuesta_final) < 10:
                        raise ValueError("Respuesta de IA vacía o muy corta")

                except Exception as e:
                    logger.error(f"Error en la llamada a Cohere: {e}")
                    respuesta_final = "En este momento, no puedo procesar tu consulta. Por favor, intenta reformularla o contacta al municipio por otros medios."
                    fuente = "error_ia"
                
                botones = render_botones_accion([
                    {"texto": "Hacer un reclamo", "payload": "Quiero hacer un reclamo por un pozo"},
                    {"texto": "Horarios de atención", "payload": "Cuales son los horarios de atencion"}
                ])

    # --- FINALIZACIÓN Y GUARDADO ---
    if respuesta_final:
        respuesta_con_placeholders = reemplazar_placeholders(respuesta_final, user_obj)
        respuesta_html_final = respuesta_con_placeholders + botones
        
        session[NOMBRE_HISTORIAL_SESION].extend([
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": respuesta_con_placeholders}
        ])
        session.modified = True
        
        guardar_conversacion_db(user_id, pregunta, respuesta_con_placeholders, fuente)
        
        return {"respuesta": respuesta_html_final, "fuente": fuente}

    return {"respuesta": "Lo siento, ocurrió un error inesperado. Por favor, intenta de nuevo.", "fuente": "error_flujo_desconocido"}
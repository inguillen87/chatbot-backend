# services/municipios.py

import logging
import re
import random
import json
from datetime import datetime
from flask import session as flask_session

from models import Conversacion, MunicipioTicket, TicketComentario, db, User
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)

# --- Constantes ---
CONTEXTO_MUNICIPIO_SESION = "contexto_municipio"
NOMBRE_HISTORIAL_SESION = "historial_chat_municipio"

# --- Funciones Auxiliares ---
def _render_botones(acciones: list) -> dict:
    """Devuelve una estructura de botones para que el frontend la renderice."""
    return {"tipo": "botones", "acciones": acciones}

def _clasificar_intencion_con_llm(pregunta: str) -> str:
    """Usa un LLM para una clasificación de intención rápida y robusta."""
    prompt = f"""
    Clasifica la siguiente consulta de un ciudadano en una de estas categorías: 'iniciar_reclamo', 'consultar_tramite', 'consultar_impuestos', 'saludo', 'pregunta_general'.
    Responde ÚNICAMENTE con la categoría.

    Consulta: "{pregunta}"
    Categoría:
    """
    try:
        respuesta = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en clasificar intenciones de ciudadanos.").strip().lower()
        # Validar que la respuesta sea una de las categorías esperadas
        categorias_validas = ['iniciar_reclamo', 'consultar_tramite', 'consultar_impuestos', 'saludo', 'pregunta_general']
        if respuesta in categorias_validas:
            return respuesta
        return "pregunta_general"
    except Exception:
        logger.error("[MUNICIPIO] Error en clasificación de intención con LLM.")
        return "pregunta_general"

# --- ARQUITECTURA DE HANDLERS PARA MUNICIPIO ---

class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

class ReclamoHandler(BaseMunicipioHandler):
    """Gestiona el flujo completo de creación de un reclamo en varios pasos."""
    def handle(self, pregunta: str) -> dict | None:
        intencion = self.context.get('intencion')
        contexto_memoria = self.context.get('contexto_municipio', {})
        estado_reclamo = contexto_memoria.get('estado_reclamo')

        # --- PASO 1: Inicia el flujo si la intención es 'iniciar_reclamo' ---
        if intencion == 'iniciar_reclamo' and not estado_reclamo:
            contexto_memoria['estado_reclamo'] = 'esperando_categoria'
            contexto_memoria['pregunta_original'] = pregunta
            respuesta = {
                "respuesta": "Entendido. Para poder dirigir tu reclamo al área correcta, por favor, seleccioná una de las siguientes categorías:",
                "botones": [
                    {"texto": "Alumbrado Público", "payload": "reclamo de alumbrado"},
                    {"texto": "Calles y Veredas", "payload": "reclamo de calles"},
                    {"texto": "Limpieza y Residuos", "payload": "reclamo de limpieza"},
                    {"texto": "Otro", "payload": "otro tipo de reclamo"}
                ]
            }
            return respuesta

        # --- PASO 2: Recibe la categoría y pide la dirección ---
        elif estado_reclamo == 'esperando_categoria':
            contexto_memoria['estado_reclamo'] = 'esperando_direccion'
            contexto_memoria['categoria_reclamo'] = pregunta # Guardamos la categoría elegida
            return {"respuesta": f"Perfecto. Categoría seleccionada: **{pregunta}**.\n\nAhora, por favor, indicame la dirección exacta del problema (calle y altura, o esquina de referencia)."}

        # --- PASO 3: Recibe la dirección, crea el ticket y finaliza ---
        elif estado_reclamo == 'esperando_direccion':
            # Recuperamos los datos guardados en la "mochila"
            categoria = contexto_memoria.get('categoria_reclamo', 'Sin categoría')
            pregunta_original = contexto_memoria.get('pregunta_original', 'No especificada')
            direccion = pregunta

            asunto = f"Reclamo de '{categoria}' en '{direccion[:50]}...'"
            comentario_inicial = f"Consulta original del vecino: '{pregunta_original}'\n\nDirección proporcionada: '{direccion}'"

            ticket_creado = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={
                    "pregunta": pregunta_original,
                    "user_id": self.context.get('user_id'),
                    "asunto": asunto,
                    "categoria": categoria,
                    "comentario": comentario_inicial,
                }
            )

            # Limpiamos la mochila después de usarla
            self.context['contexto_municipio'].clear()

            if ticket_creado:
                respuesta_texto = (f"¡Muchas gracias! Tu reclamo ha sido generado con éxito.\n\n"
                                   f"**N° de Ticket: M-{ticket_creado.nro_ticket}**\n\n"
                                   "Ya derivamos tu solicitud al área correspondiente. Podés usar este número para futuras consultas.")
                return {"respuesta": respuesta_texto}
            else:
                return {"respuesta": "Lo lamento, hubo un problema técnico y no pude generar tu ticket. Por favor, intenta nuevamente."}
        
        return None

class GeneralHandler(BaseMunicipioHandler):
    """Maneja saludos y preguntas generales con el LLM."""
    def handle(self, pregunta: str) -> dict | None:
        intencion = self.context.get('intencion')
        if intencion in ['saludo', 'pregunta_general', 'consultar_tramite', 'consultar_impuestos']:
            # Lógica para construir un prompt de alta calidad para Cohere
            nombre_municipio = getattr(self.context.get('user_obj'), "nombre_empresa", "este municipio")
            prompt_sistema = (
                f"Sos un agente de atención ciudadana experto para {nombre_municipio}. Tu identidad es la de un empleado municipal real, amable y eficiente. "
                "Tu objetivo es ayudar a los vecinos con sus consultas sobre trámites, impuestos, servicios, horarios e información general. "
                "Si no sabes algo, derívalo a la web oficial o al teléfono de contacto."
            )
            
            respuesta_llm = get_cohere_response(message=pregunta, chat_history=[], preamble=prompt_sistema)
            
            botones = {
                "respuesta": respuesta_llm,
                "botones": [
                    {"texto": "Hacer un Reclamo", "payload": "Quiero hacer un reclamo"},
                    {"texto": "Ver otros trámites", "payload": "¿Qué trámites puedo hacer?"}
                ]
            }
            return botones
        return None

# --- FUNCIÓN ORQUESTADORA PRINCIPAL PARA MUNICIPIO ---

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO_SESION, {})

    # Clasificamos la intención del usuario primero, a menos que ya estemos en un flujo
    if not contexto_municipio.get('estado_reclamo'):
        intencion = _clasificar_intencion_con_llm(pregunta)
    else:
        # Si ya estamos en un flujo, la intención es continuar ese flujo
        intencion = "continuar_flujo"

    context = {
        "contexto_municipio": contexto_municipio, # La "mochila"
        "user_obj": user_obj,
        "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "intencion": intencion
    }

    # Cadena de handlers para municipio, muy simple y ordenada
    handler_chain = [ReclamoHandler, GeneralHandler]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break
    
    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta. ¿Podrías reformularla?", "botones": []}

    # Guardado de la conversación en la DB
    if context['user_id']:
        try:
            db.session.add(Conversacion(user_id=context['user_id'], pregunta=pregunta, respuesta=json.dumps(respuesta_final), fuente=intencion, rubro='municipio'))
            db.session.commit()
        except Exception as e:
            logging.error(f"[MUNICIPIO] Error guardando conversación en DB: {e}")
            db.session.rollback()

    # Devolvemos la "mochila" actualizada junto con la respuesta
    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO_SESION: contexto_municipio}
    }
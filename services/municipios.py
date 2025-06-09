# services/municipios.py
import logging
import json
from models import Conversacion, MunicipioTicket, db
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

def _clasificar_intencion_con_llm(pregunta: str) -> str:
    """Usa un LLM para una clasificación de intención robusta."""
    prompt = f"""
    Clasifica la siguiente consulta de un ciudadano en una de estas categorías: 'iniciar_reclamo', 'consultar_tramite', 'hablar_con_agente', 'pregunta_general'.
    Responde ÚNICAMENTE con la categoría.

    Ejemplos:
    - Consulta: "se cayó un árbol en mi vereda" -> iniciar_reclamo
    - Consulta: "necesito ayuda de una persona" -> hablar_con_agente
    - Consulta: "cómo saco el carnet de conducir?" -> consultar_tramite

    Consulta a clasificar: "{pregunta}"
    Categoría:
    """
    try:
        respuesta = get_cohere_response(message=prompt, preamble="Eres un experto en clasificar intenciones de ciudadanos.").strip().lower().replace("_", "")
        categorias_validas = ['iniciar_reclamo', 'consultar_tramite', 'hablar_con_agente', 'pregunta_general']
        for cat in categorias_validas:
            if cat.replace("_", "") in respuesta:
                return cat
        return "pregunta_general"
    except Exception as e:
        logger.error(f"[MUNICIPIO] Error en clasificación de intención: {e}")
        return "pregunta_general"

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

class HumanEscalationHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={ "user_id": self.context.get('user_id'), "asunto": "Solicitud de Agente Humano", "categoria": "Escalado Urgente", "detalles": pregunta }
            )
            if ticket:
                return {"respuesta": f"Entendido. He generado un ticket de atención prioritaria (#{ticket.nro_ticket}) para que un agente se ponga en contacto contigo a la brevedad."}
            else:
                return {"respuesta": "Comprendo que necesitas asistencia. En este momento nuestros agentes no están disponibles, pero hemos registrado tu solicitud."}
        return None

class ReclamoHandler(BaseMunicipioHandler):
    """Gestiona el flujo de creación de un reclamo de forma conversacional y con memoria."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria['estado_conversacion'] = 'esperando_categoria_reclamo'
            memoria['pregunta_original'] = pregunta
            return {"respuesta": "Entendido, vamos a iniciar tu reclamo. Para poder dirigirlo al área correcta, por favor, seleccioná una de las siguientes categorías:", 
                    "botones": [{"texto": "Alumbrado Público"}, {"texto": "Calles y Veredas"}, {"texto": "Limpieza y Residuos"}, {"texto": "Arbolado"}]}
        
        elif estado == 'esperando_categoria_reclamo':
            memoria['estado_conversacion'] = 'esperando_direccion_reclamo'
            memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto, categoría: **{pregunta}**. Ahora, para ser más precisos, por favor, indicame la dirección exacta del problema (calle, altura, y entre qué calles si es posible)."}

        elif estado == 'esperando_direccion_reclamo':
            categoria = memoria.get('categoria_reclamo', 'General')
            detalles = f"Consulta original del vecino: '{memoria.get('pregunta_original')}'\n\nDirección proporcionada: '{pregunta}'"
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio", 
                ticket_data={"user_id": self.context.get('user_id'), "asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": detalles}
            )
            
            if ticket:
                memoria['estado_conversacion'] = 'ticket_creado'
                memoria['ticket_id_activo'] = ticket.id
                respuesta = (f"¡Excelente! Tu reclamo fue generado con el número de ticket **M-{ticket.nro_ticket}**.\n\n"
                             "¿Querés agregar algún otro detalle, foto o información adicional? Podés escribirlo ahora.")
                return {"respuesta": respuesta, "botones": [{"texto": "No, es todo"}, {"texto": "Finalizar"}]}
            else:
                memoria.clear()
                return {"respuesta": "Lo lamento, hubo un problema técnico y no pude generar tu ticket."}

        elif estado == 'ticket_creado':
            if any(w in pregunta.lower() for w in ["no", "finalizar", "listo", "eso es todo"]):
                memoria.clear()
                return {"respuesta": "Perfecto, quedó todo registrado. El equipo municipal se encargará. ¿Hay algo más en lo que pueda ayudarte?"}
            else:
                ticket_id = memoria.get('ticket_id_activo')
                servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="municipio", comentario_data={"comentario": pregunta})
                return {"respuesta": "He añadido tu comentario al ticket. ¿Algo más para agregar?"}
                
        return None

class GeneralHandler(BaseMunicipioHandler):
    """Maneja las preguntas generales con el LLM, actuando como un experto municipal."""
    def handle(self, pregunta: str) -> dict | None:
        intencion = self.context.get('intencion')
        if intencion == 'pregunta_general' or intencion == 'consultar_tramite':
            nombre_municipio = "este municipio" # Puedes obtenerlo del user_obj si lo tienes
            prompt_sistema = (
                f"Sos un agente de atención al ciudadano experto para {nombre_municipio}. Tu identidad es la de un empleado municipal real, amable y eficiente. "
                "Tu objetivo es ayudar a los vecinos con consultas sobre trámites, servicios, horarios e información general de forma clara y concisa."
            )
            respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt_sistema)
            return {"respuesta": respuesta_llm, "botones": [{"texto": "Hacer un Reclamo", "payload": "Quiero iniciar un reclamo"}]}
        return None

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    context = { "contexto_municipio": contexto_municipio, "user_obj": user_obj, "user_id": getattr(user_obj, "id", None), "intencion": None }

    if not contexto_municipio.get('estado_conversacion'):
        context['intencion'] = _clasificar_intencion_con_llm(pregunta)
    
    handler_chain = [ReclamoHandler, HumanEscalationHandler, GeneralHandler]
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final: break
            
    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta. ¿Podrías reformularla?"}
    
    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
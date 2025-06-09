# services/municipios.py

import logging
import json
from models import Conversacion, MunicipioTicket, db
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

def _clasificar_intencion_con_llm(pregunta: str) -> str:
    # ... (código de la función de clasificación) ...
    # Por simplicidad, usamos keywords para asegurar que funcione rápido
    if any(w in pregunta.lower() for w in ["reclamo", "roto", "problema", "bache", "luz", "queja"]):
        return 'iniciar_reclamo'
    if any(w in pregunta.lower() for w in ["hablar con un humano", "agente", "persona", "ayuda"]):
        return 'hablar_con_agente'
    return 'pregunta_general'

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
            return {"respuesta": f"Entendido. He generado un ticket de atención prioritaria (#{ticket.nro_ticket}) para que un agente se ponga en contacto contigo a la brevedad."}
        return None

class ReclamoHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado_reclamo = memoria.get('estado_reclamo')
        
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado_reclamo:
            memoria['estado_reclamo'] = 'esperando_categoria'
            return {"respuesta": "Entendido. Para dirigir tu reclamo al área correcta, por favor, seleccioná una de las siguientes categorías:", "botones": [{"texto": "Alumbrado"}, {"texto": "Calles"}, {"texto": "Limpieza"}]}
        
        elif estado_reclamo == 'esperando_categoria':
            memoria['estado_reclamo'] = 'esperando_direccion'
            memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto: **{pregunta}**. Ahora, indicame la dirección exacta del problema."}

        elif estado_reclamo == 'esperando_direccion':
            categoria = memoria.get('categoria_reclamo', 'General')
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={ "user_id": self.context.get('user_id'), "asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": f"Dirección: {pregunta}" })
            memoria.clear()
            if ticket:
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con éxito con el ticket **M-{ticket.nro_ticket}**. Un equipo lo revisará pronto."}
            else:
                return {"respuesta": "Lo lamento, hubo un problema técnico al generar tu ticket."}
        return None

class GeneralFAQHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'pregunta_general':
             prompt_sistema = "Sos un agente de atención ciudadana experto..."
             respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt_sistema)
             return {"respuesta": respuesta_llm, "botones": [{"texto": "Hacer un Reclamo", "payload": "Quiero hacer un reclamo"}]}
        return None

class IntentClassifierHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if memoria.get('estado_reclamo'):
            self.context['intencion'] = 'continuar_flujo'
            return None 
        self.context['intencion'] = _clasificar_intencion_con_llm(pregunta)
        return None

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    
    context = { "contexto_municipio": contexto_municipio, "user_obj": user_obj, "rubro_obj": rubro_obj, "user_id": getattr(user_obj, "id", None), "intencion": None }
    handler_chain = [IntentClassifierHandler, HumanEscalationHandler, ReclamoHandler, GeneralFAQHandler]
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final: break
    
    if not respuesta_final:
        respuesta_final = {"respuesta": "No entendí tu consulta. ¿Podrías reformularla?"}

    # ... (código de guardado de conversación) ...

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
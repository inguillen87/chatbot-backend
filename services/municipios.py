# services/municipios.py
import logging, json
from models import Conversacion, MunicipioTicket, db
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

def _detectar_intencion_inicial(pregunta: str) -> str:
    texto = pregunta.lower()
    if any(w in texto for w in ["reclamo", "roto", "problema", "bache", "luz", "queja", "basura", "árbol"]):
        return 'iniciar_reclamo'
    if any(w in texto for w in ["hablar con", "agente", "persona", "humano", "ayuda"]):
        return 'hablar_con_agente'
    if any(w in texto for w in ["pagar", "impuesto", "tasas", "deuda"]):
        return 'consultar_impuestos'
    if any(w in texto for w in ["licencia", "carnet", "trámite", "turno"]):
        return 'consultar_tramite'
    return 'pregunta_general'

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

class ReclamoHandler(BaseMunicipioHandler):
    """Gestiona el flujo de creación de un reclamo en varios pasos."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria['estado_conversacion'] = 'esperando_categoria_reclamo'
            memoria['pregunta_original'] = pregunta
            return {"respuesta": "Entendido. Para dirigir tu reclamo al área correcta, por favor, seleccioná una de las siguientes categorías:", "botones": [{"texto": "Alumbrado"}, {"texto": "Calles y Veredas"}, {"texto": "Limpieza"}]}
        
        elif estado == 'esperando_categoria_reclamo':
            memoria['estado_conversacion'] = 'esperando_direccion_reclamo'
            memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto: **{pregunta}**. Ahora, indicame la dirección exacta del problema."}

        elif estado == 'esperando_direccion_reclamo':
            categoria = memoria.get('categoria_reclamo', 'General')
            detalles = f"Consulta original: {memoria.get('pregunta_original')}\n\nDirección proporcionada: {pregunta}"
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": detalles})
            
            memoria.clear() # Limpiamos la memoria al finalizar el flujo
            
            if ticket:
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. El área correspondiente se encargará a la brevedad."}
            else:
                return {"respuesta": "Lo lamento, hubo un problema técnico y no pude generar tu ticket."}
        return None

class HumanEscalationHandler(BaseMunicipioHandler):
    """Maneja el pedido explícito de hablar con un humano."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": "Solicitud de Agente Humano", "categoria": "Escalado Urgente", "detalles": pregunta})
            if ticket:
                return {"respuesta": f"Entendido. He generado un ticket de atención prioritaria (#{ticket.nro_ticket}) para que un agente se ponga en contacto contigo."}
            else:
                return {"respuesta": "Comprendo que necesitas asistencia, en este momento nuestros agentes no están disponibles. Por favor intenta más tarde."}
        return None

class GeneralHandler(BaseMunicipioHandler):
    """Maneja el resto de las consultas genéricas."""
    def handle(self, pregunta: str) -> dict | None:
        # Este es el fallback final, atrapa todo lo que no sea un flujo específico
        # Aquí iría la lógica para responder con FAQs o con un LLM genérico
        return {"respuesta": f"Recibí tu consulta sobre '{pregunta}'. Estoy buscando la mejor información para darte."}


def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    
    # El contexto que se pasa a todos los handlers
    context = { "contexto_municipio": contexto_municipio, "user_obj": user_obj, "user_id": getattr(user_obj, "id", None) }

    # Clasificamos la intención solo si no estamos ya en medio de un flujo
    if not contexto_municipio.get('estado_conversacion'):
        context['intencion'] = _detectar_intencion_inicial(pregunta)
    
    handler_chain = [HumanEscalationHandler, ReclamoHandler, GeneralHandler]
    
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
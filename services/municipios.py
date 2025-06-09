import logging
import json
from models import Conversacion, MunicipioTicket, User, db
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)

# --- Constantes para la "Mochila" ---
CONTEXTO_MUNICIPIO = "contexto_municipio"

# --- Funciones Auxiliares ---
def _clasificar_intencion_con_llm(pregunta: str) -> str:
    """Usa un LLM para una clasificación de intención rápida y robusta."""
    prompt = f"""
    Clasifica la siguiente consulta de un ciudadano en una de estas categorías: 'iniciar_reclamo', 'consultar_estado_ticket', 'hablar_con_agente', 'pregunta_general'.
    Responde ÚNICAMENTE con la categoría.

    Consulta: "{pregunta}"
    Categoría:
    """
    try:
        # Hacemos que la respuesta por defecto sea 'pregunta_general'
        respuesta_default = "pregunta_general"
        respuesta_llm = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en clasificar intenciones de ciudadanos.").strip().lower().replace("_", "")
        
        # Lista de intenciones válidas para evitar respuestas inesperadas del LLM
        categorias_validas = ['iniciarreclamo', 'consultarestadoticket', 'hablarconagente', 'preguntageneral']
        
        if "reclamo" in respuesta_llm: return 'iniciar_reclamo'
        if "ticket" in respuesta_llm: return 'consultar_estado_ticket'
        if "agente" in respuesta_llm or "humano" in respuesta_llm: return 'hablar_con_agente'
        
        return respuesta_default
    except Exception as e:
        logger.error(f"[MUNICIPIO] Error en clasificación de intención con LLM: {e}")
        return "pregunta_general"

# --- ARQUITECTURA DE HANDLERS PARA MUNICIPIO ---

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

class HumanEscalationHandler(BaseMunicipioHandler):
    """Si el usuario pide hablar con una persona, crea un ticket de alta prioridad."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            asunto = "Solicitud de Agente Humano"
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={ 
                    "user_id": self.context.get('user_id'), 
                    "asunto": asunto, 
                    "categoria": "Escalado Urgente", 
                    "detalles": f"El vecino solicitó hablar con un agente. Consulta original: '{pregunta}'"
                }
            )
            return {"respuesta": f"Entendido. He generado un ticket de atención prioritaria (#{ticket.nro_ticket}) para que un agente se ponga en contacto contigo a la brevedad."}
        return None

class ReclamoHandler(BaseMunicipioHandler):
    """Gestiona el flujo completo de creación de un reclamo en varios pasos usando la 'mochila'."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado_reclamo = memoria.get('estado_reclamo')
        
        # Inicia el flujo si la intención es 'iniciar_reclamo'
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado_reclamo:
            memoria['estado_reclamo'] = 'esperando_categoria'
            memoria['pregunta_original'] = pregunta
            return {"respuesta": "Entendido. Para poder dirigir tu reclamo al área correcta, por favor, seleccioná una de las siguientes categorías:", 
                    "botones": [{"texto": "Alumbrado"}, {"texto": "Calles y Veredas"}, {"texto": "Limpieza"}]}
        
        elif estado_reclamo == 'esperando_categoria':
            memoria['estado_reclamo'] = 'esperando_direccion'
            memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto: **{pregunta}**. Ahora, por favor, indicame la dirección exacta del problema (calle y altura, o esquina de referencia)."}

        elif estado_reclamo == 'esperando_direccion':
            categoria = memoria.get('categoria_reclamo', 'General')
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={
                "user_id": self.context.get('user_id'), 
                "asunto": f"Reclamo de {categoria}", 
                "categoria": categoria, 
                "detalles": f"Original: {memoria.get('pregunta_original')}\nDirección: {pregunta}"
            })
            self.context['contexto_municipio'].clear() # Limpiamos la mochila
            if ticket:
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. Un equipo lo revisará pronto."}
            else:
                return {"respuesta": "Lo lamento, hubo un problema técnico y no pude generar tu ticket."}
        
        return None

class GeneralFAQHandler(BaseMunicipioHandler):
    """Maneja cualquier otra pregunta general usando el LLM."""
    def handle(self, pregunta: str) -> dict | None:
        # Este handler atrapa todo lo que no sea un flujo específico
        nombre_municipio = getattr(self.context.get('user_obj'), "nombre_empresa", "este municipio")
        prompt_sistema = f"Sos un agente de atención ciudadana experto para {nombre_municipio}. Tu identidad es la de un empleado municipal real, amable y eficiente. Tu objetivo es ayudar a los vecinos con sus consultas sobre trámites, impuestos, servicios, horarios, etc."
        
        respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt_sistema)
        
        return {"respuesta": respuesta_llm, "botones": [{"texto": "Hacer un Reclamo", "payload": "Quiero hacer un reclamo"}]}

class IntentClassifierHandler(BaseMunicipioHandler):
    """El primer handler de todos. Su único trabajo es clasificar la intención y añadirla al contexto."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        # Si ya estamos en un flujo (ej: pidiendo la dirección), no volvemos a clasificar.
        if memoria.get('estado_reclamo'):
            self.context['intencion'] = 'continuar_flujo'
            return None 

        # Usamos la IA para entender la intención del usuario
        self.context['intencion'] = _clasificar_intencion_con_llm(pregunta)
        logger.info(f"[MUNICIPIO] Intención clasificada como: {self.context['intencion']}")
        return None # Este handler NUNCA responde, solo enriquece el contexto para los demás.

# --- FUNCIÓN ORQUESTADORA PRINCIPAL PARA MUNICIPIO ---

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    
    context = {
        "contexto_municipio": contexto_municipio, # La "mochila" de memoria manual
        "user_obj": user_obj,
        "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "intencion": None # Se poblará por el IntentClassifierHandler
    }

    # El orden es CRUCIAL: primero clasificamos, luego los especialistas, y al final el general.
    handler_chain = [IntentClassifierHandler, HumanEscalationHandler, ReclamoHandler, GeneralFAQHandler]
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final: break
            
    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta. ¿Podrías reformularla?"}

    # Lógica de guardado de conversación (sin cambios)
    
    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
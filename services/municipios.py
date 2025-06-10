import logging
import re
from models import MunicipioTicket, TicketComentario, db # Usando tus modelos
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

def _clasificar_intencion_con_llm(pregunta: str) -> str:
    prompt = f"""
    Clasifica la consulta de un ciudadano en una de estas categorías: 'iniciar_reclamo', 'consultar_tramite', 'consultar_impuestos', 'consultar_estado_ticket', 'hablar_con_agente', 'saludo', 'pregunta_general'.
    Responde ÚNICAMENTE con la categoría.

    Ejemplos:
    - Consulta: "se cayó un árbol en mi vereda" -> iniciar_reclamo
    - Consulta: "necesito ayuda de una persona" -> hablar_con_agente
    - Consulta: "cómo saco el carnet de conducir?" -> consultar_tramite
    - Consulta: "dónde pago mis tasas?" -> consultar_impuestos
    - Consulta: "quería saber cómo va mi ticket 12345" -> consultar_estado_ticket

    Consulta a clasificar: "{pregunta}"
    Categoría:
    """
    try:
        respuesta_llm = get_cohere_response(message=prompt, preamble="Eres un experto en clasificar intenciones de ciudadanos.").strip().lower().replace(" ", "_")
        categorias_validas = ['iniciar_reclamo', 'consultar_tramite', 'consultar_impuestos', 'consultar_estado_ticket', 'hablar_con_agente', 'saludo', 'pregunta_general']
        for cat in categorias_validas:
            if cat in respuesta_llm:
                return cat.replace(" ", "_")
        return "pregunta_general"
    except Exception as e:
        logger.error(f"[MUNICIPIO] Error en clasificación LLM: {e}")
        return "pregunta_general"

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

class IntentClassifierHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if not memoria.get('estado_conversacion'):
            self.context['intencion'] = _clasificar_intencion_con_llm(pregunta)
        else:
            self.context['intencion'] = 'continuar_flujo'
        logger.info(f"[MUNICIPIO] Intención clasificada: {self.context.get('intencion')}")
        return None

class HumanEscalationHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": "Solicitud de Agente Humano", "categoria": "Escalado Urgente", "detalles": pregunta, "user_id": self.context.get("user_id")})
            if ticket: return {"respuesta": f"Entendido. Un agente revisará tu consulta. Tu número de seguimiento es #{ticket.nro_ticket}."}
        return None

class TicketStatusHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_estado_ticket':
            match = re.search(r'\d{5,}', pregunta)
            if not match: return {"respuesta": "Por favor, decime el número de ticket que querés consultar."}
            ticket = MunicipioTicket.query.filter_by(nro_ticket=int(match.group(0))).first()
            if ticket:
                respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' se encuentra en estado: **{ticket.estado}**."
                
                # --- INICIO DE LA CORRECCIÓN ---
                # Usamos 'es_admin' como está definido en tu models.py para TicketComentario
                ultimo_comentario_agente = TicketComentario.query.filter_by(
                    municipio_ticket_id=ticket.id, 
                    es_admin=True  # <-- CORREGIDO: Usando 'es_admin'
                ).order_by(TicketComentario.fecha.desc()).first()
                # --- FIN DE LA CORRECCIÓN ---

                if ultimo_comentario_agente:
                    respuesta += f"\n\nÚltima actualización de nuestro equipo: *\"{ultimo_comentario_agente.comentario}\"*"
                return {"respuesta": respuesta}
            else: return {"respuesta": f"No pude encontrar ningún ticket con el número {match.group(0)}."}
        return None

class ReclamoHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria['estado_conversacion'] = 'esperando_categoria_reclamo'; memoria['pregunta_original'] = pregunta
            return {"respuesta": "Entendido, vamos a iniciar tu reclamo. Para dirigirlo al área correcta, por favor, seleccioná una de las siguientes categorías:", "botones": [{"texto": "Alumbrado Público"}, {"texto": "Calles y Veredas"}, {"texto": "Limpieza y Residuos"}]}
        elif estado == 'esperando_categoria_reclamo':
            memoria['estado_conversacion'] = 'esperando_direccion_reclamo'; memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto, categoría: **{pregunta}**. Ahora, indicame la dirección exacta del problema."}
        elif estado == 'esperando_direccion_reclamo':
            categoria = memoria.get('categoria_reclamo', 'General')
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": f"Original: {memoria.get('pregunta_original')}\nDirección: {pregunta}", "user_id": self.context.get("user_id")})
            memoria.clear()
            if ticket: return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**."}
        return None

class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_impuestos':
            respuesta = "Podés consultar tu estado de deuda y pagar tus tasas municipales online a través de nuestro portal de autogestión, o presencialmente de Lunes a Viernes de 8 a 14hs."
            return {"respuesta": respuesta, "botones": [{"texto": "Ir al Portal de Pagos"}, {"texto": "Hacer un reclamo"}]}
        return None
        
class TramitesHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        if self.context.get('intencion') == 'consultar_tramite' and not estado:
            memoria['estado_conversacion'] = 'esperando_tipo_tramite'
            return {"respuesta": "¡Claro! Te puedo ayudar con información sobre varios trámites. ¿Cuál te interesa?", "botones": [{"texto": "Licencia de Conducir"}, {"texto": "Habilitación Comercial"}]}
        elif estado == 'esperando_tipo_tramite':
            memoria.clear()
            if "licencia" in pregunta.lower():
                return {"respuesta": "Para la Licencia de Conducir necesitás: DNI con domicilio actualizado, no tener multas pendientes y realizar el curso de seguridad vial. ¿Necesitas saber dónde hacer el curso?"}
            else: # Asumimos Habilitación Comercial
                return {"respuesta": "Para Habilitaciones Comerciales, los requisitos varían según el rubro. Es mejor que te acerques a la oficina de comercio para un asesoramiento personalizado."}
        return None

class GeneralHandler(Basemunderlyingipipoder):
    def handle(self, pregunta: str) -> dict | None:
        prompt = "Sos un agente de atención ciudadana experto..." # Tu prompt completo
        respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt)
        return {"respuesta": respuesta_llm}

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    context = {"contexto_municipio": contexto_municipio, "user_obj": user_obj, "user_id": getattr(user_obj, "id", None), "intencion": None}

    handler_chain = [IntentClassifierHandler, HumanEscalationHandler, TicketStatusHandler, ReclamoHandler, ImpuestosHandler, TramitesHandler, GeneralHandler]
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final: break
    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta."}

    return {"respuesta": respuesta_final.get('respuesta'), "botones": respuesta_final.get('botones', []), "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}}
# services/municipios.py
import logging
import json
from models import Conversacion, MunicipioTicket, db
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"


def _clasificar_intencion_con_llm(pregunta: str) -> str:
    """Usa un LLM para una clasificación de intención robusta y real."""
    prompt = f"""
    Tu tarea es clasificar la siguiente consulta de un ciudadano en una de las siguientes categorías: 'iniciar_reclamo', 'consultar_tramite', 'consultar_impuestos', 'hablar_con_agente', 'saludo', 'pregunta_general'.
    Responde ÚNICAMENTE con la categoría. Sé preciso.

    Ejemplos:
    - Consulta: "se cayó un árbol en mi vereda y rompió un cable" -> iniciar_reclamo
    - Consulta: "necesito ayuda de una persona por favor" -> hablar_con_agente
    - Consulta: "cómo hago para sacar el carnet de conducir?" -> consultar_tramite
    - Consulta: "dónde puedo ver mi deuda de tasas municipales?" -> consultar_impuestos
    - Consulta: "buenos dias" -> saludo
    - Consulta: "qué temperatura hace?" -> pregunta_general

    Ahora, clasifica esta consulta:
    Consulta: "{pregunta}"
    Categoría:
    """
    try:
        # Hacemos la llamada a la IA
        respuesta_llm = get_cohere_response(
            message=prompt, 
            preamble="Eres un experto en clasificar intenciones de ciudadanos para un municipio. Tu respuesta debe ser una única categoría de la lista provista."
        ).strip().lower().replace(" ", "_")

        # Verificamos que la respuesta sea una de las válidas para evitar errores
        categorias_validas = ['iniciar_reclamo', 'consultar_tramite', 'consultar_impuestos', 'hablar_con_agente', 'saludo', 'pregunta_general']
        for cat in categorias_validas:
            if cat in respuesta_llm:
                logger.info(f"[MUNICIPIO] Intención clasificada por LLM como: {cat}")
                return cat
        
        # Si la IA devuelve algo inesperado, tenemos un fallback seguro
        logger.warning(f"[MUNICIPIO] LLM devolvió una categoría no válida: '{respuesta_llm}'. Usando fallback.")
        return "pregunta_general"

    except Exception as e:
        logger.error(f"[MUNICIPIO] Error crítico en clasificación de intención con LLM: {e}")
        return "pregunta_general"

    except Exception as e:
        logger.error(f"[MUNICIPIO] Error en clasificación LLM: {e}")
        # Fallback a keywords si la IA falla
        texto = pregunta.lower()
        if any(w in texto for w in ["reclamo", "roto", "bache", "luz"]): return 'iniciar_reclamo'
        if any(w in texto for w in ["impuesto", "pagar", "deuda"]): return 'consultar_impuestos'
        if any(w in texto for w in ["trámite", "carnet", "licencia"]): return 'consultar_tramite'
        return 'pregunta_general'

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError


class IntentClassifierHandler(BaseMunicipioHandler):
    """El primer handler. Usa IA para clasificar la intención y la añade al contexto."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if memoria.get('estado_conversacion'):
            self.context['intencion'] = 'continuar_flujo'
        else:
            # LLAMAMOS A NUESTRA NUEVA FUNCIÓN CON IA
            self.context['intencion'] = _clasificar_intencion_con_llm(pregunta)
        
        return None # Este handler nunca responde, solo prepara el terreno

class HumanEscalationHandler(BaseMunicipioHandler):
    """Maneja el pedido de hablar con una persona."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": "Solicitud de Agente Humano", "categoria": "Escalado Urgente", "detalles": pregunta, "user_id": self.context.get("user_id")})
            if ticket: return {"respuesta": f"Entendido. Un agente revisará tu consulta. Tu número de seguimiento es #{ticket.nro_ticket}."}
        return None

class ReclamoHandler(BaseMunicipioHandler):
    """Gestiona el flujo de creación de un reclamo."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria['estado_conversacion'] = 'esperando_categoria_reclamo'; memoria['pregunta_original'] = pregunta
            return {"respuesta": "Entendido, vamos a iniciar tu reclamo. Para dirigirlo al área correcta, por favor, seleccioná una categoría:", "botones": [{"texto": "Alumbrado"}, {"texto": "Calles"}, {"texto": "Limpieza"}]}
        elif estado == 'esperando_categoria_reclamo':
            memoria['estado_conversacion'] = 'esperando_direccion_reclamo'; memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto, reclamo de **{pregunta}**. Ahora, indicame la dirección exacta."}
        elif estado == 'esperando_direccion_reclamo':
            categoria = memoria.get('categoria_reclamo', 'General')
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": f"Original: {memoria.get('pregunta_original')}\nDirección: {pregunta}", "user_id": self.context.get("user_id")})
            memoria.clear()
            if ticket: return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**."}
        return None

class ImpuestosHandler(BaseMunicipioHandler):
    """Responde preguntas frecuentes sobre impuestos y pagos."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_impuestos':
            respuesta = ("Podés consultar tu estado de deuda y pagar tus tasas municipales de forma online a través de nuestro portal de autogestión. También podés hacerlo presencialmente en el edificio municipal de Lunes a Viernes de 8 a 14hs.")
            return {"respuesta": respuesta, "botones": [{"texto": "Ir al Portal de Pagos", "payload": "link_portal_pagos"}, {"texto": "Hacer un reclamo"}]}
        return None
        
class TramitesHandler(BaseMunicipioHandler):
    """Guía al usuario a través de los trámites disponibles."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'consultar_tramite':
            respuesta = ("¡Claro! Te puedo ayudar con información sobre varios trámites. ¿Cuál te interesa?")
            return {"respuesta": respuesta, "botones": [{"texto": "Licencia de Conducir"}, {"texto": "Habilitación Comercial"}]}
        return None

class GeneralHandler(BaseMunicipioHandler):
    """Maneja el resto de las consultas genéricas con un LLM."""
    def handle(self, pregunta: str) -> dict | None:
        # Este es el fallback final para cualquier cosa que no sea un flujo específico.
        prompt_sistema = "Sos un agente de atención ciudadana experto..."
        respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt_sistema)
        return {"respuesta": respuesta_llm}

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    context = { "contexto_municipio": contexto_municipio, "user_obj": user_obj, "user_id": getattr(user_obj, "id", None), "intencion": None }

    handler_chain = [IntentClassifierHandler, HumanEscalationHandler, ReclamoHandler, ImpuestosHandler, TramitesHandler, GeneralHandler]
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final: break
    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta."}

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
# services/municipios.py

import logging
import json
from models import Conversacion, MunicipioTicket, db
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

def _detectar_intencion_reclamo_con_keywords(pregunta: str) -> str | None:
    """Usa tu lista de keywords original y mejorada para una detección precisa."""
    temas = {
        "Alumbrado Público": ["luminaria", "luz quemada", "foco", "poste sin luz", "calle oscura"],
        "Calles y Veredas": ["bache", "pozo", "calle rota", "asfalto roto", "vereda rota"],
        "Limpieza y Residuos": ["basura", "residuo", "contenedor lleno", "no pasa el basurero", "mugre"],
        "Arbolado Público": ["árbol caído", "arbol caido", "rama peligrosa", "poda de arbol"],
    }
    texto = pregunta.lower()
    for tema, keywords in temas.items():
        if any(kw in texto for kw in keywords):
            return tema
    return None

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

class ReclamoHandler(BaseMunicipioHandler):
    """Gestiona el flujo completo de creación de un reclamo."""
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado_reclamo = memoria.get('estado_reclamo')
        
        tema_reclamo = _detectar_intencion_reclamo_con_keywords(pregunta)
        if tema_reclamo and not estado_reclamo:
            memoria['estado_reclamo'] = 'confirmando_categoria'
            memoria['categoria_reclamo'] = tema_reclamo
            return {"respuesta": f"Detecté que tu consulta es sobre **{tema_reclamo}**. ¿Querés iniciar un reclamo formal?", 
                    "botones": [{"texto": "Sí, iniciar reclamo"}, {"texto": "No, es otra cosa"}]}
        
        elif estado_reclamo == 'confirmando_categoria':
            if "no" in pregunta.lower():
                memoria.clear()
                return {"respuesta": "Entendido. ¿En qué otra cosa puedo ayudarte?"}
            else:
                memoria['estado_reclamo'] = 'esperando_direccion'
                return {"respuesta": f"Perfecto. Para registrar tu reclamo de **{memoria.get('categoria_reclamo')}**, por favor, indicame la dirección exacta."}

        elif estado_reclamo == 'esperando_direccion':
            categoria = memoria.get('categoria_reclamo', 'General')
            ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={
                "user_id": self.context.get('user_id'), "asunto": f"Reclamo de {categoria}", "categoria": categoria,
                "detalles": f"Dirección indicada: {pregunta}"
            })
            memoria.clear()
            if ticket:
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. El área correspondiente se encargará."}
            else:
                return {"respuesta": "Lo lamento, hubo un problema técnico al generar tu ticket."}
        return None

class GeneralHandler(BaseMunicipioHandler):
    """Maneja el resto de las consultas."""
    def handle(self, pregunta: str) -> dict | None:
        prompt = "Sos un asistente municipal experto..." # Tu prompt completo aquí
        respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt)
        return {"respuesta": respuesta_llm}

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    context = { "contexto_municipio": contexto_municipio, "user_obj": user_obj, "user_id": getattr(user_obj, "id", None) }
    
    handler_chain = [ReclamoHandler, GeneralHandler] # Cadena simplificada y robusta
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final: break
    
    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí bien. ¿Podrías reformular tu consulta?"}

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
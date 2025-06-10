import logging
import re
import json
import os
from models import MunicipioTicket, TicketComentario, db # Usando tus modelos
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
# Importamos nuestra nueva y flamante herramienta
from .herramientas_municipio import consultar_recoleccion_por_direccion
from .logic import _clasificar_intencion_con_llm 
from twilio.rest import Client # <<<<<<<<<<<<<< CORREGIDO: Importación de Client

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

# --- REGISTRO DE HERRAMIENTAS DISPONIBLES ---
TOOL_REGISTRY = {
    "consultar_recoleccion_por_direccion": consultar_recoleccion_por_direccion,
}

# Variables de entorno para Twilio, se leen de os.environ
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER") 

# --- FUNCIÓN DE NOTIFICACIÓN REAL ---
def enviar_notificacion_sms(numero_destino: str, mensaje: str):
    """
    Envía una notificación SMS real usando Twilio.
    """
    # Esta validación básica se mantiene
    if not re.match(r'^\+\d{7,15}$', numero_destino): 
        logger.warning(f"[NOTIFICACION SMS] Número de destino '{numero_destino}' no parece ser un formato válido E.164. Intentando enviar de todas formas.")

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        logger.error("[NOTIFICACION SMS] Credenciales de Twilio no configuradas (missing SID, Token, or Number). No se puede enviar SMS.")
        return

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            to=numero_destino, 
            from_=TWILIO_PHONE_NUMBER,
            body=mensaje
        )
        logger.info(f"[NOTIFICACION SMS REAL] SMS enviado, SID: {message.sid}")
    except Exception as e:
        logger.error(f"[NOTIFICACION SMS REAL] Error al enviar SMS a {numero_destino}: {e}")


# --- PROMPT PARA EL NUEVO TOOLHANDLER ---
def crear_prompt_decision_herramienta(pregunta_usuario: str) -> str:
    descripcion_herramientas = """
    {
      "consultar_recoleccion_por_direccion": {
        "descripcion": "Se usa para obtener los horarios y días de recolección de basura, residuos o cuando pasa el camión basurero para una dirección específica. El usuario DEBE proporcionar una dirección o el nombre de una calle.",
        "parametros": { "direccion": "La dirección completa que el usuario mencionó, por ejemplo: 'Avenida San Martín 123, Junín'" }
      }
    }
    """
    prompt = f"""
    Tu única tarea es analizar la PREGUNTA DEL USUARIO y decidir si se puede resolver con una de las HERRAMIENTAS DISPONIBLES.

    HERRAMIENTAS DISPONIBLES:
    {descripcion_herramientas}

    PREGUNTA DEL USUARIO: "{pregunta_usuario}"

    INSTRUCCIONES:
    1. Si la pregunta coincide claramente con la descripción de una herramienta y contiene los parámetros necesarios, responde SÓLO con un objeto JSON con el formato:
    `{{"usar_herramienta": "nombre_de_la_herramienta", "parametros": {{"nombre_parametro": "valor_extraido"}}}}`
    2. Si la pregunta NO coincide con ninguna herramienta o le faltan parámetros (como la dirección), responde SÓLO con la palabra: `null`

    Ejemplos:
    - Pregunta: "a que hora pasa el basurero por 25 de mayo 1550?" -> Respuesta: {{"usar_herramienta": "consultar_recoleccion_por_direccion", "parametros": {{"direccion": "25 de mayo 1550"}}}}
    - Pregunta: "horarios del camion de basura?" -> Respuesta: null (falta la dirección)
    - Pregunta: "necesito hacer un reclamo" -> Respuesta: null (no es una herramienta de esta lista)

    Tu respuesta:
    """
    return prompt

# --- CLASES HANDLER (NUEVA Y EXISTENTES) ---

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

# ¡NUEVO! Este es el "Recepcionista Experto"
class ToolHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if memoria.get('estado_conversacion'):
            return None

        prompt = crear_prompt_decision_herramienta(pregunta)
        respuesta_llm = get_cohere_response(message=prompt, preamble="Eres un experto en decidir si una pregunta requiere una herramienta específica. Responde solo con JSON o 'null'.")

        try:
            decision = json.loads(respuesta_llm)
            if decision and "usar_herramienta" in decision:
                nombre_herramienta = decision["usar_herramienta"]
                parametros = decision.get("parametros", {})

                if nombre_herramienta in TOOL_REGISTRY:
                    logger.info(f"[ToolHandler] Usando la herramienta '{nombre_herramienta}'")
                    funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]
                    resultado = funcion_a_ejecutar(**parametros)
                    return {"respuesta": resultado}
        except (json.JSONDecodeError, TypeError):
            logger.info("[ToolHandler] La pregunta no requiere una herramienta específica. Pasando a la cadena principal.")
            return None
        return None

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
        memoria = self.context.get('contexto_municipio', {})
        estado_conversacion = memoria.get('estado_conversacion')
        if estado_conversacion == 'esperando_confirmacion_cierre':
            ticket_id = memoria.get('ticket_id_activo')
            ticket = db.session.get(MunicipioTicket, ticket_id)
            if "si" in pregunta.lower() or "sí" in pregunta.lower():
                ticket.estado = "resuelto"
                db.session.commit()
                memoria['estado_conversacion'] = 'esperando_calificacion'
                return {"respuesta": "¡Excelente! Me alegra que lo hayamos solucionado. Para terminar, ¿podrías calificar la atención recibida del 1 al 5? Tu opinión nos ayuda a mejorar."}
            else:
                memoria.clear()
                return {"respuesta": "Entendido. Dejaré el ticket abierto para que nuestro equipo continúe con el seguimiento. ¿Hay algo más que quieras agregar?"}
        elif estado_conversacion == 'esperando_calificacion':
            ticket_id = memoria.get('ticket_id_activo')
            servicio_tickets.crear_comentario(
                ticket_id=ticket_id, tipo_ticket="municipio",
                comentario_data={"comentario": f"Calificación del vecino: {pregunta}", "es_admin": False}
            )
            memoria.clear()
            return {"respuesta": "¡Muchas gracias por tu calificación! Hemos cerrado el ticket. ¿Necesitas ayuda con algo más?"}
        if self.context.get('intencion') == 'consultar_estado_ticket':
            match = re.search(r'\d{5,}', pregunta)
            if not match: return {"respuesta": "Por favor, decime el número de ticket que querés consultar."}
            ticket = MunicipioTicket.query.filter_by(nro_ticket=int(match.group(0))).first()
            if not ticket: return {"respuesta": f"No pude encontrar ningún ticket con el número {match.group(0)}."}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' se encuentra en estado: **{ticket.estado}**."
            ultimo_comentario_agente = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first()
            if ultimo_comentario_agente:
                respuesta += f"\n\nÚltima actualización de nuestro equipo: *\"{ultimo_comentario_agente.comentario}\"*"
                if ticket.estado == "en_proceso":
                    memoria['estado_conversacion'] = 'esperando_confirmacion_cierre'
                    memoria['ticket_id_activo'] = ticket.id
                    respuesta += "\n\n¿Tu problema fue solucionado con esta respuesta?"
                    return {"respuesta": respuesta, "botones": [{"texto": "Sí, solucionado"}, {"texto": "No, aún no"}]}
            return {"respuesta": respuesta}
        return None

class ReclamoHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')

        # Paso 1: Iniciar el reclamo y pedir categoría
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria['estado_conversacion'] = 'esperando_categoria_reclamo'
            memoria['pregunta_original'] = pregunta
            return {"respuesta": "Entendido, vamos a iniciar tu reclamo. Para dirigirlo al área correcta, por favor, seleccioná una de las siguientes categorías:", "botones": [{"texto": "Alumbrado Público"}, {"texto": "Calles y Veredas"}, {"texto": "Limpieza y Residuos"}]}
        
        # Paso 2: Recibir categoría y pedir dirección
        elif estado == 'esperando_categoria_reclamo':
            memoria['estado_conversacion'] = 'esperando_direccion_reclamo'
            memoria['categoria_reclamo'] = pregunta
            return {"respuesta": f"Perfecto, categoría: **{pregunta}**. Ahora, indicame la **dirección completa del problema**, por ejemplo: `San Martín 123, Junín, Mendoza`."}
        
        # Paso 3: Recibir dirección y pedir nombre del vecino
        elif estado == 'esperando_direccion_reclamo':
            memoria['estado_conversacion'] = 'esperando_nombre_vecino'
            memoria['direccion_reclamo'] = pregunta
            return {"respuesta": "¡Gracias por la dirección! Ahora, por favor, indicame tu **nombre completo**."}

        # Paso 4: Recibir nombre y pedir teléfono del vecino
        elif estado == 'esperando_nombre_vecino':
            memoria['estado_conversacion'] = 'esperando_telefono_vecino'
            memoria['nombre_vecino'] = pregunta
            return {"respuesta": "Gracias, **[nombre_vecino]**. Por último, ¿cuál es tu **número de teléfono** (solo los números, incluyendo el código de área, ej: `2634123456`)?"} 
        
        # Paso 5: Recibir teléfono, formatear, crear el ticket y ENVIAR NOTIFICACIÓN
        elif estado == 'esperando_telefono_vecino':
            categoria = memoria.get('categoria_reclamo', 'General')
            direccion = memoria.get('direccion_reclamo', 'No especificada')
            nombre_vecino = memoria.get('nombre_vecino', 'Anónimo')
            telefono_input = pregunta 

            # --- LÓGICA DE NORMALIZACIÓN DEL NÚMERO DE TELÉFONO ---
            telefono_formateado = telefono_input.strip()
            telefono_formateado = re.sub(r'\D', '', telefono_formateado) 

            if not telefono_formateado.startswith('+'):
                telefono_formateado = '+549' + telefono_formateado 
                logger.info(f"[RECLAMO] Número de teléfono ajustado a formato internacional: {telefono_formateado}")
            # --- FIN NUEVA LÓGICA ---

            detalles_ticket = (
                f"Consulta original: {memoria.get('pregunta_original', 'N/A')}\n"
                f"Categoría: {categoria}\n"
                f"Dirección del problema: {direccion}\n"
                f"Nombre del vecino: {nombre_vecino}\n"
                f"Teléfono de contacto: {telefono_formateado}" 
            )

            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={
                    "asunto": f"Reclamo de {categoria}",
                    "categoria": categoria,
                    "detalles": detalles_ticket,
                    "user_id": self.context.get("user_id")
                }
            )
            
            if ticket:
                mensaje_confirmacion = f"Municipio de Junín: Recibimos su reclamo (Ticket M-{ticket.nro_ticket}) sobre {categoria}. Será procesado a la brevedad. ¡Gracias por contactarnos!"
                enviar_notificacion_sms(telefono_formateado, mensaje_confirmacion) 
                logger.info(f"Notificación SMS enviada para ticket M-{ticket.nro_ticket} a {telefono_formateado}")

                memoria.clear() 
                # El mensaje final actualizado (sin mención de email)
                return {"respuesta": f"¡Gracias, **{nombre_vecino}**! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. Te llegará un SMS de confirmación a tu celular con el número de reclamo. Por favor, consulta el estado de tu reclamo por este mismo medio usando ese número. ¡Estamos para ayudarte!"}
            else:
                memoria.clear() 
                return {"respuesta": "Disculpa, no pudimos generar tu reclamo en este momento. Por favor, intenta de nuevo más tarde o comunícate con la municipalidad."}
        
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
        intencion = self.context.get('intencion') 

        logger.info(f"[TRAMITES] Recibido: Pregunta='{pregunta}', Estado='{estado}', Intencion='{intencion}'")

        # Paso 1: Iniciar el flujo de trámites / dar opciones iniciales
        if intencion == 'consultar_tramite' and not estado:
            memoria['estado_conversacion'] = 'esperando_seleccion_tramite_general' 
            return {
                "respuesta": "¡Claro! Te puedo ayudar con información sobre la Licencia de Conducir, o puedes explorar otros trámites municipales.",
                "botones": [
                    {"texto": "Licencia de Conducir"}, 
                    {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"}
                ]
            }
        
        # Paso 2: El usuario ha seleccionado/preguntado por Licencia de Conducir
        # y estamos iniciando ese sub-flujo (o re-ingresando si el LLM redirige)
        elif (estado == 'esperando_seleccion_tramite_general' and "licencia" in pregunta.lower()) or \
             (intencion == 'consultar_tramite' and "licencia" in pregunta.lower() and not estado):
            
            memoria['estado_conversacion'] = 'esperando_pregunta_curso_licencia' # Avanzamos al estado de preguntar por el curso
            
            return {
                "respuesta": "Para la Licencia de Conducir necesitás: DNI actualizado, no tener multas, y hacer el curso de seguridad vial.",
                "botones": [
                    {"texto": "Sacar Turno Licencia de Conducir", "url": "https://tlc.mendoza.gov.ar/turnos"},
                    {"texto": "¿Dónde hacer el curso?", "accion_interna": "preguntar_curso"}, # Nuevo: acción interna para el curso
                    {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"} 
                ]
            }
        
        # Paso 3: El usuario pregunta específicamente "¿Dónde hacer el curso?" (ya sea por texto o botón)
        elif estado == 'esperando_pregunta_curso_licencia' and \
             ("donde" in pregunta.lower() or "si" in pregunta.lower() or "sí" in pregunta.lower() or "preguntar_curso" == pregunta.lower()): # Incluye el "acción_interna"
            
            memoria.clear() # Limpiamos la memoria al dar la info final del curso
            return {
                "respuesta": "El curso de seguridad vial es online en la Agencia Nacional de Seguridad Vial o presencialmente en [Dirección del Centro].",
                "botones": [
                    {"texto": "Sacar Turno Licencia de Conducir", "url": "https://tlc.mendoza.gov.ar/turnos"},
                    {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"} 
                ]
            }
        
        # Paso 4: El usuario dice "no" a la pregunta del curso (si no necesita más info del curso)
        elif estado == 'esperando_pregunta_curso_licencia' and ("no" in pregunta.lower()):
            memoria.clear() # Limpiamos la memoria al finalizar el flujo
            return {
                "respuesta": "Entendido. Para sacar el turno de tu Licencia de Conducir, haz clic aquí:",
                "botones": [
                    {"texto": "Sacar Turno Licencia de Conducir", "url": "https://tlc.mendoza.gov.ar/turnos"},
                    {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"} 
                ]
            }
        
        # Paso 5: El usuario hace clic en "Más Trámites" (enviado como texto) desde el estado inicial o cualquier otro punto
        elif estado == 'esperando_seleccion_tramite_general' and "más trámites" in pregunta.lower():
            memoria.clear() 
            return {
                "respuesta": "¡Claro! Te dirijo a la página de Trámites del municipio para que explores todas las opciones.",
                "botones": [
                    {"texto": "Ir a Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"}
                ]
            }
        
        # Manejo de casos donde el usuario está en el estado inicial de trámite pero la pregunta no es 'licencia' ni 'más trámites'
        elif estado == 'esperando_seleccion_tramite_general':
             return {"respuesta": "Disculpa, ¿qué tipo de trámite te interesa? Puedes preguntar por 'Licencia de Conducir' o hacer clic en 'Más Trámites'."}

        return None


class GeneralHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        prompt = "Sos un agente de atención ciudadana experto..."
        respuesta_llm = get_cohere_response(message=pregunta, preamble=prompt)
        return {"respuesta": respuesta_llm}
     
# ¡ENGANCHE PARA ANÓNIMOS SIN USER_ID! (último recurso en landing demo)
class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if not self.context.get("user_id"):
            return {
                "respuesta": "Para darte una mejor atención personalizada, por favor registrate o iniciá sesión. Así vas a poder hacer reclamos reales y recibir respuestas oficiales del municipio.",
                "botones": [
                    {"texto": "Iniciar Sesión", "url": "/login"},
                    {"texto": "Registrarme Gratis", "url": "/register"},
                    {"texto": "Planes Premium", "url": "/precios"}
                ]
            }
        return None

# --- FUNCIÓN PRINCIPAL ORQUESTADORA (MODIFICADA) ---
def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    context = {"contexto_municipio": contexto_municipio, "user_obj": user_obj, "user_id": getattr(user_obj, "id", None), "intencion": None}

    handler_chain = [
        ToolHandler, 
        IntentClassifierHandler, 
        HumanEscalationHandler, 
        TicketStatusHandler, 
        ReclamoHandler, 
        ImpuestosHandler, 
        TramitesHandler, # Este handler ha sido modificado
        GeneralHandler,
        EngancheAnonimoMunicipioHandler  
    ]
    
    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta."}

    if "[nombre_vecino]" in respuesta_final.get('respuesta', ''):
        nombre_vecino_memoria = context.get('contexto_municipio', {}).get('nombre_vecino', 'vecino')
        respuesta_final['respuesta'] = respuesta_final['respuesta'].replace("[nombre_vecino]", nombre_vecino_memoria)

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
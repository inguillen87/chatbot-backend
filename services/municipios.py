import logging
import re
import json
import os
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
from .logic import _clasificar_intencion_con_llm
from twilio.rest import Client
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes
)

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886' # Número del Sandbox de Twilio
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")

PROMPT_MUNICIPIO_CON_CONTEXTO = """
Eres un asistente virtual experto del municipio. Tu deber es responder la PREGUNTA DEL USUARIO de manera precisa y amigable, utilizando únicamente la INFORMACIÓN DE CONTEXTO que te proporciono. No inventes información que no esté en el contexto. Si la respuesta no se encuentra en el contexto, indica amablemente que no tienes esa información específica y sugiere contactar a la municipalidad.
--- INFORMACIÓN DE CONTEXTO ---
{contexto_scraped}
---------------------------------
PREGUNTA DEL USUARIO: "{pregunta_usuario}"
Respuesta:
"""

def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
    """
    Usa el LLM para determinar si la respuesta de un usuario se desvía de la pregunta anterior.
    """
    prompt = f"""
    Mi chatbot esperaba que el usuario respondiera con {tipo_esperado}.
    El usuario respondió: "{texto_usuario}".

    Analiza la respuesta del usuario. Si parece que el usuario está intentando responder a mi solicitud (por ejemplo, dando la dirección, el teléfono, etc.), responde SÓLO con la palabra 'RESPUESTA_VALIDA'.
    Si parece que el usuario está ignorando mi solicitud y haciendo una pregunta completamente nueva o cambiando de tema, responde SÓLO con la palabra 'PREGUNTA_NUEVA'.

    Tu decisión:
    """
    try:
        decision = get_cohere_response(message=prompt, preamble="Eres un clasificador de respuestas. Responde solo con 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.")
        logger.info(f"[Guardián de Flujo] Decisión para '{texto_usuario}': {decision.strip()}")
        return "PREGUNTA_NUEVA" in decision
    except Exception as e:
        logger.error(f"[Guardián de Flujo] Error: {e}")
        return False # En caso de error, asumimos que la respuesta es válida para no romper el flujo.

class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

class GreetingHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if not memoria.get('estado_conversacion'):
            saludos = ['hola', 'buenos dias', 'buenas tardes', 'buenas noches', 'hey', 'que tal']
            pregunta_limpia = pregunta.strip().lower()
            if pregunta_limpia in saludos:
                memoria.clear()
                respuesta = (
                    "¡Hola! 👋 Soy tu asistente virtual del Municipio de Junín. "
                    "Estoy aquí para ayudarte de forma rápida y automática. "
                    "Puedes preguntarme cosas como:\n\n"
                    "✅ '¿Cuándo pasa el basurero por San Martín 123?'\n"
                    "✅ 'Quiero hacer un reclamo por una luz quemada'\n"
                    "✅ '¿Cómo saco el carnet de conducir?'\n\n"
                    "¿En qué te puedo ayudar hoy?"
                )
                return {"respuesta": respuesta}
        return None

def enviar_notificacion_whatsapp_con_plantilla(numero_destino: str, nombre: str, nro_ticket: str, categoria: str):
    """
    Envía una notificación por WhatsApp usando una PLANTILLA APROBADA (del Sandbox o de Producción).
    Esta es la versión final y recomendada.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
        logger.error("[NOTIFICACION WHATSAPP] Credenciales de Twilio no configuradas.")
        return

    # IMPORTANTE: Busca este ID en tu consola de Twilio, en la sección de plantillas del Sandbox.
    # Corresponde a la plantilla "Appointment Reminders".
    CONTENT_SID_PLANTILLA = TWILIO_WHATSAPP_CONTENT_SID

    destinatario_whatsapp = f'whatsapp:{numero_destino}'
    
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # Las variables que llenarán los campos {{1}} y {{2}} de la plantilla.
        variables_plantilla = {
            '1': f"Hola {nombre}! Se generó tu ticket M-{nro_ticket}",
            '2': f"El área de {categoria} lo revisará pronto."
        }

        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=destinatario_whatsapp,
            content_sid=CONTENT_SID_PLANTILLA,
            content_variables=json.dumps(variables_plantilla)
        )
        logger.info(f"[NOTIFICACION WHATSAPP] Mensaje de plantilla enviado, SID: {message.sid}")
    except Exception as e:
        logger.error(f"[NOTIFICACION WHATSAPP] Error al enviar plantilla a {destinatario_whatsapp}: {e}")



def enviar_notificacion_sms(numero_destino: str, mensaje: str):
    """
    Envía una notificación por SMS usando las credenciales de Twilio.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        logger.error("[NOTIFICACION SMS] Credenciales de Twilio no configuradas. No se puede enviar SMS.")
        return
    
    # Verificación simple del formato del número
    if not re.match(r'^\+\d{7,15}$', numero_destino):
        logger.warning(f"[NOTIFICACION SMS] Número de destino '{numero_destino}' no parece un formato E.164 válido. Intentando enviar de todas formas.")

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            to=numero_destino,
            from_=TWILIO_PHONE_NUMBER,
            body=mensaje
        )
        logger.info(f"[NOTIFICACION SMS] SMS enviado, SID: {message.sid}")
    except Exception as e:
        logger.error(f"[NOTIFICACION SMS] Error al enviar SMS a {numero_destino}: {e}")


TOOL_REGISTRY = {
    "consultar_recoleccion_por_direccion": {
        "funcion": consultar_recoleccion_por_direccion,
        "descripcion": "Se usa para obtener los horarios y días de recolección de basura para una dirección específica. El usuario DEBE proporcionar una dirección, calle o descripción de un lugar.",
        "parametros": {
            "direccion": "La dirección completa o descripción del lugar que el usuario mencionó. Por ejemplo: 'Avenida San Martín 123, Junín'."
        },
        "ejemplo_pregunta": "a que hora pasa el basurero por 25 de mayo 1550?",
        "ejemplo_llamada": '{"usar_herramienta": "consultar_recoleccion_por_direccion", "parametros": {"direccion": "25 de mayo 1550"}}'
    }
}

def crear_prompt_decision_herramienta(pregunta_usuario: str) -> str:
    descripcion_herramientas = "{\n"
    for nombre, detalles in TOOL_REGISTRY.items():
        descripcion_herramientas += f'   "{nombre}": {{\n'
        descripcion_herramientas += f'     "descripcion": "{detalles["descripcion"]}",\n'
        descripcion_herramientas += f'     "parametros": {json.dumps(detalles["parametros"])}\n'
        descripcion_herramientas += '   },\n'
    descripcion_herramientas = descripcion_herramientas.rstrip(',\n') + "\n}"
    ejemplos_str = ""
    for detalles in TOOL_REGISTRY.values():
        ejemplos_str += f'  - Pregunta: "{detalles["ejemplo_pregunta"]}" -> Respuesta: {detalles["ejemplo_llamada"]}\n'
    prompt = f"""
Tu única tarea es analizar la PREGUNTA DEL USUARIO y decidir si se puede resolver con una de las HERRAMIENTAS DISPONIBLES.
Si una herramienta requiere un parámetro y no está en la pregunta, indica que falta (faltan_parametros).

HERRAMIENTAS DISPONIBLES:
{descripcion_herramientas}

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

INSTRUCCIONES:
1. Si la pregunta coincide claramente con la descripción de una herramienta y contiene los parámetros necesarios, responde SÓLO con un objeto JSON con el formato:
   {{"usar_herramienta": "nombre_de_la_herramienta", "parametros": {{"nombre_parametro": "valor_extraido"}}}}
2. Si la pregunta coincide con una herramienta pero le FALTAN PARÁMETROS ESENCIALES, responde SÓLO con un objeto JSON con el formato:
   {{"usar_herramienta": "nombre_de_la_herramienta", "faltan_parametros": ["nombre_parametro"]}}
3. Si la pregunta NO coincide con ninguna herramienta o no es relevante para ellas, responde SÓLO con la palabra: null

Ejemplos:
{ejemplos_str}
- Pregunta: "necesito hacer un reclamo" -> Respuesta: null

Tu respuesta:
"""
    return prompt

class ToolHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if memoria.get('estado_conversacion') == 'esperando_param_recoleccion':
            if es_pregunta_nueva(pregunta, "una dirección"):
                memoria.clear(); return None
            memoria['estado_conversacion'] = None
            return {"respuesta": consultar_recoleccion_por_direccion(direccion=pregunta)}
        
        if memoria.get('estado_conversacion') and memoria.get('estado_conversacion') != 'esperando_param_recoleccion':
            return None
            
        prompt = crear_prompt_decision_herramienta(pregunta)
        respuesta_llm = get_cohere_response(message=prompt, preamble="Eres un experto en decidir si una pregunta requiere una herramienta específica. Responde solo con JSON o 'null'.")
        try:
            decision = json.loads(respuesta_llm)
            if decision and "usar_herramienta" in decision:
                nombre_herramienta = decision["usar_herramienta"]
                if nombre_herramienta in TOOL_REGISTRY:
                    if "faltan_parametros" in decision:
                        param_faltante = decision["faltan_parametros"][0]
                        if param_faltante == "direccion":
                            memoria['estado_conversacion'] = 'esperando_param_recoleccion'
                            return {"respuesta": "Claro, para decirte el horario exacto de recolección de basura, necesito la dirección completa. Por favor, indicame la calle y el número."}
                    parametros = decision.get("parametros", {})
                    logger.info(f"[ToolHandler] Usando la herramienta '{nombre_herramienta}' con parámetros: {parametros}")
                    funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]["funcion"]
                    resultado = funcion_a_ejecutar(**parametros)
                    return {"respuesta": resultado}
        except (json.JSONDecodeError, TypeError):
            logger.info("[ToolHandler] La pregunta no requiere una herramienta específica o la respuesta del LLM no es JSON válida. Pasando a la cadena principal.")
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
            logger.info(f"[HumanEscalationHandler] El usuario {self.context.get('user_id')} solicita un agente.")
            ticket_data = {
                "asunto": "Solicitud de Chat en Vivo", "categoria": "Atención en Vivo",
                "detalles": f"El vecino solicitó atención en vivo con el mensaje: '{pregunta}'",
                "user_id": self.context.get("user_id"), "estado": "esperando_agente_en_vivo"
            }
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
            if not sala_de_chat:
                return {"respuesta": "Disculpa, hubo un problema técnico al intentar conectar con un agente."}
            
            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id, tipo_ticket="municipio",
                comentario_data={"comentario": pregunta, "es_admin": False, "user_id": self.context.get("user_id")}
            )
            logger.info(f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada.")
            respuesta_al_vecino = (
                f"¡Entendido! He abierto una sala de chat directa con nuestro equipo. "
                f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. \n\n"
                "Por favor, aguarda un momento mientras un agente se conecta. **No cierres esta ventana.**"
            )
            self.context.get('contexto_municipio', {}).clear()
            return {"respuesta": respuesta_al_vecino, "ticket_id": sala_de_chat.id}
        return None

class TicketStatusHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado_conversacion = memoria.get('estado_conversacion')
        
        if estado_conversacion == 'esperando_confirmacion_cierre':
            # --- GUARDIÁN INTELIGENTE ---
            if es_pregunta_nueva(pregunta, "una confirmación (sí o no)"): memoria.clear(); return None
            
            ticket_id = memoria.get('ticket_id_activo')
            ticket = db.session.get(MunicipioTicket, ticket_id)
            if "si" in pregunta.lower() or "sí" in pregunta.lower():
                ticket.estado = "resuelto"
                db.session.commit()
                memoria['estado_conversacion'] = 'esperando_calificacion'
                return {"respuesta": "¡Excelente! Me alegra que lo hayamos solucionado. Para terminar, ¿podrías calificar la atención recibida del 1 al 5?"}
            else:
                memoria.clear()
                return {"respuesta": "Entendido. Dejaré el ticket abierto para que nuestro equipo continúe con el seguimiento."}
        
        elif estado_conversacion == 'esperando_calificacion':
            # --- GUARDIÁN INTELIGENTE ---
            if es_pregunta_nueva(pregunta, "una calificación del 1 al 5"): memoria.clear(); return None
                
            ticket_id = memoria.get('ticket_id_activo')
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="municipio", comentario_data={"comentario": f"Calificación del vecino: {pregunta}", "es_admin": False})
            memoria.clear()
            return {"respuesta": "¡Muchas gracias por tu calificación! Hemos cerrado el ticket. ¿Necesitas ayuda con algo más?"}

        if self.context.get('intencion') == 'consultar_estado_ticket':
            match = re.search(r'\d{5,}', pregunta)
            if not match: return {"respuesta": "Por favor, decime el número de ticket que querés consultar."}
            ticket = MunicipioTicket.query.filter_by(nro_ticket=int(match.group(0))).first()
            if not ticket: return {"respuesta": f"No pude encontrar ningún ticket con el número {match.group(0)}."}
            
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' se encuentra en estado: **{ticket.estado}**."
            ultimo_comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first()
            if ultimo_comentario:
                respuesta += f"\n\nÚltima actualización de nuestro equipo: *\"{ultimo_comentario.comentario}\"*"
                if ticket.estado == "en_proceso":
                    memoria['estado_conversacion'] = 'esperando_confirmacion_cierre'
                    memoria['ticket_id_activo'] = ticket.id
                    respuesta += "\n\n¿Tu problema fue solucionado con esta respuesta?"
                    return {"respuesta": respuesta, "botones": [{"texto": "Sí, solucionado"}, {"texto": "No, aún no"}]}
            return {"respuesta": respuesta}
        return None

# --- CÓDIGO CORRECTO PARA ReclamoHandler ---
# Pega esto en tu archivo y reemplaza la clase ReclamoHandler completa para estar 100% seguros.

# Reemplaza esta clase completa en municipios.py
class ReclamoHandler(BaseMunicipioHandler):
    def build_detalles_memoria(self, memoria: dict) -> str:
        return (
            f"Consulta original: {memoria.get('pregunta_original', '')}\n"
            f"Categoría: {memoria.get('categoria_reclamo', '')}\n"
            f"Dirección: {memoria.get('direccion_reclamo', '')}\n"
            f"Nombre: {memoria.get('nombre_vecino', '')}\n"
            f"Teléfono: {memoria.get('telefono_vecino', '')}"
        )

    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')

        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria.clear()
            memoria['pregunta_original'] = pregunta
            categoria_adivinada = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_adivinada != "Otros":
                logger.info(f"[ReclamoHandler] Nivel 1: Categoría por keyword: {categoria_adivinada}")
                memoria['estado_conversacion'] = 'esperando_direccion_reclamo'
                memoria['categoria_reclamo'] = categoria_adivinada
                return {"respuesta": f"Entendido. Reclamo clasificado como **{categoria_adivinada}**. Para continuar, indicame la **dirección del problema**."}
            else:
                logger.info("[ReclamoHandler] Nivel 2: Mostrando lista de respaldo.")
                memoria['estado_conversacion'] = 'esperando_categoria_reclamo'
                return {"respuesta": "Entendido, vamos a iniciar tu reclamo. ¿Podrías seleccionarla de esta lista?", "botones": [{"texto": "Luminaria"}, {"texto": "Limpieza"}, {"texto": "Arbol Caido"}, {"texto": "Otros"}]}

        if estado == 'esperando_categoria_reclamo':
            if es_pregunta_nueva(pregunta, "una categoría de reclamo"):
                memoria.clear()
                return None

            categoria_final = categorizar_reclamo_por_palabra_clave(pregunta)
            logger.info(f"[ReclamoHandler] Categoría final seleccionada/mapeada: {categoria_final}")
            memoria['categoria_reclamo'] = categoria_final
            memoria['estado_conversacion'] = 'esperando_direccion_reclamo'
            return {"respuesta": f"Perfecto, categoría: **{categoria_final}**. Ahora, indicame la **dirección completa del problema**."}

        if estado == 'esperando_direccion_reclamo':
            memoria['direccion_reclamo'] = pregunta
            memoria['estado_conversacion'] = 'esperando_nombre_vecino'
            return {"respuesta": "¡Gracias! Ahora, por favor, tu **nombre completo**."}

        if estado == 'esperando_nombre_vecino':
            memoria['nombre_vecino'] = pregunta
            memoria['estado_conversacion'] = 'esperando_telefono_vecino'
            return {"respuesta": f"Gracias, {pregunta}. Por último, tu **número de teléfono** (con código de área)."}

        if estado == 'esperando_telefono_vecino':
            memoria['telefono_vecino'] = pregunta.strip()  # Guardamos el tel crudo
            categoria = memoria.get('categoria_reclamo', 'General')
            direccion = memoria.get('direccion_reclamo', '')
            nombre = memoria.get('nombre_vecino', '')
            telefono = re.sub(r'\D', '', memoria.get('telefono_vecino', ''))
            if not telefono.startswith('+'): telefono = '+549' + telefono

            detalles = self.build_detalles_memoria(memoria)
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={
                    "asunto": f"Reclamo de {categoria}",
                    "categoria": categoria,
                    "detalles": detalles,
                    "user_id": self.context.get("user_id")
                }
            )
            if ticket:
                enviar_notificacion_whatsapp_con_plantilla(numero_destino=telefono, nombre=nombre, nro_ticket=ticket.nro_ticket, categoria=categoria)
                mensaje_sms = f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado. Te mantendremos al tanto por SMS."
                enviar_notificacion_sms(telefono, mensaje_sms)
                memoria.clear()
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. Te hemos enviado una notificación por WhatsApp y SMS con los detalles."}
            else:
                memoria.clear()
                return {"respuesta": "Disculpa, no pudimos generar tu reclamo. Por favor, intenta de nuevo más tarde."}
        return None

class TramitesHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        intencion = self.context.get('intencion')

        if intencion == 'consultar_tramite' and not estado:
            memoria.clear()
            memoria['estado_conversacion'] = 'esperando_seleccion_tramite'
            return {"respuesta": "¡Claro! Te puedo ayudar con información sobre la Licencia de Conducir, o puedes explorar otros trámites municipales.", "botones": [{"texto": "Licencia de Conducir"}, {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"}]}

        if estado == 'esperando_seleccion_tramite':
            if es_pregunta_nueva(pregunta, "una opción de trámite"): memoria.clear(); return None
            if "licencia" in pregunta.lower():
                memoria.clear()
                memoria['estado_conversacion'] = 'esperando_pregunta_curso_licencia'
                return {"respuesta": "Para la Licencia de Conducir necesitás: DNI actualizado, no tener multas, и hacer el curso de seguridad vial.", "botones": [{"texto": "Sacar Turno", "url": "https://tlc.mendoza.gov.ar/turnos"}, {"texto": "¿Dónde hacer el curso?"}]}
            # Si el usuario escribe cualquier otra cosa, salimos del flujo de trámites
            memoria.clear(); return None

        if estado == 'esperando_pregunta_curso_licencia':
            if es_pregunta_nueva(pregunta, "una pregunta sobre el curso"): memoria.clear(); return None
            memoria.clear()
            return {"respuesta": "El curso de seguridad vial es online en la web de la Agencia Nacional de Seguridad Vial o presencialmente en el Centro de Emisión de Licencias de tu municipio."}
        return None

class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        return None

# Después de estas, debería empezar la clase GeneralHandler
# class GeneralHandler(BaseMunicipioHandler):
# ...
class GeneralHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        logger.info("[GeneralHandler] Manejando como consulta general con contexto de DB.")
        user_obj = self.context.get("user_obj")
        if not user_obj:
            return None # Dejamos que el EngancheAnonimoHandler se encargue

        contexto_scraped = ""
        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            textos_relevantes = [json.loads(item.datos_json).get("contenido", "") for item in contenidos if json.loads(item.datos_json).get("tipo") == "contenido_general"]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            if not contexto_scraped: contexto_scraped = "No hay información de contexto disponible para esta consulta."
        except Exception as e:
            logger.error(f"[GeneralHandler] Error al obtener contexto de la DB: {e}")
            contexto_scraped = "Hubo un error al cargar la información de contexto."
        
        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(contexto_scraped=contexto_scraped, pregunta_usuario=pregunta)
        respuesta_llm = get_cohere_response(message=prompt_final, preamble="Eres un asistente municipal que responde basado en información oficial.")
        return {"respuesta": respuesta_llm}

class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if not self.context.get("user_id"):
            return {
                "respuesta": "Para darte una mejor atención personalizada, por favor registrate o iniciá sesión. Así vas a poder hacer reclamos reales y recibir respuestas oficiales del municipio.",
                "botones": [{"texto": "Iniciar Sesión", "url": "/login"}, {"texto": "Registrarme Gratis", "url": "/register"}]
            }
        return None

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    CONTEXTO_MUNICIPIO = "contexto_municipio"
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    context = {
        "contexto_municipio": contexto_municipio,
        "user_obj": user_obj,
        "user_id": getattr(user_obj, "id", None),
        "intencion": None
    }

    estado_antes_de_procesar = contexto_municipio.get('estado_conversacion')

    handler_chain = [
        GreetingHandler, ToolHandler, IntentClassifierHandler, HumanEscalationHandler,
        TicketStatusHandler, ReclamoHandler, TramitesHandler, ImpuestosHandler,
        GeneralHandler, EngancheAnonimoMunicipioHandler
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta. Por favor, intenta reformular tu pregunta."}

    estado_despues_de_procesar = contexto_municipio.get('estado_conversacion')
    texto_respuesta = respuesta_final.get('respuesta', '')

    # Evitar el mensaje de "cambio de tema" si se acaba de crear un ticket/reclamo
    FRASES_EXITO = [
        "Tu reclamo fue generado con el ticket",  # ajustá esto según la frase exacta de éxito
        "Te hemos enviado una notificación por WhatsApp y SMS con los detalles.",
        "Fue generado el ticket"
    ]
    es_cierre_flujo = any(frase in texto_respuesta for frase in FRASES_EXITO)

    if estado_antes_de_procesar and not estado_despues_de_procesar and texto_respuesta and not es_cierre_flujo:
        mensaje_transicion = "Entendido, cambiemos de tema. Sobre tu nueva consulta:\n\n"
        respuesta_final['respuesta'] = mensaje_transicion + texto_respuesta

    # Placeholder para nombre_vecino si lo hubiera
    if "[nombre_vecino]" in respuesta_final.get('respuesta', ''):
        nombre_vecino_memoria = contexto_municipio.get('nombre_vecino', 'vecino')
        respuesta_final['respuesta'] = respuesta_final['respuesta'].replace("[nombre_vecino]", nombre_vecino_memoria)

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }

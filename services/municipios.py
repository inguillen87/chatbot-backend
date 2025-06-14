import logging
import re
import json
import os
from enum import Enum, auto

# --- IMPORTS ---
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
from .logic import _clasificar_intencion_con_llm
from twilio.rest import Client
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes,
    normalizar_texto,
    TOOL_REGISTRY,
    KEYWORD_TO_CATEGORY_MAP
)

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")

TODAS_LAS_CATEGORIAS_UNICAS = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
BOTONES_TODAS_CATEGORIAS = [{"texto": cat} for cat in TODAS_LAS_CATEGORIAS_UNICAS]

# === Mini-FAQ por trámite ===
MINI_FAQ_TRAMITES = {
    "licencia_de_conducir": [
        {"q": "cuánto cuesta", "a": "El costo actual de la Licencia de Conducir depende de la categoría. Podés consultarlo en la web oficial del municipio o preguntarlo en mesa de entrada."},
        {"q": "pago multa", "a": "Para poder sacar o renovar tu Licencia, necesitás no tener multas impagas. Podés consultar y pagar tus multas en el portal de Rentas: https://rentas.juninmendoza.gov.ar/"},
        {"q": "vencimiento", "a": "Podés consultar la fecha de vencimiento y los requisitos de renovación en el reverso de tu Licencia o directamente desde la web del municipio."},
        {"q": "curso", "a": "El curso de seguridad vial se puede hacer online en la web de la Agencia Nacional de Seguridad Vial, o presencial en el Centro de Emisión de Licencias."},
        {"q": "requisitos", "a": "Requisitos: DNI actualizado, no tener multas, hacer el curso y, si corresponde, apto médico."},
        {"q": "turno", "a": "Para solicitar un turno ingresá acá: https://tlc.mendoza.gov.ar/turnos"}
    ],
    # Podés sumar más trámites a este dict...
}

EJEMPLO_DIRECCION = "Ejemplo: San Martín 123, Barrio Centro, Junín"

class ConversationState(Enum):
    ESPERANDO_CONFIRMACION_CIERRE = auto()
    ESPERANDO_CALIFICACION = auto()
    ESPERANDO_PARAM_RECOLECCION = auto()
    ESPERANDO_CATEGORIA_RECLAMO = auto()
    ESPERANDO_DIRECCION_RECLAMO = auto()
    ESPERANDO_NOMBRE_VECINO = auto()
    ESPERANDO_TELEFONO_VECINO = auto()
    ESPERANDO_SELECCION_TRAMITE = auto()
    ESPERANDO_PREGUNTA_CURSO_LICENCIA = auto()
    ESPERANDO_DETALLE_TRAMITE = auto()

def enviar_notificacion_whatsapp_con_plantilla(numero_destino: str, nombre: str, nro_ticket: str, categoria: str):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER, TWILIO_WHATSAPP_CONTENT_SID]):
        logger.error("[NOTIFICACION WHATSAPP] Credenciales o Content SID de Twilio no configuradas.")
        return
    destinatario_whatsapp = f'whatsapp:{numero_destino}'
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        variables_plantilla = {'1': nombre, '2': f"M-{nro_ticket}", '3': categoria}
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=destinatario_whatsapp,
            content_sid=TWILIO_WHATSAPP_CONTENT_SID,
            content_variables=json.dumps(variables_plantilla)
        )
        logger.info(f"[NOTIFICACION WHATSAPP] Mensaje de plantilla enviado correctamente, SID: {message.sid}")
    except Exception as e:
        logger.error(f"[NOTIFICACION WHATSAPP] Error al enviar plantilla a {destinatario_whatsapp}: {e}", exc_info=True)

def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
    prompt = f"""
    Analiza la RESPUESTA DEL USUARIO. Mi chatbot esperaba una respuesta relacionada con: '{tipo_esperado}'.
    Por ejemplo, si esperaba una dirección, respuestas como 'San Martín 123' son válidas.
    Si esperaba una confirmación, respuestas como 'sí' o 'no' son válidas.
    RESPUESTA DEL USUARIO: "{texto_usuario}"
    Si la respuesta del usuario parece ser un intento de responder a mi solicitud, responde SÓLO con la palabra 'RESPUESTA_VALIDA'.
    Si el usuario está ignorando mi solicitud y cambiando de tema, responde SÓLO con la palabra 'PREGUNTA_NUEVA'.
    Tu decisión:
    """
    try:
        decision = get_cohere_response(message=prompt, preamble="Eres un clasificador de respuestas. Responde solo con 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.")
        logger.info(f"[Guardián de Flujo] Decisión para '{texto_usuario}': {decision.strip()}")
        return "PREGUNTA_NUEVA" in decision
    except Exception as e:
        logger.error(f"[Guardián de Flujo] Error: {e}")
        return False

PROMPT_MUNICIPIO_CON_CONTEXTO = """
Eres un asistente virtual experto del municipio. Tu deber es responder la PREGUNTA DEL USUARIO de manera precisa y amigable, utilizando únicamente la INFORMACIÓN DE CONTEXTO que te proporciono. No inventes información que no esté en el contexto. Si la respuesta no se encuentra en el contexto, indica amablemente que no tienes esa información específica y sugiere contactar a la municipalidad.
--- INFORMACIÓN DE CONTEXTO ---
{contexto_scraped}
---------------------------------
PREGUNTA DEL USUARIO: "{pregunta_usuario}"
Respuesta:
"""

class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

class GreetingHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if not memoria.get('estado_conversacion'):
            saludos = ['hola', 'buenos dias', 'buenas tardes', 'buenas noches', 'hey', 'que tal', 'buenas']
            if normalizar_texto(pregunta.strip("!.,?")) in saludos:
                memoria.clear()
                respuesta = (
                    "¡Hola! 👋 Soy tu asistente virtual del Municipio.\n"
                    "Consultame trámites, servicios, reclamos y más.\n\n"
                    "Por ejemplo:\n"
                    "✅ 'Quiero reclamar por un bache en mi calle'\n"
                    "✅ '¿Cuándo pasa el basurero por San Martín 123?'\n"
                    "✅ '¿Qué necesito para sacar el carnet de conducir?'\n\n"
                    "¿En qué te puedo ayudar hoy?"
                )
                return {"respuesta": respuesta}
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

class TicketStatusHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado_conversacion = memoria.get('estado_conversacion')
        if estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            if es_pregunta_nueva(pregunta, "una confirmación (sí o no)"):
                memoria.clear(); return None
            ticket_id = memoria.get('ticket_id_activo')
            ticket = db.session.get(MunicipioTicket, ticket_id)
            if "si" in normalizar_texto(pregunta):
                ticket.estado = "resuelto"
                db.session.commit()
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_CALIFICACION
                return {"respuesta": "¡Excelente! Me alegra que lo hayamos solucionado. Para terminar, ¿podrías calificar la atención recibida del 1 al 5?"}
            else:
                memoria.clear()
                return {"respuesta": "Entendido. Dejaré el ticket abierto para que nuestro equipo continúe con el seguimiento."}
        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
            if es_pregunta_nueva(pregunta, "una calificación del 1 al 5"):
                memoria.clear(); return None
            ticket_id = memoria.get('ticket_id_activo')
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="municipio", comentario_data={"comentario": f"Calificación del vecino: {pregunta}", "es_admin": False})
            memoria.clear()
            return {"respuesta": "¡Muchas gracias por tu calificación! Hemos cerrado el ticket. ¿Necesitás algo más o te puedo ayudar en otro trámite?"}
        if self.context.get('intencion') == 'consultar_estado_ticket':
            match = re.search(r'\d{5,}', pregunta)
            if not match: return {"respuesta": "Por favor, decime el número de ticket que querés consultar."}
            ticket = MunicipioTicket.query.filter_by(nro_ticket=int(match.group(0))).first()
            if not ticket: return {"respuesta": f"No pude encontrar ningún ticket con el número {match.group(0)}."}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado}**."
            ultimo_comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first()
            if ultimo_comentario:
                respuesta += f"\n\nÚltima actualización: *\"{ultimo_comentario.comentario}\"*"
                if ticket.estado == "en_proceso":
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                    memoria['ticket_id_activo'] = ticket.id
                    respuesta += "\n\n¿Se resolvió tu problema con esta respuesta?"
                    return {"respuesta": respuesta, "botones": [{"texto": "Sí, solucionado"}, {"texto": "No, aún no"}]}
            return {"respuesta": respuesta}
        return None

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
                logger.info(f"[ReclamoHandler] Nivel 1: Éxito por keyword. Categoría: {categoria_adivinada}")
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                memoria['categoria_reclamo'] = categoria_adivinada
                return {
                    "respuesta": (
                        f"Entendido. Tu reclamo parece ser sobre **{categoria_adivinada}**. "
                        f"Para continuar, por favor indicame la **dirección exacta** del problema.\n\n"
                        f"{EJEMPLO_DIRECCION}"
                    )
                }
            else:
                logger.info("[ReclamoHandler] Nivel 1 falló. Pasando a Nivel 2: Sugerencias con IA.")
                sugerencias = sugerir_categorias_relevantes(pregunta)
                if sugerencias:
                    logger.info(f"[ReclamoHandler] IA sugirió: {sugerencias}")
                    botones_sugeridos = [{"texto": sug} for sug in sugerencias]
                    botones_sugeridos.append({"texto": "Otro motivo"})
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                    return {"respuesta": "Entendido. ¿Cuál de estas opciones se ajusta más a tu problema?", "botones": botones_sugeridos}
                else:
                    logger.info("[ReclamoHandler] La IA no encontró sugerencias. Mostrando lista completa como fallback.")
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                    return {"respuesta": "Seleccioná la categoría que mejor describa tu reclamo:", "botones": BOTONES_TODAS_CATEGORIAS}
        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            if es_pregunta_nueva(pregunta, "una categoría de reclamo"):
                memoria.clear(); return None
            if normalizar_texto(pregunta) == "otro motivo":
                logger.info("[ReclamoHandler] Usuario eligió 'Otro motivo', mostrando lista completa.")
                return {"respuesta": "Elegí una categoría de la lista general:", "botones": BOTONES_TODAS_CATEGORIAS}
            categoria_final = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_final == "Otros":
                categoria_final = pregunta.strip().capitalize()
            logger.info(f"[ReclamoHandler] Categoría final seleccionada/mapeada: {categoria_final}")
            memoria['categoria_reclamo'] = categoria_final
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_DIRECCION_RECLAMO
            return {
                "respuesta": (
                    f"Perfecto, categoría: **{categoria_final}**. "
                    f"Ahora, por favor, indicame la **dirección completa** donde ocurre el problema.\n\n"
                    f"{EJEMPLO_DIRECCION}"
                )
            }
        if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            memoria['direccion_reclamo'] = pregunta
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_NOMBRE_VECINO
            return {"respuesta": "¡Gracias! Ahora, por favor, necesito tu **nombre completo**."}
        if estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            memoria['nombre_vecino'] = pregunta
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_TELEFONO_VECINO
            return {"respuesta": f"Gracias, {pregunta}. Por último, dejame tu **número de teléfono** (con código de área)."}
        if estado == ConversationState.ESPERANDO_TELEFONO_VECINO:
            memoria['telefono_vecino'] = pregunta.strip()
            detalles = self.build_detalles_memoria(memoria)
            categoria = memoria.get('categoria_reclamo', 'General')
            nombre = memoria.get('nombre_vecino', '')
            telefono_raw = memoria.get('telefono_vecino', '')
            telefono_limpio = re.sub(r'\D', '', telefono_raw)
            if not telefono_limpio.startswith('+') and len(telefono_limpio) > 8:
                telefono_e164 = '+549' + telefono_limpio
            else:
                telefono_e164 = telefono_limpio
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="municipio",
                ticket_data={"asunto": f"Reclamo de {categoria}", "categoria": categoria, "detalles": detalles, "user_id": self.context.get("user_id")}
            )
            if ticket:
                enviar_notificacion_whatsapp_con_plantilla(telefono_e164, nombre, ticket.nro_ticket, categoria)
                enviar_notificacion_sms(telefono_e164, f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado.")
                memoria.clear()
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. Te mantenemos al tanto del progreso 🙌"}
            else:
                memoria.clear()
                return {"respuesta": "Disculpá, hubo un problema técnico y no pudimos generar tu reclamo. Probá de nuevo más tarde o comunicate al 0800-MUNICIPIO."}
        return None

class TramitesHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        intencion = self.context.get('intencion')
        if intencion == 'consultar_tramite' and not estado:
            memoria.clear()
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_TRAMITE
            return {
                "respuesta": "¡Claro! Te puedo ayudar con info sobre la Licencia de Conducir u otros trámites.",
                "botones": [
                    {"texto": "Licencia de Conducir"},
                    {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"}
                ]
            }
        if estado == ConversationState.ESPERANDO_SELECCION_TRAMITE:
            if es_pregunta_nueva(pregunta, "una opción de trámite"):
                memoria.clear(); return None
            if "licencia" in normalizar_texto(pregunta):
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA
                return {
                    "respuesta": (
                        "Para la Licencia de Conducir necesitás: DNI actualizado, no tener multas, y hacer el curso de seguridad vial."
                    ),
                    "botones": [
                        {"texto": "Sacar Turno", "url": "https://tlc.mendoza.gov.ar/turnos"},
                        {"texto": "¿Dónde hacer el curso?"}
                    ]
                }
            else:
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_DETALLE_TRAMITE
                return {
                    "respuesta": "Contame sobre qué trámite o consulta puntual querés información, así te ayudo mejor."
                }
        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            if es_pregunta_nueva(pregunta, "una pregunta sobre el curso de licencia"):
                memoria.clear(); return None
            # Busca en las FAQs antes de contestar fijo:
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                return {"respuesta": respuesta_faq}
            memoria.clear()
            return {"respuesta": "El curso de seguridad vial es online en la web de la Agencia Nacional de Seguridad Vial o presencialmente en el Centro de Emisión de Licencias de tu municipio."}
        if estado == ConversationState.ESPERANDO_DETALLE_TRAMITE:
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                return {"respuesta": respuesta_faq}
            memoria.clear()
            return {"respuesta": "¡Listo! Si es sobre otro trámite, avisame cuál y te paso la info o el link directo 😉"}
        return None

def buscar_en_faqs(pregunta, tramite):
    """Busca en mini-faqs por palabra clave."""
    if tramite not in MINI_FAQ_TRAMITES:
        return None
    pregunta_norm = normalizar_texto(pregunta)
    for item in MINI_FAQ_TRAMITES[tramite]:
        if any(palabra in pregunta_norm for palabra in item["q"].split()):
            return item["a"]
    return None

class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        intencion = self.context.get('intencion')
        if intencion == 'consultar_impuestos':
            self.context.get('contexto_municipio', {}).clear()
            return {
                "respuesta": "Podés consultar tus impuestos, descargar boletas y pagar online en Rentas.",
                "botones": [{"texto": "Ir a Rentas", "url": "https://rentas.juninmendoza.gov.ar/"}]
            }
        return None

class GeneralHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        logger.info("[GeneralHandler] Consulta general con contexto de DB.")
        user_obj = self.context.get("user_obj")
        memoria = self.context.get("contexto_municipio", {})
        # Toma el contexto actual (ej. Licencia de Conducir)
        estado = memoria.get('estado_conversacion')
        if estado in [ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA, ConversationState.ESPERANDO_DETALLE_TRAMITE]:
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                return {"respuesta": respuesta_faq}
        # Handler general normal:
        if not user_obj:
            return None
        contexto_scraped = ""
        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            textos_relevantes = [json.loads(item.datos_json).get("contenido", "") for item in contenidos if json.loads(item.datos_json).get("tipo") == "contenido_general"]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            if not contexto_scraped:
                contexto_scraped = "No hay información de contexto disponible para esta consulta."
        except Exception as e:
            logger.error(f"[GeneralHandler] Error al obtener contexto de la DB: {e}")
            contexto_scraped = "Hubo un error al cargar la información de contexto."
        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(contexto_scraped=contexto_scraped, pregunta_usuario=pregunta)
        respuesta_llm = get_cohere_response(message=prompt_final, preamble="Eres un asistente municipal que responde basado en información oficial.")
        # Fallback mucho más humano:
        if not respuesta_llm or "no tengo información específica" in respuesta_llm.lower():
            return {
                "respuesta": (
                    "No encontré la respuesta exacta a tu consulta, pero podés contactarnos directo al WhatsApp del municipio, o hacer otra pregunta y te ayudo al toque. "
                    "Recordá que en la web oficial también hay info útil y links rápidos para cualquier trámite o consulta."
                ),
                "botones": [
                    {"texto": "Ir a la Web", "url": "https://www.juninmendoza.gov.ar/"},
                    {"texto": "Enviar WhatsApp", "url": "https://wa.me/542634612777"}
                ]
            }
        return {"respuesta": respuesta_llm}

class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if not self.context.get("user_id"):
            return {
                "respuesta": "Para darte una mejor atención personalizada, por favor registrate o iniciá sesión. Así podés hacer reclamos y recibir respuestas oficiales del municipio.",
                "botones": [
                    {"texto": "Iniciar Sesión", "url": "/login"},
                    {"texto": "Registrarme Gratis", "url": "/register"}
                ]
            }
        return None

def crear_prompt_decision_herramienta(pregunta_usuario: str) -> str:
    descripcion_herramientas_json = {}
    for nombre, detalles in TOOL_REGISTRY.items():
        descripcion_herramientas_json[nombre] = {
            "descripcion": detalles["descripcion"],
            "parametros": detalles["parametros"]
        }
    prompt = f"""
Tu única tarea es actuar como un despachador de herramientas inteligente. Analiza la PREGUNTA DEL USUARIO y decidí si alguna de las HERRAMIENTAS DISPONIBLES puede resolverla.
HERRAMIENTAS DISPONIBLES:
{json.dumps(descripcion_herramientas_json, indent=2)}
PREGUNTA DEL USUARIO: "{pregunta_usuario}"
INSTRUCCIONES DE RESPUESTA:
- Si la pregunta coincide con una herramienta y tiene los parámetros necesarios, respondé SOLO con un objeto JSON: {{"herramienta": "nombre_herramienta", "parametros": {{"nombre_param": "valor_extraido"}}}}.
- Si la pregunta coincide pero FALTAN PARÁMETROS, respondé SOLO con un JSON que indique qué falta: {{"herramienta": "nombre_herramienta", "faltan_parametros": ["nombre_param"]}}.
- Si no se puede resolver con ninguna herramienta, respondé SOLO con la palabra: null.
"""
    return prompt

class ToolHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if memoria.get('estado_conversacion') == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta, "una dirección"):
                memoria.clear(); return None
            memoria.clear()
            return {
                "respuesta": (
                    f"{consultar_recoleccion_por_direccion(direccion=pregunta)}\n\n"
                    f"Si necesitás consultar otra dirección, recordá escribir el formato: {EJEMPLO_DIRECCION}"
                )
            }
        if memoria.get('estado_conversacion'):
            return None
        prompt = crear_prompt_decision_herramienta(pregunta)
        try:
            respuesta_llm_str = get_cohere_response(message=prompt, preamble="Eres un experto en decidir si una pregunta requiere una herramienta. Responde solo con JSON o 'null'.")
            if not respuesta_llm_str or respuesta_llm_str.strip().lower() == "null":
                return None
            decision = json.loads(respuesta_llm_str)
            nombre_herramienta = decision.get("herramienta")
            if not nombre_herramienta or nombre_herramienta not in TOOL_REGISTRY:
                return None
            if "faltan_parametros" in decision:
                param_faltante = decision["faltan_parametros"][0]
                if param_faltante == "direccion":
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_PARAM_RECOLECCION
                    return {
                        "respuesta": (
                            "¡Por supuesto! Decime la dirección completa donde querés consultar el servicio municipal.\n\n"
                            f"{EJEMPLO_DIRECCION}"
                        )
                    }
            elif "parametros" in decision:
                parametros = decision["parametros"]
                funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]["funcion"]
                logger.info(f"[ToolHandler] Ejecutando herramienta '{nombre_herramienta}' con parámetros: {parametros}")
                resultado = funcion_a_ejecutar(**parametros)
                try:
                    resultado_dict = json.loads(resultado)
                    return resultado_dict
                except (json.JSONDecodeError, TypeError):
                    return {"respuesta": resultado}
        except (json.JSONDecodeError, TypeError, Exception) as e:
            logger.error(f"[ToolHandler] Error procesando decisión de herramienta: {e}", exc_info=True)
            return None
        palabras_clave_recoleccion = ["basurero", "recoleccion", "residuos", "basura"]
        if any(palabra in normalizar_texto(pregunta) for palabra in palabras_clave_recoleccion):
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_PARAM_RECOLECCION
            return {
                "respuesta": (
                    "Para darte el horario de recolección, necesito la dirección completa.\n\n"
                    f"{EJEMPLO_DIRECCION}"
                )
            }
        return None

class HumanEscalationHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            logger.info(f"[HumanEscalationHandler] El usuario {self.context.get('user_id')} solicita un agente.")
            ticket_data = {
                "asunto": "Solicitud de Chat en Vivo",
                "categoria": "Atención en Vivo",
                "detalles": f"El vecino solicitó atención en vivo con el mensaje: '{pregunta}'",
                "user_id": self.context.get("user_id"),
                "estado": "esperando_agente_en_vivo"
            }
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
            if not sala_de_chat:
                return {"respuesta": "Disculpá, hubo un problema al conectar con un agente. Probá más tarde o comunicate directo al municipio."}
            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id,
                tipo_ticket="municipio",
                comentario_data={"comentario": pregunta, "es_admin": False, "user_id": self.context.get("user_id")}
            )
            logger.info(f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada.")
            respuesta_al_vecino = (
                f"¡Entendido! Abrí una sala de chat directa con nuestro equipo.\n"
                f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. "
                "Aguardá un momento y no cierres esta ventana: un agente se conecta a la brevedad."
            )
            self.context.get('contexto_municipio', {}).clear()
            return {"respuesta": respuesta_al_vecino, "ticket_id": sala_de_chat.id}
        return None

def serializar_enum(obj):
    if isinstance(obj, Enum):
        return obj.name
    elif isinstance(obj, dict):
        return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serializar_enum(v) for v in obj]
    else:
        return obj

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    estado_guardado = contexto_municipio.get('estado_conversacion')
    if estado_guardado and isinstance(estado_guardado, str):
        try:
            contexto_municipio['estado_conversacion'] = ConversationState[estado_guardado]
        except KeyError:
            logger.warning(f"Se encontró un estado inválido en el contexto: {estado_guardado}")
            contexto_municipio['estado_conversacion'] = None
    context = {
        "contexto_municipio": contexto_municipio,
        "user_obj": user_obj,
        "user_id": getattr(user_obj, "id", None),
        "intencion": None
    }
    estado_antes = contexto_municipio.get('estado_conversacion')
    handler_chain = [
        GreetingHandler,
        ToolHandler,
        HumanEscalationHandler,
        IntentClassifierHandler,
        TicketStatusHandler,
        ReclamoHandler,
        TramitesHandler,
        ImpuestosHandler,
        GeneralHandler,
        EngancheAnonimoMunicipioHandler
    ]
    respuesta_final = None
    for handler_class in handler_chain:
        try:
            handler_instance = handler_class(context)
            respuesta_parcial = handler_instance.handle(pregunta)
            if respuesta_parcial:
                respuesta_final = respuesta_parcial
                break
        except Exception as e:
            logger.error(f"Error en handler {handler_class.__name__}: {e}", exc_info=True)
    if not respuesta_final:
        if not context.get("user_id"):
            respuesta_final = EngancheAnonimoMunicipioHandler(context).handle(pregunta)
        else:
            respuesta_final = {"respuesta": "Disculpá, no entendí tu consulta. Probá reformular la pregunta o elegí una opción del menú principal."}
    estado_despues = contexto_municipio.get('estado_conversacion')
    texto_respuesta = respuesta_final.get('respuesta', '')
    FRASES_EXITO = [
        "Tu reclamo fue generado", "¡Muchas gracias por tu calificación!",
        "Dejaré el ticket abierto", "El curso de seguridad vial es online",
        "He abierto una sala de chat directa", "Tu número de chat es"
    ]
    es_cierre_flujo = any(frase in texto_respuesta for frase in FRASES_EXITO)
    if estado_antes and not estado_despues and texto_respuesta and not es_cierre_flujo:
        mensaje_transicion = "Entendido, cambiemos de tema. Sobre tu nueva consulta:\n\n"
        respuesta_final['respuesta'] = mensaje_transicion + texto_respuesta
    if "[nombre_vecino]" in respuesta_final.get('respuesta', ''):
        nombre_vecino_memoria = contexto_municipio.get('nombre_vecino', 'vecino')
        respuesta_final['respuesta'] = respuesta_final['respuesta'].replace("[nombre_vecino]", nombre_vecino_memoria)
    contexto_para_guardar = serializar_enum(context["contexto_municipio"])
    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_para_guardar}
    }

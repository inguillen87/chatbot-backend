import logging
import re
import json
import os
from enum import Enum, auto

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

MINI_FAQ_TRAMITES = {
    "licencia_de_conducir": [
        {"q": "cuánto cuesta", "a": "El costo de la Licencia depende de la categoría. Consultalo en la web oficial del municipio o en mesa de entrada."},
        {"q": "pago multa", "a": "Para sacar o renovar tu Licencia necesitás no tener multas impagas. Consultá y pagalas en Rentas: https://rentas.juninmendoza.gov.ar/"},
        {"q": "vencimiento", "a": "La fecha de vencimiento y los requisitos están en el reverso de tu Licencia o en la web del municipio."},
        {"q": "curso", "a": "El curso de seguridad vial se hace online (Agencia Nacional de Seguridad Vial) o presencial en el Centro de Licencias."},
        {"q": "requisitos", "a": "DNI actualizado, no tener multas, hacer el curso y, si corresponde, apto médico."},
        {"q": "turno", "a": "Solicitá turno en: https://tlc.mendoza.gov.ar/turnos"}
    ]
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
        logger.error("[NOTIFICACION WHATSAPP] Faltan credenciales de Twilio.")
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
        logger.info(f"[NOTIFICACION WHATSAPP] Plantilla enviada, SID: {message.sid}")
    except Exception as e:
        logger.error(f"[NOTIFICACION WHATSAPP] Error: {e}", exc_info=True)

def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
    prompt = f"""
    Analiza la RESPUESTA DEL USUARIO. El chatbot esperaba algo relacionado a: '{tipo_esperado}'.
    RESPUESTA DEL USUARIO: "{texto_usuario}"
    Si responde lo que esperabas, contestá 'RESPUESTA_VALIDA'.
    Si cambia de tema, contestá 'PREGUNTA_NUEVA'.
    """
    try:
        decision = get_cohere_response(message=prompt, preamble="Sos un clasificador. Solo respondé 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.")
        logger.info(f"[Guardián de Flujo] Decisión: {decision.strip()}")
        return "PREGUNTA_NUEVA" in decision
    except Exception as e:
        logger.error(f"[Guardián de Flujo] Error: {e}")
        return False

PROMPT_MUNICIPIO_CON_CONTEXTO = """
Sos el asistente digital del municipio. Respondé la PREGUNTA DEL USUARIO usando solo la INFORMACIÓN DE CONTEXTO.
Si no tenés info suficiente, decilo y sugerí contactar al municipio.
--- CONTEXTO ---
{contexto_scraped}
-----------------
PREGUNTA: "{pregunta_usuario}"
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
                return {
                    "respuesta": (
                        "¡Hola! 👋 Soy el asistente virtual del Municipio. "
                        "Consultame trámites, reclamos, turnos o lo que necesites. ¿En qué te ayudo hoy?"
                    )
                }
        return None

class IntentClassifierHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if not memoria.get('estado_conversacion'):
            self.context['intencion'] = _clasificar_intencion_con_llm(pregunta)
        else:
            self.context['intencion'] = 'continuar_flujo'
        logger.info(f"[MUNICIPIO] Intención: {self.context.get('intencion')}")
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
                return {"respuesta": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?"}
            else:
                memoria.clear()
                return {"respuesta": "Dejamos el ticket abierto para seguimiento del equipo. ¿Necesitás algo más?"}
        elif estado_conversacion == ConversationState.ESPERANDO_CALIFICACION:
            if es_pregunta_nueva(pregunta, "una calificación del 1 al 5"):
                memoria.clear(); return None
            ticket_id = memoria.get('ticket_id_activo')
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="municipio", comentario_data={"comentario": f"Calificación: {pregunta}", "es_admin": False})
            memoria.clear()
            return {"respuesta": "¡Gracias por tu calificación! ¿Te ayudo con otro trámite o reclamo?", "botones": [
                {"texto": "Nuevo reclamo"},
                {"texto": "Consultar otro ticket"},
                {"texto": "Hablar con un agente"}
            ]}
        if self.context.get('intencion') == 'consultar_estado_ticket':
            match = re.search(r'\d{5,}', pregunta)
            if not match: return {"respuesta": "Decime el número de ticket que querés consultar."}
            ticket = MunicipioTicket.query.filter_by(nro_ticket=int(match.group(0))).first()
            if not ticket: return {"respuesta": f"No encontré ticket {match.group(0)}."}
            respuesta = f"El ticket **M-{ticket.nro_ticket}** sobre '{ticket.asunto}' está en estado: **{ticket.estado}**."
            ultimo_comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first()
            if ultimo_comentario:
                respuesta += f"\nÚltima actualización: *{ultimo_comentario.comentario}*"
                if ticket.estado == "en_proceso":
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                    memoria['ticket_id_activo'] = ticket.id
                    respuesta += "\n¿Se resolvió tu problema?"
                    return {"respuesta": respuesta, "botones": [
                        {"texto": "Sí, solucionado"}, {"texto": "No, aún no"}
                    ]}
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

        if estado in [
            ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
            ConversationState.ESPERANDO_DIRECCION_RECLAMO,
            ConversationState.ESPERANDO_NOMBRE_VECINO,
            ConversationState.ESPERANDO_TELEFONO_VECINO,
        ] and es_pregunta_nueva(pregunta, "el dato solicitado"):
            memoria.clear()
            return {
                "respuesta": (
                    "Veo que cambiaste de tema. No hay problema, "
                    "¿con qué otro trámite o consulta te ayudo?"
                ),
                "botones": [
                    {"texto": "Nuevo reclamo"},
                    {"texto": "Consultar estado de ticket"},
                    {"texto": "Hablar con un agente"}
                ]
            }

        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            memoria.clear()
            memoria['pregunta_original'] = pregunta
            categoria_adivinada = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_adivinada != "Otros":
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_DIRECCION_RECLAMO
                memoria['categoria_reclamo'] = categoria_adivinada
                return {
                    "respuesta": (
                        f"Ok, el reclamo es sobre **{categoria_adivinada}**. ¿Me pasás la dirección exacta del problema?\n{EJEMPLO_DIRECCION}"
                    )
                }
            sugerencias = sugerir_categorias_relevantes(pregunta)
            if sugerencias:
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                return {
                    "respuesta": "¿Cuál opción representa mejor tu problema?",
                    "botones": [{"texto": s} for s in sugerencias] + [{"texto": "Otro motivo"}]
                }
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
            return {
                "respuesta": "Seleccioná la categoría que mejor describa tu reclamo:",
                "botones": BOTONES_TODAS_CATEGORIAS + [{"texto": "Otro motivo"}]
            }

        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            categoria_final = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_final == "Otros":
                categoria_final = pregunta.strip().capitalize()
            memoria['categoria_reclamo'] = categoria_final
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_DIRECCION_RECLAMO
            return {
                "respuesta": (
                    f"Perfecto, categoría: **{categoria_final}**. ¿La dirección exacta?\n{EJEMPLO_DIRECCION}"
                )
            }

        if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            memoria['direccion_reclamo'] = pregunta
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_NOMBRE_VECINO
            return {"respuesta": "¡Gracias! Ahora tu nombre completo."}

        if estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            memoria['nombre_vecino'] = pregunta
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_TELEFONO_VECINO
            return {"respuesta": f"Gracias, {pregunta}. ¿Me pasás tu teléfono con código de área?"}

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
            memoria.clear()
            if ticket:
                enviar_notificacion_whatsapp_con_plantilla(telefono_e164, nombre, ticket.nro_ticket, categoria)
                enviar_notificacion_sms(telefono_e164, f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado.")
                return {
                    "respuesta": (
                        f"¡Listo! Reclamo registrado (**M-{ticket.nro_ticket}**). ¿Hacés otro reclamo, consultás un ticket o hablás con un agente?"
                    ),
                    "botones": [
                        {"texto": "Nuevo reclamo"},
                        {"texto": "Consultar estado de ticket"},
                        {"texto": "Hablar con un agente"}
                    ]
                }
            return {
                "respuesta": "No se pudo generar el reclamo. Probá de nuevo o llamanos al 0800-MUNICIPIO.",
                "botones": [{"texto": "Hablar con un agente"}]
            }
        return None

def buscar_en_faqs(pregunta, tramite):
    if tramite not in MINI_FAQ_TRAMITES:
        return None
    pregunta_norm = normalizar_texto(pregunta)
    for item in MINI_FAQ_TRAMITES[tramite]:
        if any(palabra in pregunta_norm for palabra in item["q"].split()):
            return item["a"]
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
                "respuesta": "¿Sobre qué trámite necesitás información? Te ayudo con Licencia, pagos, etc.",
                "botones": [
                    {"texto": "Licencia de Conducir"},
                    {"texto": "Pagos y Deudas"},
                    {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"},
                ]
            }

        if estado == ConversationState.ESPERANDO_SELECCION_TRAMITE:
            if es_pregunta_nueva(pregunta, "una opción de trámite"):
                memoria.clear()
                return {
                    "respuesta": "¿Sobre qué otra gestión necesitás ayuda?",
                    "botones": [
                        {"texto": "Licencia de Conducir"},
                        {"texto": "Pagos y Deudas"},
                        {"texto": "Más Trámites"},
                    ]
                }
            if "licencia" in normalizar_texto(pregunta):
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA
                return {
                    "respuesta": (
                        "Para la Licencia: DNI actualizado, no tener multas y hacer el curso de seguridad vial."
                    ),
                    "botones": [
                        {"texto": "Sacar Turno", "url": "https://tlc.mendoza.gov.ar/turnos"},
                        {"texto": "¿Dónde hacer el curso?"}
                    ]
                }
            else:
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_DETALLE_TRAMITE
                return {
                    "respuesta": "¿Sobre qué trámite puntual querés información?",
                }
        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            if es_pregunta_nueva(pregunta, "una pregunta sobre el curso de licencia"):
                memoria.clear()
                return {
                    "respuesta": "¿Sobre qué otro trámite querés info?",
                    "botones": [
                        {"texto": "Licencia de Conducir"}
                    ]
                }
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                return {
                    "respuesta": respuesta_faq
                }
            memoria.clear()
            return {
                "respuesta": "El curso se hace online (Agencia Nacional de Seguridad Vial) o presencial en el municipio."
            }

        if estado == ConversationState.ESPERANDO_DETALLE_TRAMITE:
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                return {
                    "respuesta": respuesta_faq
                }
            memoria.clear()
            return {
                "respuesta": "Listo. Si es sobre otro trámite, decime cuál y te paso la info."
            }
        return None

class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        intencion = self.context.get('intencion')
        if intencion == 'consultar_impuestos':
            self.context.get('contexto_municipio', {}).clear()
            return {
                "respuesta": "Consultá impuestos, descargá boletas y pagá online en Rentas.",
                "botones": [{"texto": "Ir a Rentas", "url": "https://rentas.juninmendoza.gov.ar/"}]
            }
        return None

class GeneralHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        logger.info("[GeneralHandler] Consulta general con contexto de DB.")
        user_obj = self.context.get("user_obj")
        memoria = self.context.get("contexto_municipio", {})
        estado = memoria.get('estado_conversacion')

        if estado in [ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA, ConversationState.ESPERANDO_DETALLE_TRAMITE]:
            respuesta_faq = buscar_en_faqs(pregunta, "licencia_de_conducir")
            if respuesta_faq:
                memoria.clear()
                return {"respuesta": respuesta_faq}
            memoria.clear()
            return {"respuesta": "¿Sobre qué más te puedo ayudar?"}

        if not user_obj:
            return None

        contexto_scraped = ""
        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            textos_relevantes = [
                json.loads(item.datos_json).get("contenido", "") 
                for item in contenidos 
                if json.loads(item.datos_json).get("tipo") == "contenido_general"
            ]
            contexto_scraped = " ".join(filter(None, textos_relevantes))
            if not contexto_scraped:
                contexto_scraped = "No hay información disponible para esta consulta."
        except Exception as e:
            logger.error(f"[GeneralHandler] Error: {e}")
            contexto_scraped = "Hubo un error al cargar la información."

        prompt_final = PROMPT_MUNICIPIO_CON_CONTEXTO.format(contexto_scraped=contexto_scraped, pregunta_usuario=pregunta)
        respuesta_llm = get_cohere_response(message=prompt_final, preamble="Sos un asistente municipal que responde basado en info oficial.")

        if not respuesta_llm or "no tengo información específica" in respuesta_llm.lower():
            return {
                "respuesta": (
                    "No encontré respuesta exacta, pero podés contactarnos por WhatsApp o hacer otra consulta."
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
                "respuesta": (
                    "¡Hola! Para poder darte atención completa (reclamos, seguimiento oficial), registrate o iniciá sesión. "
                    "¿Querés seguir como invitado? Solo podés consultar info general o iniciar sesión para más funciones."
                ),
                "botones": [
                    {"texto": "Iniciar sesión", "url": "/login"},
                    {"texto": "Registrarme Gratis", "url": "/register"},
                    {"texto": "Consultar info general"},
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
Sos un despachador de herramientas inteligente. Analizá la PREGUNTA DEL USUARIO y decidí si alguna herramienta puede resolverla.
HERRAMIENTAS DISPONIBLES:
{json.dumps(descripcion_herramientas_json, indent=2)}
PREGUNTA: "{pregunta_usuario}"
- Si coincide y hay parámetros, devolvé JSON: {{"herramienta": "nombre_herramienta", "parametros": {{"nombre_param": "valor"}}}}
- Si faltan parámetros, devolvé JSON: {{"herramienta": "nombre_herramienta", "faltan_parametros": ["nombre_param"]}}
- Si no aplica, devolvé 'null'.
"""
    return prompt

class ToolHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        if memoria.get('estado_conversacion') == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta, "una dirección"):
                memoria.clear()
                return {
                    "respuesta": (
                        "Noté que cambiaste de tema. Si querés volver a consultar la recolección, decime la dirección. "
                        "Si preferís hacer otra consulta, decime cómo te ayudo."
                    ),
                    "botones": [
                        {"texto": "Consultar recolección"},
                        {"texto": "Hacer un reclamo"},
                        {"texto": "Hablar con un agente"}
                    ]
                }
            memoria.clear()
            resultado = consultar_recoleccion_por_direccion(direccion=pregunta)
            if not resultado or "No encontrado" in resultado:
                return {
                    "respuesta": (
                        "No encontré información de recolección para esa dirección. Revisá si está bien escrita, o consultá directo al municipio."
                    ),
                    "botones": [
                        {"texto": "Volver a intentar"},
                        {"texto": "Hablar con un agente"}
                    ]
                }
            return {
                "respuesta": f"{resultado}\n¿Consultás otra dirección o hacés otro trámite?",
                "botones": [
                    {"texto": "Consultar otra dirección"},
                    {"texto": "Hacer un reclamo"}
                ]
            }
        if memoria.get('estado_conversacion'):
            return None

        prompt = crear_prompt_decision_herramienta(pregunta)
        try:
            respuesta_llm_str = get_cohere_response(
                message=prompt,
                preamble="Sos experto en decidir si una pregunta requiere una herramienta. Respondé JSON o 'null'."
            )
            if not respuesta_llm_str or respuesta_llm_str.strip().lower() == "null":
                return {
                    "respuesta": (
                        "No tengo una herramienta directa para esa consulta, pero decime más detalles o elegí otra opción:"
                    ),
                    "botones": [
                        {"texto": "Hacer un reclamo"},
                        {"texto": "Consultar estado de un trámite"},
                        {"texto": "Hablar con un agente"}
                    ]
                }
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
                            "¡Perfecto! Decime la dirección completa donde querés consultar el servicio municipal.\n"
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
                    return {
                        "respuesta": resultado
                    }
        except Exception as e:
            logger.error(f"[ToolHandler] Error: {e}", exc_info=True)
            return {
                "respuesta": "Hubo un error técnico. Probá de nuevo o comunicate con el municipio.",
                "botones": [{"texto": "Hablar con un agente"}]
            }
        palabras_clave_recoleccion = ["basurero", "recoleccion", "residuos", "basura"]
        if any(palabra in normalizar_texto(pregunta) for palabra in palabras_clave_recoleccion):
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_PARAM_RECOLECCION
            return {
                "respuesta": (
                    "¿La dirección para consultar el horario de recolección?\n"
                    f"{EJEMPLO_DIRECCION}"
                )
            }
        return None

class HumanEscalationHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('intencion') == 'hablar_con_agente':
            logger.info(f"[HumanEscalationHandler] Usuario {self.context.get('user_id')} pide agente.")
            ticket_data = {
                "asunto": "Solicitud de Chat en Vivo",
                "categoria": "Atención en Vivo",
                "detalles": f"El vecino solicitó chat en vivo: '{pregunta}'",
                "user_id": self.context.get("user_id"),
                "estado": "esperando_agente_en_vivo"
            }
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)
            if not sala_de_chat:
                return {"respuesta": "No pudimos conectar con un agente. Probá más tarde o llamá al municipio."}
            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id,
                tipo_ticket="municipio",
                comentario_data={"comentario": pregunta, "es_admin": False, "user_id": self.context.get("user_id")}
            )
            logger.info(f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada.")
            self.context.get('contexto_municipio', {}).clear()
            return {"respuesta": (
                f"¡Listo! Abrimos una sala de chat directa con el equipo.\n"
                f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. Esperá, un agente se conecta en breve."
            ), "ticket_id": sala_de_chat.id}
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

BOTONES_COMANDOS_MUNICIPIO = {
    "Hacer un reclamo": "iniciar_reclamo",
    "Consultar estado de un trámite": "consultar_estado_ticket",
    "Consultar estado de ticket": "consultar_estado_ticket",
    "Consultar otro ticket": "consultar_estado_ticket",
    "Hablar con un agente": "hablar_con_agente",
    "Nuevo reclamo": "iniciar_reclamo",
}

def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})
    estado_guardado = contexto_municipio.get('estado_conversacion')
    if estado_guardado and isinstance(estado_guardado, str):
        try:
            contexto_municipio['estado_conversacion'] = ConversationState[estado_guardado]
        except KeyError:
            logger.warning(f"Estado inválido en el contexto: {estado_guardado}")
            contexto_municipio['estado_conversacion'] = None
    context = {
        "contexto_municipio": contexto_municipio,
        "user_obj": user_obj,
        "user_id": getattr(user_obj, "id", None),
        "intencion": None
    }
    # --- INTERCEPTA COMANDOS DE BOTONES ---
    comando = BOTONES_COMANDOS_MUNICIPIO.get(pregunta.strip())
    if comando:
        context['intencion'] = comando
        if comando == "iniciar_reclamo":
            return ReclamoHandler(context).handle("Quiero hacer un reclamo")
        elif comando == "consultar_estado_ticket":
            return TicketStatusHandler(context).handle("Consultar estado de ticket")
        elif comando == "hablar_con_agente":
            return HumanEscalationHandler(context).handle("Hablar con un agente")
    # --- SIGUE EL FLUJO NORMAL ---
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
            respuesta_final = {"respuesta": "No entendí tu consulta. Reformulá la pregunta o elegí una opción."}
    estado_despues = contexto_municipio.get('estado_conversacion')
    texto_respuesta = respuesta_final.get('respuesta', '')
    FRASES_EXITO = [
        "Tu reclamo fue generado", "¡Gracias por tu calificación!",
        "Dejamos el ticket abierto", "El curso de seguridad vial es online",
        "Abrimos una sala de chat directa", "Tu número de chat es"
    ]
    es_cierre_flujo = any(frase in texto_respuesta for frase in FRASES_EXITO)
    if estado_antes and not estado_despues and texto_respuesta and not es_cierre_flujo:
        mensaje_transicion = "Entendido, cambiamos de tema. Sobre tu nueva consulta:\n\n"
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

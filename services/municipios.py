import logging
import re
import json
import os
from enum import Enum, auto

# Asumimos que estos módulos y modelos existen y son importables
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
from .logic import _clasificar_intencion_con_llm
from twilio.rest import Client
# Importamos TODAS las herramientas necesarias del archivo de herramientas
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes,
    normalizar_texto,
    KEYWORD_TO_CATEGORY_MAP
)

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

# --- Constantes y Configuración ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'
TWILIO_WHATSAPP_CONTENT_SID = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")

TODAS_LAS_CATEGORIAS_UNICAS = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values())))
BOTONES_TODAS_CATEGORIAS = [{"texto": cat} for cat in TODAS_LAS_CATEGORIAS_UNICAS]

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

def enviar_notificacion_whatsapp_con_plantilla(numero_destino: str, nombre: str, nro_ticket: str, categoria: str):
    """
    Envía una notificación por WhatsApp usando la plantilla de Twilio configurada.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER, TWILIO_WHATSAPP_CONTENT_SID]):
        logger.error("[NOTIFICACION WHATSAPP] Credenciales o Content SID de Twilio no configuradas.")
        return

    # Nos aseguramos que el número de teléfono esté en el formato E.164 que Twilio necesita.
    destinatario_whatsapp = f'whatsapp:{numero_destino}'
    
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # <<< CORRECCIÓN FINAL: Las variables ahora contienen solo los datos puros
        # para que coincidan 1 a 1 con la plantilla de la imagen.
        variables_plantilla = {
            '1': nombre,
            '2': f"M-{nro_ticket}", # Enviamos el número de ticket completo.
            '3': categoria
        }

        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=destinatario_whatsapp,
            content_sid=TWILIO_WHATSAPP_CONTENT_SID,
            content_variables=json.dumps(variables_plantilla)
        )
        logger.info(f"[NOTIFICACION WHATSAPP] Mensaje de plantilla enviado correctamente, SID: {message.sid}")
    except Exception as e:
        # Usamos exc_info=True para loguear el traceback completo del error.
        logger.error(f"[NOTIFICACION WHATSAPP] Error al enviar plantilla a {destinatario_whatsapp}: {e}", exc_info=True)

# --- Funciones de Ayuda y Prompts ---

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

# (Aquí se pueden agregar las funciones de notificación y herramientas si no están en su propio archivo)
PROMPT_MUNICIPIO_CON_CONTEXTO = """
Eres un asistente virtual experto del municipio. Tu deber es responder la PREGUNTA DEL USUARIO de manera precisa y amigable, utilizando únicamente la INFORMACIÓN DE CONTEXTO que te proporciono. No inventes información que no esté en el contexto. Si la respuesta no se encuentra en el contexto, indica amablemente que no tienes esa información específica y sugiere contactar a la municipalidad.
--- INFORMACIÓN DE CONTEXTO ---
{contexto_scraped}
---------------------------------
PREGUNTA DEL USUARIO: "{pregunta_usuario}"
Respuesta:
"""

# --- Definición de Handlers ---

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
                    "¡Hola! 👋 Soy tu asistente virtual del Municipio. "
                    "Estoy aquí para ayudarte de forma rápida y automática.\n\n"
                    "Puedes consultarme sobre trámites, servicios o iniciar un reclamo. "
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
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CONFIRMACION_CIERRE
                    memoria['ticket_id_activo'] = ticket.id
                    respuesta += "\n\n¿Tu problema fue solucionado con esta respuesta?"
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
                return {"respuesta": f"Entendido. Tu reclamo parece ser sobre **{categoria_adivinada}**. Para continuar, por favor indicame la **dirección exacta** del problema."}
            else:
                logger.info("[ReclamoHandler] Nivel 1 falló. Pasando a Nivel 2: Sugerencias con IA.")
                sugerencias = sugerir_categorias_relevantes(pregunta)
                
                if sugerencias:
                    logger.info(f"[ReclamoHandler] IA sugirió: {sugerencias}")
                    botones_sugeridos = [{"texto": sug} for sug in sugerencias]
                    botones_sugeridos.append({"texto": "Otro motivo"})
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                    return {"respuesta": "Entendido. Para clasificar mejor tu reclamo, ¿cuál de estas opciones se ajusta más a tu problema?", "botones": botones_sugeridos}
                else:
                    logger.info("[ReclamoHandler] La IA no encontró sugerencias. Mostrando lista completa como fallback.")
                    memoria['estado_conversacion'] = ConversationState.ESPERANDO_CATEGORIA_RECLAMO
                    return {"respuesta": "Entendido, vamos a iniciar tu reclamo. Por favor, selecciona la categoría que mejor lo describa:", "botones": BOTONES_TODAS_CATEGORIAS}

        if estado == ConversationState.ESPERANDO_CATEGORIA_RECLAMO:
            if es_pregunta_nueva(pregunta, "una categoría de reclamo"):
                memoria.clear(); return None
            
            if normalizar_texto(pregunta) == "otro motivo":
                logger.info("[ReclamoHandler] Usuario eligió 'Otro motivo', mostrando lista completa.")
                return {"respuesta": "De acuerdo, por favor selecciona una categoría de la lista general:", "botones": BOTONES_TODAS_CATEGORIAS}

            categoria_final = categorizar_reclamo_por_palabra_clave(pregunta)
            if categoria_final == "Otros":
                categoria_final = pregunta.strip().capitalize()

            logger.info(f"[ReclamoHandler] Categoría final seleccionada/mapeada: {categoria_final}")
            memoria['categoria_reclamo'] = categoria_final
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_DIRECCION_RECLAMO
            return {"respuesta": f"Perfecto, categoría: **{categoria_final}**. Ahora, indicame la **dirección completa** donde ocurre el problema."}

        if estado == ConversationState.ESPERANDO_DIRECCION_RECLAMO:
            memoria['direccion_reclamo'] = pregunta
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_NOMBRE_VECINO
            return {"respuesta": "¡Gracias! Ahora, por favor, necesito tu **nombre completo**."}

        if estado == ConversationState.ESPERANDO_NOMBRE_VECINO:
            memoria['nombre_vecino'] = pregunta
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_TELEFONO_VECINO
            return {"respuesta": f"Gracias, {pregunta}. Por último, déjame tu **número de teléfono** (con código de área) para poder contactarte si es necesario."}

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
                # Aquí irían las llamadas a las funciones de notificación
                enviar_notificacion_whatsapp_con_plantilla(telefono_e164, nombre, ticket.nro_ticket, categoria)
                enviar_notificacion_sms(telefono_e164, f"Hola {nombre}! Tu reclamo M-{ticket.nro_ticket} ({categoria}) fue generado.")
                memoria.clear()
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. Te mantendremos al tanto del progreso."}
            else:
                memoria.clear()
                return {"respuesta": "Disculpa, hubo un problema técnico y no pudimos generar tu reclamo. Por favor, intenta de nuevo más tarde."}
        return None

# <<< INICIO DE HANDLERS FALTANTES >>>

class TramitesHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        estado = memoria.get('estado_conversacion')
        intencion = self.context.get('intencion')

        if intencion == 'consultar_tramite' and not estado:
            memoria.clear()
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_TRAMITE
            return {"respuesta": "¡Claro! Te puedo ayudar con información sobre la Licencia de Conducir, o puedes explorar otros trámites municipales.", "botones": [{"texto": "Licencia de Conducir"}, {"texto": "Más Trámites", "url": "https://www.juninmendoza.gov.ar/tramites/"}]}

        if estado == ConversationState.ESPERANDO_SELECCION_TRAMITE:
            if es_pregunta_nueva(pregunta, "una opción de trámite"):
                memoria.clear(); return None
            
            if "licencia" in normalizar_texto(pregunta):
                memoria['estado_conversacion'] = ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA
                return {"respuesta": "Para la Licencia de Conducir necesitás: DNI actualizado, no tener multas, y hacer el curso de seguridad vial.", "botones": [{"texto": "Sacar Turno", "url": "https://tlc.mendoza.gov.ar/turnos"}, {"texto": "¿Dónde hacer el curso?"}]}
            else:
                # Si el usuario responde otra cosa, se asume que cambió de tema.
                memoria.clear()
                return None

        if estado == ConversationState.ESPERANDO_PREGUNTA_CURSO_LICENCIA:
            if es_pregunta_nueva(pregunta, "una pregunta sobre el curso de licencia"):
                memoria.clear(); return None
            
            memoria.clear()
            return {"respuesta": "El curso de seguridad vial es online en la web de la Agencia Nacional de Seguridad Vial o presencialmente en el Centro de Emisión de Licencias de tu municipio."}
        
        return None

class ImpuestosHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        # Aquí se podría desarrollar la lógica para consultar impuestos.
        # Por ahora, es un placeholder.
        intencion = self.context.get('intencion')
        if intencion == 'consultar_impuestos':
            # Se limpiaría la memoria para asegurar que no hay flujos activos.
            self.context.get('contexto_municipio', {}).clear()
            return {"respuesta": "Puedes consultar el estado de tus impuestos municipales, descargar boletas y pagar online a través de nuestro portal de Rentas.", "botones": [{"texto": "Ir a Rentas", "url": "https://rentas.juninmendoza.gov.ar/"}]}
        return None

class GeneralHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        logger.info("[GeneralHandler] Manejando como consulta general con contexto de DB.")
        user_obj = self.context.get("user_obj")
        
        # Este handler solo se activa si hay un usuario logueado.
        if not user_obj:
            return None

        contexto_scraped = ""
        try:
            # Busca en la DB la información scrapeada relevante para este usuario/municipio
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
        return {"respuesta": respuesta_llm}

class EngancheAnonimoMunicipioHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        # Este es el último handler en la cadena. Si ningún otro pudo responder
        # y el usuario no está logueado, se le ofrece registrarse.
        if not self.context.get("user_id"):
            return {
                "respuesta": "Para darte una mejor atención personalizada, por favor regístrate o inicia sesión. Así podrás hacer reclamos y recibir respuestas oficiales del municipio.",
                "botones": [{"texto": "Iniciar Sesión", "url": "/login"}, {"texto": "Registrarme Gratis", "url": "/register"}]
            }
        return None

# Pega esta clase completa en tu archivo municipios.py

class ToolHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        
        # Flujo para cuando ya se pidió un parámetro (dirección) y el usuario responde
        if memoria.get('estado_conversacion') == ConversationState.ESPERANDO_PARAM_RECOLECCION:
            if es_pregunta_nueva(pregunta, "una dirección"):
                memoria.clear()
                return None  # Permite que otro handler tome la nueva pregunta
            
            # Si el usuario da la dirección, se ejecuta la herramienta y se limpia el estado
            memoria['estado_conversacion'] = None
            return {"respuesta": consultar_recoleccion_por_direccion(direccion=pregunta)}
        
        # Si estamos en medio de otro flujo, este handler no debe actuar
        if memoria.get('estado_conversacion'):
            return None
            
        # --- Lógica principal para decidir si usar una herramienta ---
        
        # (Asegúrate de tener la función crear_prompt_decision_herramienta y TOOL_REGISTRY definidos)
        # Por ahora, esta parte la dejamos como la tenías, si quieres la podemos refinar luego.
        # prompt = crear_prompt_decision_herramienta(pregunta) 
        # respuesta_llm = get_cohere_response(...)
        
        # Simplificamos por ahora para que busque la intención de "recolección"
        palabras_clave_recoleccion = ["basurero", "recoleccion", "residuos", "basura"]
        if any(palabra in normalizar_texto(pregunta) for palabra in palabras_clave_recoleccion):
            # Si se menciona la recolección pero no se da una dirección, la pedimos.
            # (Aquí una lógica más avanzada con el LLM podría extraer la dirección si ya está presente)
            memoria['estado_conversacion'] = ConversationState.ESPERANDO_PARAM_RECOLECCION
            return {"respuesta": "Claro, para darte el horario de recolección, necesito la dirección completa, por favor."}

        return None
# <<< FIN DE HANDLERS FALTANTES >>>




def responder_municipio(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_municipio = contexto_previo.get(CONTEXTO_MUNICIPIO, {})

    # Corrección para cargar el estado Enum desde el contexto JSON
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

    estado_antes_de_procesar = contexto_municipio.get('estado_conversacion')

    # <<< CORRECCIÓN: Handler chain completo y en orden estratégico
    # Este es el "cerebro" del bot, decidiendo qué habilidad usar primero.
    handler_chain = [
        GreetingHandler,              # 1. Responde saludos básicos.
        ToolHandler,                  # 2. Revisa si puede usar una herramienta (¡muy importante!).
        HumanEscalationHandler,       # 3. Revisa si el usuario quiere hablar con una persona (¡clave!).
        IntentClassifierHandler,      # 4. Si no hay herramienta ni escalamiento, clasifica la intención general.
        TicketStatusHandler,          # 5. Si la intención es consultar ticket, maneja ese flujo.
        ReclamoHandler,               # 6. Si la intención es iniciar reclamo, maneja ese flujo.
        TramitesHandler,              # 7. Si es sobre trámites, maneja ese flujo.
        ImpuestosHandler,             # 8. Si es sobre impuestos, etc.
        GeneralHandler,               # 9. Si nada de lo anterior coincide, intenta una respuesta general.
        EngancheAnonimoMunicipioHandler # 10. Como último recurso, si es anónimo, le pide que se registre.
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
             respuesta_final = {"respuesta": "Disculpa, no entendí tu consulta. ¿Podrías intentar reformular tu pregunta?"}

    estado_despues_de_procesar = contexto_municipio.get('estado_conversacion')
    texto_respuesta = respuesta_final.get('respuesta', '')

    FRASES_EXITO = [
        "Tu reclamo fue generado", "¡Muchas gracias por tu calificación!",
        "Dejaré el ticket abierto", "El curso de seguridad vial es online",
        "He abierto una sala de chat directa"
    ]
    es_cierre_flujo = any(frase in texto_respuesta for frase in FRASES_EXITO)

    if estado_antes_de_procesar and not estado_despues_de_procesar and texto_respuesta and not es_cierre_flujo:
        mensaje_transicion = "Entendido, cambiemos de tema. Sobre tu nueva consulta:\n\n"
        respuesta_final['respuesta'] = mensaje_transicion + texto_respuesta

    if "[nombre_vecino]" in respuesta_final.get('respuesta', ''):
        nombre_vecino_memoria = contexto_municipio.get('nombre_vecino', 'vecino')
        respuesta_final['respuesta'] = respuesta_final['respuesta'].replace("[nombre_vecino]", nombre_vecino_memoria)

    # Corrección para guardar el estado Enum como texto en el JSON
    contexto_para_guardar = context["contexto_municipio"]
    if 'estado_conversacion' in contexto_para_guardar and isinstance(contexto_para_guardar['estado_conversacion'], ConversationState):
        contexto_para_guardar['estado_conversacion'] = contexto_para_guardar['estado_conversacion'].name

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_para_guardar}
    }
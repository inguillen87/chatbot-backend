import logging
import re
import json
import os
from models import MunicipioTicket, TicketComentario, db # Usando tus modelos
from services.cohere_ai import get_cohere_response
from services.ticket_service import servicio_tickets
# Importamos nuestra nueva y flamante herramienta
from .logic import _clasificar_intencion_con_llm 
from twilio.rest import Client # <<<<<<<<<<<<<< CORREGIDO: Importación de Client

# --- ¡CORRECTO! Importamos las funciones desde la caja de herramientas ---
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion, 
    categorizar_reclamo_por_palabra_clave
)

logger = logging.getLogger(__name__)
CONTEXTO_MUNICIPIO = "contexto_municipio"

# Variables de entorno para Twilio, se leen de os.environ
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER") 

class GreetingHandler(BaseMunicipioHandler):
    """
    Handler especializado en capturar el primer saludo de un usuario
    para darle la bienvenida y guiarlo, evitando escalados innecesarios.
    """
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        # Solo se activa si NO hay un estado de conversación previo
        if not memoria.get('estado_conversacion'):
            saludos = ['hola', 'buenos dias', 'buenas tardes', 'buenas noches', 'hey', 'que tal']
            pregunta_limpia = pregunta.strip().lower()

            # Verificamos si el mensaje es un saludo simple
            if pregunta_limpia in saludos:
                # Limpiamos la memoria por si quedó algo de una conversación anterior
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
        
        # Si no es un saludo inicial o ya hay una conversación, no hace nada.
        return None
    
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
    """
    Genera un prompt para el LLM dinámicamente a partir del TOOL_REGISTRY.
    """
    # Construcción dinámica de la descripción de herramientas
    descripcion_herramientas = "{\n"
    for nombre, detalles in TOOL_REGISTRY.items():
        descripcion_herramientas += f'  "{nombre}": {{\n'
        descripcion_herramientas += f'    "descripcion": "{detalles["descripcion"]}",\n'
        descripcion_herramientas += f'    "parametros": {json.dumps(detalles["parametros"])}\n'
        descripcion_herramientas += '  },\n'
    descripcion_herramientas = descripcion_herramientas.rstrip(',\n') + "\n}"

    # Construcción dinámica de los ejemplos
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
   `{{"usar_herramienta": "nombre_de_la_herramienta", "parametros": {{"nombre_parametro": "valor_extraido"}}}}`
2. Si la pregunta coincide con una herramienta pero le FALTAN PARÁMETROS ESENCIALES, responde SÓLO con un objeto JSON con el formato:
   `{{"usar_herramienta": "nombre_de_la_herramienta", "faltan_parametros": ["nombre_parametro"]}}`
3. Si la pregunta NO coincide con ninguna herramienta o no es relevante para ellas, responde SÓLO con la palabra: `null`

Ejemplos:
{ejemplos_str}
- Pregunta: "necesito hacer un reclamo" -> Respuesta: null

Tu respuesta:
"""
    return prompt


# --- CLASES HANDLER (NUEVA Y EXISTENTES) ---

class BaseMunicipioHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta: str) -> dict | None: raise NotImplementedError

class ToolHandler(BaseMunicipioHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_municipio', {})
        
        # Si ya estamos esperando un parámetro para una herramienta, lo manejamos.
        if memoria.get('estado_conversacion') == 'esperando_param_recoleccion':
            memoria['estado_conversacion'] = None # Limpiamos el estado después de obtener el parámetro
            return {"respuesta": consultar_recoleccion_por_direccion(direccion=pregunta)}
            
        # Solo intentamos usar herramientas si no estamos en medio de OTRA conversación
        # PERO permitimos que se inicie un flujo de herramienta si no hay estado o si el estado es el de la herramienta
        if memoria.get('estado_conversacion') and memoria.get('estado_conversacion') != 'esperando_param_recoleccion':
            return None # Si hay otro flujo activo, no usamos herramientas

        prompt = crear_prompt_decision_herramienta(pregunta)
        respuesta_llm = get_cohere_response(message=prompt, preamble="Eres un experto en decidir si una pregunta requiere una herramienta específica. Responde solo con JSON o 'null'.")

        try:
            decision = json.loads(respuesta_llm)
            if decision and "usar_herramienta" in decision:
                nombre_herramienta = decision["usar_herramienta"]
                
                if nombre_herramienta in TOOL_REGISTRY:
                    # Caso 1: Faltan parámetros
                    if "faltan_parametros" in decision:
                        param_faltante = decision["faltan_parametros"][0] # Tomamos el primero si hay varios
                        if param_faltante == "direccion":
                            memoria['estado_conversacion'] = 'esperando_param_recoleccion' # Guardamos estado para esperar la dirección
                            return {"respuesta": "Claro, para decirte el horario exacto de recolección de basura, necesito la dirección completa. Por favor, indicame la calle y el número, por ejemplo: `Avenida San Martín 123, Junín`."}
                    
                    # Caso 2: Parámetros presentes, ejecutar herramienta
                    parametros = decision.get("parametros", {})
                    logger.info(f"[ToolHandler] Usando la herramienta '{nombre_herramienta}' con parámetros: {parametros}")
                    funcion_a_ejecutar = TOOL_REGISTRY[nombre_herramienta]
                    resultado = funcion_a_ejecutar(**parametros)
                    return {"respuesta": resultado}
        except (json.JSONDecodeError, TypeError):
            logger.info("[ToolHandler] La pregunta no requiere una herramienta específica o la respuesta del LLM no es JSON válida. Pasando a la cadena principal.")
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
    """
    Gestiona la solicitud de hablar con un agente.
    En lugar de solo crear un ticket, crea una sala de chat en vivo 
    y la pone en estado de espera para que un agente se conecte.
    """
    def handle(self, pregunta: str) -> dict | None:
        # Este handler se activa cuando la intención es hablar con un agente.
        if self.context.get('intencion') == 'hablar_con_agente':
            
            logger.info(f"[HumanEscalationHandler] El usuario {self.context.get('user_id')} solicita un agente. Creando sala de chat...")

            # --- Paso 1: Crear la "Sala de Chat" (el Ticket) ---
            # Usamos tu servicio de tickets para mantener la consistencia del código.
            # El estado 'esperando_agente_en_vivo' es la clave de esta nueva lógica.
            ticket_data = {
                "asunto": "Solicitud de Chat en Vivo",
                "categoria": "Atención en Vivo",
                "detalles": f"El vecino solicitó atención en vivo con el mensaje: '{pregunta}'", # Guardamos el mensaje inicial
                "user_id": self.context.get("user_id"),
                "estado": "esperando_agente_en_vivo"  # <-- ¡NUEVO ESTADO CLAVE!
            }
            
            # Asumimos que tu servicio crea el ticket y lo devuelve
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data)

            if not sala_de_chat:
                logger.error("[HumanEscalationHandler] No se pudo crear la sala de chat (ticket).")
                return {"respuesta": "Disculpa, hubo un problema técnico al intentar conectar con un agente. Por favor, intenta de nuevo más tarde."}

            # --- Paso 2: Guardar el primer mensaje del vecino en el historial del chat ---
            # Es importante que el agente vea por qué el vecino pidió ayuda.
            servicio_tickets.crear_comentario(
                ticket_id=sala_de_chat.id,
                tipo_ticket="municipio",
                comentario_data={
                    "comentario": pregunta,
                    "es_admin": False, # Es un mensaje del vecino
                    "user_id": self.context.get("user_id")
                }
            )
            
            logger.info(f"[HumanEscalationHandler] Sala de chat #{sala_de_chat.nro_ticket} creada y en espera.")

            # --- Paso 3: Responder al vecino para que espere en la ventana ---
            respuesta_al_vecino = (
                f"¡Entendido! He abierto una sala de chat directa con nuestro equipo. "
                f"Tu número de chat es **M-{sala_de_chat.nro_ticket}**. \n\n"
                "Por favor, aguarda un momento mientras un agente se conecta. **No cierres esta ventana.**"
            )
            
            # Limpiamos el contexto para futuras interacciones dentro del chat.
            self.context.get('contexto_municipio', {}).clear()

            return {"respuesta": respuesta_al_vecino}

        # Si la intención no es hablar con un agente, este handler no hace nada.
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

         # --- Paso 1: Iniciar el reclamo Y AUTOCATEGORIZAR ---
        if self.context.get('intencion') == 'iniciar_reclamo' and not estado:
            
            # --- NUEVA LÓGICA DE AUTOCATEGORIZACIÓN ---
            categoria_adivinada = categorizar_reclamo_por_palabra_clave(pregunta)
            memoria['pregunta_original'] = pregunta
            
            if categoria_adivinada != "Otros":
                # ¡Éxito! Adivinamos la categoría.
                logger.info(f"[ReclamoHandler] Categoría detectada automáticamente: {categoria_adivinada}")
                memoria['estado_conversacion'] = 'esperando_direccion_reclamo'
                memoria['categoria_reclamo'] = categoria_adivinada
                # Nos salteamos un paso y pedimos la dirección directamente.
                return {"respuesta": f"Entendido. He clasificado tu reclamo en la categoría **{categoria_adivinada}**. Para continuar, por favor, indícame la **dirección completa del problema** (calle y número)."}
            else:
                # No se pudo adivinar. Volvemos al flujo normal de preguntar con botones.
                logger.info("[ReclamoHandler] No se detectó categoría, mostrando opciones.")
                memoria['estado_conversacion'] = 'esperando_categoria_reclamo'
                return {"respuesta": "Entendido, vamos a iniciar tu reclamo. Para dirigirlo al área correcta, por favor, seleccioná una de las siguientes categorías:", 
                        "botones": [
                            {"texto": "Luminaria"}, {"texto": "Arreglo de calle"}, {"texto": "Limpieza"},
                            {"texto": "Arbol Caido"}, {"texto": "Fumigacion"}, {"texto": "Falta de agua"},
                            {"texto": "Rotura de semaforo"}, {"texto": "Otros"}
                        ]}
            
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
                return {"respuesta": f"¡Gracias! Tu reclamo fue generado con el ticket **M-{ticket.nro_ticket}**. El equipo de **{categoria}** lo revisará y podrá contactarte al número que nos proporcionaste."}
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

    # ¡LA CADENA DE HANDLERS ES LA MISMA! -> AHORA LA VAMOS A MEJORAR
    handler_chain = [
        GreetingHandler,           # <-- NUEVO: Lo ponemos primero para capturar saludos.
        ToolHandler, 
        IntentClassifierHandler, 
        HumanEscalationHandler,    # La versión modificada ahora funciona perfectamente aquí.
        TicketStatusHandler, 
        ReclamoHandler, 
        ImpuestosHandler, 
        TramitesHandler, 
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

    # Aquí se reemplaza el placeholder [nombre_vecino] si existe
    if "[nombre_vecino]" in respuesta_final.get('respuesta', ''):
        nombre_vecino_memoria = context.get('contexto_municipio', {}).get('nombre_vecino', 'vecino')
        respuesta_final['respuesta'] = respuesta_final['respuesta'].replace("[nombre_vecino]", nombre_vecino_memoria)

    return {
        "respuesta": respuesta_final.get('respuesta'),
        "botones": respuesta_final.get('botones', []),
        "contexto_actualizado": {CONTEXTO_MUNICIPIO: contexto_municipio}
    }
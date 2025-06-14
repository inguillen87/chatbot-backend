import logging
import re
import random
import json
from datetime import datetime
from flask import session as flask_session

# --- Importaciones ---
# MODIFICACIÓN 1: Agregamos SitioWebInfo a la lista de importaciones de modelos
from models import Conversacion, PymeTicket, TicketComentario, PymePedido, Rubro, SitioWebInfo, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.qdrant_search import buscar_catalogo_qdrant, armar_respuesta_legible
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.ticket_service import servicio_tickets
from services.email_service import enviar_email_ticket_admin, enviar_sms
from services.webinfo import obtener_info_web
from services.pedido_service import servicio_pedidos
from services.herramientas_pyme import TOOL_REGISTRY_PYME
from .logic import _clasificar_intencion_con_llm

logger = logging.getLogger(__name__)

# --- Constantes ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 14


def es_pregunta_nueva(texto_usuario: str, tipo_esperado: str) -> bool:
    """Determina si el usuario cambió de tema cuando se esperaba un dato."""
    texto = texto_usuario.strip().lower()
    if tipo_esperado == "el dato solicitado" and re.search(r"\d", texto):
        return False
    prompt = (
        f"Analiza la RESPUESTA DEL USUARIO. El chatbot esperaba algo relacionado a: '{tipo_esperado}'.\n"
        f"RESPUESTA DEL USUARIO: '{texto_usuario}'\n"
        "Si responde lo que esperabas, contestá 'RESPUESTA_VALIDA'."
        " Si cambia de tema, contestá 'PREGUNTA_NUEVA'."
    )
    try:
        decision = get_cohere_response(message=prompt, preamble="Sos un clasificador. Solo respondé 'RESPUESTA_VALIDA' o 'PREGUNTA_NUEVA'.")
        return "PREGUNTA_NUEVA" in decision
    except Exception:
        return False

# MODIFICACIÓN 2: Definimos el nuevo prompt inteligente para Pymes, que usará el contexto de la DB
PROMPT_PYME_CON_CONTEXTO = """
Eres "Chatboc", un agente de ventas y atención al cliente experto para la empresa "{nombre_pyme}".
Tu tarea principal es responder la PREGUNTA DEL USUARIO de forma clara y útil, basándote ESTRICTAMENTE en la INFORMACIÓN DE CONTEXTO extraída de la página web oficial de la empresa.
No inventes detalles, precios o políticas que no estén explícitamente mencionadas en el contexto. Si la información no está disponible, indícalo amablemente y ofrece ayuda para contactar a un representante.

--- INFORMACIÓN DE CONTEXTO (Extraída de la web de {nombre_pyme}) ---
{contexto_scraped}
--------------------------------------------------------------------

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Respuesta:
"""

# --- PROMPT PARA CLASIFICACIÓN DE INTENCIÓN DE PYME ---
PROMPT_CLASIFICACION_INTENCION_PYME = """
Analiza la siguiente PREGUNTA DEL USUARIO y clasifica su INTENCIÓN en el contexto de una PYME.
Si la pregunta no encaja en ninguna de las categorías, clasifícala como 'general_pyme'.

INTENCIONES POSIBLES:
- iniciar_pedido: El usuario quiere hacer un pedido, solicitar un producto, cotización, o información para compra. (ej. "quiero pedir 5 cajas de vino", "cotización de este producto", "cómo compro", "quiero encargar")
- consultar_estado_pedido: El usuario quiere saber el estado de un pedido existente. (ej. "estado de mi reclamo", "cómo va mi ticket 12345")
- consultar_stock: El usuario pregunta sobre la disponibilidad de un producto o stock.
- consultar_horario: El usuario pregunta sobre horarios de atención.
- consultar_ubicacion: El usuario pregunta por la dirección física.
- hablar_con_agente_pyme: El usuario quiere hablar con una persona de la empresa (ej. "necesito hablar con alguien", "quiero hablar con una persona", "pasame con un humano", "me pasas con un operador", "quiero un representante")
- general_pyme: Cualquier otra consulta que no encaje en las anteriores.

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Tu respuesta debe ser SÓLO una de las INTENCIONES POSIBLES.
"""

# --- Funciones Auxiliares (sin cambios) ---
def _generar_asunto_con_llm(pregunta: str) -> str:
    try:
        prompt = f"Resume la siguiente consulta de un cliente en un título breve de 4 a 8 palabras para un ticket de soporte. La consulta es: '{pregunta}'"
        asunto = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en resumir consultas de clientes.")
        return asunto.strip().replace('"', '')
    except Exception as e:
        logger.error(f"[PYME] Error generando asunto con LLM: {e}")
        return (pregunta[:75] + '...') if len(pregunta) > 75 else pregunta

def _extraer_cantidades_con_llm(pregunta_cliente: str, productos_disponibles_raw: list) -> list:
    nombres_y_sku = []
    for p in productos_disponibles_raw:
        nombre_completo = p.get('nombre', '')
        sku = p.get('sku', '')
        display_name = nombre_completo
        
        if sku and sku != nombre_completo and sku != "N/A":
            display_name = f"{nombre_completo} (SKU: {sku})"
        elif p.get('descripcion'):
            desc_para_display = p['descripcion'][:30].replace('\n', ' ').strip()
            display_name = f"{nombre_completo} ({desc_para_display}...)" 
        nombres_y_sku.append(display_name)

    prompt = f"""
    Tu tarea es analizar la respuesta de un cliente y extraer los productos y cantidades que solicita, basándote en la lista de PRODUCTOS DISPONIBLES.
    Si el cliente menciona "cada variedad" o "todos los que me mostraste", debes incluir TODOS los productos de la lista de PRODUCTOS DISPONIBLES con la cantidad especificada.
    Tu respuesta DEBE SER ÚNICAMENTE un objeto JSON en formato de lista. Cada objeto debe tener "producto_identificador", "cantidad" y "unidad".
    "producto_identificador" debe ser el nombre exacto o el nombre con SKU que se te proporcionó en la lista PRODUCTOS_DISPONIBLES para una identificación precisa.
    Si no se especifica unidad (como 'caja', 'botella'), usa 'unidad'. Si no se puede determinar la cantidad, asume '1'.
    Si el producto no está en la lista de PRODUCTOS_DISPONIBLES, NO lo incluyas.

    PRODUCTOS_DISPONIBLES: {json.dumps(nombres_y_sku, ensure_ascii=False)}

    RESPUESTA DEL CLIENTE: "{pregunta_cliente}"

    JSON de Salida:
    """
    try:
        respuesta_llm = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un asistente experto en procesar pedidos en formato JSON.")
        json_limpio = respuesta_llm.strip().replace("```json", "").replace("```", "")
        parsed_json = json.loads(json_limpio)
        
        productos_parseados = []
        pedio_todos = "cada variedad" in pregunta_cliente.lower() or "todos los que me mostraste" in pregunta_cliente.lower()

        if pedio_todos and productos_disponibles_raw:
            cantidad_general = 1
            match_cantidad_general = re.search(r'(\d+)\s*(?:caj(?:a|as)|botell(?:a|as)|unidad(?:es)?)', pregunta_cliente, re.IGNORECASE)
            if match_cantidad_general:
                cantidad_general = int(match_cantidad_general.group(1))

            for p_raw in productos_disponibles_raw:
                productos_parseados.append({
                    "nombre": p_raw.get('nombre'),
                    "sku": p_raw.get('sku', 'N/A'),
                    "precio": p_raw.get('precio', 0.0),
                    "precio_str": p_raw.get('precio_str', 'Consultar'),
                    "cantidad": cantidad_general, 
                    "unidad": p_raw.get('unidad', 'unidad'),
                    "detalles_originales": p_raw 
                })
        else: 
            for item_llm in parsed_json:
                identificador = item_llm.get('producto_identificador', '').strip()
                cantidad = item_llm.get('cantidad', 1)
                unidad = item_llm.get('unidad', 'unidad')

                matched_product = None
                for p_raw in productos_disponibles_raw:
                    nombre_completo = p_raw.get('nombre', '')
                    sku = p_raw.get('sku', '')
                    if identificador == nombre_completo or identificador == sku:
                        matched_product = p_raw
                        break
                    if identificador.lower() in nombre_completo.lower():
                        matched_product = p_raw
                        break

                if matched_product:
                    productos_parseados.append({
                        "nombre": matched_product.get('nombre'),
                        "sku": matched_product.get('sku', 'N/A'),
                        "precio": matched_product.get('precio', 0.0), 
                        "precio_str": matched_product.get('precio_str', 'Consultar'),
                        "cantidad": cantidad,
                        "unidad": unidad,
                        "detalles_originales": matched_product 
                    })
        return productos_parseados
    except Exception as e:
        logger.error(f"[PYMES] Error al extraer cantidades con LLM: {e}", exc_info=True)
        return []

# --- ARQUITECTURA DE HANDLERS (sin cambios en la mayoría) ---

class BaseHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError


class GreetingHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        texto = pregunta.strip().lower().strip("!.,?")
        saludos = ["hola", "buenos dias", "buenas tardes", "buenas noches", "hey", "que tal", "buenas"]
        tokens = re.sub(r"[!.,?]", "", texto).split()
        set_saludo = {"hola", "buenos", "dias", "buenas", "tardes", "noches", "hey", "que", "tal"}
        if texto in saludos or (0 < len(tokens) <= 3 and all(t in set_saludo for t in tokens)):
            return {"respuesta": "¡Hola! Soy Chatboc. ¿En qué puedo ayudarte hoy?", "fuente": "saludo_pyme"}
        return None

class LimitHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('preguntas_usadas', 0) >= self.context.get('limite_preguntas', 50):
            return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.", "fuente": "sistema_limite", "estado_respuesta": "limite_alcanzado"}
        return None

# (Todos los demás handlers como FollowUpHandler, PedidoHandler, etc., se mantienen exactamente igual)
class FollowUpHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        if 'esperando_detalles_reclamo' in contexto_pyme:
            if es_pregunta_nueva(pregunta, "el dato solicitado"):
                contexto_pyme.clear()
                return None
            ticket_id = contexto_pyme.pop('esperando_detalles_reclamo')
            ticket = db.session.get(PymeTicket, ticket_id)
            if ticket:
                servicio_tickets.crear_comentario(ticket_id=ticket.id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "user_id": self.context['user_id']})
                return {"respuesta": "Perfecto, he añadido tus comentarios al reclamo.", "fuente": "detalle_reclamo_agregado", "estado_respuesta": "exito_seguimiento"}
        elif 'esperando_datos_reclamo_roto' in contexto_pyme:
            if es_pregunta_nueva(pregunta, "el dato solicitado"):
                contexto_pyme.clear()
                return None
            ticket_id = contexto_pyme.pop('esperando_datos_reclamo_roto')
            ticket = db.session.get(PymeTicket, ticket_id)
            if ticket:
                servicio_tickets.crear_comentario(ticket_id=ticket.id, tipo_ticket="pyme", comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']})
                return {"respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.", "fuente": "datos_reemplazo_recibidos", "estado_respuesta": "exito_seguimiento"}
        elif 'confirmando_pedido_final_paso_2' in contexto_pyme:
            if es_pregunta_nueva(pregunta, "el dato solicitado"):
                contexto_pyme.clear()
                return None
            productos_a_confirmar = contexto_pyme.pop('productos_a_confirmar_en_paso_2')
            monto_total_final = contexto_pyme.pop('monto_total_final_en_paso_2', 0.0)
            nombre, email, telefono = None, None, None
            if self.context.get('user_obj'):
                nombre, email, telefono = self.context['user_obj'].name, self.context['user_obj'].email, self.context['user_obj'].telefono
            if not nombre or not email or not telefono:
                email_match = re.search(r'[\w\.-]+@[\w\.-]+', pregunta)
                if email_match: email = email_match.group(0)
                phone_match = re.search(r'(\+?\d{1,3}[-.\s]?)?(\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{4,8}', pregunta)
                if phone_match: telefono = phone_match.group(0)
                temp_pregunta = pregunta
                if email: temp_pregunta = temp_pregunta.replace(email, "").strip()
                if telefono: temp_pregunta = temp_pregunta.replace(telefono, "").strip()
                nombre = temp_pregunta if temp_pregunta else nombre if nombre else "Cliente Anónimo"
            pedido_data = {
                "asunto": f"Pedido Web: {contexto_pyme.get('pregunta_original_pedido', 'Solicitud de Producto')}",
                "detalles": json.dumps(productos_a_confirmar, indent=2, ensure_ascii=False),
                "rubro": self.context.get("rubro_nombre", "general_pyme"), 
                "nombre_cliente": nombre, "email_cliente": email, "telefono_cliente": telefono,
                "user_id": self.context.get("user_id"), "monto_total": monto_total_final
            }
            nuevo_pedido = servicio_pedidos.crear_nuevo_pedido(pedido_data)
            self.context['contexto_pyme'].clear() 
            if nuevo_pedido:
                return {
                    "respuesta": f"¡Excelente! Tu pedido **Nº {nuevo_pedido.nro_pedido}** fue registrado. Te contactaremos pronto para coordinar el pago y la entrega. ¡Muchas gracias!", 
                    "fuente": "handler_pedido_creado", "estado_respuesta": "exito_pedido_creado", 
                    "pedido_data": nuevo_pedido.to_dict()
                }
            else:
                return {"respuesta": "Disculpa, hubo un problema técnico al registrar tu pedido.", "fuente": "handler_pedido_error", "estado_respuesta": "error_pedido"}
        return None

class IntentClassifierPymeHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_pyme', {})
        if not memoria.get('estado_conversacion'):
            self.context['intencion'] = _clasificar_intencion_con_llm(pregunta)
        else:
            self.context['intencion'] = 'continuar_flujo_pyme'
        logger.info(f"[PYME] Intención clasificada: {self.context.get('intencion')}")
        return None

class PedidoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        estado_conversacion = contexto_pyme.get('estado_conversacion')
        if self.context.get('intencion') == 'iniciar_pedido' and not estado_conversacion:
            contexto_pyme['estado_conversacion'] = 'esperando_detalles_pedido'
            contexto_pyme['pregunta_original_pedido'] = pregunta
            productos_referencia = contexto_pyme.get('productos_mostrados_catalogo', [])
            if productos_referencia:
                resumen_productos = "\n".join([f"- **{p.get('nombre', '')}** (SKU: {p.get('sku', 'N/A')}): ${p.get('precio_str', 'Consultar')}" for p in productos_referencia])
                return {"respuesta": f"¡Claro! Encontré estos productos:\n{resumen_productos}\n\n¿Cuáles y cuántos te gustaría pedir? Por ejemplo: '1 caja de Malbec y 2 de Cabernet'.", "fuente": "handler_pedido_iniciado_con_catalogo", "estado_respuesta": "pyme_pregunta_pedido"}
            else:
                return {"respuesta": "¡Claro! Para tu pedido, contame qué productos o servicios te interesan y en qué cantidad.", "fuente": "handler_pedido_iniciado_generico", "estado_respuesta": "pyme_pregunta_pedido"}
        elif estado_conversacion == 'esperando_detalles_pedido':
            productos_referencia = contexto_pyme.get('productos_mostrados_catalogo', [])
            detalles_estructurados = _extraer_cantidades_con_llm(pregunta, productos_referencia)
            if not detalles_estructurados:
                return {"respuesta": "No pude identificar los productos que mencionas. Por favor, sé más específico sobre lo que te interesa de nuestro catálogo.", "fuente": "handler_pedido_error_productos", "estado_respuesta": "pyme_error_productos"}
            contexto_pyme['productos_solicitados_temp'] = detalles_estructurados
            contexto_pyme['estado_conversacion'] = 'confirmando_pedido_temp'
            resumen_productos_confirmacion = "Tenemos lo siguiente para tu pedido:\n"
            monto_total_temp = 0.0
            for p in detalles_estructurados:
                subtotal = (float(p.get('cantidad', 1)) * float(p.get('precio', 0.0)))
                monto_total_temp += subtotal
                resumen_productos_confirmacion += f"- **{p.get('nombre', 'N/A')}**: {p.get('cantidad', 1)} {p.get('unidad', 'u')} @ ${p.get('precio_str', 'N/A')} = ${subtotal:,.2f}\n"
            resumen_productos_confirmacion += f"\n**Monto estimado: ${monto_total_temp:,.2f}**\n\n¿Confirmas este pedido? También podés indicarme tus datos (nombre, teléfono, email)."
            contexto_pyme['monto_total_temp'] = monto_total_temp
            return {"respuesta": resumen_productos_confirmacion, "fuente": "handler_pedido_detalles_para_confirmar", "estado_respuesta": "pyme_confirmar_pedido"}
        elif estado_conversacion == 'confirmando_pedido_temp':
            if any(p in pregunta.lower().strip() for p in ["si", "sí", "dale", "quiero", "confirmar", "ok"]):
                contexto_pyme['productos_a_confirmar_en_paso_2'] = contexto_pyme.pop('productos_solicitados_temp')
                contexto_pyme['monto_total_final_en_paso_2'] = contexto_pyme.pop('monto_total_temp')
                contexto_pyme['estado_conversacion'] = 'confirmando_pedido_final_paso_2'
                return {"respuesta": "¡Excelente! Estoy procesando los últimos detalles. Por favor, confirmame tu nombre y un contacto (teléfono o email) para finalizar.", "fuente": "pedido_confirmado_paso_1", "estado_respuesta": "pyme_pregunta_contacto"}
            else:
                contexto_pyme.clear()
                return {"respuesta": "Entendido. No se generará el pedido. ¿Hay algo más en lo que pueda ayudarte?", "fuente": "pedido_cancelado"}
        elif estado_conversacion == 'esperando_numero_pedido':
            pedido_match = re.search(r"(pedido|orden)\s*#?\s*([a-zA-Z0-9-]+)", pregunta, re.IGNORECASE)
            if not pedido_match:
                return {"respuesta": "No entendí el número de pedido. ¿Podés repetirlo?", "fuente": "pedido_falta_numero"}
            nro_pedido_str = pedido_match.group(2).upper()
            pedido = servicio_pedidos.obtener_pedido_por_nro(nro_pedido_str)
            contexto_pyme.clear()
            if pedido:
                return {"respuesta": f"El pedido **Nº {pedido.nro_pedido}** se encuentra en estado: **{pedido.estado}**.", "fuente": "consulta_estado_pedido_ok", "estado_respuesta": "mostrar_pedido_en_panel", "pedido_data": pedido.to_dict()}
            else:
                return {"respuesta": f"No se encontró ningún pedido con el número #{nro_pedido_str}.", "fuente": "pedido_no_encontrado"}
        elif self.context.get('intencion') == 'consultar_estado_pedido':
            pedido_match = re.search(r"(pedido|orden)\s*#?\s*([a-zA-Z0-9-]+)", pregunta, re.IGNORECASE)
            if pedido_match:
                nro_pedido_str = pedido_match.group(2).upper()
                pedido = servicio_pedidos.obtener_pedido_por_nro(nro_pedido_str)
                if pedido:
                    return {"respuesta": f"El pedido **Nº {pedido.nro_pedido}** se encuentra en estado: **{pedido.estado}**.", "fuente": "consulta_estado_pedido_ok", "estado_respuesta": "mostrar_pedido_en_panel", "pedido_data": pedido.to_dict()}
                else:
                    return {"respuesta": f"No se encontró ningún pedido con el número #{nro_pedido_str}.", "fuente": "pedido_no_encontrado"}
            else:
                contexto_pyme['estado_conversacion'] = 'esperando_numero_pedido'
                return {"respuesta": "Para consultar, por favor, decime el número de pedido.", "fuente": "pedido_falta_numero"}
        return None

class TicketStatusHandler(BaseHandler):
    """Permite consultar el estado de un ticket de soporte de la PyME."""

    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_pyme', {})
        estado = memoria.get('estado_conversacion')

        if estado == 'esperando_numero_ticket':
            match = re.search(r"\d{5,}", pregunta)
            if not match:
                return {"respuesta": "No entendí el número de ticket. ¿Podés repetirlo?", "fuente": "ticket_falta_numero"}
            nro = int(match.group(0))
            ticket = PymeTicket.query.filter_by(nro_ticket=nro).first()
            memoria.clear()
            if ticket:
                return {"respuesta": f"El ticket **{ticket.nro_ticket}** está en estado **{ticket.estado}**.", "fuente": "ticket_ok"}
            return {"respuesta": f"No encontré ticket {nro}.", "fuente": "ticket_no_encontrado"}

        if self.context.get('intencion') == 'consultar_estado_ticket':
            match = re.search(r"\d{5,}", pregunta)
            if match:
                nro = int(match.group(0))
                ticket = PymeTicket.query.filter_by(nro_ticket=nro).first()
                if ticket:
                    return {"respuesta": f"El ticket **{ticket.nro_ticket}** está en estado **{ticket.estado}**.", "fuente": "ticket_ok"}
                return {"respuesta": f"No encontré ticket {nro}.", "fuente": "ticket_no_encontrado"}
            memoria['estado_conversacion'] = 'esperando_numero_ticket'
            return {"respuesta": "Decime el número de ticket que querés consultar.", "fuente": "ticket_pedir_numero"}

        return None

class BrokenProductHandler(BaseHandler):
    """Registra un reclamo por producto roto y solicita más información."""

    PALABRAS_CLAVE = ["roto", "quebrado", "defectuoso", "dañado"]

    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_pyme', {})

        if memoria.get('estado_conversacion') == 'esperando_datos_reclamo_roto':
            ticket_id = memoria.pop('ticket_id_roto', None)
            if ticket_id:
                servicio_tickets.crear_comentario(
                    ticket_id=ticket_id,
                    tipo_ticket="pyme",
                    comentario_data={"comentario": pregunta, "user_id": self.context.get('user_id')}
                )
                memoria.clear()
                return {"respuesta": "Gracias, registramos el detalle de tu reclamo y pronto te contactaremos.", "fuente": "reclamo_roto_comentado"}
            return None

        if any(pal in pregunta.lower() for pal in self.PALABRAS_CLAVE):
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme",
                ticket_data={
                    "asunto": "Producto roto",
                    "categoria": "producto_roto",
                    "pregunta": pregunta,
                    "user_id": self.context.get('user_id'),
                    "rubro_id": getattr(self.context.get('rubro_obj'), 'id', None)
                }
            )
            if ticket:
                try:
                    enviar_email_ticket_admin(ticket)
                except Exception as e:
                    logger.error(f"Error enviando email de ticket roto: {e}")
                try:
                    telefono = getattr(self.context.get('user_obj'), 'telefono', '')
                    if telefono:
                        tel = re.sub(r"\D", "", telefono)
                        if not tel.startswith("+") and len(tel) > 8:
                            tel = "+549" + tel
                        enviar_sms(tel, f"Tu reclamo {ticket.nro_ticket} fue registrado")
                except Exception as e:
                    logger.error(f"Error enviando SMS de ticket roto: {e}")
                memoria['estado_conversacion'] = 'esperando_datos_reclamo_roto'
                memoria['ticket_id_roto'] = ticket.id
                return {"respuesta": f"Lamentamos lo ocurrido. Creamos el ticket **{ticket.nro_ticket}**. ¿Podés contarnos más detalles o enviar una foto?", "fuente": "reclamo_roto"}
        return None

class ClaimHandler(BaseHandler):
    """Genera un ticket de reclamo general."""

    PALABRAS_CLAVE = ["reclamo", "queja", "mala atención", "problema"]

    def handle(self, pregunta: str) -> dict | None:
        if any(pal in pregunta.lower() for pal in self.PALABRAS_CLAVE):
            ticket = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme",
                ticket_data={
                    "asunto": _generar_asunto_con_llm(pregunta),
                    "categoria": "reclamo",
                    "pregunta": pregunta,
                    "user_id": self.context.get('user_id'),
                    "rubro_id": getattr(self.context.get('rubro_obj'), 'id', None)
                }
            )
            if ticket:
                try:
                    enviar_email_ticket_admin(ticket)
                except Exception as e:
                    logger.error(f"Error enviando email de ticket: {e}")
                try:
                    telefono = getattr(self.context.get('user_obj'), 'telefono', '')
                    if telefono:
                        tel = re.sub(r"\D", "", telefono)
                        if not tel.startswith("+") and len(tel) > 8:
                            tel = "+549" + tel
                        enviar_sms(tel, f"Tu reclamo {ticket.nro_ticket} fue registrado")
                except Exception as e:
                    logger.error(f"Error enviando SMS de ticket: {e}")
                self.context.get('contexto_pyme', {})['esperando_detalles_reclamo'] = ticket.id
                return {"respuesta": f"Registré tu reclamo con número **{ticket.nro_ticket}**. ¿Podés brindarme más detalles?", "fuente": "reclamo_registrado"}
        return None

class VectorCatalogHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        user_id = self.context.get('user_id')
        if not user_id:
            return None

        resultados = buscar_catalogo_qdrant(user_id=user_id, pregunta=pregunta, limite=3)
        if not resultados:
            return None

        productos_mostrados = []
        for hit in resultados:
            if hasattr(hit, 'payload') and isinstance(hit.payload, dict):
                productos_mostrados.append(hit.payload)

        if productos_mostrados:
            self.context['contexto_pyme']['productos_mostrados_catalogo'] = productos_mostrados
            respuesta = armar_respuesta_legible(resultados)
            if respuesta:
                return {
                    'respuesta': respuesta,
                    'fuente': 'catalogo_vector',
                    'estado_respuesta': 'mostrar_catalogo'
                }

        return None

class SalesEngageHandler(BaseHandler):
    """Ofrece sugerencias comerciales cuando no se detecta otra intención."""

    def handle(self, pregunta: str) -> dict | None:
        sugerencias = sugerencias_por_rubro(self.context.get('rubro_nombre'))
        if sugerencias:
            texto = reemplazar_placeholders(random.choice(sugerencias), self.context.get('user_obj'))
            return {"respuesta": texto, "fuente": "sugerencia_venta"}
        return None

class FaqHandler(BaseHandler):
    """Responde usando la base de preguntas frecuentes."""

    def handle(self, pregunta: str) -> dict | None:
        rubro = self.context.get('rubro_obj')
        if not rubro:
            return None
        match = buscar_en_faq_spacy(pregunta, rubro.id)
        if match:
            return {
                "respuesta": reemplazar_placeholders(match.answer, self.context.get('user_obj')),
                "fuente": "faq"
            }
        return None

class ToolHandlerPyme(BaseHandler):
    """Resuelve consultas directas mediante pequeñas herramientas."""

    def handle(self, pregunta: str) -> dict | None:
        texto = pregunta.lower()
        if 'horario' in texto or 'abren' in texto:
            res = TOOL_REGISTRY_PYME['consultar_horario']['funcion'](self.context.get('user_obj'))
            return json.loads(res)
        if 'stock' in texto:
            match = re.search(r'stock (?:de )?(.*)', texto)
            producto = match.group(1).strip() if match else ''
            if producto:
                res = TOOL_REGISTRY_PYME['verificar_stock']['funcion'](producto, self.context.get('user_id'))
                return json.loads(res)
        if 'envío' in texto or 'envio' in texto:
            match = re.search(r'env(?:i|ío)\s+a\s+([\w\s]+)', texto)
            ciudad = match.group(1).strip() if match else ''
            if ciudad:
                res = TOOL_REGISTRY_PYME['calcular_envio']['funcion'](ciudad)
                return json.loads(res)
        return None

class IntentHandler(BaseHandler):
    """Fallback basado en intents cargados desde data/intents.json."""

    def handle(self, pregunta: str) -> dict | None:
        rubro = self.context.get('rubro_nombre')
        respuesta = buscar_en_intents(pregunta, rubro)
        if respuesta:
            return {"respuesta": reemplazar_placeholders(respuesta, self.context.get('user_obj')), "fuente": "intent"}
        return None

# --- MODIFICACIÓN 3: El LLMHandler ahora es el que usa el contexto de la base de datos ---
class LLMHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        logger.info("[LLMHandler_PYME] Manejando como consulta general. Buscando contexto en la DB.")
        user_obj = self.context.get("user_obj")

        if not user_obj:
            logger.warning("[LLMHandler_PYME] No se encontró user_obj en el contexto.")
            return {"respuesta": "Disculpa, no pude procesar tu consulta en este momento.", "fuente": "error_no_contexto_pyme"}

        # 1. Buscar contexto scrapeado desde la base de datos
        contexto_scraped = ""
        try:
            contenidos = SitioWebInfo.query.filter_by(user_id=user_obj.id).all()
            textos_relevantes = []
            for item in contenidos:
                datos = json.loads(item.datos_json)
                if datos.get("tipo") == "contenido_general" and datos.get("contenido"):
                    textos_relevantes.append(datos["contenido"])
                elif datos.get("tipo") == "productos":
                    for prod in datos.get("productos", []):
                        nombre = prod.get('nombre', '')
                        precio = prod.get('precio_str', 'Consultar')
                        desc = prod.get('descripcion', '')
                        textos_relevantes.append(f"Producto: {nombre}. Precio: {precio}. Descripción: {desc}.")
            
            contexto_scraped = " ".join(textos_relevantes)
            if not contexto_scraped:
                contexto_scraped = "No se encontró información adicional en la web de la empresa para responder esta consulta."
            logger.info(f"[LLMHandler_PYME] {len(contexto_scraped)} caracteres de contexto encontrados.")

        except Exception as e:
            logger.error(f"[LLMHandler_PYME] Error al obtener contexto de la DB: {e}")
            contexto_scraped = "Error al cargar información de contexto."

        # 2. Construir el prompt final y llamar a Cohere
        prompt_final = PROMPT_PYME_CON_CONTEXTO.format(
            nombre_pyme=self.context.get('nombre_pyme', 'la empresa'),
            contexto_scraped=contexto_scraped,
            pregunta_usuario=pregunta
        )

        respuesta_llm = get_cohere_response(
            message=prompt_final,
            chat_history=self.context.get('mensajes_previos', []),
            preamble=f"Eres un agente de ventas y atención al cliente de {self.context.get('nombre_pyme')}."
        )

        if respuesta_llm:
            return {"respuesta": reemplazar_placeholders(respuesta_llm, user_obj), "fuente": "llm_con_contexto_db", "estado_respuesta": "general_llm_ok"}
        else:
            return {"respuesta": "No pude encontrar una respuesta para tu consulta en este momento.", "fuente": "llm_fallback"}

class EngancheAnonimoHandler(BaseHandler):
    """Invita a registrarse si el usuario es anónimo."""

    def handle(self, pregunta: str) -> dict | None:
        if not self.context.get('user_id') or self.context.get('plan') == 'anonimo':
            return {
                "respuesta": (
                    "Para seguir con la atención personalizada y guardar tu historial, registrate o iniciá sesión."),
                "botones": [
                    {"texto": "Iniciar sesión", "url": "/login"},
                    {"texto": "Registrarme Gratis", "url": "/register"}
                ]
            }
        return None

# --- FUNCIÓN ORQUESTADORA PRINCIPAL (sin cambios) ---
def responder_pyme(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_previo_valido = contexto_previo if contexto_previo is not None else {}
    contexto_pyme = contexto_previo_valido.get(CONTEXTO_PYME_SESION, {})

    context = {
        "contexto_pyme": contexto_pyme, "user_obj": user_obj, "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa") if user_obj else "la empresa",
        "telefono": getattr(user_obj, "telefono", "") if user_obj else "",
        "direccion": getattr(user_obj, "direccion", "") if user_obj else "",
        "email": getattr(user_obj, "email", "") if user_obj else "",
        "plan": getattr(user_obj, "plan", "anonimo") if user_obj else "anonimo",
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0) if user_obj else 0,
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10) if user_obj else 10,
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa").lower() if rubro_obj else "desconocido",
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    }
    
    handler_chain = [
        LimitHandler,
        GreetingHandler,
        FollowUpHandler,
        IntentClassifierPymeHandler, # 1. Clasifica la intención
        VectorCatalogHandler,      # 2. BUSCA EN EL CATÁLOGO VECTORIAL PRIMERO
        PedidoHandler,             # 3. Ahora sí, gestiona el pedido con el contexto de productos
        BrokenProductHandler,
        ClaimHandler,
        FaqHandler,
        ToolHandlerPyme,
        LLMHandler,                # Este ahora es un excelente fallback inteligente
        IntentHandler,
        SalesEngageHandler,
        EngancheAnonimoHandler,
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            if not isinstance(respuesta_final, dict):
                logging.error(f"[HANDLER_ERROR] Handler '{handler_class.__name__}' devolvió tipo incorrecto: {type(respuesta_final)}")
                respuesta_final = None 
            else:
                break 

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no pude procesar tu solicitud. Por favor, intenta de nuevo.", "fuente": "error_no_handler", "estado_respuesta": "error_critico"}
    
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    asistente_content = str(respuesta_final.get('respuesta','')) 
    historial.append({"role": "assistant", "content": asistente_content})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:] 
    
    try:
        if context['user_id']: 
            db.session.add(Conversacion(
                user_id=context['user_id'], pregunta=pregunta, 
                respuesta=respuesta_final.get('respuesta', ''), 
                fuente=respuesta_final.get('fuente', 'desconocida'), 
                rubro=context['rubro_nombre']
            ))
            db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}", exc_info=True)
        db.session.rollback()

    return {
        "respuesta": respuesta_final.get('respuesta', "Error: respuesta mal formada."),
        "fuente": respuesta_final.get('fuente', 'desconocida'),
        "contexto_actualizado": {CONTEXTO_PYME_SESION: contexto_pyme},
        "estado_respuesta": respuesta_final.get('estado_respuesta', 'no_entendido'),
        "pedido_data": respuesta_final.get('pedido_data', None)
    }
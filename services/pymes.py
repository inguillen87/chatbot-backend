# src/services/pymes.py

import logging
import re
import random
import json
from datetime import datetime
from flask import session as flask_session

# --- Importaciones ---
from models import Conversacion, PymeTicket, TicketComentario, PymePedido, Rubro, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.ticket_service import servicio_tickets
from services.webinfo import obtener_info_web
from services.pedido_service import servicio_pedidos
from .logic import _clasificar_intencion_con_llm

logger = logging.getLogger(__name__)

# --- Constantes ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 14

# --- PROMPT PARA CLASIFICACIÓN DE INTENCIÓN DE PYME ---
PROMPT_CLASIFICACION_INTENCION_PYME = """
Analiza la siguiente PREGUNTA DEL USUARIO y clasifica su INTENCIÓN en el contexto de una PYME.
Si la pregunta no encaja en ninguna de las categorías, clasifícala como 'general_pyme'.

INTENCIONES POSIBLES:
- iniciar_pedido: El usuario quiere hacer un pedido, solicitar un producto, cotización, o información para compra. (ej. "quiero pedir 5 cajas de vino", "cotización de este producto", "cómo compro", "quiero encargar")
- consultar_estado_pedido: El usuario quiere saber el estado de un pedido existente. (ej. "estado de mi pedido PED-123", "cómo va mi orden", "mi compra está lista?")
- consultar_stock: El usuario pregunta sobre la disponibilidad de un producto o stock.
- consultar_horario: El usuario pregunta sobre horarios de atención.
- consultar_ubicacion: El usuario pregunta por la dirección física.
- hablar_con_agente_pyme: El usuario quiere hablar con una persona de la empresa.
- general_pyme: Cualquier otra consulta que no encaje en las anteriores.

PREGUNTA DEL USUARIO: "{pregunta_usuario}"

Tu respuesta debe ser SÓLO una de las INTENCIONES POSIBLES.
"""

# --- Funciones Auxiliares ---
def _generar_asunto_con_llm(pregunta: str) -> str:
    try:
        prompt = f"Resume la siguiente consulta de un cliente en un título breve de 4 a 8 palabras para un ticket de soporte. La consulta es: '{pregunta}'"
        asunto = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en resumir consultas de clientes.")
        return asunto.strip().replace('"', '')
    except Exception as e:
        logger.error(f"[PYME] Error generando asunto con LLM: {e}")
        return (pregunta[:75] + '...') if len(pregunta) > 75 else pregunta

def _extraer_cantidades_con_llm(pregunta_cliente: str, productos_disponibles_raw: list) -> list:
    """
    Analiza la respuesta del cliente para extraer productos y cantidades.
    productos_disponibles_raw: Lista de diccionarios completos de productos (ej. de Qdrant payload).
    """
    nombres_y_sku = []
    for p in productos_disponibles_raw:
        nombre_completo = p.get('nombre', '')
        sku = p.get('sku', '')
        display_name = nombre_completo # Inicializar display_name aquí como valor por defecto
        
        if sku and sku != nombre_completo and sku != "N/A":
            display_name = f"{nombre_completo} (SKU: {sku})"
        elif p.get('descripcion'):
            # --- CORRECCIÓN DE INDENTACIÓN Y F-STRING ---
            # Esta línea DEBE estar indentada correctamente dentro del 'elif'
            display_name = f"{nombre_completo} ({descripcion_corta}...)"
        nombres_y_sku.append(display_name) # Esta línea debe estar al nivel del bucle 'for'

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
        else: # Procesar lo que el LLM extrajo si no se pidió "todos"
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

# --- ARQUITECTURA DE HANDLERS ---

class BaseHandler:
    def __init__(self, context):
        self.context = context
    def handle(self, pregunta: str) -> dict | None:
        raise NotImplementedError

class LimitHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if self.context.get('preguntas_usadas', 0) >= self.context.get('limite_preguntas', 50):
            return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.", "fuente": "sistema_limite", "estado_respuesta": "limite_alcanzado"}
        return None

class FollowUpHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        
        # --- Flujo de seguimiento de Reclamos ---
        if 'esperando_detalles_reclamo' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_detalles_reclamo')
            ticket = db.session.get(PymeTicket, ticket_id)
            if ticket:
                servicio_tickets.crear_comentario(ticket_id=ticket.id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "user_id": self.context['user_id']})
                return {"respuesta": "Perfecto, he añadido tus comentarios al reclamo.", "fuente": "detalle_reclamo_agregado", "estado_respuesta": "exito_seguimiento"}
        elif 'esperando_datos_reclamo_roto' in contexto_pyme:
            ticket_id = contexto_pyme.pop('esperando_datos_reclamo_roto')
            ticket = db.session.get(PymeTicket, ticket_id)
            if ticket:
                servicio_tickets.crear_comentario(ticket_id=ticket.id, tipo_ticket="pyme", comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']})
                return {"respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.", "fuente": "datos_reemplazo_recibidos", "estado_respuesta": "exito_seguimiento"}
        
        # --- FLUJO DE SEGUIMIENTO: Creación final del Pedido (Paso 2) ---
        elif 'confirmando_pedido_final_paso_2' in contexto_pyme:
            productos_a_confirmar = contexto_pyme.pop('productos_a_confirmar_en_paso_2')
            monto_total_final = contexto_pyme.pop('monto_total_final_en_paso_2', 0.0)
            
            nombre = None
            email = None
            telefono = None

            if self.context.get('user_obj'):
                nombre = self.context['user_obj'].name
                email = self.context['user_obj'].email
                telefono = self.context['user_obj'].telefono
            
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
                "detalles": json.dumps(productos_a_confirmar, indent=2, ensure_ascii=False), # Guardar el JSON de productos
                "rubro": self.context.get("rubro_nombre", "general_pyme"), 
                "nombre_cliente": nombre,
                "email_cliente": email,
                "telefono_cliente": telefono,
                "user_id": self.context.get("user_id"),
                "monto_total": monto_total_final
            }
            
            nuevo_pedido = servicio_pedidos.crear_nuevo_pedido(pedido_data)
            self.context['contexto_pyme'].clear() 
            
            if nuevo_pedido:
                return {
                    "respuesta": f"¡Excelente! Tu pedido **Nº {nuevo_pedido.nro_pedido}** fue registrado. Te contactaremos pronto para coordinar el pago y la entrega. ¡Muchas gracias!", 
                    "fuente": "handler_pedido_creado",
                    "estado_respuesta": "exito_pedido_creado", 
                    "pedido_data": { 
                        "nro_pedido": nuevo_pedido.nro_pedido,
                        "estado": nuevo_pedido.estado,
                        "asunto": nuevo_pedido.asunto,
                        "detalles": json.loads(nuevo_pedido.detalles), 
                        "fecha_creacion": nuevo_pedido.fecha.isoformat(),
                        "nombre_cliente": nuevo_pedido.nombre_cliente,
                        "email_cliente": nuevo_pedido.email_cliente,
                        "telefono_cliente": nuevo_pedido.telefono_cliente
                    }
                }
            else:
                return {
                    "respuesta": "Disculpa, hubo un problema técnico al registrar tu pedido. Por favor, intenta de nuevo o comunícate directamente con la empresa.", 
                    "fuente": "handler_pedido_error",
                    "estado_respuesta": "error_pedido"
                }
        return None

class IntentClassifierPymeHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        memoria = self.context.get('contexto_pyme', {})
        if not memoria.get('estado_conversacion'):
            self.context['intencion'] = _clasificar_intencion_con_llm(pregunta, prompt_base=PROMPT_CLASIFICACION_INTENCION_PYME)
        else:
            self.context['intencion'] = 'continuar_flujo_pyme'
        logger.info(f"[PYME] Intención clasificada: {self.context.get('intencion')}")
        return None

class PedidoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        estado_conversacion = contexto_pyme.get('estado_conversacion')
        
        # --- INICIAR PEDIDO: Desde la intención clasificada ---
        if self.context.get('intencion') == 'iniciar_pedido' and not estado_conversacion:
            contexto_pyme['estado_conversacion'] = 'esperando_detalles_pedido'
            contexto_pyme['pregunta_original_pedido'] = pregunta
            
            productos_referencia = contexto_pyme.get('productos_mostrados_catalogo', [])
            if productos_referencia:
                resumen_productos = "\n".join([
                    f"- **{p.get('nombre', '')}** (SKU: {p.get('sku', 'N/A')}): ${p.get('precio_str', 'Consultar')}"
                    for p in productos_referencia
                ])
                return {
                    "respuesta": f"¡Claro! Encontré estos productos:\n{resumen_productos}\n\n¿Cuáles y cuántos de estos productos te gustaría pedir? Por ejemplo: '1 caja de Malbec Joven y 2 de Cabernet Gran Reserva'.", 
                    "fuente": "handler_pedido_iniciado_con_catalogo",
                    "estado_respuesta": "pyme_pregunta_pedido"
                }
            else:
                return {
                    "respuesta": "¡Claro! Para tu pedido, contame qué productos o servicios te interesan y en qué cantidad o con qué especificaciones.", 
                    "fuente": "handler_pedido_iniciado_generico",
                    "estado_respuesta": "pyme_pregunta_pedido"
                }

        # --- RECIBIENDO DETALLES DEL PEDIDO (después de 'iniciar_pedido') ---
        elif estado_conversacion == 'esperando_detalles_pedido':
            productos_referencia = contexto_pyme.get('productos_mostrados_catalogo', [])
            detalles_estructurados = _extraer_cantidades_con_llm(pregunta, productos_referencia)
            
            if not detalles_estructurados:
                return {
                    "respuesta": "No pude identificar los productos o cantidades que mencionas. Por favor, sé más específico sobre lo que te interesa de nuestro catálogo.",
                    "fuente": "handler_pedido_error_productos",
                    "estado_respuesta": "pyme_error_productos"
                }

            contexto_pyme['productos_solicitados_temp'] = detalles_estructurados
            contexto_pyme['estado_conversacion'] = 'confirmando_pedido_temp'
            
            resumen_productos_confirmacion = "Tenemos lo siguiente para tu pedido:\n"
            monto_total_temp = 0.0
            for p in detalles_estructurados:
                nombre = p.get('nombre', 'Producto Desconocido')
                sku = p.get('sku', 'N/A')
                cantidad = p.get('cantidad', 1)
                unidad = p.get('unidad', 'unidad')
                precio = p.get('precio', 0.0)
                precio_str = p.get('precio_str', 'Consultar')
                
                try:
                    subtotal = float(cantidad) * float(precio)
                except (ValueError, TypeError):
                    subtotal = 0.0 
                monto_total_temp += subtotal

                resumen_productos_confirmacion += f"- **{nombre}** (SKU: {sku}): {cantidad} {unidad} @ ${precio_str} = ${subtotal:,.2f}\n"

            resumen_productos_confirmacion += f"\n**Monto estimado total: ${monto_total_temp:,.2f}**"
            resumen_productos_confirmacion += "\n\n¿Confirmas este pedido? También podés indicarme tus datos de contacto (nombre, teléfono o email) si no los tengo."
            
            contexto_pyme['monto_total_temp'] = monto_total_temp
            
            return {
                "respuesta": resumen_productos_confirmacion, 
                "fuente": "handler_pedido_detalles_para_confirmar",
                "estado_respuesta": "pyme_confirmar_pedido"
            }
        
        # --- CONFIRMANDO PEDIDO (respuesta "sí" a la confirmación detallada) ---
        elif estado_conversacion == 'confirmando_pedido_temp':
            palabras_confirmacion = ["si", "sí", "dale", "quiero", "generar", "confirmar", "ok", "me gustaria", "si quiero"]
            if any(p in pregunta.lower().strip() for p in palabras_confirmacion):
                # Guardar los datos para el FollowUpHandler
                contexto_pyme['productos_a_confirmar_en_paso_2'] = contexto_pyme.pop('productos_solicitados_temp')
                contexto_pyme['monto_total_final_en_paso_2'] = contexto_pyme.pop('monto_total_temp')
                contexto_pyme['estado_conversacion'] = 'confirmando_pedido_final_paso_2' # Establecer el nuevo estado para FollowUpHandler
                
                # Devolvemos una respuesta para el usuario y no None, para que el flujo sea más claro
                return {
                    "respuesta": "¡Excelente! Estoy procesando los últimos detalles. Por favor, confirmame tu nombre y un contacto (teléfono o email) para finalizar.",
                    "fuente": "pedido_confirmado_paso_1",
                    "estado_respuesta": "pyme_pregunta_contacto"
                }
            else:
                contexto_pyme.clear() # El usuario no confirmó, resetear
                return {"respuesta": "Entendido. No se generará el pedido en este momento. ¿Hay algo más en lo que pueda ayudarte?", "fuente": "pedido_cancelado"}

        # --- CONSULTA DE ESTADO DE PEDIDO ---
        elif self.context.get('intencion') == 'consultar_estado_pedido':
            pedido_match = re.search(r"(pedido|orden|compra)\s*#?\s*([a-zA-Z0-9-]+)", pregunta, re.IGNORECASE)
            if pedido_match:
                nro_pedido_str = pedido_match.group(2).upper()
                pedido = servicio_pedidos.obtener_pedido_por_nro(nro_pedido_str)
                if pedido:
                    return {
                        "respuesta": f"El pedido **Nº {pedido.nro_pedido}** se encuentra en estado: **{pedido.estado}**.",
                        "fuente": "consulta_estado_pedido_ok",
                        "estado_respuesta": "mostrar_pedido_en_panel",
                        "pedido_data": { # Datos para mostrar en el PedidoPanel
                            "nro_pedido": pedido.nro_pedido,
                            "estado": pedido.estado,
                            "asunto": pedido.asunto,
                            "detalles": json.loads(pedido.detalles), # Parsear detalles para el frontend
                            "fecha_creacion": pedido.fecha.isoformat(),
                            "nombre_cliente": pedido.nombre_cliente,
                            "email_cliente": pedido.email_cliente,
                            "telefono_cliente": pedido.telefono_cliente
                        }
                    }
                else:
                    return {"respuesta": f"No se encontró ningún pedido con el número #{nro_pedido_str}. Por favor, verificá el número.", "fuente": "pedido_no_encontrado"}
            else:
                return {"respuesta": "Para consultar el estado de tu pedido, por favor, decime el número de pedido.", "fuente": "pedido_falta_numero"}

        return None # Si no coincide con ningún flujo de Pedido, pasamos al siguiente handler

class TicketStatusHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        ticket_match = re.search(r"(ticket|reclamo|consulta)\s*#?\s*([0-9]{5,})", pregunta, re.IGNORECASE)
        if ticket_match:
            nro = ticket_match.group(2)
            ticket = PymeTicket.query.filter_by(nro_ticket=int(nro), user_id=self.context.get('user_id')).first()
            if ticket:
                msg = f"El ticket de reclamo #{nro} (Asunto: '{ticket.asunto}') está en estado: '{ticket.estado}'."
                return {"respuesta": msg, "fuente": "consulta_estado_ticket"}
            else:
                return {"respuesta": f"No se encontró ningún ticket con el número #{nro}.", "fuente": "ticket_no_encontrado"}
        return None

class BrokenProductHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if any(keyword in pregunta.lower() for keyword in ["botella rota", "llegó roto", "producto dañado"]):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data={"pregunta": pregunta, "user_id": self.context['user_id'], "asunto": asunto, "categoria": "Reclamo - Producto Dañado"})
            if ticket_creado:
                self.context['contexto_pyme']['esperando_datos_reclamo_roto'] = ticket_creado.id
            respuesta = (f"Lamento muchísimo escuchar eso. En {self.context.get('nombre_pyme')} nos aseguramos de que recibas todo en perfectas condiciones.\n\nPara gestionar el nuevo envío, por favor, indícame en tu próximo mensaje el número del pedido original.")
            return {"respuesta": respuesta, "fuente": "handler_producto_dañado", "estado_respuesta": "ticket_creado"}
        return None

class ClaimHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if any(w in pregunta.lower() for w in ["mal servicio", "problema", "no llegó", "demora", "reclamo", "queja"]):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data={"pregunta": pregunta, "user_id": self.context['user_id'],"asunto": asunto, "categoria": "Reclamo"})
            if ticket_creado:
                respuesta = (f"Lamento mucho el inconveniente. He generado un reclamo con el ticket #{ticket_creado.nro_ticket}.\nPara poder ayudarte mejor, ¿podrías darme más detalles?")
                self.context['contexto_pyme']['esperando_detalles_reclamo'] = ticket_creado.id
                return {"respuesta": respuesta, "fuente": "registro_reclamo", "estado_respuesta": "ticket_creado"}
        return None

class VectorCatalogHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        if any(w in pregunta.lower() for w in ["comprar", "precio", "pedido", "catalogo", "stock", "quiero", "vinos", "compra"]):
            try:
                resultados = buscar_item_vectorizado(pregunta, self.context['user_id']) 
                if resultados:
                    respuesta_texto = "¡Claro! En nuestro catálogo detallado encontré esto:\n"
                    items_payload = [item.payload for item in resultados] 
                    
                    self.context['contexto_pyme']['productos_mostrados_catalogo'] = items_payload

                    for payload in items_payload:
                        nombre = payload.get('nombre', 'Producto')
                        sku = payload.get('sku', 'N/A')
                        precio_str = payload.get('precio_str', 'Consultar')
                        
                        respuesta_texto += f"- **{nombre}** (SKU: {sku}): ${precio_str}\n"
                    
                    respuesta_texto += "\n¿Te gustaría que genere un pedido con alguno de estos productos?"
                    return {"respuesta": respuesta_texto, "fuente": "catalogo_qdrant", "estado_respuesta": "exito_catalogo_mostrado"}
            except Exception as e:
                logging.warning(f"[PYMES] Error buscando en Qdrant o procesando catálogo: {e}")
        return None

class SalesEngageHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context.get('contexto_pyme', {})
        if contexto_pyme.get('aviso_sin_catalogo_dado'): return None
        palabras_venta = ["comprar", "precio", "producto", "catalogo", "stock", "vinos"]
        if any(palabra in pregunta.lower() for palabra in palabras_venta):
            nombre_pyme = self.context.get('nombre_pyme', 'nuestra empresa')
            respuesta = (f"Veo que te interesa consultar sobre nuestros productos en {nombre_pyme}, ¡qué bueno!\n\n"
                         "En este momento no encuentro información detallada en mi sistema para responderte.\n\n"
                         "¿Te gustaría que tome nota de tu consulta y tus datos para que un representante comercial se ponga en contacto contigo a la brevedad?")
            contexto_pyme['aviso_sin_catalogo_dado'] = True
            return {"respuesta": respuesta, "fuente": "handler_sin_catalogo", "estado_respuesta": "info_generica_venta"}
        return None

class FaqHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        try:
            faq = buscar_en_faq_spacy(pregunta, self.context['rubro_obj'].id if self.context.get('rubro_obj') else 1)
            if faq and faq.answer:
                return {"respuesta": reemplazar_placeholders(faq.answer, self.context['user_obj']), "fuente": "faq", "estado_respuesta": "info_faq"}
        except Exception as e:
            logging.warning(f"[PYMES] Error en FaqHandler: {e}")
        return None

class IntentHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        try:
            intent_resp = buscar_en_intents(pregunta, self.context.get('rubro_nombre', 'general'))
            if intent_resp:
                return {"respuesta": reemplazar_placeholders(intent_resp, self.context['user_obj']), "fuente": "intent", "estado_respuesta": "info_intent"}
        except Exception as e:
            logging.warning(f"[PYMES] Error en IntentHandler: {e}")
        return None

class LLMHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        try:
            prompt_pyme = f"""
            "Chatboc", el agente de ventas y atención al cliente de {self.context.get('nombre_pyme', 'la empresa')}.
            Tus Datos de Contacto: Teléfono {self.context.get('telefono', 'no provisto')}, Email {self.context.get('email', 'no provisto')}.
            Regla de Oro: Si no sabes una respuesta sobre un producto, NO inventes. Ofrece amablemente los canales de contacto para que un humano pueda ayudar.
            Historial reciente:
            """
            for msg in self.context.get('mensajes_previos', []):
                prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
            prompt_pyme += f"\n- Cliente: {pregunta}\n- Chatboc:"

            respuesta_llm = get_cohere_response(message=pregunta, chat_history=self.context.get('mensajes_previos', []), preamble=prompt_pyme)
            if respuesta_llm:
                return {"respuesta": reemplazar_placeholders(respuesta_llm, self.context.get('user_obj')), "fuente": "llm", "estado_respuesta": "general_llm_ok"}
        except Exception as e:
            logging.error(f"[PYMES] Error fatal en LLMHandler: {e}", exc_info=True)

        rubro = self.context.get('rubro_nombre', 'empresa')
        sugs = sugerencias_por_rubro(rubro)
        sugerencias_texto = " · ".join(f"“{s}”" for s in sugs if s)
        return {
            "respuesta": f"No encontré una respuesta directa. Probá con: {sugerencias_texto if sugerencias_texto else 'otras palabras clave'}.",
            "fuente": "sugerencia_fallback",
            "estado_respuesta": "sugerencia_fallback"
        }

class EngancheAnonimoHandler(BaseHandler):
    def handle(self, pregunta: str) -> dict | None:
        user = self.context.get("user_obj")
        rubro = self.context.get("rubro_obj")
        tiene_catalogo_para_pyme = False 
        if rubro and hasattr(rubro, 'tiene_catalogo'): 
            tiene_catalogo_para_pyme = rubro.tiene_catalogo
        elif user and hasattr(user, 'tiene_catalogo_personalizado'):
            tiene_catalogo_para_pyme = user.tiene_catalogo_personalizado

        if user is None and tiene_catalogo_para_pyme:
            return {
                "respuesta": "Para darte una atención más personalizada con nuestro catálogo, promociones y atención preferencial, te invito a registrarte o iniciar sesión.",
                "acciones": [
                    {"texto": "Registrarme gratis", "url": "/register"},
                    {"texto": "Planes y beneficios", "url": "/precios"}
                ],
                "fuente": "enganche_anonimo",
                "estado_respuesta": "enganche_anonimo"
            }
        return None 
    
# --- FUNCIÓN ORQUESTADORA PRINCIPAL ---

def responder_pyme(pregunta, user_obj, rubro_obj, **kwargs):
    contexto_previo = kwargs.get('contexto_previo', {})
    contexto_previo_valido = contexto_previo if contexto_previo is not None else {}
    contexto_pyme = contexto_previo_valido.get(CONTEXTO_PYME_SESION, {})

    context = {
        "contexto_pyme": contexto_pyme,
        "user_obj": user_obj, 
        "rubro_obj": rubro_obj,
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
        FollowUpHandler,            
        IntentClassifierPymeHandler,
        PedidoHandler,              
        VectorCatalogHandler,       
        BrokenProductHandler,       
        ClaimHandler,               
        FaqHandler,                 
        IntentHandler,              
        SalesEngageHandler,         
        LLMHandler,                 
        EngancheAnonimoHandler      
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
        respuesta_final = {"respuesta": "Disculpa, no pude procesar tu solicitud en este momento. Por favor, intenta de nuevo o reformula tu pregunta.", 
                           "fuente": "error_no_handler", 
                           "estado_respuesta": "error_critico"}
    
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    asistente_content = str(respuesta_final.get('respuesta','')) 
    historial.append({"role": "assistant", "content": asistente_content})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:] 
    
    try:
        if context['user_id']: 
            db.session.add(Conversacion(
                user_id=context['user_id'], 
                pregunta=pregunta, 
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
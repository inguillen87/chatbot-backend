# services/pymes.py

import logging
import re
import random
import json
import datetime
from flask import session as flask_session

# --- Importaciones de la Base de Datos y Servicios ---
from models import Conversacion, PymeTicket, TicketComentario, PymePedido, db
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.cohere_ai import get_cohere_response
from services.vector_search import buscar_item_vectorizado
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.intent_matcher import buscar_en_intents
from services.ticket_service import servicio_tickets

logger = logging.getLogger(__name__)

# --- Constantes de Sesión ---
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente"
CONTEXTO_PYME_SESION = "contexto_pyme"
MAX_HISTORIAL_CHAT = 14

# --- Función Auxiliar: Generador de Asuntos ---
def _generar_asunto_con_llm(pregunta: str) -> str:
    """Usa el LLM para generar un asunto de ticket conciso y claro."""
    try:
        prompt = f"Resume la siguiente consulta de un cliente en un título breve de 4 a 8 palabras para un ticket de soporte. La consulta es: '{pregunta}'"
        asunto = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un experto en resumir consultas de clientes.")
        return asunto.strip().replace('"', '')
    except Exception as e:
        logger.error(f"[PYME] Error generando asunto con LLM: {e}")
        return (pregunta[:75] + '...') if len(pregunta) > 75 else pregunta

def _extraer_cantidades_con_llm(pregunta_cliente: str, productos_disponibles: list) -> list:
    """
    Usa el LLM para analizar una frase y extraer productos y cantidades en formato JSON.
    """
    # Creamos una lista de nombres de productos para darle contexto al LLM
    nombres_productos = [p.get('nombre', '') for p in productos_disponibles]
    
    prompt = f"""
    Tu tarea es analizar la respuesta de un cliente y extraer los productos y cantidades que solicita, basándote en una lista de productos válidos.
    Tu respuesta DEBE SER ÚNICAMENTE un objeto JSON en formato de lista, nada más. Cada objeto en la lista debe tener "producto", "cantidad" y "unidad".
    Si el cliente no especifica una unidad (como 'caja' o 'botella'), asumí que es 'unidad'.
    Hacé tu mejor esfuerzo por asociar lo que pide el cliente con un producto de la lista de productos válidos.

    **Productos Válidos:** {nombres_productos}

    ---
    EJEMPLO 1:
    Respuesta del Cliente: "quiero 2 cajas de cabernet franc y una de blanco"
    JSON de Salida:
    [
        {{"producto": "cabernet franc", "cantidad": 2, "unidad": "caja"}},
        {{"producto": "blanco dulce", "cantidad": 1, "unidad": "unidad"}}
    ]
    ---
    EJEMPLO 2:
    Respuesta del Cliente: "mandame 3 del malbec y 6 del sauvignon"
    JSON de Salida:
    [
        {{"producto": "malbec", "cantidad": 3, "unidad": "unidad"}},
        {{"producto": "cabernet sauvignon", "cantidad": 6, "unidad": "unidad"}}
    ]
    ---
    
    Ahora, procesá la siguiente respuesta. Recordá: respondé solo con el JSON.

    **Respuesta del Cliente:** "{pregunta_cliente}"
    **JSON de Salida:**
    """
    
    try:
        respuesta_llm = get_cohere_response(message=prompt, chat_history=[], preamble="Eres un asistente experto en procesar pedidos en formato JSON.")
        # Limpiamos la respuesta para asegurarnos de que es un JSON válido
        json_limpio = respuesta_llm.strip().replace("```json", "").replace("```", "")
        return json.loads(json_limpio)
    except Exception as e:
        logger.error(f"[PYMES] Error al extraer cantidades con LLM: {e}")
        # Si el LLM falla, guardamos la respuesta cruda como fallback
        return [{"error": "No se pudo procesar la solicitud", "texto_original": pregunta_cliente}]

# --- PATRÓN DE DISEÑO: ORQUESTADOR CON MANEJADORES (ORDENADO Y CORREGIDO) ---

class BaseHandler:
    """Clase base para todos los manejadores. Define la interfaz."""
    def __init__(self, context):
        self.context = context

    def handle(self, pregunta: str) -> dict | None:
        """Si el manejador puede responder, devuelve un dict. Si no, devuelve None."""
        raise NotImplementedError

class LimitHandler(BaseHandler):
    """Verifica si el usuario ha alcanzado el límite de preguntas de su plan."""
    def handle(self, pregunta: str) -> dict | None:
        if self.context['preguntas_usadas'] >= self.context['limite_preguntas']:
            return {"respuesta": "🔒 Límite de preguntas alcanzado. Actualizá tu plan para continuar.", "fuente": "sistema_limite"}
        return None

class FollowUpHandler(BaseHandler):
    """Manejador de seguimiento para conversaciones en curso."""
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        
        if 'esperando_detalles_reclamo' in contexto_pyme:
            # Asumimos que servicio_tickets.crear_nuevo_ticket devuelve el objeto completo
            ticket_id = contexto_pyme['esperando_detalles_reclamo']
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "user_id": self.context['user_id']})
            self.context['session'][CONTEXTO_PYME_SESION] = {}
            self.context['session'].modified = True
            return {"respuesta": "Perfecto, he añadido tus comentarios al reclamo.", "fuente": "detalle_reclamo_agregado"}
            
        elif 'esperando_datos_reclamo_roto' in contexto_pyme:
            ticket_id = contexto_pyme['esperando_datos_reclamo_roto']
            servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": f"Info adicional del cliente: {pregunta}", "user_id": self.context['user_id']})
            self.context['session'][CONTEXTO_PYME_SESION] = {}
            self.context['session'].modified = True
            return {"respuesta": "Recibido. Gracias por la información. Ya estamos procesando el envío de tu reemplazo.", "fuente": "datos_reemplazo_recibidos"}
            
        return None


class PedidoHandler(BaseHandler):
    """
    Handler especialista que gestiona una conversación de varios pasos
    y USA UN LLM para estructurar los detalles del pedido.
    """
    def handle(self, pregunta: str) -> dict | None:
        logging.warning(f"[DEBUG-PEDIDOS] Contexto de sesión al entrar al PedidoHandler: {self.context['session'].get(CONTEXTO_PYME_SESION)}")
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        
        # --- PASO 2: El usuario envía las cantidades y las procesamos con IA ---
        if 'detallando_pedido' in contexto_pyme:
            productos_para_pedido = contexto_pyme['detallando_pedido']
            
            # ¡AQUÍ USAMOS NUESTRA NUEVA FUNCIÓN INTELIGENTE!
            detalles_estructurados = _extraer_cantidades_con_llm(pregunta, productos_para_pedido)
            
            try:
                nro_pedido = f"P-{random.randint(10000, 99999)}"
                nuevo_pedido = PymePedido(
                    user_id=self.context['user_id'],
                    nro_pedido=nro_pedido,
                    # Guardamos el JSON estructurado en la base de datos
                    detalles=json.dumps(detalles_estructurados, indent=2, ensure_ascii=False),
                    estado="pendiente"
                )
                db.session.add(nuevo_pedido)
                db.session.commit()

                self.context['session'][CONTEXTO_PYME_SESION] = {}
                self.context['session'].modified = True
                
                respuesta = (
                    f"¡Pedido recibido! He generado tu orden con el número **{nro_pedido}**.\n"
                    "Un representante de ventas se pondrá en contacto contigo. ¡Muchas gracias!"
                )
                return {"respuesta": respuesta, "fuente": "handler_pedido_confirmado_ia"}
            except Exception as e:
                logging.error(f"[PYMES] Error fatal guardando pedido estructurado: {e}", exc_info=True)
                db.session.rollback()
                return {"respuesta": "Hubo un problema al guardar tu pedido. Un representante te contactará.", "fuente": "error_handler_pedido"}

        # --- PASO 1: El usuario confirma que quiere iniciar un pedido ---
        elif 'confirmando_pedido' in contexto_pyme:
            palabras_confirmacion = ["si", "sí", "dale", "quiero", "generar", "confirmar", "ok", "me gustaria"]
            pregunta_limpia = pregunta.lower().strip()
            if any(pregunta_limpia.startswith(palabra) for palabra in palabras_confirmacion):
                productos_encontrados = contexto_pyme['confirmando_pedido']
                
                self.context['session'][CONTEXTO_PYME_SESION] = {'detallando_pedido': productos_encontrados}
                self.context['session'].modified = True
                
                respuesta = "¡Perfecto! Para continuar, por favor, decime qué productos y qué cantidades querés. Por ejemplo: 'una caja de cabernet y 2 de blanco dulce'."
                return {"respuesta": respuesta, "fuente": "handler_pedido_iniciado"}

        return None
    
class TicketStatusHandler(BaseHandler):
    """Busca el estado de un ticket existente."""
    def handle(self, pregunta: str) -> dict | None:
        ticket_match = re.search(r"(ticket|reclamo|consulta)\s*#?\s*([0-9]{5,})", pregunta, re.IGNORECASE)
        if ticket_match:
            nro = ticket_match.group(2)
            ticket = PymeTicket.query.filter_by(nro_ticket=int(nro), user_id=self.context['user_id']).first()
            if ticket:
                msg_estado = f"El ticket de reclamo #{nro} (Asunto: '{ticket.asunto}') está en estado: '{ticket.estado}'."
                if ticket.comentarios.count() > 0:
                    ult_com = ticket.comentarios.order_by(TicketComentario.fecha.desc()).first()
                    msg_estado += f" Último comentario: \"{ult_com.comentario}\""
                return {"respuesta": msg_estado, "fuente": "consulta_estado_ticket"}
            else:
                return {"respuesta": f"No se encontró ningún ticket con el número #{nro}.", "fuente": "ticket_no_encontrado"}
        return None

class BrokenProductHandler(BaseHandler):
    """Handler especialista para reclamos de productos rotos o dañados."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_clave = ["botella rota", "llegó roto", "producto dañado", "está roto", "vino roto"]
        if any(keyword in pregunta.lower() for keyword in palabras_clave):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme",
                ticket_data={
                    "pregunta": pregunta, "user_id": self.context['user_id'],
                    "comentario": pregunta, "asunto": asunto,
                    "categoria": "Reclamo - Producto Dañado"
                }
            )
            # Suponemos que crear_nuevo_ticket devuelve el objeto ticket para obtener su ID
            if ticket_creado:
                self.context['session'][CONTEXTO_PYME_SESION] = {'esperando_datos_reclamo_roto': ticket_creado.id}
                self.context['session'].modified = True
            
            nombre_empresa = self.context.get('nombre_pyme', 'nuestra bodega')
            respuesta = (
                f"Lamento muchísimo escuchar eso. En {nombre_empresa} nos aseguramos de que recibas todo en perfectas condiciones.\n\n"
                "No te preocupes, te enviaremos una nueva botella sin ningún costo adicional.\n\n"
                "Para gestionar el nuevo envío, por favor, indícame en tu próximo mensaje el **número del pedido original** (el número de tu compra). Si puedes adjuntar una foto del daño, nos sería de gran ayuda para documentar el incidente."
            )
            return {"respuesta": respuesta, "fuente": "handler_producto_dañado"}
        return None

class ClaimHandler(BaseHandler):
    """Detecta y registra nuevos reclamos genéricos."""
    def handle(self, pregunta: str) -> dict | None:
        palabras_reclamo = ["mal servicio", "problema", "no llegó", "demora", "defectuoso", "reclamo", "falló", "devolución", "cancelar", "queja"]
        if any(w in pregunta.lower() for w in palabras_reclamo):
            asunto = _generar_asunto_con_llm(pregunta)
            ticket_creado = servicio_tickets.crear_nuevo_ticket(
                tipo_ticket="pyme",
                ticket_data={
                    "pregunta": pregunta, "user_id": self.context['user_id'],
                    "comentario": pregunta, "asunto": asunto, "categoria": "Reclamo"
                }
            )
            if ticket_creado:
                respuesta = (
                    f"Lamento mucho el inconveniente. He generado un reclamo con el ticket #{ticket_creado.nro_ticket} (Asunto: '{asunto}'). "
                    "Para poder ayudarte mejor, ¿podrías darme más detalles? Tu próximo mensaje se agregará automáticamente."
                )
                self.context['session'][CONTEXTO_PYME_SESION] = {'esperando_detalles_reclamo': ticket_creado.id}
                self.context['session'].modified = True
                return {"respuesta": respuesta, "fuente": "registro_reclamo"}
        return None

from services.webinfo import obtener_info_web # Asegúrate de que esta importación exista


class VectorCatalogHandler(BaseHandler):
    """
    Busca en el catálogo. Primero en Qdrant (data premium del PDF),
    y si no encuentra, busca en la data del Scraper (data base).
    """
    def handle(self, pregunta: str) -> dict | None:
        palabras_pedido = ["comprar", "precio", "pedido", "catalogo", "stock", "quiero", "vinos", "productos"]
        if any(w in pregunta.lower() for w in palabras_pedido):
            user_id = self.context.get('user_id')
            user_obj = self.context.get('user_obj')
            
            if not user_id or not user_obj:
                return None # No podemos continuar sin un usuario

            # --- CAPA 1: Búsqueda en Qdrant (Catálogo PDF/Excel) ---
            try:
                resultados_qdrant = buscar_item_vectorizado(pregunta, user_id)
                if resultados_qdrant:
                    logging.info(f"[VectorCatalogHandler] Se encontraron {len(resultados_qdrant)} resultados en QDRANT.")
                    respuesta_texto = "¡Claro! En nuestro catálogo detallado encontré esto:\n"
                    items_encontrados = []
                    for item in resultados_qdrant:
                        nombre = item.payload.get('nombre', 'Producto sin nombre')
                        precio = item.payload.get('precio_str', 'Consultar precio')
                        respuesta_texto += f"- **{nombre}**: ${precio}\n"
                        items_encontrados.append(item.payload)
                    
                    respuesta_texto += "\n¿Te gustaría que genere un pedido con alguno de estos productos?"
                    self.context['session'][CONTEXTO_PYME_SESION] = {'confirmando_pedido': items_encontrados}
                    self.context['session'].modified = True
                    return {"respuesta": respuesta_texto, "fuente": "catalogo_qdrant"}
            except Exception as e:
                logging.warning(f"[VectorCatalogHandler] Error buscando en Qdrant: {e}")

            # --- CAPA 2: Búsqueda en Scraper (Conocimiento Base) ---
            # Si no hubo resultados en Qdrant, pasamos a la siguiente capa.
            try:
                link_web = getattr(user_obj, 'link_web', None)
                if link_web:
                    info_web = obtener_info_web(user_id, link_web)
                    if info_web and info_web.get('productos'):
                        logging.info(f"[VectorCatalogHandler] No hubo data en Qdrant, pero se encontró data del SCRAPER.")
                        nombres_productos = [p.get('nombre') for p in info_web['productos'] if p.get('nombre')]
                        if nombres_productos:
                            respuesta_texto = "Revisando la información de nuestro sitio web, te puedo mencionar estos productos:\n"
                            respuesta_texto += "\n".join([f"- {nombre}" for nombre in nombres_productos])
                            respuesta_texto += "\n\n¿Te interesa alguno en particular para darte más detalles?"
                            return {"respuesta": respuesta_texto, "fuente": "catalogo_scraper"}
            except Exception as e:
                 logging.warning(f"[VectorCatalogHandler] Error buscando info del scraper: {e}")

        # Si ninguna de las dos capas funcionó, retorna None y deja pasar al SalesEngageHandler
        return None

class SalesEngageHandler(BaseHandler):
    """
    Handler intermedio. Se activa si hay intención de compra y no hay NINGUNA 
    fuente de datos (ni Qdrant ni Scraper).
    """
    def handle(self, pregunta: str) -> dict | None:
        contexto_pyme = self.context['session'].get(CONTEXTO_PYME_SESION, {})
        # Si ya dimos este aviso, no lo repetimos.
        if contexto_pyme.get('aviso_sin_catalogo_dado'):
            return None

        palabras_venta = ["comprar", "precio", "producto", "catalogo", "stock", "vinos"] # Lista simplificada
        if any(palabra in pregunta.lower() for palabra in palabras_venta):
            nombre_pyme = self.context.get('nombre_pyme', 'nuestra empresa')
            respuesta = (
                f"Veo que te interesa consultar sobre nuestros productos en {nombre_pyme}, ¡qué bueno!\n\n"
                "En este momento no encuentro información detallada en mi sistema para responderte.\n\n"
                "¿Te gustaría que tome nota de tu consulta y tus datos para que un representante comercial se ponga en contacto contigo a la brevedad?"
            )
            
            # ¡LA MEMORIA! Ponemos la bandera para no repetirnos.
            self.context['session'].setdefault(CONTEXTO_PYME_SESION, {})['aviso_sin_catalogo_dado'] = True
            self.context['session'].modified = True

            return {"respuesta": respuesta, "fuente": "handler_sin_catalogo"}
            
        return None
    
class LLMHandler(BaseHandler):
    """
    El último recurso: llama al LLM con un prompt optimizado que incluye
    la instrucción de usar los datos de contacto como fallback.
    """
    def handle(self, pregunta: str) -> dict | None:
        try:
            # Creamos el prompt dinámicamente con los datos de la pyme
            prompt_pyme = f"""
Eres "Chatboc", el agente de ventas y atención al cliente de {self.context['nombre_pyme']}.
**Tus Datos Clave:**
- Contacto Principal: Teléfono {self.context['telefono'] or 'no provisto'}, Email {self.context['email'] or 'no provisto'}.

**Reglas de Oro:**
1. Actúa como un humano experto, amable, profesional y argentino.
2. Sé proactivo. Si podés vender o resolver algo, ofrecelo.
3. Si no sabes una respuesta sobre un producto o servicio específico, NO inventes. En su lugar, ofrecé amablemente los canales de contacto para que un humano pueda ayudar. Di algo como: 'No tengo ese detalle a mano, pero podés consultar directamente a nuestro equipo al {self.context['telefono']}'.
4. Revisa el historial reciente para dar continuidad a la charla.

Historial reciente:
"""
            # Añadimos el historial de chat al prompt
            for msg in self.context['mensajes_previos']:
                prompt_pyme += f"\n- {msg.get('role', 'user')}: {msg.get('content','')}"
            prompt_pyme += f"\n- Cliente: {pregunta}\n- Chatboc:"

            # Llamamos al LLM con el prompt completo
            respuesta_llm = get_cohere_response(
                message=pregunta,
                chat_history=[{"role": m.get("role", "user"), "message": m.get("content", "")} for m in self.context['mensajes_previos']],
                preamble=prompt_pyme
            )
            
            if respuesta_llm:
                respuesta_final = reemplazar_placeholders(respuesta_llm, self.context['user_obj'])
                return {"respuesta": respuesta_final, "fuente": "llm"}

        except Exception as e:
            logging.error(f"[PYMES] Error fatal en LLMHandler: {e}", exc_info=True)
        
        # Si el LLM falla por alguna razón, devolvemos las sugerencias como último recurso
        sugs = sugerencias_por_rubro(self.context['rubro_nombre'])
        return {"respuesta": "No encontré una respuesta directa. Probá con: " + " · ".join(f"“{s}”" for s in sugs if s), "fuente": "sugerencia_fallback"}
# --- FUNCIÓN PRINCIPAL: EL ORQUESTADOR ---
def responder_pyme(pregunta, user_obj, rubro_obj, session_obj=None, **kwargs):
    session = session_obj if session_obj is not None else flask_session
    
    context = {
        "session": session, "user_obj": user_obj, "rubro_obj": rubro_obj,
        "user_id": getattr(user_obj, "id", None),
        "nombre_pyme": getattr(user_obj, "nombre_empresa", "la empresa"),
        "telefono": getattr(user_obj, "telefono", ""), "direccion": getattr(user_obj, "direccion", ""),
        "email": getattr(user_obj, "email", ""), "plan": getattr(user_obj, "plan", "anonimo"),
        "preguntas_usadas": getattr(user_obj, "preguntas_usadas", 0),
        "limite_preguntas": getattr(user_obj, "limite_preguntas", 10),
        "rubro_nombre": getattr(rubro_obj, "nombre", "empresa"),
        "mensajes_previos": session.setdefault(NOMBRE_HISTORIAL_SESION, [])[-12:]
    }
    
    # En services/pymes.py, dentro de responder_pyme()

    handler_chain = [
        LimitHandler, 
        FollowUpHandler, 
        PedidoHandler,
        TicketStatusHandler, 
        BrokenProductHandler,
        ClaimHandler,
        VectorCatalogHandler,      # 1. Primero intenta buscar en el catálogo...
        SalesEngageHandler,        # 2. ...si falla, este nuevo handler lo atrapa.
        FaqHandler, 
        IntentHandler, 
        LLMHandler                 # 3. El LLM es el último, último recurso.
    ]

    respuesta_final = None
    for handler_class in handler_chain:
        handler_instance = handler_class(context)
        respuesta_final = handler_instance.handle(pregunta)
        if respuesta_final:
            break

    if not respuesta_final:
        respuesta_final = {"respuesta": "Disculpa, no pude procesar tu solicitud en este momento. Inténtalo de nuevo.", "fuente": "error_no_handler"}
        logging.error("[PYMES] Ningún handler pudo procesar la pregunta: %s", pregunta)


    # Guardamos la conversación y actualizamos la sesión
    historial = session.setdefault(NOMBRE_HISTORIAL_SESION, [])
    historial.extend([
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": respuesta_final['respuesta']}
    ])
    if len(historial) > MAX_HISTORIAL_CHAT:
        session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]
    session.modified = True
    
    try:
        db.session.add(Conversacion(
            user_id=context['user_id'], pregunta=pregunta,
            respuesta=respuesta_final['respuesta'],
            fuente=respuesta_final.get('fuente', 'desconocida'),
            rubro=context['rubro_nombre']
        ))
        db.session.commit()
    except Exception as e:
        logging.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    return respuesta_final
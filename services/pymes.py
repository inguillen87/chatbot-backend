import logging
import re
import random
import json
from enum import Enum, auto
try:
    from flask import session as flask_session, current_app
except Exception:
    flask_session = {}
    current_app = None

from services.cohere_ai import robust_chat
from models import Conversacion, db, ArchivoAdjunto, PymePedido, User # Asegúrate que PymeTicket, TicketComentario estén importados si se usan directamente
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    armar_respuesta_legible,
    formatear_tabla_catalogo,
    CATALOGO_PYME # Importar para usar como default
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import reemplazar_placeholders
from services.utils_placeholders import sugerencias_por_rubro
from services.logic import detectar_small_talk_con_llm, generar_respuesta_small_talk
from services.ticket_service import servicio_tickets # Asumiendo que PymeTicket está aquí
from services.webinfo import obtener_info_web
from services.preferences import add_preference
# Epic 1 Enhancement: Importing the LLM utility for contact extraction
from .llm_utils import extract_multiple_contact_details_llm
from .common_utils import validar_email, validar_telefono # For validating extracted details

logger = logging.getLogger(__name__)

CONTEXTO_PYME = "contexto_pyme"
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme"
MAX_HISTORIAL_CHAT = 30
CANCEL_KEYWORDS = {"cancel", "cancelar", "cancelalo", "anular", "borrar", "no gracias"}

PROMPT_VALIDAR_PRODUCTO = """
¿El USUARIO menciona un producto o código y una cantidad para comprar? Responde
solo con SI o NO.
MENSAJE DEL USUARIO: "{texto}"
"""

def es_producto_valido_llm(texto: str) -> bool:
    try:
        decision = robust_chat(message=PROMPT_VALIDAR_PRODUCTO.format(texto=texto))
        return decision.strip().upper().startswith("SI") if decision else False
    except Exception as e:
        logger.error(f"[PYME] Error validando producto con LLM: {e}")
    return True # Default a True para no interrumpir flujo si falla LLM

def tiene_archivo_catalogo(user_id: int) -> bool: # This is for the Pyme owner's own catalog
    if not user_id: return False
    try:
        return ArchivoAdjunto.query.filter_by(user_id=user_id, tipo="catalogo").first() is not None
    except Exception: return False

def url_descargar_catalogo() -> str: # This is for the Pyme owner's own catalog
    from flask import request
    return f"{request.url_root.rstrip('/')}/catalogo/descargar"

import ast # For literal_eval

def extraer_productos_llm(texto: str) -> list[dict]:
    prompt = (
        "Analiza el MENSAJE DEL USUARIO para extraer productos, sus cantidades numéricas y sus unidades de medida específicas si se mencionan (ej. 'kilos', 'cajas', 'paquetes', 'docenas', 'metros'). "
        "Responde ÚNICAMENTE con una lista de objetos JSON válida. Cada objeto debe tener:\n"
        "- \"nombre\": El nombre descriptivo del producto (string).\n"
        "- \"cantidad\": La cantidad numérica asociada al producto (integer).\n"
        "- \"unidad\" (opcional): La unidad de medida específica si se menciona (string). Si la unidad es implícita o genérica como 'unidades' o 'ítems', puedes omitirla.\n"
        "IMPORTANTE: La 'cantidad' debe ser siempre el NÚMERO. Si el usuario dice 'una docena de huevos', la cantidad es 12 y la unidad 'huevos' (o puedes poner 'docena' como unidad y cantidad 1, pero sé consistente). Si dice 'un pallet de cemento', cantidad es 1 y unidad 'pallet'. Si dice '2 cajas de leche', cantidad es 2 y unidad 'cajas'.\n"
        "Ejemplos:\n"
        "- MENSAJE: \"quiero 2 kilos de manzanas y 1 pan lactal\"\n"
        "  RESPUESTA: [{\"nombre\": \"manzanas\", \"cantidad\": 2, \"unidad\": \"kilos\"}, {\"nombre\": \"pan lactal\", \"cantidad\": 1}]\n"
        "- MENSAJE: \"necesito 3 cajas de tornillos de 5mm y 1 destornillador grande\"\n"
        "  RESPUESTA: [{\"nombre\": \"tornillos de 5mm\", \"cantidad\": 3, \"unidad\": \"cajas\"}, {\"nombre\": \"destornillador grande\", \"cantidad\": 1}]\n"
        "- MENSAJE: \"un pallet de bolsas de cemento\"\n"
        "  RESPUESTA: [{\"nombre\": \"bolsas de cemento\", \"cantidad\": 1, \"unidad\": \"pallet\"}]\n"
        "- MENSAJE: \"5 metros de cable rojo y 2 enchufes\"\n"
        "  RESPUESTA: [{\"nombre\": \"cable rojo\", \"cantidad\": 5, \"unidad\": \"metros\"}, {\"nombre\": \"enchufes\", \"cantidad\": 2}]\n"
        "- MENSAJE: \"una docena de huevos\"\n"
        "  RESPUESTA: [{\"nombre\": \"huevos\", \"cantidad\": 12}]\n"
        "  (Alternativa aceptable para 'una docena de huevos': [{\"nombre\": \"huevos\", \"cantidad\": 1, \"unidad\": \"docena\"}])\n"
        f"MENSAJE DEL USUARIO: '{texto}'\n"
        "RESPUESTA:"
    )
    resp_content = "" # Para logging en caso de error
    try:
        resp_content = robust_chat(message=prompt)
        if not resp_content:
            logger.warning("[PYME_LLM_PARSE] LLM devolvió respuesta vacía para extraer productos.")
            return []

        datos = []
        try:
            datos = json.loads(resp_content)
        except json.JSONDecodeError as e_json:
            logger.warning(f"[PYME_LLM_PARSE] JSONDecodeError para: '{resp_content}'. Error: {e_json}. Intentando con ast.literal_eval.")
            try:
                # Corregir booleanos/null de JS/Python antes de ast.literal_eval
                # No es perfecto, pero cubre casos comunes de LLMs.
                resp_corrected = resp_content.replace("true", "True").replace("false", "False").replace("null", "None")
                # Intentar quitar un posible ```json ... ``` de markdown si el LLM lo añade
                match_md_json = re.match(r"^\s*```json\s*([\s\S]*?)\s*```\s*$", resp_corrected, re.DOTALL)
                if match_md_json:
                    resp_corrected = match_md_json.group(1)
                
                datos = ast.literal_eval(resp_corrected)
            except (SyntaxError, ValueError) as e_ast:
                logger.error(f"[PYME_LLM_PARSE] ast.literal_eval también falló para: '{resp_corrected}'. Error: {e_ast}. Se devuelve lista vacía.")
                return [] # Fallback a lista vacía si todo falla

        items: list[dict] = []
        if isinstance(datos, list):
            for it in datos:
                if not isinstance(it, dict): # Asegurar que cada item de la lista sea un dict
                    logger.warning(f"[PYME_LLM_PARSE] Item no es un diccionario en datos de LLM: {it}")
                    continue
                nombre = str(it.get("nombre", "")).strip()
                cantidad_raw = it.get("cantidad", 1)
                unidad = str(it.get("unidad", "")).strip() # Extraer unidad
                cantidad = 1
                if isinstance(cantidad_raw, (int, float)):
                    cantidad = int(cantidad_raw)
                elif isinstance(cantidad_raw, str) and cantidad_raw.isdigit():
                    cantidad = int(cantidad_raw)
                
                if cantidad < 1: cantidad = 1 # Asegurar cantidad mínima

                if nombre: 
                    item_data = {"nombre": nombre, "cantidad": cantidad}
                    if unidad: # Añadir unidad si existe
                        item_data["unidad"] = unidad
                    items.append(item_data)
        elif isinstance(datos, dict): # Si el LLM devuelve un solo objeto en lugar de una lista
            logger.warning(f"[PYME_LLM_PARSE] LLM devolvió un diccionario en lugar de una lista: {datos}. Intentando procesarlo.")
            nombre = str(datos.get("nombre", "")).strip()
            cantidad_raw = datos.get("cantidad", 1)
            unidad = str(datos.get("unidad", "")).strip() # Extraer unidad
            cantidad = 1
            if isinstance(cantidad_raw, (int, float)):
                cantidad = int(cantidad_raw)
            elif isinstance(cantidad_raw, str) and cantidad_raw.isdigit():
                cantidad = int(cantidad_raw)
            if cantidad < 1: cantidad = 1
            if nombre:
                item_data = {"nombre": nombre, "cantidad": cantidad}
                if unidad: # Añadir unidad si existe
                    item_data["unidad"] = unidad
                items.append(item_data)
        else:
            logger.error(f"[PYME_LLM_PARSE] Datos de LLM no son lista ni diccionario después de parseo: {datos} (Tipo: {type(datos)}) (Original: '{resp_content}')")

        return items
    except Exception as e_outer:
        logger.exception(f"[PYME] Error general en extraer_productos_llm. Respuesta original del LLM (si hubo): '{resp_content}'. Error: {e_outer}")
    return []

def extraer_productos(texto: str) -> list[dict]:
    partes = re.split(r",| y ", texto)
    items: list[dict] = []
    for p in partes:
        m = re.search(r"(\d+)[^a-zA-Z0-9]*(.+)", p.strip())
        if m:
            try:
                cantidad = int(m.group(1))
                nombre = m.group(2).strip()
                if nombre: items.append({"nombre": nombre, "cantidad": cantidad})
            except ValueError: continue
    partes = re.split(r',| y ', texto)
    items: list[dict] = []
    # Regex mejorado para capturar opcionalmente una unidad después de la cantidad
    # Ejemplo: "2 kg de manzanas", "3 cajas de tornillos", "10 metros de cable"
    # (\d+\.?\d*|\d+)  -> captura números enteros o decimales para cantidad
    # \s*              -> espacio opcional
    # ([a-zA-Záéíóúñ]+)? -> unidad opcional (palabra)
    # [^a-zA-Z0-9]*    -> separador no alfanumérico (como antes)
    # (.+)             -> nombre del producto (como antes)
    pattern = re.compile(r"(\d+\.?\d*|\d+)\s*([a-zA-Záéíóúñ]+)?\s*[^a-zA-Z0-9]*(.+)", re.IGNORECASE)

    for p_str in partes:
        p_str = p_str.strip()
        match = pattern.match(p_str)
        if match:
            try:
                cantidad_str = match.group(1)
                unidad_str = match.group(2) # Puede ser None si no hay unidad explícita
                nombre_str = match.group(3).strip()

                # Eliminar palabras comunes de unidad del inicio del nombre si la unidad ya fue capturada
                if unidad_str and nombre_str.lower().startswith(unidad_str.lower() + " de "):
                    nombre_str = nombre_str[len(unidad_str) + 4:].strip()
                elif unidad_str and nombre_str.lower().startswith(unidad_str.lower() + " "):
                     nombre_str = nombre_str[len(unidad_str) + 1:].strip()


                if nombre_str: # Asegurar que quede un nombre de producto
                    item_data = {"nombre": nombre_str, "cantidad": float(cantidad_str) if '.' in cantidad_str else int(cantidad_str)}
                    if unidad_str:
                        item_data["unidad"] = unidad_str.lower()
                    items.append(item_data)
            except ValueError:
                logger.warning(f"[EXTRACT_REGEX] ValueError parseando: {p_str}")
                continue
            except IndexError:
                 logger.warning(f"[EXTRACT_REGEX] IndexError parseando: {p_str}")
                 continue
        else: # Si el regex mejorado no funciona, intentar con el original como fallback simple
            m_simple = re.search(r"(\d+)[^a-zA-Z0-9]*(.+)", p_str)
            if m_simple:
                try:
                    cantidad = int(m_simple.group(1))
                    nombre = m_simple.group(2).strip()
                    if nombre: items.append({"nombre": nombre, "cantidad": cantidad})
                except ValueError: continue


    return items if items else extraer_productos_llm(texto)

def formatear_carrito(carrito: list[dict], context: dict = None) -> str: # context es opcional por ahora
    if not carrito: return "Tu carrito está vacío."

    lineas_carrito = []
    subtotal_pedido = 0.0
    productos_sin_precio_cont = 0

    for item in carrito:
        nombre = item.get("nombre", "Producto desconocido")
        cantidad_pedida = item.get("cantidad_pedido", 0) # Esta es la cantidad que el usuario quiere pedir
        unidad_pedido_usuario = item.get("unidad_pedido_usuario", "") # Unidad que el usuario especificó al pedir, ej "kilos"
        
        precio_catalogo = item.get("precio_unitario_catalogo", 0.0) 
        unidad_original_catalogo = item.get("unidad_original_catalogo", "") # Ej: "Caja x 6 botellas"
        unidad_descripcion_catalogo = item.get("unidad_descripcion_catalogo", "") # Ej: "Caja" o "Botella"
        cantidad_empaque_catalogo = item.get("cantidad_empaque_catalogo") # Ej: 6 (si es un pack)
        precio_str_catalogo = item.get("precio_str_catalogo", "Consultar")

        linea = f"{cantidad_pedida}"
        if unidad_pedido_usuario:
            linea += f" {unidad_pedido_usuario}"
        linea += f" x {nombre}"
        
        # Mostrar la unidad del catálogo si es relevante y diferente de la pedida, o si la pedida no existe
        display_unidad_info_catalogo = ""
        if unidad_descripcion_catalogo and isinstance(cantidad_empaque_catalogo, int) and cantidad_empaque_catalogo > 1:
            display_unidad_info_catalogo = f"{unidad_descripcion_catalogo} (de {cantidad_empaque_catalogo} items)"
        elif unidad_descripcion_catalogo:
            display_unidad_info_catalogo = unidad_descripcion_catalogo
        elif unidad_original_catalogo:
            display_unidad_info_catalogo = unidad_original_catalogo
        
        if display_unidad_info_catalogo and display_unidad_info_catalogo.lower() != unidad_pedido_usuario.lower():
            linea += f" (presentación: {display_unidad_info_catalogo})"

        # Precio y subtotal
        # Asumimos que precio_catalogo es el precio de la unidad/empaque descrito por display_unidad_info_catalogo
        # precio_catalogo is the price for the 'display_unidad_carrito'
        if precio_catalogo > 0:
            precio_total_item = cantidad_pedida * precio_catalogo
            linea += f" - ${precio_catalogo:,.2f} c/u = ${precio_total_item:,.2f}"
            subtotal_pedido += precio_total_item
            
            # If the price displayed is for a pack, and it's different from individual item price, show individual
            if isinstance(cantidad_empaque, int) and cantidad_empaque > 1:
                # This assumes precio_catalogo is for the pack of 'cantidad_empaque' items
                precio_individual_calc = precio_catalogo / cantidad_empaque
                if abs(precio_individual_calc - precio_catalogo) > 0.01: # Only show if different
                    linea += f" <i style='font-size:smaller;'>(equiv. ${precio_individual_calc:,.2f} por item individual)</i>"
        else:
            linea += f" - {precio_str_catalogo}" # "Consultar" or other string
            productos_sin_precio_cont +=1

        lineas_carrito.append(linea)

    if subtotal_pedido > 0:
        lineas_carrito.append(f"\n**Subtotal del pedido: ${subtotal_pedido:,.2f}**")

    if productos_sin_precio_cont > 0:
        nota_precio = "Algunos precios se confirmarán al finalizar el pedido." if subtotal_pedido > 0 else "Los precios se confirmarán al finalizar el pedido."
        lineas_carrito.append(f"_{nota_precio}_")

    return "\n".join(lineas_carrito)

class PymeConversationState(Enum):
    IDLE = auto()
    ESPERANDO_PRODUCTO = auto()
    CONFIRMANDO_PEDIDO = auto()
    PEDIDO_FINALIZADO = auto()
    # ESPERANDO_CONTACTO = auto() # Se fusionará con la lógica de recopilación de datos del cliente
    ESPERANDO_DATOS_CLIENTE_NOMBRE = auto()
    ESPERANDO_DATOS_CLIENTE_TELEFONO = auto()
    ESPERANDO_DATOS_CLIENTE_DIRECCION = auto()
    ESPERANDO_DATOS_CLIENTE_EMAIL = auto() # Opcional si ya está logueado y tiene email
    ESPERANDO_CONFIRMACION_FINAL_CON_DATOS = auto() # Nuevo estado para confirmar pedido Y datos del cliente
    ESPERANDO_FEEDBACK = auto() # Nuevo estado para solicitar feedback
    ESPERANDO_NUMERO_TICKET = auto() # Para reclamos/consultas, no para pedidos
    ESPERANDO_CONFIRMACION_CIERRE = auto() # Para reclamos/consultas
    ESPERANDO_CALIFICACION = auto()
    ESPERANDO_DETALLES_RECLAMO = auto()

def serialize_state(state): return state.name if state else None
def deserialize_state(value):
    if not value: return None
    try: return PymeConversationState[value]
    except KeyError: return None

PROMPT_CLASIFICAR_INTENCION = """
Sos el cerebro comercial de un chatbot para una pyme. Analizá la PREGUNTA DEL USUARIO y respondé sólo con una de estas intenciones de la lista.
Si no encaja claramente, usa 'pregunta_ambigua'.

INTENCIONES POSIBLES:
- saludo: El usuario está saludando.
    Ejemplos: "hola", "buenas tardes", "qué tal?"
- ver_catalogo: El usuario quiere ver productos, el catálogo general o buscar tipos de productos.
    Ejemplos: "qué productos tienen?", "mostrame el catálogo", "tienen herramientas?", "busco camperas"
- consultar_ofertas: El usuario pregunta por ofertas, promociones o descuentos.
    Ejemplos: "qué ofertas hay?", "tienen alguna promo?", "hay descuentos hoy?"
- iniciar_pedido: El usuario quiere comprar o pedir productos específicos, usualmente mencionando producto y/o cantidad.
    Ejemplos: "quiero 2kg de pan", "necesito una docena de facturas", "mandame 3 de esos tornillos", "agregar al carrito la remera azul"
- agregar_al_carrito: El usuario confirma que quiere agregar un producto previamente discutido al carrito.
    Ejemplos: "sí, agregalo", "dale, ponelo en el carrito", "lo quiero"
- ver_carrito: El usuario quiere revisar los productos que ya ha seleccionado en su carrito.
    Ejemplos: "qué tengo en el carrito?", "mostrar mi pedido", "cómo va mi compra?"
- eliminar_del_carrito: El usuario quiere quitar un producto de su carrito.
    Ejemplos: "sacar el pan del carrito", "ya no quiero las facturas", "eliminar el último producto"
- finalizar_pedido: El usuario indica que quiere completar la compra de los productos en su carrito.
    Ejemplos: "eso es todo", "quiero pagar", "finalizar la compra", "terminar pedido"
- continuar_flujo: El usuario da una respuesta afirmativa o de continuación a una pregunta del bot en medio de un flujo (ej. confirmación de pedido, seguir agregando productos).
    Ejemplos: "sí", "ok", "dale", "continuar", "perfecto"
- cancelar_flujo: El usuario quiere detener la acción actual (ej. cancelar un pedido, salir de una consulta).
    Ejemplos: "cancelar", "no gracias", "mejor no", "olvidalo"
- pregunta_faq: El usuario hace una pregunta general sobre la empresa, envíos, pagos, horarios, etc., que podría estar en una FAQ.
    Ejemplos: "cuánto cuesta el envío?", "aceptan tarjeta?", "dónde están ubicados?", "cuál es su horario?"
- consultar_estado_ticket: El usuario quiere saber sobre un pedido o consulta anterior (si la pyme usa sistema de tickets).
    Ejemplos: "cómo va mi pedido 123?", "estado de mi consulta P-456"
- hablar_con_agente: El usuario solicita explícitamente hablar con una persona.
    Ejemplos: "quiero hablar con un vendedor", "pasame con alguien de atención al cliente"
- pregunta_ambigua: La pregunta del usuario no es clara o no encaja en las otras categorías.
    Ejemplos: "y eso?", "contame más", "no sé"

PREGUNTA DEL USUARIO: "{pregunta_usuario}"
INTENCIÓN: """

def clasificar_intencion_llm(pregunta):
    prompt = PROMPT_CLASIFICAR_INTENCION.format(pregunta_usuario=pregunta)
    try:
        res = robust_chat(message=prompt)
        return res.strip().lower() if res else "pregunta_ambigua"
    except Exception as e:
        logger.error(f"[PYME] Error clasificando intención: {e}")
        return "pregunta_ambigua"

def _clasificar_intencion_pyme_con_llm(pregunta: str) -> str: return clasificar_intencion_llm(pregunta)

def analizar_sentimiento_llm(texto: str) -> str:
    prompt = ("Analiza el sentimiento del siguiente texto y responde solo 'positivo', 'negativo' o 'neutral'.\n"
              f"TEXTO: '{texto}'\nSENTIMIENTO:")
    try:
        res = robust_chat(message=prompt)
        sentimiento = res.strip().lower()
        return sentimiento if sentimiento in {"positivo", "negativo"} else "neutral"
    except Exception as e:
        logger.error(f"[PYME] Error analizando sentimiento: {e}")
        return "neutral"

class BaseHandler:
    def __init__(self, context): self.context = context
    def handle(self, pregunta): raise NotImplementedError

class SaludoHandler(BaseHandler):
    def handle(self, pregunta):
        nombre = self.context.get("nombre_pyme", "la empresa")
        return {"respuesta": f"¡Hola! Soy tu asistente para {nombre}. ¿En qué puedo ayudarte hoy?",
                "fuente": "saludo", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Ver ofertas", "action": "ver_ofertas"}]}

class CatalogoHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get("user_id")
        if not user_id: return {"respuesta": "Iniciá sesión para ver el catálogo.", "fuente": "catalogo_sin_login"}
        
        resultados = buscar_catalogo_qdrant(user_id=user_id, pregunta=pregunta, categoria=self.context.get("rubro_nombre"), limite=10, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME))
        add_preference("busquedas", pregunta)
        botones_base = []

        if resultados:
            # Usamos armar_respuesta_legible en lugar de formatear_tabla_catalogo para consistencia y mejor formato
            respuesta_catalogo = armar_respuesta_legible(resultados, max_items=7) # Aumentamos un poco para catálogo
            mensaje = f"Encontré esto para ti:\n\n{respuesta_catalogo}\n\nSi querés pedir alguno de estos, dime cuál y qué cantidad. También puedes ver más opciones o descargar el catálogo completo si está disponible."
            fuente = "catalogo_vector_dinamico_legible"
            botones_base = [{"texto": "Hacer un pedido", "action": "iniciar_pedido"}, {"texto": "Buscar otra cosa", "action": "ver_catalogo"}]
        else:
            if es_producto_valido_llm(pregunta): # Si la pregunta parecía ser un producto específico
                mensaje = f"Hmm, no encontré resultados exactos para '{pregunta}'. \n\n¿Te gustaría que intente con una búsqueda más general, ver el catálogo completo (si está disponible), o prefieres hablar con un agente?"
                fuente = "catalogo_no_encontrado_especifico"
                botones_base = [{"texto": "Buscar algo más general", "action": "ver_catalogo"}, 
                                # {"texto": "Ver catálogo completo", "action": "ver_catalogo_completo_accion"}, # Replaced by download
                                {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
            else: # No specific product in query, but no results from general search
                mensaje = "No encontré productos que coincidan con tu búsqueda. Puedes intentar con otras palabras."
                fuente = "catalogo_no_encontrado_general"
                # botones_base already includes "Hablar con un agente" if this path is taken often.
                # Default buttons for no results could be just "Hablar con un agente" or "Intentar otra búsqueda".
                # The download link will be added below if available.
                if not botones_base: # Ensure there's always some action
                    botones_base = [{"texto": "Intentar otra búsqueda", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
            # Restore original logic for pyme owner download if applicable
            if tiene_archivo_catalogo(user_id) and any(k in pregunta.lower() for k in ["descargar", "pdf", "completo"]):
                 botones_base.append({"texto": "Descargar mi catálogo", "action": "descargar_catalogo"}) # Action for owner
                 mensaje += f"\n\nPodés [descargar tu catálogo completo aquí]({url_descargar_catalogo()})."


        return {"respuesta": mensaje, "fuente": fuente, "botones": botones_base}

class OfertasHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get("user_id")
        if not user_id: return {"respuesta": "Inicia sesión para ver las ofertas.", "fuente": "ofertas_sin_login"}
        
        resultados_ofertas = buscar_catalogo_qdrant(user_id=user_id, pregunta="ofertas promociones descuentos", limite=5, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME), en_promocion=True)
        
        mensaje = ""
        botones = []
        fuente = ""

        if resultados_ofertas:
            mensaje = "Estas son algunas de nuestras ofertas destacadas:\n" + armar_respuesta_legible(resultados_ofertas, max_items=5) + "\n\n¿Te interesa alguna o quieres ver más?"
            fuente = "ofertas_dinamicas"
            botones = [{"texto": "Hacer un pedido", "action": "iniciar_pedido"}, {"texto": "Ver catálogo", "action": "ver_catalogo"}]
        else:
            mensaje = "Por el momento no tenemos ofertas especiales destacadas, pero puedes ver nuestro catálogo completo."
            fuente = "sin_ofertas_dinamicas"
            botones = [{"texto": "Ver catálogo", "action": "ver_catalogo"}]
            
        return {"respuesta": mensaje, "fuente": fuente, "botones": botones}

class SmallTalkHandler(BaseHandler):
    def handle(self, pregunta): return {"respuesta": generar_respuesta_small_talk(pregunta), "fuente": "smalltalk_pyme_llm"}

class SentimentHandler(BaseHandler):
    def __init__(self, context, sentimiento):
        super().__init__(context)
        self.sentimiento = sentimiento

    def handle(self, pregunta):
        if self.sentimiento == "negativo":
            pyme_ctx = self.context.setdefault(CONTEXTO_PYME, flask_session.get(CONTEXTO_PYME, {}))
            pyme_ctx["pregunta_reclamo_original"] = pregunta
            pyme_ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_DETALLES_RECLAMO)
            flask_session[CONTEXTO_PYME] = pyme_ctx
            self.context[CONTEXTO_PYME] = pyme_ctx
            return {
                "respuesta": "Lamento mucho escuchar eso. Para poder ayudarte mejor, ¿podrías contarme un poco más sobre lo que sucedió o cuál es tu reclamo? Así puedo pasarle la información a un agente.",
                "fuente": "sentimiento_negativo_pide_detalles",
                "botones": [{"texto": "Prefiero no dar detalles", "action": "escalar_sin_detalles"}],
                "estado_respuesta": "pyme_esperando_detalles_reclamo" 
            }
        return {"respuesta": "¡Gracias por tu comentario!", "fuente": "sentimiento_positivo", "botones": [{"texto": "Ver ofertas", "action": "ver_ofertas"}]}

class PedidoHandler(BaseHandler):
    def _sugerir_productos_complementarios(self, ultimo_producto_nombre: str, carrito_actual: list, items_recien_agregados: list) -> tuple[str, list]:
        user_id = self.context.get("user_id")
        if not user_id or not items_recien_agregados:
            return "", []

        categoria_ultimo_producto = None
        # Placeholder: Lógica para obtener categoría del último producto si es necesario para búsqueda contextual.
        # Por ahora, se usará una búsqueda más genérica.

        pregunta_sugerencias = "ofertas destacadas"
        if categoria_ultimo_producto: # Ejemplo si tuviéramos la categoría
            pregunta_sugerencias = f"{categoria_ultimo_producto} ofertas"

        productos_sugeridos_qdrant = buscar_catalogo_qdrant(
            user_id=user_id,
            pregunta=pregunta_sugerencias,
            limite=5,
            en_promocion=True,
            coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME)
        )

        sugerencias_finales_texto = []
        sugerencias_botones = []
        nombres_en_carrito_y_agregados = {item['nombre'].lower().strip() for item in carrito_actual}
        for item_agregado in items_recien_agregados: # Excluir los que se acaban de agregar explícitamente
            nombres_en_carrito_y_agregados.add(item_agregado['nombre'].lower().strip())

        if productos_sugeridos_qdrant:
            for oferta_qdrant in productos_sugeridos_qdrant:
                payload = getattr(oferta_qdrant, "payload", {})
                nombre_oferta = payload.get("nombre", "").strip()

                if nombre_oferta and nombre_oferta.lower() not in nombres_en_carrito_y_agregados:
                    precio_val = payload.get("precio_float")
                    precio_str_original = payload.get("precio_str", "")
                    unidad_desc_parsed = payload.get("unidad_descripcion", "")
                    cantidad_empaque_val = payload.get("cantidad_empaque")
                    promocion_texto_oferta = payload.get("promocion_info", "")

                    precio_display_sugerencia = "Consultar"
                    if precio_val is not None:
                        precio_display_sugerencia = f"${float(precio_val):,.2f}"
                    elif precio_str_original:
                        precio_display_sugerencia = precio_str_original

                    texto_sugerencia_item = f"'{nombre_oferta}'"

                    display_unidad_sug = ""
                    if unidad_desc_parsed and cantidad_empaque_val and isinstance(cantidad_empaque_val, int) and cantidad_empaque_val > 1:
                        display_unidad_sug = f" ({unidad_desc_parsed} x{cantidad_empaque_val})"
                    elif unidad_desc_parsed:
                        display_unidad_sug = f" ({unidad_desc_parsed})"

                    if display_unidad_sug:
                        texto_sugerencia_item += display_unidad_sug

                    texto_sugerencia_item += f" a {precio_display_sugerencia}"

                    if promocion_texto_oferta:
                        texto_sugerencia_item += f" <b style='color:green;'>({promocion_texto_oferta})</b>"

                    sugerencias_finales_texto.append(texto_sugerencia_item)
                    sugerencias_botones.append({"texto": f"Agregar: {nombre_oferta}", "action": f"agregar_sugerido_{nombre_oferta.replace(' ', '_')[:30]}"}) # Limitar longitud del action

                if len(sugerencias_finales_texto) >= 2:
                    break

            if sugerencias_finales_texto:
                sugerencia_final_str = " y ".join(sugerencias_finales_texto)
                if sugerencia_final_str:
                    return f"\n\n✨ ¡Quizás también te interese! Tenemos {sugerencia_final_str}. ¿Añadimos alguno?", sugerencias_botones
        return "", []

    def handle(self, pregunta):
        ctx = self.context.setdefault(CONTEXTO_PYME, flask_session.get(CONTEXTO_PYME, {}))
        estado = deserialize_state(ctx.get("estado_conversacion")) or PymeConversationState.IDLE
        texto = pregunta.lower()
        intentos = ctx.get("reintentos", 0)
        carrito = ctx.setdefault("carrito", [])
        self.context["coleccion_qdrant"] = self.context.get("coleccion_qdrant") or coleccion_catalogo_para_rubro(self.context.get("rubro_nombre", "generico"))

        if estado == PymeConversationState.ESPERANDO_PRODUCTO:
            if any(k in texto for k in CANCEL_KEYWORDS):
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "Pedido cancelado. ¿Necesitás otra cosa?", "fuente": "pedido_cancelado", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]}
            
            if any(k in texto for k in ["mostrar", "carrito", "pedido", "ver mi pedido"]):
                flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": f"Tu pedido actual es:\n{formatear_carrito(carrito)}\n¿Quieres agregar/quitar algo o finalizar el pedido?", "fuente": "mostrar_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}

            if any(k in texto for k in ["sacar", "quitar", "eliminar", "remover"]):
                producto_a_eliminar = texto
                for kw in ["sacar", "quitar", "eliminar", "remover"]: producto_a_eliminar = producto_a_eliminar.replace(kw, "").strip()
                original_len = len(carrito)
                carrito[:] = [item for item in carrito if not (producto_a_eliminar and producto_a_eliminar in item["nombre"].lower())]
                flask_session[CONTEXTO_PYME] = ctx
                if len(carrito) < original_len: return {"respuesta": f"Producto/s eliminados. Carrito:\n{formatear_carrito(carrito)}", "fuente": "producto_eliminado"}
                return {"respuesta": f"No encontré '{producto_a_eliminar}' en tu carrito. Carrito actual:\n{formatear_carrito(carrito)}", "fuente": "producto_no_encontrado_eliminar"}

            if "cambiar" in texto and ("cantidad" in texto or re.search(r'\d+', texto)):
                items_cambio = extraer_productos(texto.replace("cambiar", "").strip())
                actualizado_nombres = []
                if items_cambio:
                    for it_c in items_cambio:
                        for c_item in carrito:
                            if it_c["nombre"].lower() in c_item["nombre"].lower() or c_item["nombre"].lower() in it_c["nombre"].lower():
                                c_item["cantidad"] = it_c["cantidad"]
                                actualizado_nombres.append(c_item["nombre"])
                                break 
                flask_session[CONTEXTO_PYME] = ctx
                if actualizado_nombres: return {"respuesta": f"Cantidades actualizadas para: {', '.join(actualizado_nombres)}. Carrito:\n{formatear_carrito(carrito)}", "fuente": "cantidades_actualizadas"}
                return {"respuesta": "No pude identificar qué producto o cantidad cambiar. Ejemplo: 'cambiar 2 [nombre_producto]'.", "fuente": "producto_no_encontrado_cambiar"}

            if any(k in texto for k in ["finalizar", "terminar", "eso es todo"]):
                if not carrito: return {"respuesta": "Tu carrito está vacío. ¿Quieres agregar algo antes?", "fuente": "finalizar_carrito_vacio", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}]}
                ctx.update({"estado_conversacion": serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": f"Perfecto. Este es tu pedido:\n{formatear_carrito(carrito)}\n\nEl total es ESTIMATIVO. ¿Confirmas?", "fuente": "confirmando_pedido", "botones": [{"texto": "Sí, confirmar", "action": "confirmar_pedido"}, {"texto": "Modificar pedido", "action": "modificar_pedido"}, {"texto": "Cancelar", "action": "cancelar_pedido"}]}

            items_agregados_info = []
            if not es_producto_valido_llm(pregunta): # Usar pregunta original
                intentos += 1; ctx["reintentos"] = intentos; flask_session[CONTEXTO_PYME] = ctx
                if intentos >= 2:
                    ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                    return {"respuesta": "Parece que no nos entendemos. Cancelé el pedido. Podemos intentar de nuevo o ver el catálogo.", "fuente": "pedido_cancelado_reintentos_val", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con agente", "action": "hablar_con_agente"}]}
                return {"respuesta": "No entendí qué producto agregar. Indica nombre o código y cantidad. Para anular, escribe 'cancelar'.", "fuente": "producto_no_reconocido_val"}

            items_extraidos = extraer_productos(texto)
            mensaje_principal = ""
            productos_no_encontrados_o_sin_precio = []

            if items_extraidos:
                for item_ext in items_extraidos:
                    # Buscar producto en el catálogo para obtener precio y unidad
                    resultados_qdrant = buscar_catalogo_qdrant(
                        user_id=self.context.get("user_id"),
                        pregunta=item_ext["nombre"],
                        limite=1,
                        coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME)
                    )

                    nombre_producto_catalogo = item_ext["nombre"]
                    precio_unitario_catalogo = 0.0
                    unidad_catalogo = ""
                    precio_str_catalogo = "Consultar"
                    producto_encontrado_en_qdrant = False

                    if resultados_qdrant:
                        payload = getattr(resultados_qdrant[0], "payload", {})
                        if payload:
                            nombre_producto_catalogo = payload.get("nombre", item_ext["nombre"])
                            precio_unitario_catalogo = payload.get("precio_float", 0.0)
                            if precio_unitario_catalogo is None: precio_unitario_catalogo = 0.0 # Asegurar que sea float
                            unidad_catalogo = payload.get("unidad", "")

                            if precio_unitario_catalogo > 0:
                                precio_str_catalogo = f"${precio_unitario_catalogo:,.2f}"
                            elif payload.get("precio_str"):
                                precio_str_catalogo = payload.get("precio_str")

                            producto_encontrado_en_qdrant = True

                    if not producto_encontrado_en_qdrant or precio_unitario_catalogo == 0:
                        productos_no_encontrados_o_sin_precio.append(nombre_producto_catalogo)
                    
                    # Extraer toda la info de unidad del payload para el carrito
                    # 'unidad_catalogo' era payload.get("unidad", "") -> ahora es payload.get("unidad_original", "")
                    unidad_original_qdrant = payload.get("unidad_original", unidad_catalogo) # unidad_catalogo was a fallback
                    unidad_desc_qdrant = payload.get("unidad_descripcion", "") 
                    cantidad_empaque_qdrant = payload.get("cantidad_empaque")


                    # Lógica para agregar o actualizar cantidad en carrito
                    found_in_cart = False
                    for item_car_existente in carrito:
                        # Comparar también por unidad si ambas existen, para diferenciar "1 kg de X" de "1 bolsa de X" si son items distintos en carrito
                        match_nombre = nombre_producto_catalogo.lower() == item_car_existente["nombre"].lower()
                        match_unidad = item_ext.get("unidad","").lower() == item_car_existente.get("unidad_pedido_usuario","").lower() if item_ext.get("unidad") and item_car_existente.get("unidad_pedido_usuario") else True

                        if match_nombre and match_unidad:
                            item_car_existente["cantidad_pedido"] = item_car_existente.get("cantidad_pedido",0) + item_ext["cantidad"]
                            items_agregados_info.append(item_car_existente)
                            found_in_cart = True
                            break

                    if not found_in_cart:
                        item_para_carrito_nuevo = {
                            "nombre": nombre_producto_catalogo,
                            "cantidad_pedido": item_ext["cantidad"],
                            "unidad_pedido_usuario": item_ext.get("unidad", ""), # Unidad especificada por el usuario
                            "precio_unitario_catalogo": precio_unitario_catalogo,
                            "unidad_original_catalogo": unidad_original_qdrant,
                            "unidad_descripcion_catalogo": unidad_desc_qdrant,
                            "cantidad_empaque_catalogo": cantidad_empaque_qdrant,
                            "precio_str_catalogo": precio_str_catalogo
                        }
                        carrito.append(item_para_carrito_nuevo)
                        items_agregados_info.append(item_para_carrito_nuevo)

                    add_preference("productos", nombre_producto_catalogo)

                mensaje_principal = f"Agregado. Carrito actual:\n{formatear_carrito(carrito, self.context)}"
                if productos_no_encontrados_o_sin_precio:
                    nombres_problematicos = list(set(productos_no_encontrados_o_sin_precio)) # Evitar duplicados
                    mensaje_principal += f"\n\n_Nota: No se encontró precio para: {', '.join(nombres_problematicos)}. Se mostrarán como 'Consultar' y se confirmarán al finalizar el pedido._"
                ctx["reintentos"] = 0
            else: # No se extrajeron items del mensaje del usuario
                if not carrito and len(texto.split()) <= 3: # Pregunta corta, probablemente pidiendo iniciar
                    mensaje_principal = "¿Qué producto o productos y qué cantidades te gustaría pedir?"
                elif carrito: # Ya hay cosas en el carrito, pero no se entendió lo último
                    mensaje_principal = f"No identifiqué nuevos productos en tu último mensaje. Tu carrito actual es:\n{formatear_carrito(carrito, self.context)}\n\n¿Qué más quieres agregar o hacemos para finalizar?"
                else: # Sin carrito y sin entender el producto
                    sug_fb = buscar_catalogo_qdrant(user_id=self.context.get("user_id"), pregunta=pregunta, limite=2, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME))
                    if sug_fb:
                        mensaje_principal = f"No entendí bien qué producto buscas. Quizás te interese algo de esto:\n{armar_respuesta_legible(sug_fb, max_items=2)}\n\nO puedes intentar describir el producto de nuevo."
                    else:
                        mensaje_principal = "No entendí qué producto agregar. Puedes ver el catálogo o intentar describirlo de nuevo (ej: '2 kilos de pan')."
            
            flask_session[CONTEXTO_PYME] = ctx
            # Usar items_agregados_info que contiene los productos con su info de catálogo (potencialmente)
            ultimo_agregado_nombre = items_agregados_info[-1]['nombre'] if items_agregados_info else ""

            sug_compl_texto, sug_compl_botones = self._sugerir_productos_complementarios(ultimo_agregado_nombre, carrito, items_agregados_info)

            respuesta_dict = {
                "respuesta": f"{mensaje_principal}{sug_compl_texto}",
                "fuente": "pedido_progreso_sug" if sug_compl_texto else "pedido_progreso",
                "estado_respuesta": "pyme_pregunta_pedido"
            }
            botones_existentes = [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]
            respuesta_dict["botones"] = botones_existentes + sug_compl_botones
            return respuesta_dict
        
        # Manejar acción de agregar producto sugerido
        action = pregunta.lower() # Asumimos que la 'pregunta' puede ser la acción del botón
        if isinstance(self.context.get("action"), str): # Priorizar action del payload si existe
            action = self.context.get("action").lower()

        if action.startswith("agregar_sugerido_"):
            nombre_sugerido_codificado = action.replace("agregar_sugerido_", "")
            nombre_sugerido = nombre_sugerido_codificado.replace("_", " ") # Decodificar simple

            # Buscar el producto sugerido en Qdrant para obtener todos sus detalles
            resultados_qdrant_sug = buscar_catalogo_qdrant(
                user_id=self.context.get("user_id"),
                pregunta=nombre_sugerido, # Búsqueda exacta por nombre
                limite=1,
                coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME)
            )
            if resultados_qdrant_sug:
                payload_sug = getattr(resultados_qdrant_sug[0], "payload", {})
                if payload_sug:
                    item_sugerido_para_carrito = {
                        "nombre": payload_sug.get("nombre", nombre_sugerido),
                        "cantidad_pedido": 1, # Asumir cantidad 1 para sugerencias
                        "unidad_pedido_usuario": "", # Asumir unidad base
                        "precio_unitario_catalogo": payload_sug.get("precio_float", 0.0),
                        "unidad_original_catalogo": payload_sug.get("unidad_original", ""),
                        "unidad_descripcion_catalogo": payload_sug.get("unidad_descripcion", ""),
                        "cantidad_empaque_catalogo": payload_sug.get("cantidad_empaque"),
                        "precio_str_catalogo": payload_sug.get("precio_str", "Consultar")
                    }
                    carrito.append(item_sugerido_para_carrito)
                    ctx["reintentos"] = 0
                    flask_session[CONTEXTO_PYME] = ctx
                    sug_compl_texto_2, sug_compl_botones_2 = self._sugerir_productos_complementarios(nombre_sugerido, carrito, [item_sugerido_para_carrito])
                    respuesta_dict_sug = {
                        "respuesta": f"¡'{nombre_sugerido}' agregado al carrito!\n{formatear_carrito(carrito, self.context)}{sug_compl_texto_2}",
                        "fuente": "pedido_sugerencia_agregada",
                        "estado_respuesta": "pyme_pregunta_pedido"
                    }
                    botones_exist_sug = [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]
                    respuesta_dict_sug["botones"] = botones_exist_sug + sug_compl_botones_2
                    return respuesta_dict_sug
            # Si no se encontró el producto sugerido (raro, pero posible), simplemente mostrar el carrito
            return {"respuesta": f"No pude agregar la sugerencia. Carrito actual:\n{formatear_carrito(carrito, self.context)}", "fuente": "pedido_progreso", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}

        elif estado == PymeConversationState.CONFIRMANDO_PEDIDO:
            if any(k in texto for k in CANCEL_KEYWORDS):
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "Pedido cancelado. ¿Necesitás otra cosa?", "fuente": "pedido_cancelado_confirmacion", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con agente", "action": "hablar_con_agente"}]}

            if texto.strip() in {"si", "sí", "confirmo", "confirmar"}:
                # Antes de finalizar, verificar si necesitamos datos del cliente
                viewer_user_id = self.context.get("cliente_id")
                cliente = User.query.get(viewer_user_id) if viewer_user_id else None

                # Pre-fill from profile if available and not already in context
                if cliente:
                    if not ctx.get("nombre_cliente") and cliente.name:
                        ctx["nombre_cliente"] = cliente.name
                    if not ctx.get("telefono_cliente") and cliente.telefono:
                         # Validate phone from profile before using
                        if validar_telefono(cliente.telefono):
                            ctx["telefono_cliente"] = cliente.telefono
                        else:
                            logger.warning(f"Invalid phone format in profile for user {cliente.id}: {cliente.telefono}")
                    if not ctx.get("email_cliente") and cliente.email:
                        if validar_email(cliente.email): # Validate email from profile
                            ctx["email_cliente"] = cliente.email
                        else:
                            logger.warning(f"Invalid email format in profile for user {cliente.id}: {cliente.email}")
                    # Direccion is usually specific to the order, so we don't pre-fill from general profile address here.

                # Check which essential fields are still missing for the order
                campos_necesarios_pedido = ["nombre_cliente", "telefono_cliente", "direccion_cliente"] # Email es opcional
                campos_faltantes = [campo for campo in campos_necesarios_pedido if not ctx.get(campo)]

                if campos_faltantes:
                    if "nombre_cliente" in campos_faltantes:
                        ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE)
                        flask_session[CONTEXTO_PYME] = ctx
                        return {"respuesta": "¡Casi listo! Para completar tu pedido, ¿podrías decirme tu nombre completo?", "fuente": "solicitando_nombre_cliente"}
                    elif "telefono_cliente" in campos_faltantes:
                        ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO)
                        flask_session[CONTEXTO_PYME] = ctx
                        return {"respuesta": "Entendido. Ahora, ¿cuál es tu número de teléfono (con código de área)?", "fuente": "solicitando_telefono_cliente"}
                    elif "direccion_cliente" in campos_faltantes:
                        ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION)
                        flask_session[CONTEXTO_PYME] = ctx
                        return {"respuesta": "Perfecto. ¿Cuál es la dirección de entrega para este pedido?", "fuente": "solicitando_direccion_cliente"}
                else:
                    # Todos los datos necesarios están, pasar a confirmación final con datos
                    ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS)
                    flask_session[CONTEXTO_PYME] = ctx
                    resumen_pedido = formatear_carrito(carrito)
                    resumen_datos = f"Nombre: {ctx.get('nombre_cliente', 'N/A')}\nTeléfono: {ctx.get('telefono_cliente', 'N/A')}\nDirección de Entrega: {ctx.get('direccion_cliente', 'N/A')}"
                    if ctx.get("email_cliente"): # Email es opcional
                        resumen_datos += f"\nEmail: {ctx['email_cliente']}"
                    return {
                        "respuesta": f"¡Excelente! Revisemos todo antes de finalizar:\n\n**Pedido:**\n{resumen_pedido}\n\n**Datos de Contacto y Entrega:**\n{resumen_datos}\n\n¿Es todo correcto para generar el pedido?",
                        "fuente": "confirmando_pedido_con_datos",
                        "botones": [{"texto": "Sí, todo correcto", "action": "confirmar_final_con_datos"}, {"texto": "Modificar datos", "action": "modificar_datos_cliente"}, {"texto": "Modificar pedido", "action": "modificar_pedido"}]
                    }

            elif any(k in texto for k in ["modificar", "cambiar", "agregar", "quitar"]): # User wants to modify order items
                ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_PRODUCTO), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": f"Ok, volvemos a tu pedido. Carrito actual:\n{formatear_carrito(carrito)}\n¿Qué quieres hacer?", "fuente": "modificando_pedido_confirmacion", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}

            # Fallback if input is not a clear "yes", "cancel", or "modify order"
            intentos += 1; ctx["reintentos"] = intentos; flask_session[CONTEXTO_PYME] = ctx
            if intentos >= 2:
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "No pudimos confirmar tu pedido y fue cancelado. Puedes intentar de nuevo.", "fuente": "pedido_cancelado_confirmacion_fallida_reintentos", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}]}
            return {"respuesta": "¿Confirmás el pedido? (Sí/Modificar/Cancelar)", "fuente": "reconfirmando_pedido", "botones": [{"texto": "Sí, confirmar", "action": "confirmar_pedido"}, {"texto": "Modificar pedido", "action": "modificar_pedido"}, {"texto": "Cancelar", "action": "cancelar_pedido"}]}

        elif estado == PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE:
            # Define potential fields to extract, prioritizing the current one (nombre)
            # but also allowing others if provided by the user.
            potential_fields_to_extract = ['nombre_cliente', 'telefono_cliente', 'direccion_cliente', 'email_cliente']

            # Attempt to extract multiple details using LLM
            # pregunta contains the raw user input
            extracted_data_llm = extract_multiple_contact_details_llm(pregunta, potential_fields_to_extract)
            llm_updated_any_field = False

            if extracted_data_llm:
                logger.info(f"[PYME_LLM_CONTACT] Extracted by LLM (Nombre Stage): {extracted_data_llm}")
                if 'nombre_cliente' in extracted_data_llm and extracted_data_llm['nombre_cliente']:
                    ctx["nombre_cliente"] = str(extracted_data_llm['nombre_cliente']).strip()
                    llm_updated_any_field = True
                if 'telefono_cliente' in extracted_data_llm and extracted_data_llm['telefono_cliente']:
                    telefono_valido = validar_telefono(str(extracted_data_llm['telefono_cliente']).strip())
                    if telefono_valido:
                        ctx["telefono_cliente"] = telefono_valido # Use validated/formatted phone
                        llm_updated_any_field = True
                    else:
                        logger.warning(f"LLM extracted invalid phone '{extracted_data_llm['telefono_cliente']}' at NOMBRE stage.")
                if 'direccion_cliente' in extracted_data_llm and extracted_data_llm['direccion_cliente']:
                    ctx["direccion_cliente"] = str(extracted_data_llm['direccion_cliente']).strip()
                    llm_updated_any_field = True
                if 'email_cliente' in extracted_data_llm and extracted_data_llm['email_cliente']:
                    email_str = str(extracted_data_llm['email_cliente']).strip()
                    if validar_email(email_str):
                        ctx["email_cliente"] = email_str
                        llm_updated_any_field = True
                    else:
                        logger.warning(f"LLM extracted invalid email '{email_str}' at NOMBRE stage.")

            # If LLM didn't get the name, or if no LLM update happened, try direct assignment for name.
            # This also handles cases where LLM might get other fields but miss the current one.
            if not ctx.get("nombre_cliente"):
                nombre_cliente_directo = pregunta.strip()
                # Basic validation for name: not empty and at least two words (first and last name)
                if nombre_cliente_directo and len(nombre_cliente_directo.split()) >= 2:
                    ctx["nombre_cliente"] = nombre_cliente_directo
                    logger.info(f"[PYME_CONTACT] Nombre cliente set directly: {nombre_cliente_directo}")
                elif not llm_updated_any_field: # Only ask again if LLM also failed to provide anything useful
                    return {"respuesta": "Por favor, ingresa tu nombre y apellido.", "fuente": "re_solicitando_nombre_cliente"}

            # After attempting extraction (LLM or direct), proceed to check/confirm all data
            return self.handle("confirmar_datos_internos")


        elif estado == PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO:
            potential_fields_to_extract = ['telefono_cliente', 'direccion_cliente', 'email_cliente'] # Nombre should be set
            extracted_data_llm = extract_multiple_contact_details_llm(pregunta, potential_fields_to_extract)
            llm_updated_any_field = False

            if extracted_data_llm:
                logger.info(f"[PYME_LLM_CONTACT] Extracted by LLM (Telefono Stage): {extracted_data_llm}")
                if 'telefono_cliente' in extracted_data_llm and extracted_data_llm['telefono_cliente']:
                    telefono_valido = validar_telefono(str(extracted_data_llm['telefono_cliente']).strip())
                    if telefono_valido:
                        ctx["telefono_cliente"] = telefono_valido
                        llm_updated_any_field = True
                    else:
                        logger.warning(f"LLM extracted invalid phone '{extracted_data_llm['telefono_cliente']}' at TELEFONO stage.")
                if 'direccion_cliente' in extracted_data_llm and extracted_data_llm['direccion_cliente']:
                    ctx["direccion_cliente"] = str(extracted_data_llm['direccion_cliente']).strip()
                    llm_updated_any_field = True
                if 'email_cliente' in extracted_data_llm and extracted_data_llm['email_cliente']:
                    email_str = str(extracted_data_llm['email_cliente']).strip()
                    if validar_email(email_str):
                        ctx["email_cliente"] = email_str
                        llm_updated_any_field = True
                    else:
                        logger.warning(f"LLM extracted invalid email '{email_str}' at TELEFONO stage.")

            if not ctx.get("telefono_cliente"):
                telefono_directo = validar_telefono(pregunta.strip())
                if telefono_directo:
                    ctx["telefono_cliente"] = telefono_directo
                    logger.info(f"[PYME_CONTACT] Telefono cliente set directly: {telefono_directo}")
                elif not llm_updated_any_field:
                     return {"respuesta": "El número de teléfono no parece válido. Por favor, ingresalo de nuevo (solo números, con código de área).", "fuente": "re_solicitando_telefono_cliente"}

            return self.handle("confirmar_datos_internos")


        elif estado == PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION:
            potential_fields_to_extract = ['direccion_cliente', 'email_cliente'] # Nombre y telefono should be set
            extracted_data_llm = extract_multiple_contact_details_llm(pregunta, potential_fields_to_extract)
            llm_updated_any_field = False

            if extracted_data_llm:
                logger.info(f"[PYME_LLM_CONTACT] Extracted by LLM (Direccion Stage): {extracted_data_llm}")
                if 'direccion_cliente' in extracted_data_llm and extracted_data_llm['direccion_cliente']:
                    ctx["direccion_cliente"] = str(extracted_data_llm['direccion_cliente']).strip()
                    llm_updated_any_field = True
                if 'email_cliente' in extracted_data_llm and extracted_data_llm['email_cliente']:
                    email_str = str(extracted_data_llm['email_cliente']).strip()
                    if validar_email(email_str):
                        ctx["email_cliente"] = email_str
                        llm_updated_any_field = True
                    else:
                        logger.warning(f"LLM extracted invalid email '{email_str}' at DIRECCION stage.")

            if not ctx.get("direccion_cliente"):
                direccion_directa = pregunta.strip()
                if direccion_directa and len(direccion_directa) >= 5: # Basic validation
                    ctx["direccion_cliente"] = direccion_directa
                    logger.info(f"[PYME_CONTACT] Direccion cliente set directly: {direccion_directa}")
                elif not llm_updated_any_field:
                    return {"respuesta": "La dirección parece muy corta. Por favor, ingresala completa.", "fuente": "re_solicitando_direccion_cliente"}

            return self.handle("confirmar_datos_internos")

        # This internal "confirmar_datos_internos" action is used to re-trigger the data checking logic
        # after any data collection state (ESPERANDO_DATOS_CLIENTE_*) has processed input.
        # It's effectively what "confirmar" was doing but more explicitly named for this loop.
        elif texto == "confirmar_datos_internos" or estado == PymeConversationState.CONFIRMANDO_PEDIDO:
            # This part is reached from ESPERANDO_DATOS_CLIENTE_* states via self.handle("confirmar_datos_internos")
            # or if the state was already CONFIRMANDO_PEDIDO and user said "si"
            if texto == "confirmar_datos_internos": # Reset to CONFIRMANDO_PEDIDO if coming from data collection
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.CONFIRMANDO_PEDIDO)
                # This ensures that if we jumped here via "confirmar_datos_internos",
                # the profile pre-fill and missing fields check logic in CONFIRMANDO_PEDIDO runs.
                # The 'pregunta' here would be "confirmar_datos_internos", so we pass a neutral "si" to trigger the checks.
                return self.handle("si")


            # Original logic from CONFIRMANDO_PEDIDO starts here
            if any(k in texto for k in CANCEL_KEYWORDS) and texto != "confirmar_datos_internos": # Avoid cancelling on internal call
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "Pedido cancelado. ¿Necesitás otra cosa?", "fuente": "pedido_cancelado_confirmacion", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con agente", "action": "hablar_con_agente"}]}

            if texto.strip() in {"si", "sí", "confirmo", "confirmar"}: # This is the user's "si"
                # Ensure profile data is pre-filled if not already set or overridden by user
                viewer_user_id = self.context.get("cliente_id")
                cliente = User.query.get(viewer_user_id) if viewer_user_id else None
                if cliente:
                    if not ctx.get("nombre_cliente") and cliente.name:
                        ctx["nombre_cliente"] = cliente.name
                    if not ctx.get("telefono_cliente") and cliente.telefono:
                        tel_valido_profile = validar_telefono(cliente.telefono)
                        if tel_valido_profile: ctx["telefono_cliente"] = tel_valido_profile
                        else: logger.warning(f"Invalid phone in profile for user {cliente.id}: {cliente.telefono}")
                    if not ctx.get("email_cliente") and cliente.email:
                        if validar_email(cliente.email): ctx["email_cliente"] = cliente.email
                        else: logger.warning(f"Invalid email in profile for user {cliente.id}: {cliente.email}")

                campos_necesarios_pedido = ["nombre_cliente", "telefono_cliente", "direccion_cliente"]
                campos_faltantes = [campo for campo in campos_necesarios_pedido if not ctx.get(campo) or not str(ctx.get(campo, "")).strip()]


                if campos_faltantes:
                    next_missing_field_map = {
                        "nombre_cliente": (PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE, "¡Casi listo! Para completar tu pedido, ¿podrías decirme tu nombre completo?"),
                        "telefono_cliente": (PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO, "Entendido. Ahora, ¿cuál es tu número de teléfono (con código de área)?"),
                        "direccion_cliente": (PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION, "Perfecto. ¿Cuál es la dirección de entrega para este pedido?"),
                    }
                    next_field_key = campos_faltantes[0]
                    new_state, question_for_next_field = next_missing_field_map[next_field_key]

                    ctx["estado_conversacion"] = serialize_state(new_state)
                    flask_session[CONTEXTO_PYME] = ctx
                    return {"respuesta": question_for_next_field, "fuente": f"solicitando_{next_field_key}"}
                else:
                    # Todos los datos necesarios están, pasar a confirmación final con datos
                    ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS)
                    flask_session[CONTEXTO_PYME] = ctx
                    resumen_pedido = formatear_carrito(carrito)
                    resumen_datos = f"Nombre: {ctx.get('nombre_cliente', 'N/A')}\nTeléfono: {ctx.get('telefono_cliente', 'N/A')}\nDirección de Entrega: {ctx.get('direccion_cliente', 'N/A')}"
                    if ctx.get("email_cliente"):
                        resumen_datos += f"\nEmail: {ctx['email_cliente']}"
                    return {
                        "respuesta": f"¡Excelente! Revisemos todo antes de finalizar:\n\n**Pedido:**\n{resumen_pedido}\n\n**Datos de Contacto y Entrega:**\n{resumen_datos}\n\n¿Es todo correcto para generar el pedido?",
                        "fuente": "confirmando_pedido_con_datos",
                        "botones": [{"texto": "Sí, todo correcto", "action": "confirmar_final_con_datos"}, {"texto": "Modificar datos", "action": "modificar_datos_cliente"}, {"texto": "Modificar pedido", "action": "modificar_pedido"}]
                    }

            elif any(k in texto for k in ["modificar", "cambiar", "agregar", "quitar"]) and texto != "confirmar_datos_internos":
                ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_PRODUCTO), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": f"Ok, volvemos a tu pedido. Carrito actual:\n{formatear_carrito(carrito)}\n¿Qué quieres hacer?", "fuente": "modificando_pedido_confirmacion", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}

            intentos += 1; ctx["reintentos"] = intentos; flask_session[CONTEXTO_PYME] = ctx
            if intentos >= 2:
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "No pudimos confirmar tu pedido y fue cancelado. Puedes intentar de nuevo.", "fuente": "pedido_cancelado_confirmacion_fallida_reintentos", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}]}
            return {"respuesta": "¿Confirmás el pedido? (Sí/Modificar/Cancelar)", "fuente": "reconfirmando_pedido", "botones": [{"texto": "Sí, confirmar", "action": "confirmar_pedido"}, {"texto": "Modificar pedido", "action": "modificar_pedido"}, {"texto": "Cancelar", "action": "cancelar_pedido"}]}


        elif estado == PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS:
            if texto.strip() in {"si", "sí", "confirmo", "confirmar", "todo correcto", "si todo correcto"}:
                # Crear el PymePedido
                try:
                    monto_total_calculado = sum(item.get("precio_unitario_catalogo", 0) * item.get("cantidad_pedido", 0) for item in carrito if item.get("precio_unitario_catalogo") and item.get("precio_unitario_catalogo") > 0)

                    nuevo_pedido = PymePedido(
                        asunto=f"Pedido de {ctx.get('nombre_cliente', 'Cliente Chat')}",
                        detalles=json.dumps(carrito), # Guardar el carrito como JSON
                        rubro=self.context.get("rubro_nombre", "general"),
                        nombre_cliente=ctx.get("nombre_cliente"),
                        email_cliente=ctx.get("email_cliente"), # Puede ser None
                        telefono_cliente=ctx.get("telefono_cliente"),
                        user_id=self.context.get("cliente_id"), # ID del ChatUser/User final
                        direccion=ctx.get("direccion_cliente"),
                        monto_total=monto_total_calculado
                        # latitud y longitud podrían obtenerse si la dirección se geocodifica
                    )
                    db.session.add(nuevo_pedido)
                    db.session.commit()
                    logger.info(f"PymePedido {nuevo_pedido.nro_pedido} creado para user_id {self.context.get('cliente_id') or 'anon'}: {carrito}")

                    # Limpiar contexto de pedido
                    ctx.update({
                        "estado_conversacion": serialize_state(PymeConversationState.PEDIDO_FINALIZADO),
                        "reintentos": 0,
                        "nro_pedido_confirmado": nuevo_pedido.nro_pedido
                        # No limpiar carrito aquí por si el usuario quiere preguntar sobre el pedido recién hecho.
                        # Se limpiará cuando inicie un nuevo pedido o salga del flujo.
                    })
                    flask_session[CONTEXTO_PYME] = ctx

                    ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_FEEDBACK) # Transicionar a pedir feedback
                    flask_session[CONTEXTO_PYME] = ctx
                    return {
                        "respuesta": f"¡Listo! Tu pedido **#{nuevo_pedido.nro_pedido}** fue registrado. En breve nos comunicaremos para coordinar el pago y la entrega. ¿Te gustaría dejarnos algún comentario sobre tu experiencia de compra? (Sí/No)",
                        "fuente": "pedido_finalizado_con_datos",
                        "botones": [{"texto": "Sí, dejar comentario"}, {"texto": "No, gracias"}]
                    }

                except Exception as e:
                    current_app.logger.error(f"Error al crear PymePedido: {e}", exc_info=True)
                    db.session.rollback()
                    return {"respuesta": "Hubo un error al registrar tu pedido. Por favor, intenta de nuevo en un momento.", "fuente": "error_guardando_pedido"}

            elif any(k in texto for k in ["modificar datos", "modificar pedido", "no"]):
                # Volver a pedir el primer dato (nombre) para reiniciar el flujo de datos del cliente
                # O si es "modificar pedido", volver al estado de agregar productos
                if "modificar pedido" in texto:
                     ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_PRODUCTO), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                     return {"respuesta": f"Ok, volvemos a tu pedido. Carrito:\n{formatear_carrito(carrito)}\n¿Qué quieres hacer?", "fuente": "modificando_pedido_confirmacion_final", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}
                else: # Modificar datos del cliente
                    ctx.pop("nombre_cliente", None); ctx.pop("telefono_cliente", None); ctx.pop("direccion_cliente", None); ctx.pop("email_cliente", None)
                    ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE)
                    flask_session[CONTEXTO_PYME] = ctx
                    return {"respuesta": "Entendido. Vamos a ingresar tus datos de nuevo. ¿Cuál es tu nombre completo?", "fuente": "modificando_datos_cliente"}
            else:
                return {"respuesta": "No entendí. ¿Confirmas el pedido y los datos, o querés modificar algo? (Sí/Modificar datos/Modificar pedido)", "fuente": "reconfirmando_pedido_con_datos_fallback"}


        elif estado == PymeConversationState.PEDIDO_FINALIZADO:
            # Ahora PEDIDO_FINALIZADO es un estado transitorio antes de ESPERANDO_FEEDBACK
            # Si por alguna razón se llega aquí directamente, ofrecer feedback o nuevo pedido.
            nro_pedido_previo = ctx.get("nro_pedido_confirmado", "anterior")
            ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_FEEDBACK)
            flask_session[CONTEXTO_PYME] = ctx
            return {
                "respuesta": f"Tu pedido #{nro_pedido_previo} ya fue registrado. ¿Te gustaría dejarnos algún comentario sobre tu experiencia? (Sí/No)",
                "fuente": "pedido_ya_finalizado_pide_feedback",
                "botones": [{"texto": "Sí, dejar comentario"}, {"texto": "No, gracias (nuevo pedido)"}]
            }

        elif estado == PymeConversationState.ESPERANDO_FEEDBACK:
            if texto.strip().lower() in {"si", "sí", "yes", "dale", "bueno"}:
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE) # Preparar para siguiente interaccion
                ctx["carrito"] = [] # Limpiar carrito después de feedback
                ctx.pop("nro_pedido_confirmado", None)
                flask_session[CONTEXTO_PYME] = ctx
                # TODO: Guardar el feedback si se desea. Por ahora solo agradece.
                return {"respuesta": "¡Gracias por tus comentarios! Valoramos tu opinión. ¿Puedo ayudarte con algo más?", "fuente": "agradecimiento_feedback"}
            else:  # "no", "no gracias", o cualquier otra cosa
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.IDLE)
                ctx["carrito"] = []
                ctx.pop("nro_pedido_confirmado", None)
                flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "Entendido. ¿Querés iniciar un nuevo pedido o ver el catálogo?", "fuente": "feedback_omitido", "botones": [{"texto": "Nuevo pedido", "action": "iniciar_pedido"}, {"texto": "Ver catálogo", "action": "ver_catalogo"}]}
        
        elif estado == PymeConversationState.IDLE:
            # Limpiar carrito y nro de pedido anterior al iniciar un nuevo pedido desde IDLE
            ctx.clear()
            ctx.update({
                "estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_PRODUCTO),
                "reintentos": 0,
                "carrito": []
            });
            flask_session[CONTEXTO_PYME] = ctx
            # carrito ya está inicializado como [] arriba por ctx.clear() y re-set
            items_ini = extraer_productos(pregunta)
            msg_ini = ""; sug_ini = ""
            if items_ini:
                # Al iniciar un pedido nuevo, el carrito que se pasa a formatear_carrito es el que se está construyendo aquí.
                current_building_cart = [] # Carrito temporal para este mensaje inicial
                current_building_cart.extend(items_ini)
                for it in items_ini: add_preference("productos", it["nombre"])
                # Actualizar el carrito en el contexto (ctx) para que PedidoHandler lo use en los siguientes pasos
                ctx["carrito"] = current_building_cart
                flask_session[CONTEXTO_PYME] = ctx # Guardar el ctx actualizado en la sesión

                msg_ini = f"¡Entendido! Agregué a tu pedido:\n{formatear_carrito(current_building_cart)}\n\n"
                if items_ini: sug_ini = self._sugerir_productos_complementarios(items_ini[-1]['nombre'], current_building_cart, items_ini)
            
            ofertas_hdl = OfertasHandler(self.context)
            ofertas_dict = ofertas_hdl.handle(pregunta if not items_ini and len(pregunta.split()) > 2 else "ofertas")
            msg_ofertas = ""
            if "No tenemos ofertas especiales" not in ofertas_dict.get("respuesta","") and "Inicia sesión" not in ofertas_dict.get("respuesta",""):
                lista_ofertas = ofertas_dict.get("respuesta","").replace("Estas son algunas de nuestras ofertas destacadas:\n","").split("\n\n¿Te interesa alguna o quieres ver más?")[0]
                if lista_ofertas.strip(): msg_ofertas = "\n\nPara empezar, aquí tienes algunas ofertas:\n" + lista_ofertas
            
            return {"respuesta": f"{msg_ini}Dime qué más productos y cantidades quieres. Escribe 'finalizar pedido' cuando termines.{msg_ofertas}{sug_ini}", "fuente": "iniciar_pedido_flujo", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}

        logger.error(f"[PYME_PEDIDO] Estado no manejado: {estado}"); ctx.clear()
        return {"respuesta": "Hubo un problema. ¿Empezamos de nuevo el pedido?", "fuente":"error_pedido_estado_desconocido_reinicio"}

class FaqHandler(BaseHandler):
    def handle(self, pregunta):
        respuesta_faq = buscar_en_faq_spacy(pregunta, self.context.get("user_id"))
        return {"respuesta": respuesta_faq, "fuente": "faq"} if respuesta_faq else None

class HumanHandler(BaseHandler):
    def handle(self, pregunta):
        # Verificar si es anónimo
        if self.context.get("anon_id") and not self.context.get("cliente_id"):
            return {
                "respuesta": "Para hablar con un agente y recibir atención personalizada, necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo ahora?",
                "botones": [
                    {"texto": "Iniciar Sesión", "action": "login"},
                    {"texto": "Registrarme Gratis", "action": "register"},
                    {"texto": "No, gracias"}
                ],
                "fuente": "anon_escalation_prompt"
            }
        elif not self.context.get("cliente_id"): # No anónimo pero sin cliente_id (caso raro)
             return {
                "respuesta": "Para hablar con un agente, por favor inicia sesión.",
                "botones": [{"texto": "Iniciar Sesión", "action": "login"}],
                "fuente": "login_required_escalation"
            }

        ticket_data = {"asunto": "Solicitud de Chat en Vivo", "categoria": "Atención en Vivo", "detalles": f"Cliente solicitó chat: '{pregunta}'", 
                       "user_id": self.context.get("cliente_id"), "rubro_id": self.context.get("rubro_id"), 
                       "anon_id": self.context.get("anon_id"), "estado": "esperando_agente_en_vivo"}
        try:
            sala_de_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data=ticket_data)
            if not sala_de_chat: raise Exception("No se pudo crear ticket de sala de chat.")
            servicio_tickets.crear_comentario(ticket_id=sala_de_chat.id, tipo_ticket="pyme", comentario_data={"comentario": pregunta, "es_admin": False, "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id")})
            self.context.get(CONTEXTO_PYME, {}).clear() # Limpiar contexto pyme al escalar
            flask_session[CONTEXTO_PYME] = {} # También limpiar de la sesión de Flask
            return {"respuesta": f"¡Listo! Abrimos una sala de chat directa con el equipo. Tu número de chat es **P-{sala_de_chat.nro_ticket}**. Esperá, un agente se conecta en breve.", "ticket_id": sala_de_chat.id, "fuente": "escalamiento_humano_exitoso"}
        except Exception as e:
            logger.error(f"[HumanHandler] Error al escalar: {e}", exc_info=True)
            return {"respuesta": "No pudimos conectar con un agente ahora. Probá más tarde o llamá.", "botones": [{"texto": "Hablar con un agente"}]}

class UnclearHandler(BaseHandler):
    def handle(self, pregunta):
        # Similar a HumanHandler, pero podría tener un asunto/categoría diferente o estado 'pendiente'.
        # Por ahora, reutilizamos lógica de HumanHandler, pero con un mensaje inicial distinto.
        if not self.context.get("cliente_id"):
            return {"respuesta": "No entendí tu consulta. ¿Querés hablar con un agente? Necesitarás iniciar sesión.", "botones": [{"texto": "Iniciar sesión", "action": "login"}, {"texto": "Registrarme Gratis", "action": "register"}]}
        
        # Crear un ticket pendiente si la consulta es ambigua y el usuario no está ya en un flujo.
        # No vamos a crear un ticket automáticamente aquí, solo ofrecer la opción.
        return {"respuesta": "No estoy seguro de cómo ayudarte con eso. ¿Te gustaría que te pase con un agente para que pueda asistirte mejor?", 
                "fuente": "pregunta_ambigua_ofrece_agente",
                "botones": [{"texto": "Sí, hablar con un agente", "action": "hablar_con_agente"}, {"texto": "No, gracias", "action": "cancelar_escalamiento"}]}

class TicketStatusHandler(BaseHandler): # Sin cambios importantes en esta iteración
    def handle(self, pregunta):
        ctx = self.context.setdefault(CONTEXTO_PYME, flask_session.get(CONTEXTO_PYME, {}))
        estado = deserialize_state(ctx.get("estado_conversacion"))
        texto = pregunta.lower()

        if estado == PymeConversationState.ESPERANDO_CONFIRMACION_CIERRE:
            if texto in {"si", "sí", "yes", "y"}:
                ticket_id = ctx.get("ticket_id_activo"); ticket = db.session.get(PymeTicket, ticket_id) if ticket_id else None # Necesita PymeTicket
                if ticket: ticket.estado = "resuelto"; db.session.commit()
                ctx["estado_conversacion"] = serialize_state(PymeConversationState.ESPERANDO_CALIFICACION)
                return {"respuesta": "¡Excelente! ¿Podés calificar la atención recibida del 1 al 5?"}
            ctx.clear(); return {"respuesta": "Dejamos el ticket abierto. ¿Necesitás algo más?"}

        if estado == PymeConversationState.ESPERANDO_CALIFICACION:
            if not re.fullmatch(r"[1-5]", texto.strip()): return {"respuesta": "Por favor, ingresa una calificación del 1 al 5."}
            ticket_id = ctx.get("ticket_id_activo")
            if ticket_id: servicio_tickets.crear_comentario(ticket_id=ticket_id, tipo_ticket="pyme", comentario_data={"comentario": f"Calificación: {texto}", "es_admin": False, "anon_id": self.context.get("anon_id")})
            ctx.clear(); return {"respuesta": "¡Gracias por tu calificación! ¿Te ayudo con algo más?", "botones": [{"texto": "Hablar con un agente", "action": "hablar_con_agente"}]}

        if estado == PymeConversationState.ESPERANDO_NUMERO_TICKET:
            match = re.search(r"\d{5,}", texto)
            if not match: return {"respuesta": "No entendí el número de ticket. ¿Podés repetirlo? (al menos 5 dígitos)"}
            numero = int(match.group(0)); ticket = PymeTicket.query.filter_by(nro_ticket=numero).first() # Necesita PymeTicket
            ctx.pop("estado_conversacion", None)
            if not ticket: return {"respuesta": f"No encontré ticket P-{numero}. Verificá el número."}
            
            respuesta_txt = f"El ticket **P-{ticket.nro_ticket}** sobre '{getattr(ticket,'asunto','')}' está **{ticket.estado.replace('_',' ').title()}**."
            ultimo_com = TicketComentario.query.filter_by(pyme_ticket_id=ticket.id, es_admin=True).order_by(TicketComentario.fecha.desc()).first() # Necesita TicketComentario
            if ultimo_com: respuesta_txt += f"\nÚltima actualización: *{ultimo_com.comentario}*"
            
            if ticket.estado == "en_proceso": # O el estado que indique "esperando respuesta del cliente"
                ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_CONFIRMACION_CIERRE), "ticket_id_activo": ticket.id})
                return {"respuesta": respuesta_txt + "\n¿Se resolvió tu problema?", "botones": [{"texto": "Sí, solucionado"}, {"texto": "No, aún no"}]}
            return {"respuesta": respuesta_txt}

        # Si la intención es consultar estado y no se dio número aún
        if self.context.get("intencion") == "consultar_estado_ticket":
             ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_NUMERO_TICKET)})
             return {"respuesta": "Para consultar el estado de un ticket, decime el número de ticket por favor."}
        return None # No debería llegar aquí si la intención es correcta

class FallbackHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get("user_id")
        rubro = self.context.get("rubro_nombre")
        fuente_final = "fallback_generico_final" # Default
        mensaje_final = "No entendí bien tu consulta. ¿Podrías reformularla?"
        botones_finales = [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]

        resultados = buscar_catalogo_qdrant(user_id=user_id, pregunta=pregunta, categoria=rubro, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME))
        if resultados:
            respuesta_legible = armar_respuesta_legible(resultados, max_items=3)
            mensaje_final = f"Esto es lo que encontré relacionado:\n{respuesta_legible}\n¿Te sirve o necesitas más ayuda?"
            fuente_final = "fallback_catalogo_encontrado"
            botones_finales = [{"texto": "Hacer un pedido", "action": "iniciar_pedido"}, {"texto": "Buscar otra cosa", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
        
        elif es_producto_valido_llm(pregunta):
            mensaje_final = f"No pude encontrar '{pregunta}' en nuestro catálogo. Puedes intentar describirlo de otra manera o hablar con un agente."
            fuente_final = "fallback_producto_no_encontrado_especifico"
            botones_finales = [{"texto": "Intentar otra búsqueda", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
        
        elif sugerencias_por_rubro(rubro): # Check if list is not empty
            sugerencias = sugerencias_por_rubro(rubro)
            mensaje_final = f"{random.choice(sugerencias)} ¿Querés una oferta personalizada o ayuda para comprar?"
            fuente_final = "fallback_sugerencia_rubro"
            botones_finales = [{"texto": "Ver ofertas", "action": "ver_ofertas"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
        
        elif user_id: # Only try webinfo if there's a user_id context for the Pyme
            info_web = obtener_info_web(user_id, self.context.get("nombre_pyme"))
            if info_web:
                mensaje_info_items = [f"{k.capitalize()}: {v}" for k, v in info_web.items() if v]
                if mensaje_info_items:
                    mensaje_final = f"Sobre nosotros: {', '.join(mensaje_info_items)}"
                    fuente_final = "fallback_info_web"
                    # Botones podrían ser genéricos o relacionados con la info web si es parseable
                    botones_finales = [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
        
        # Restore original logic for pyme owner download if applicable and question implies it
        if resultados and tiene_archivo_catalogo(user_id) and any(k in pregunta.lower() for k in ["descargar", "pdf", "completo"]):
            # 'resultados' check ensures we only offer download if we also showed some catalog items.
            # 'pregunta.lower()' check ensures user expressed some intent to download.
            botones_finales.append({"texto": "Descargar mi catálogo", "action": "descargar_catalogo"}) # Action for owner
            mensaje_final += f"\n\nPodés [descargar tu catálogo completo aquí]({url_descargar_catalogo()})."

            
        return {"respuesta": mensaje_final, "fuente": fuente_final, "botones": botones_finales}

# --- ROUTER PRINCIPAL ---
def responder_pyme(pregunta, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
    rubro_nombre_para_coleccion = getattr(rubro_obj, "nombre", None)
    if not rubro_nombre_para_coleccion and owner_user and hasattr(owner_user, "rubro") and hasattr(owner_user.rubro, "nombre"):
        rubro_nombre_para_coleccion = owner_user.rubro.nombre
    if not rubro_nombre_para_coleccion: rubro_nombre_para_coleccion = "empresa"
    
    from services.qdrant_search import coleccion_catalogo_para_rubro, CATALOGO_PYME
    coleccion_qdrant_ctx = coleccion_catalogo_para_rubro(rubro_nombre_para_coleccion.lower())

    user_id_ctx = getattr(owner_user, "id", None) if owner_user else (getattr(viewer_user, "empresa_id", None) or getattr(viewer_user, "id", None) if viewer_user else None)
    nombre_pyme_ctx = getattr(owner_user, "nombre_empresa", "la empresa") if owner_user else (getattr(viewer_user, "nombre_empresa", "la empresa") if viewer_user else "la empresa")

    # --- Gestión del Historial ---
    historial_en_sesion = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    chat_session_uuid = kwargs.get("chat_session_uuid") # Obtener de kwargs

    if not historial_en_sesion and chat_session_uuid:
        logger.info(f"[PYME_HISTORIAL] Historial de sesión vacío. Intentando cargar desde DB para session_uuid: {chat_session_uuid}")
        try:
            num_pares = MAX_HISTORIAL_CHAT // 2
            # Asumiendo que Conversacion tiene un campo 'session_id'
            conversaciones_db = Conversacion.query.filter_by(session_id=chat_session_uuid) \
                                                .order_by(Conversacion.timestamp.desc()) \
                                                .limit(num_pares * 2).all() # *2 porque son mensajes individuales

            if conversaciones_db:
                conversaciones_db.reverse()
                historial_reconstruido = []
                for conv in conversaciones_db:
                    # Reconstruir el formato {"role": "USER/CHATBOT", "content": ...}
                    historial_reconstruido.append({"role": "USER", "content": conv.pregunta})
                    historial_reconstruido.append({"role": "CHATBOT", "content": conv.respuesta})

                if historial_reconstruido:
                    logger.info(f"[PYME_HISTORIAL] Reconstruidos {len(historial_reconstruido)} mensajes desde DB para session_uuid: {chat_session_uuid}")
                    historial_en_sesion = historial_reconstruido[-MAX_HISTORIAL_CHAT:] # Aplicar límite
                    flask_session[NOMBRE_HISTORIAL_SESION] = historial_en_sesion # Actualizar la sesión de Flask
        except Exception as e:
            logger.error(f"[PYME_HISTORIAL] Error cargando historial desde DB: {e}", exc_info=True)
            # Continuar con el historial de sesión (posiblemente vacío) si falla la carga de DB

    context = {
        "user_id": user_id_ctx, "nombre_pyme": nombre_pyme_ctx,
        "rubro_nombre": rubro_nombre_para_coleccion.lower(),
        "mensajes_previos": historial_en_sesion, # Usar el historial potencialmente cargado de DB
        CONTEXTO_PYME: flask_session.get(CONTEXTO_PYME, {}),
        "cliente_id": getattr(viewer_user, "id", None), "anon_id": anon_id,
        "rubro_id": getattr(rubro_obj, "id", None) if rubro_obj else (getattr(owner_user.rubro, "id", None) if owner_user and hasattr(owner_user, "rubro") else None),
        "coleccion_qdrant": coleccion_qdrant_ctx,
        "chat_session_uuid": chat_session_uuid
    }
    
    estado_conversacion_actual_str = context[CONTEXTO_PYME].get("estado_conversacion")
    estado_conversacion_actual = deserialize_state(estado_conversacion_actual_str)
    texto_pregunta_lower = pregunta.lower() # Para comparaciones de botones

    if estado_conversacion_actual == PymeConversationState.ESPERANDO_DETALLES_RECLAMO:
        # Verificar límite de tickets para anónimos si es un reclamo y se va a crear un ticket
        if context.get("anon_id") and not context.get("cliente_id"): # Es anónimo
            from flask import current_app
            from models import PymeTicket # Necesario para contar
            from datetime import datetime, timedelta # Asegurar imports

            max_tickets_anon = current_app.config.get("ANONYMOUS_MAX_TICKETS_PER_SESSION", 1)
            session_timeout_minutes_config = current_app.config.get("ANONYMOUS_SESSION_TIMEOUT_MINUTES", 15)

            # Contar tickets existentes para este anon_id DENTRO de la ventana de sesión actual
            anon_tickets_count = PymeTicket.query\
                .filter_by(anon_id=context["anon_id"])\
                .filter(PymeTicket.fecha >= datetime.utcnow() - timedelta(minutes=session_timeout_minutes_config))\
                .count()

            current_app.logger.info(f"Usuario anónimo {context['anon_id']} (Pyme): {anon_tickets_count} tickets en la sesión actual (límite: {max_tickets_anon}).")

            if anon_tickets_count >= max_tickets_anon:
                current_app.logger.info(f"Límite de tickets ({max_tickets_anon}) alcanzado para anon_id {context['anon_id']} en Pyme.")
                context[CONTEXTO_PYME].clear() # Limpiar el flujo de reclamo
                flask_session[CONTEXTO_PYME] = context[CONTEXTO_PYME]
                return {
                    "respuesta": "Alcanzaste el límite de reclamos/consultas que requieren seguimiento para usuarios invitados. Para continuar, por favor inicia sesión o regístrate.",
                    "botones": [
                        {"texto": "Iniciar Sesión", "action": "login"},
                        {"texto": "Registrarme Gratis", "action": "register"}
                    ],
                    "fuente": "anon_pyme_reclamo_limite_alcanzado"
                }

        detalles_reclamo = pregunta 
        pregunta_original_reclamo = context[CONTEXTO_PYME].get("pregunta_reclamo_original", "Reclamo sin detalles previos.")
        
        context[CONTEXTO_PYME].pop("estado_conversacion", None); context[CONTEXTO_PYME].pop("pregunta_reclamo_original", None)
        flask_session[CONTEXTO_PYME] = context[CONTEXTO_PYME] 

        pregunta_para_agente = f"Reclamo (Pregunta original: \"{pregunta_original_reclamo}\"). Detalles del cliente: \"{detalles_reclamo}\""
        if texto_pregunta_lower == "prefiero no dar detalles": 
            pregunta_para_agente = f"Reclamo (Pregunta original: \"{pregunta_original_reclamo}\"). El cliente prefirió no dar más detalles por chat."
        
        # HumanHandler se encarga de la respuesta y de limpiar el contexto si es necesario.
        # También guarda la conversación y crea el ticket (que ya tiene el check de anon vs cliente_id).
        return HumanHandler(context).handle(pregunta_para_agente) 

    puede_buscar_faq = estado_conversacion_actual == PymeConversationState.IDLE or estado_conversacion_actual is None
    if not puede_buscar_faq and any(k in texto_pregunta_lower for k in ["ayuda", "info", "pregunta", "duda", "cómo", "qué es", "saber"]):
        puede_buscar_faq = True

    if puede_buscar_faq:
        respuesta_faq_directa = buscar_en_faq_spacy(pregunta, context.get("user_id"))
        if respuesta_faq_directa:
            logger.info(f"[PYME] FAQ directa para '{pregunta}'")
            # El guardado de conversación se hace al final
            return {"respuesta": respuesta_faq_directa, "fuente": "faq_directa", "botones": [{"texto": "Más opciones", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]}

    intencion = _clasificar_intencion_pyme_con_llm(pregunta)
    logger.info(f"[PYME] Intención para '{pregunta}': {intencion}")
    context["intencion"] = intencion 

    INTENT_MAP = { "saludo": SaludoHandler, "ver_catalogo": CatalogoHandler, "consultar_ofertas": OfertasHandler,
                   "iniciar_pedido": PedidoHandler, "continuar_flujo": PedidoHandler, 
                   "pregunta_faq": FaqHandler, "hablar_con_agente": HumanHandler, 
                   "hablar_con_agente_pyme": HumanHandler, "consultar_estado_ticket": TicketStatusHandler,
                   "pregunta_ambigua": UnclearHandler }

    handler = None
    if detectar_small_talk_con_llm(pregunta) and intencion not in {"iniciar_pedido", "ver_catalogo", "consultar_ofertas"}:
        handler = SmallTalkHandler(context)
    else:
        palabras_compra_catalogo = ["comprar", "vender", "precio", "tenés", "hay", "oferta", "promo", "descuento", "unidades", "sku", "stock", "catálogo", "catalogo", "producto", "productos"]
        es_compra_o_catalogo = any(pal in texto_pregunta_lower for pal in palabras_compra_catalogo)

        if intencion in INTENT_MAP:
            handler = INTENT_MAP[intencion](context) if not (intencion == "pregunta_ambigua" and es_compra_o_catalogo) else CatalogoHandler(context)
        elif es_compra_o_catalogo: 
            handler = CatalogoHandler(context)
        else: 
            sentimiento = analizar_sentimiento_llm(pregunta)
            if sentimiento == "negativo": handler = SentimentHandler(context, "negativo")
            elif sentimiento == "positivo": handler = SentimentHandler(context, "positivo")
            else: handler = FallbackHandler(context)
    
    respuesta_final = handler.handle(pregunta)
    
    if respuesta_final is None: # Si el handler principal no dio respuesta
        logger.info(f"[PYME] Handler ({type(handler).__name__}) devolvió None para '{pregunta}', usando FallbackHandler.")
        respuesta_final = FallbackHandler(context).handle(pregunta)
        if respuesta_final is None: # FallbackHandler DEBE devolver algo
            logger.error(f"[PYME] FallbackHandler también devolvió None para '{pregunta}'. Esto es un error.")
            respuesta_final = {"respuesta": "Lo siento, no pude procesar tu solicitud en este momento. Intenta de nuevo.", "fuente": "error_fallback_definitivo"}

    # Guardar historial y conversación en DB
    historial = flask_session.get(NOMBRE_HISTORIAL_SESION, [])
    historial.append({"role": "user", "content": pregunta})
    historial.append({"role": "assistant", "content": respuesta_final.get("respuesta", "")})
    flask_session[NOMBRE_HISTORIAL_SESION] = historial[-MAX_HISTORIAL_CHAT:]

    try:
        # Usar el chat_session_uuid del contexto para guardar en la BD
        current_chat_session_uuid = context.get("chat_session_uuid")
        if context.get("user_id") or anon_id: # Guardar si hay algún identificador
            db.session.add(Conversacion(
                user_id=context.get("cliente_id") or context.get("user_id"), # Priorizar cliente_id si existe
                pregunta=pregunta,
                respuesta=respuesta_final.get("respuesta", ""),
                fuente=respuesta_final.get("fuente", "desconocida"),
                rubro=context["rubro_nombre"],
                session_id=current_chat_session_uuid # Asegurar que se guarda el session_uuid
            ))
            db.session.commit()
        else:
            logger.warning("[PYMES] No se guardó conversación por falta de user_id y anon_id.")

    except Exception as e:
        logger.error(f"[PYMES] Error guardando conversación en DB: {e}")
        db.session.rollback()

    flask_session[CONTEXTO_PYME] = context.get(CONTEXTO_PYME, {}) # Guardar cualquier cambio en el contexto pyme en la sesión

    return {
        "respuesta": respuesta_final.get("respuesta", "Ocurrió un error."),
        "fuente": respuesta_final.get("fuente", "desconocida"),
        "botones": respuesta_final.get("botones", []),
        "estado_respuesta": respuesta_final.get("estado_respuesta"), 
        "contexto_actualizado": {CONTEXTO_PYME: context.get(CONTEXTO_PYME, {})},
    }

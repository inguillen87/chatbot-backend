import logging
import re
import random
import json
from enum import Enum, auto
from flask import session as flask_session

from services.cohere_ai import robust_chat
from models import Conversacion, db, ArchivoAdjunto # Asegúrate que PymeTicket, TicketComentario estén importados si se usan directamente
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    armar_respuesta_legible,
    formatear_tabla_catalogo,
    CATALOGO_PYME # Importar para usar como default
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import reemplazar_placeholders
from services.utils import sugerencias_por_rubro
from services.logic import detectar_small_talk_con_llm, generar_respuesta_small_talk
from services.ticket_service import servicio_tickets # Asumiendo que PymeTicket está aquí
from services.webinfo import obtener_info_web
from services.preferences import add_preference

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

def tiene_archivo_catalogo(user_id: int) -> bool:
    if not user_id: return False
    try:
        return ArchivoAdjunto.query.filter_by(user_id=user_id, tipo="catalogo").first() is not None
    except Exception: return False

def url_descargar_catalogo() -> str:
    from flask import request
    return f"{request.url_root.rstrip('/')}/catalogo/descargar"

import ast # For literal_eval

def extraer_productos_llm(texto: str) -> list[dict]:
    prompt = (
        "Extrae producto y cantidad del MENSAJE. Responde ÚNICAMENTE con una lista de objetos JSON válida. "
        "Cada objeto debe tener claves \"nombre\" (string) y \"cantidad\" (integer). "
        "Ejemplo: [{\"nombre\": \"manzanas\", \"cantidad\": 2}, {\"nombre\": \"peras\", \"cantidad\": 1}]\n"
        f"MENSAJE: '{texto}'"
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
                cantidad = 1
                if isinstance(cantidad_raw, (int, float)):
                    cantidad = int(cantidad_raw)
                elif isinstance(cantidad_raw, str) and cantidad_raw.isdigit():
                    cantidad = int(cantidad_raw)

                if cantidad < 1: cantidad = 1 # Asegurar cantidad mínima

                if nombre:
                    items.append({"nombre": nombre, "cantidad": cantidad})
        elif isinstance(datos, dict): # Si el LLM devuelve un solo objeto en lugar de una lista
            logger.warning(f"[PYME_LLM_PARSE] LLM devolvió un diccionario en lugar de una lista: {datos}. Intentando procesarlo.")
            nombre = str(datos.get("nombre", "")).strip()
            cantidad_raw = datos.get("cantidad", 1)
            cantidad = 1
            if isinstance(cantidad_raw, (int, float)):
                cantidad = int(cantidad_raw)
            elif isinstance(cantidad_raw, str) and cantidad_raw.isdigit():
                cantidad = int(cantidad_raw)
            if cantidad < 1: cantidad = 1
            if nombre:
                items.append({"nombre": nombre, "cantidad": cantidad})
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
    return items if items else extraer_productos_llm(texto)

def formatear_carrito(carrito: list[dict], context: dict = None) -> str: # context es opcional por ahora
    if not carrito: return "Tu carrito está vacío."

    lineas_carrito = []
    subtotal_pedido = 0.0
    productos_sin_precio_cont = 0

    for item in carrito:
        nombre = item.get("nombre", "Producto desconocido")
        cantidad = item.get("cantidad", 0)
        precio_unitario = item.get("precio_unitario", 0.0)
        unidad = item.get("unidad", "")
        precio_str = item.get("precio_str", "Consultar")

        linea = f"{cantidad} x {nombre}"
        if unidad:
            linea += f" ({unidad})"

        if precio_unitario > 0:
            precio_total_item = cantidad * precio_unitario
            linea += f" - ${precio_unitario:,.2f} c/u = ${precio_total_item:,.2f}"
            subtotal_pedido += precio_total_item
        else:
            linea += f" - {precio_str}"
            productos_sin_precio_cont +=1

        lineas_carrito.append(linea)

    if subtotal_pedido > 0:
        lineas_carrito.append(f"\n**Subtotal del pedido: ${subtotal_pedido:,.2f}**")

    if productos_sin_precio_cont > 0:
        nota_precio = "Algunos precios se confirmarán al finalizar." if subtotal_pedido > 0 else "Los precios se confirmarán al finalizar."
        lineas_carrito.append(f"_{nota_precio}_")

    return "\n".join(lineas_carrito)

class PymeConversationState(Enum):
    IDLE = auto()
    ESPERANDO_PRODUCTO = auto()
    CONFIRMANDO_PEDIDO = auto()
    PEDIDO_FINALIZADO = auto()
    ESPERANDO_CONTACTO = auto()
    ESPERANDO_NUMERO_TICKET = auto()
    ESPERANDO_CONFIRMACION_CIERRE = auto()
    ESPERANDO_CALIFICACION = auto()
    ESPERANDO_DETALLES_RECLAMO = auto()

def serialize_state(state): return state.name if state else None
def deserialize_state(value):
    if not value: return None
    try: return PymeConversationState[value]
    except KeyError: return None

PROMPT_CLASIFICAR_INTENCION = """
Sos el cerebro comercial de un chatbot para una pyme. Analizá la PREGUNTA DEL USUARIO y respondé sólo con una de estas intenciones:
- saludo, ver_catalogo, consultar_ofertas, iniciar_pedido, pregunta_faq, hablar_con_agente, continuar_flujo, consultar_estado_ticket, pregunta_ambigua
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
                                {"texto": "Ver catálogo completo", "action": "ver_catalogo_completo_accion"}, # Necesitaría una acción específica o el frontend maneja "ver_catalogo" sin pregunta.
                                {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
            else:
                mensaje = "No encontré productos que coincidan con tu búsqueda. Puedes intentar con otras palabras o ver nuestro catálogo completo."
                fuente = "catalogo_no_encontrado_general"
                botones_base = [{"texto": "Ver catálogo completo", "action": "ver_catalogo_completo_accion"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
        
        if tiene_archivo_catalogo(user_id) and any(k in pregunta.lower() for k in ["descargar", "pdf", "completo"]):
            botones_base.append({"texto": "Descargar catálogo", "action": "descargar_catalogo"})
            mensaje += f"\n\nDescargá el catálogo completo aquí: {url_descargar_catalogo()}"
        return {"respuesta": mensaje, "fuente": fuente, "botones": botones_base}

class OfertasHandler(BaseHandler):
    def handle(self, pregunta):
        user_id = self.context.get("user_id")
        if not user_id: return {"respuesta": "Inicia sesión para ver las ofertas.", "fuente": "ofertas_sin_login"}
        
        resultados_ofertas = buscar_catalogo_qdrant(user_id=user_id, pregunta="ofertas promociones descuentos", limite=5, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME), en_promocion=True)
        if resultados_ofertas:
            respuesta = "Estas son algunas de nuestras ofertas destacadas:\n" + armar_respuesta_legible(resultados_ofertas, max_items=5) + "\n\n¿Te interesa alguna o quieres ver más?"
            return {"respuesta": respuesta, "fuente": "ofertas_dinamicas", "botones": [{"texto": "Hacer un pedido", "action": "iniciar_pedido"}, {"texto": "Ver catálogo", "action": "ver_catalogo"}]}
        return {"respuesta": "Por el momento no tenemos ofertas especiales destacadas, pero puedes ver nuestro catálogo completo.", "fuente": "sin_ofertas_dinamicas", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}]}

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
    def _sugerir_productos_complementarios(self, ultimo_producto_nombre: str, carrito_actual: list, items_recien_agregados: list) -> str:
        user_id = self.context.get("user_id")
        if not user_id or not items_recien_agregados: return ""
        
        ofertas_destacadas = buscar_catalogo_qdrant(user_id=user_id, pregunta="ofertas productos complementarios", limite=3, en_promocion=True, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME))
        if ofertas_destacadas:
            nombres_en_carrito = {item['nombre'].lower().strip() for item in carrito_actual}
            sugerencias_validas = []
            for oferta in ofertas_destacadas:
                payload = getattr(oferta, "payload", {})
                nombre_oferta = payload.get("nombre", "").strip()
                if nombre_oferta and nombre_oferta.lower() not in nombres_en_carrito:
                    if not any(nombre_oferta.lower() == item_agregado['nombre'].lower() for item_agregado in items_recien_agregados):
                        precio_oferta_str = payload.get("precio_str", "") or (f"${payload['precio_float']:,.2f}" if payload.get("precio_float") is not None else "")
                        texto_sugerencia = f"'{nombre_oferta}'"
                        if precio_oferta_str: texto_sugerencia += f" a {precio_oferta_str}"
                        if payload.get("promocion_texto"): texto_sugerencia += f" ({payload.get('promocion_texto')})"
                        sugerencias_validas.append(texto_sugerencia)
                if len(sugerencias_validas) >= 1: break 
            if sugerencias_validas: return f"\n\n✨ ¡Aprovecha también! Tenemos {', '.join(sugerencias_validas)}. ¿Te interesa alguno?"
        return ""

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

                    # Lógica para agregar o actualizar cantidad en carrito
                    found_in_cart = False
                    for item_car_existente in carrito:
                        if nombre_producto_catalogo.lower() == item_car_existente["nombre"].lower():
                            item_car_existente["cantidad"] += item_ext["cantidad"]
                            # El precio y unidad ya están en item_car_existente desde que se agregó por primera vez.
                            # Si se encontró ahora con precio y antes no, se podría actualizar, pero simplificamos por ahora.
                            items_agregados_info.append(item_car_existente)
                            found_in_cart = True
                            break

                    if not found_in_cart:
                        item_para_carrito_nuevo = {
                            "nombre": nombre_producto_catalogo,
                            "cantidad": item_ext["cantidad"],
                            "precio_unitario": precio_unitario_catalogo,
                            "unidad": unidad_catalogo,
                            "precio_str": precio_str_catalogo
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
            sug_compl = self._sugerir_productos_complementarios(ultimo_agregado_nombre, carrito, items_agregados_info)
            return {"respuesta": f"{mensaje_principal}{sug_compl}", "fuente": "pedido_progreso_sug" if sug_compl else "pedido_progreso", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}
        
        elif estado == PymeConversationState.CONFIRMANDO_PEDIDO:
            if any(k in texto for k in CANCEL_KEYWORDS):
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "Pedido cancelado. ¿Necesitás otra cosa?", "fuente": "pedido_cancelado_confirmacion", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con agente", "action": "hablar_con_agente"}]}
            if texto.strip() in {"si", "sí", "confirmo", "confirmar"}:
                ctx.update({"estado_conversacion": serialize_state(PymeConversationState.PEDIDO_FINALIZADO), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                # TODO: Aquí se debería registrar el pedido en la base de datos (PymePedido)
                logger.info(f"Pedido confirmado para user_id {self.context.get('user_id')}: {carrito}")
                return {"respuesta": "¡Listo! Tu pedido fue registrado. En breve nos comunicaremos para coordinar el pago y la entrega.", "fuente": "pedido_finalizado_confirmado"}
            if any(k in texto for k in ["modificar", "cambiar", "agregar", "quitar"]):
                ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_PRODUCTO), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": f"Ok, volvemos a tu pedido. Carrito:\n{formatear_carrito(carrito)}\n¿Qué quieres hacer?", "fuente": "modificando_pedido_confirmacion", "estado_respuesta": "pyme_pregunta_pedido", "botones": [{"texto": "Agregar más", "action": "agregar_mas_pedido"}, {"texto": "Finalizar pedido", "action": "finalizar_pedido"}]}
            intentos += 1; ctx["reintentos"] = intentos; flask_session[CONTEXTO_PYME] = ctx
            if intentos >= 2: # Reducido para no ser molesto
                ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0}); flask_session[CONTEXTO_PYME] = ctx
                return {"respuesta": "No pudimos confirmar tu pedido y fue cancelado. Puedes intentar de nuevo.", "fuente": "pedido_cancelado_confirmacion_fallida_reintentos", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}]}
            return {"respuesta": "¿Confirmás el pedido? (Sí/Modificar/Cancelar)", "fuente": "reconfirmando_pedido", "botones": [{"texto": "Sí, confirmar", "action": "confirmar_pedido"}, {"texto": "Modificar pedido", "action": "modificar_pedido"}, {"texto": "Cancelar", "action": "cancelar_pedido"}]}

        elif estado == PymeConversationState.PEDIDO_FINALIZADO:
            ctx.update({"estado_conversacion": serialize_state(PymeConversationState.IDLE), "reintentos": 0, "carrito": []}); flask_session[CONTEXTO_PYME] = ctx
            return {"respuesta": "Tu pedido anterior ya fue finalizado. ¿Querés iniciar uno nuevo o ver el catálogo?", "fuente": "pedido_ya_finalizado_multi", "botones": [{"texto": "Nuevo pedido", "action": "iniciar_pedido"}, {"texto": "Ver catálogo", "action": "ver_catalogo"}]}
        
        elif estado == PymeConversationState.IDLE:
            ctx.clear(); ctx.update({"estado_conversacion": serialize_state(PymeConversationState.ESPERANDO_PRODUCTO), "reintentos": 0, "carrito": []}); flask_session[CONTEXTO_PYME] = ctx
            items_ini = extraer_productos(pregunta)
            msg_ini = ""; sug_ini = ""
            if items_ini:
                carrito.extend(items_ini)
                for it in items_ini: add_preference("productos", it["nombre"])
                msg_ini = f"¡Entendido! Agregué a tu pedido:\n{formatear_carrito(carrito)}\n\n"
                if items_ini: sug_ini = self._sugerir_productos_complementarios(items_ini[-1]['nombre'], carrito, items_ini)
            
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
        if not self.context.get("cliente_id"):
            return {"respuesta": "Para hablar con un agente necesitás iniciar sesión o registrarte. ¿Te gustaría hacerlo?", "botones": [{"texto": "Iniciar sesión", "action": "login"}, {"texto": "Registrarme Gratis", "action": "register"}]}
        
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
        
        resultados = buscar_catalogo_qdrant(user_id=user_id, pregunta=pregunta, categoria=rubro, coleccion=self.context.get("coleccion_qdrant", CATALOGO_PYME))
        if resultados:
            respuesta_legible = armar_respuesta_legible(resultados, max_items=3)
            botones = [{"texto": "Hacer un pedido", "action": "iniciar_pedido"}, {"texto": "Ver catálogo completo", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]
            if tiene_archivo_catalogo(user_id) and any(k in pregunta.lower() for k in ["descargar", "pdf"]):
                botones.append({"texto": "Descargar catálogo", "action": "descargar_catalogo"})
                respuesta_legible += f"\n\nDescargá el catálogo aquí: {url_descargar_catalogo()}"
            return {"respuesta": f"Esto es lo que encontré relacionado:\n{respuesta_legible}\n¿Te sirve o necesitas más ayuda?", "fuente": "fallback_catalogo_encontrado", "botones": botones}

        if es_producto_valido_llm(pregunta):
            mensaje = f"No pude encontrar '{pregunta}' en nuestro catálogo. Puedes intentar describirlo de otra manera, ver el catálogo completo, o hablar con un agente."
            return {"respuesta": mensaje, "fuente": "fallback_producto_no_encontrado_especifico", "botones": [{"texto": "Intentar otra búsqueda", "action": "ver_catalogo"}, {"texto": "Ver catálogo completo", "action": "ver_catalogo_completo_accion"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]}

        sugerencias = sugerencias_por_rubro(rubro)
        if sugerencias:
            return {"respuesta": f"{random.choice(sugerencias)} ¿Querés una oferta personalizada o ayuda para comprar?", "fuente": "fallback_sugerencia_rubro", "botones": [{"texto": "Ver ofertas", "action": "ver_ofertas"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]}
        
        info_web = (obtener_info_web(user_id, self.context.get("nombre_pyme")) if user_id else {})
        if info_web:
            mensaje_info = ", ".join(f"{k.capitalize()}: {v}" for k, v in info_web.items())
            return {"respuesta": f"Sobre nosotros: {mensaje_info}", "fuente": "fallback_info_web"}
            
        return {"respuesta": "No entendí bien tu consulta. ¿Podrías reformularla? También puedes ver el catálogo o hablar con un agente.", "fuente": "fallback_generico_final", "botones": [{"texto": "Ver catálogo", "action": "ver_catalogo"}, {"texto": "Hablar con un agente", "action": "hablar_con_agente"}]}

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

    context = {
        "user_id": user_id_ctx, "nombre_pyme": nombre_pyme_ctx,
        "rubro_nombre": rubro_nombre_para_coleccion.lower(),
        "mensajes_previos": flask_session.get(NOMBRE_HISTORIAL_SESION, []),
        CONTEXTO_PYME: flask_session.get(CONTEXTO_PYME, {}),
        "cliente_id": getattr(viewer_user, "id", None), "anon_id": anon_id,
        "rubro_id": getattr(rubro_obj, "id", None) if rubro_obj else (getattr(owner_user.rubro, "id", None) if owner_user and hasattr(owner_user, "rubro") else None),
        "coleccion_qdrant": coleccion_qdrant_ctx
    }
    
    estado_conversacion_actual_str = context[CONTEXTO_PYME].get("estado_conversacion")
    estado_conversacion_actual = deserialize_state(estado_conversacion_actual_str)
    texto_pregunta_lower = pregunta.lower() # Para comparaciones de botones

    if estado_conversacion_actual == PymeConversationState.ESPERANDO_DETALLES_RECLAMO:
        detalles_reclamo = pregunta 
        pregunta_original_reclamo = context[CONTEXTO_PYME].get("pregunta_reclamo_original", "Reclamo sin detalles previos.")
        
        context[CONTEXTO_PYME].pop("estado_conversacion", None); context[CONTEXTO_PYME].pop("pregunta_reclamo_original", None)
        flask_session[CONTEXTO_PYME] = context[CONTEXTO_PYME] 

        pregunta_para_agente = f"Reclamo (Pregunta original: \"{pregunta_original_reclamo}\"). Detalles del cliente: \"{detalles_reclamo}\""
        if texto_pregunta_lower == "prefiero no dar detalles": 
            pregunta_para_agente = f"Reclamo (Pregunta original: \"{pregunta_original_reclamo}\"). El cliente prefirió no dar más detalles por chat."
        
        # HumanHandler se encarga de la respuesta y de limpiar el contexto si es necesario.
        # También guarda la conversación.
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
        if context["user_id"]: # Solo guardar si hay user_id (propietario o cliente logueado)
            db.session.add(Conversacion(user_id=context["user_id"], pregunta=pregunta, respuesta=respuesta_final.get("respuesta", ""),
                                        fuente=respuesta_final.get("fuente", "desconocida"), rubro=context["rubro_nombre"]))
            db.session.commit()
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

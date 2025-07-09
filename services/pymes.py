import logging
import re
import random
import json
import uuid
from enum import Enum, auto
try:
    from flask import session as flask_session, current_app, request # Añadir request
except Exception:
    flask_session = {}
    current_app = None
    request = None # Mock request si no hay contexto Flask

from services.cohere_ai import robust_chat, get_cohere_response
from models import Conversacion, db, ArchivoAdjunto, PymePedido, User, CatalogoItem, TicketComentario # Añadir TicketComentario
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    armar_respuesta_legible,
    CATALOGO_PYME
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import sugerencias_por_rubro
from services.logic import detectar_small_talk_con_llm, generar_respuesta_small_talk, es_rubro_publico
from services.ticket_service import servicio_tickets
from services.webinfo import obtener_info_web
from .common_utils import construir_respuesta_sugerir_registro # <--- NUEVA IMPORTACIÓN
from services.preferences import add_preference
from services import cart as cart_service
from services.promocion_service import promocion_service
from .llm_utils import extract_multiple_contact_details_llm, resumir_descripcion_producto_llm
from .common_utils import validar_email, validar_telefono

logger = logging.getLogger(__name__)

CONTEXTO_PYME = "contexto_pyme_v2"
NOMBRE_HISTORIAL_SESION = "historial_chat_cliente_pyme_v2"
MAX_HISTORIAL_CHAT = 30
CANCEL_KEYWORDS = {"cancel", "cancelar", "cancelalo", "anular", "borrar", "no gracias", "mejor no", "olvidalo"}

def tiene_archivo_catalogo(user_id: int) -> bool:
    if not user_id: return False
    try:
        return ArchivoAdjunto.query.filter_by(user_id=user_id, tipo="catalogo").first() is not None
    except Exception: return False

def url_descargar_catalogo_pyme(pyme_id: int) -> str:
    if not current_app or not request: # Si no hay contexto de app/request (ej. prueba unitaria)
        return f"/catalogo/publico/{pyme_id}/descargar" # Fallback a URL relativa
    base_url = current_app.config.get("APP_BASE_URL", request.url_root.rstrip('/'))
    return f"{base_url}/catalogo/publico/{pyme_id}/descargar"

import ast

def extraer_productos_llm(texto: str) -> list[dict]:
    prompt = (
        "Analiza el MENSAJE DEL USUARIO para extraer productos, sus cantidades numéricas y sus unidades de medida específicas si se mencionan (ej. 'kilos', 'cajas', 'paquetes', 'docenas', 'metros'). "
        "Responde ÚNICAMENTE con una lista de objetos JSON válida. Cada objeto debe tener:\n"
        "- \"nombre\": El nombre descriptivo del producto (string).\n"
        "- \"cantidad\": La cantidad numérica asociada al producto (integer).\n"
        "- \"unidad\" (opcional): La unidad de medida específica si se menciona (string). Si la unidad es implícita o genérica como 'unidades' o 'ítems', puedes omitirla.\n"
        f"MENSAJE DEL USUARIO: '{texto}'\n"
        "RESPUESTA:"
    )
    resp_content = ""
    try:
        resp_content = robust_chat(message=prompt)
        if not resp_content: return []
        datos = []
        try: datos = json.loads(resp_content)
        except json.JSONDecodeError:
            try:
                resp_corrected = resp_content.replace("true", "True").replace("false", "False").replace("null", "None")
                match_md_json = re.match(r"^\s*```json\s*([\s\S]*?)\s*```\s*$", resp_corrected, re.DOTALL)
                if match_md_json: resp_corrected = match_md_json.group(1)
                datos = ast.literal_eval(resp_corrected)
            except: return []
        items: list[dict] = []
        if isinstance(datos, list):
            for it in datos:
                if not isinstance(it, dict): continue
                nombre = str(it.get("nombre", "")).strip()
                cantidad_raw = it.get("cantidad", 1); unidad = str(it.get("unidad", "")).strip(); cantidad = 1
                try: cantidad = int(float(str(cantidad_raw).replace(',','.')))
                except: pass
                if cantidad < 1: cantidad = 1
                if nombre: 
                    item_data = {"nombre": nombre, "cantidad": cantidad}
                    if unidad: item_data["unidad"] = unidad
                    items.append(item_data)
        elif isinstance(datos, dict):
            nombre = str(datos.get("nombre", "")).strip(); cantidad_raw = datos.get("cantidad", 1)
            unidad = str(datos.get("unidad", "")).strip(); cantidad = 1
            try: cantidad = int(float(str(cantidad_raw).replace(',','.')))
            except: pass
            if cantidad < 1: cantidad = 1
            if nombre:
                item_data = {"nombre": nombre, "cantidad": cantidad}
                if unidad: item_data["unidad"] = unidad
                items.append(item_data)
        return items
    except Exception as e: logger.exception(f"Error en extraer_productos_llm: {e}")
    return []

def extraer_productos_regex(texto: str) -> list[dict]:
    partes = re.split(r",\s*(?![^()]*\))|\s+y\s+(?![^()]*\))", texto)
    items: list[dict] = []
    pattern = re.compile(r"^\s*(\d+\.?\d*|\d+)\s*([a-zA-Záéíóúñ/\-]+(?:\s+[a-zA-Záéíóúñ/\-]+)*)?\s*(?:de\s+)?(.+)", re.IGNORECASE)
    for p_str in partes:
        p_str = p_str.strip()
        match = pattern.match(p_str)
        if match:
            try:
                cantidad_str = match.group(1); unidad_str = match.group(2); nombre_str = match.group(3).strip()
                if unidad_str and len(unidad_str.split()) > 2: nombre_str = f"{unidad_str} {nombre_str}".strip(); unidad_str = None
                if nombre_str.lower().endswith(" de"): nombre_str = nombre_str[:-3].strip()
                if nombre_str:
                    item_data = {"nombre": nombre_str, "cantidad": int(float(cantidad_str.replace(',','.')))}
                    if unidad_str and unidad_str.lower() not in ["unidad", "unidades"]: item_data["unidad"] = unidad_str.lower()
                    items.append(item_data)
            except: pass
        elif p_str:
            m_simple_nombre_primero = re.match(r"(.+?)\s+(\d+\.?\d*|\d+)\s*([a-zA-Záéíóúñ/\-]+)?$", p_str)
            if m_simple_nombre_primero:
                try:
                    nombre = m_simple_nombre_primero.group(1).strip(); cantidad = int(float(m_simple_nombre_primero.group(2).replace(',','.'))); unidad = m_simple_nombre_primero.group(3)
                    if nombre:
                        item_data = {"nombre": nombre, "cantidad": cantidad}
                        if unidad and unidad.lower() not in ["unidad", "unidades"]: item_data["unidad"] = unidad.lower()
                        items.append(item_data)
                except: pass
            elif p_str: items.append({"nombre": p_str, "cantidad": 1})
    return items

def extraer_productos_pedido(texto: str) -> list[dict]:
    items_regex = extraer_productos_regex(texto)
    if items_regex: return items_regex
    return extraer_productos_llm(texto)

def formatear_carrito_desde_summary(summary_cart_obj: dict, context: dict = None) -> str:
    if not summary_cart_obj or not summary_cart_obj.get("items_detalle"):
        return "Tu carrito está vacío."
    items_detalle = summary_cart_obj.get("items_detalle", [])
    lineas_carrito = ["**Tu Carrito de Compras:**"]
    for item in items_detalle:
        nombre = item.get("nombre_producto", "Desconocido"); cantidad = item.get("cantidad", 0)
        precio_original_unit = item.get("precio_unitario_original", 0.0)
        subtotal_con_descuento_item = item.get("subtotal_con_descuento", cantidad * precio_original_unit)
        descuento_aplicado_linea = item.get("descuento_aplicado_linea", 0.0)
        moneda = item.get("moneda", "ARS"); presentacion = item.get("presentacion", "")
        promocion_aplicada_item_info = item.get("promocion_aplicada_info")
        linea = f"- {cantidad} x {nombre}"
        if presentacion: linea += f" ({presentacion})"
        linea += f" @ ${precio_original_unit:,.2f} {moneda} c/u"
        if descuento_aplicado_linea > 0:
            precio_original_total_linea = cantidad * precio_original_unit
            linea += f" (Original: <s style='color:grey;'>${precio_original_total_linea:,.2f}</s>)"
            linea += f" <b style='color:green;'>Ahora: ${subtotal_con_descuento_item:,.2f} {moneda}</b>"
            if promocion_aplicada_item_info and promocion_aplicada_item_info.get("nombre_promocion"):
                 linea += f" <i style='font-size:smaller; color:green;'>({promocion_aplicada_item_info['nombre_promocion']})</i>"
        else: linea += f" = ${subtotal_con_descuento_item:,.2f} {moneda}"
        lineas_carrito.append(linea)
    total_original_calc = summary_cart_obj.get("total_original_calculado", 0.0)
    total_final_desc = summary_cart_obj.get("total_final_con_descuento", total_original_calc)
    total_ahorrado = summary_cart_obj.get("total_ahorrado_final", 0.0)
    moneda_carrito = items_detalle[0].get("moneda", "ARS") if items_detalle else "ARS"
    if total_original_calc > 0:
        lineas_carrito.append(f"\nSubtotal Original: ${total_original_calc:,.2f} {moneda_carrito}")
        if total_ahorrado > 0:
            lineas_carrito.append(f"**Descuentos Totales: -${total_ahorrado:,.2f} {moneda_carrito}** 🎉")
            promo_total_info = summary_cart_obj.get("promo_total_carrito_aplicada_info")
            if promo_total_info: lineas_carrito.append(f"<i style='font-size:smaller; color:green;'>Promo sobre el total: '{promo_total_info['nombre_promocion']}' (-${promo_total_info['descuento_sobre_total_aplicado']:.2f})</i>")
            nombres_promos_items_unicos = set()
            for item_det in items_detalle:
                if item_det.get("promocion_aplicada_info") and item_det["promocion_aplicada_info"].get("nombre_promocion"):
                    nombres_promos_items_unicos.add(item_det["promocion_aplicada_info"]["nombre_promocion"])
            if nombres_promos_items_unicos and not promo_total_info:
                 if len(nombres_promos_items_unicos) == 1: lineas_carrito.append(f"<i style='font-size:smaller; color:green;'>Promoción aplicada: {list(nombres_promos_items_unicos)[0]}</i>")
                 elif len(nombres_promos_items_unicos) > 1: lineas_carrito.append(f"<i style='font-size:smaller; color:green;'>Promociones aplicadas: {', '.join(list(nombres_promos_items_unicos))}</i>")
        lineas_carrito.append(f"\n**TOTAL A PAGAR: ${total_final_desc:,.2f} {moneda_carrito}**")
    return "\n".join(lineas_carrito)

class PymeConversationState(Enum):
    IDLE = auto(); ESPERANDO_PRODUCTO = auto(); CONFIRMANDO_PEDIDO = auto(); PEDIDO_FINALIZADO = auto()
    ESPERANDO_DATOS_CLIENTE_NOMBRE = auto(); ESPERANDO_DATOS_CLIENTE_TELEFONO = auto()
    ESPERANDO_DATOS_CLIENTE_DIRECCION = auto(); ESPERANDO_DATOS_CLIENTE_EMAIL = auto()
    ESPERANDO_CONFIRMACION_FINAL_CON_DATOS = auto(); ESPERANDO_FEEDBACK = auto()
    ESPERANDO_NUMERO_TICKET = auto(); ESPERANDO_CONFIRMACION_CIERRE = auto()
    ESPERANDO_CALIFICACION = auto(); ESPERANDO_DETALLES_RECLAMO = auto()

def serialize_state(state): return state.name if state else None
def deserialize_state(value):
    if not value: return None
    try: return PymeConversationState[value]
    except KeyError: return None

# Helper to serialize Enum objects within dicts/lists for JSON
def serializar_enum(obj):
    if isinstance(obj, Enum):
        return obj.name
    elif isinstance(obj, dict):
        return {k: serializar_enum(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serializar_enum(v) for v in obj]
    else:
        return obj

PROMPT_CLASIFICAR_INTENCION = """
Sos el cerebro comercial de un chatbot para una pyme. Analizá la PREGUNTA DEL USUARIO y respondé sólo con una de estas intenciones de la lista.
Si no encaja claramente, usa 'pregunta_ambigua'.
INTENCIONES POSIBLES:
- saludo
- ver_catalogo
- consultar_ofertas
- iniciar_pedido
- agregar_al_carrito
- ver_carrito
- modificar_cantidad_carrito
- eliminar_del_carrito
- vaciar_carrito
- finalizar_pedido
- continuar_flujo
- cancelar_flujo
- pregunta_faq
- consultar_estado_ticket
- hablar_con_agente
- descargar_catalogo
- pregunta_ambigua
PREGUNTA DEL USUARIO: "{pregunta_usuario}"
ESTADO_CONVERSACION_ACTUAL: {estado_conversacion_actual}
INTENCIÓN: """ # Añadido estado_conversacion_actual al prompt

def _clasificar_intencion_pyme_con_llm(pregunta: str, context_dict: dict) -> str:
    estado_actual_str = context_dict.get(CONTEXTO_PYME, {}).get("estado_conversacion", "IDLE")
    prompt_enriquecido = PROMPT_CLASIFICAR_INTENCION.format(pregunta_usuario=pregunta, estado_conversacion_actual=estado_actual_str)
    historial_mensajes = context_dict.get("mensajes_previos", [])
    try:
        res = get_cohere_response(message=prompt_enriquecido, preamble="Eres un clasificador de intenciones para PYME.", chat_history=historial_mensajes[-6:]) # Últimos 3 intercambios
        return res.strip().lower() if res else "pregunta_ambigua"
    except Exception as e: logger.error(f"Error clasificando intención PYME: {e}"); return "pregunta_ambigua"

def analizar_sentimiento_llm(texto: str) -> str:
    prompt = (f"Analiza el sentimiento del texto y responde 'positivo', 'negativo' o 'neutral'. TEXTO: '{texto}'\nSENTIMIENTO:")
    try:
        res = robust_chat(message=prompt); sentimiento = res.strip().lower()
        return sentimiento if sentimiento in {"positivo", "negativo"} else "neutral"
    except: return "neutral"

class BaseHandler:
    def __init__(self, context):
        self.context = context
        # self.pyme_ctx se inicializa directamente desde context_general[CONTEXTO_PYME]
        # que ya fue cargado desde chat_db_context.context_data en responder_pyme
        self.pyme_ctx = self.context[CONTEXTO_PYME]
        self.pyme_id_actual = self.context.get("user_id"); self.cliente_id_actual = self.context.get("cliente_id")
        self.chat_session_uuid_actual = self.context.get("chat_session_uuid")

    def _guardar_contexto_pyme(self):
        # self.pyme_ctx es una referencia al diccionario dentro de self.context["chat_db_context_data"][CONTEXTO_PYME] (o similar)
        # Las modificaciones a self.pyme_ctx ya se reflejan en self.context["chat_db_context_data"]
        # La persistencia final de self.context["chat_db_context_data"] (que es chat_db_context.context_data)
        # se hace en routes/chat.py después de que responder_pyme retorna.
        # Esta función podría volverse un no-op o usarse para validaciones si es necesario.
        # Por ahora, nos aseguramos que pyme_ctx esté en el lugar correcto en chat_db_context_data.
        if self.context.get("chat_db_context_data"):
            self.context["chat_db_context_data"][CONTEXTO_PYME] = self.pyme_ctx
        else: # Fallback por si chat_db_context_data no está (no debería ocurrir)
            logger.error("[BaseHandler._guardar_contexto_pyme] chat_db_context_data no encontrado en self.context.")

    def _actualizar_estado(self, nuevo_estado: PymeConversationState, reintentos: int = 0):
        self.pyme_ctx["estado_conversacion"] = serialize_state(nuevo_estado)
        self.pyme_ctx["reintentos"] = reintentos; self._guardar_contexto_pyme()
    def _obtener_contexto_llm(self) -> dict:
        summary = {"estado_actual_flujo": self.pyme_ctx.get("estado_conversacion"), "ultima_intencion_registrada": self.context.get("intencion")}
        if self.pyme_id_actual:
            cart_summary = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
            if cart_summary and cart_summary.get("items_detalle"):
                summary["items_carrito_nombres"] = [item.get("nombre_producto") for item in cart_summary["items_detalle"][:2]]
                summary["total_carrito_actual"] = cart_summary.get("total_final_con_descuento")
        for k in ["nombre_cliente", "producto_interes_previo", "productos_vistos_o_mencionados"]: # Añadir más del pyme_ctx
            if self.pyme_ctx.get(k): summary[k] = self.pyme_ctx[k]
        return {k: v for k, v in summary.items() if v is not None}
    def handle(self, pregunta): raise NotImplementedError

class SaludoHandler(BaseHandler):
    def handle(self, pregunta):
        nombre = self.context.get("nombre_pyme", "la empresa")
        body = f"¡Hola! Soy tu asistente para {nombre}. ¿En qué puedo ayudarte hoy?"
        options = [
            {"id": "ver_catalogo_pyme", "texto": "Ver catálogo"},
            {"id": "ver_ofertas_pyme", "texto": "Ver ofertas"}
        ]

        if self.pyme_id_actual:
            resumen_carrito_existente = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
            if resumen_carrito_existente and resumen_carrito_existente.get("items_detalle"):
                body += f"\n\nVeo que tienes algunos productos en tu carrito. ¿Quieres continuar con ese pedido o empezar uno nuevo?"
                options = [
                    {"id": "ver_carrito_pyme", "texto": "Continuar pedido"},
                    {"id": "limpiar_y_nuevo_pedido_saludo_pyme", "texto": "Nuevo pedido"},
                    {"id": "ver_catalogo_pyme_con_carrito", "texto": "Ver catálogo"}
                ]

        message_type = 'interactive_buttons' # Max 3 options in both cases

        return {
            "message_body": body,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_saludo_interactivo_v2"
        }

class CatalogoHandler(BaseHandler):
    def handle(self, pregunta):
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "catalogo_sin_pyme_id_v2"}
        query_qdrant = pregunta
        if self.context.get("intencion") == "ver_catalogo" and len(pregunta.split()) < 3: query_qdrant = "productos populares"

        resultados_qdrant = buscar_catalogo_qdrant(self.pyme_id_actual, query_qdrant, self.context.get("rubro_nombre"), 3, self.context.get("coleccion_qdrant", CATALOGO_PYME))
        add_preference("busquedas", pregunta)
        
        respuesta_texto = ""; botones_catalogo = []; fuente_catalogo = "catalogo_qdrant_sin_resultados_v2"

        if resultados_qdrant:
            productos_formateados = []
            for idx, hit in enumerate(resultados_qdrant):
                payload = getattr(hit, "payload", {}); item_db_id = payload.get("db_id")
                item_obj = db.session.get(CatalogoItem, item_db_id) if item_db_id else None
                nombre = payload.get("nombre", "Producto"); desc = resumir_descripcion_producto_llm(payload.get("descripcion_corta") or payload.get("descripcion",""), 80, 20)
                precio_s, precio_f, moneda = parse_precio_flexible(payload.get("precio_str", ""))
                linea = f"**{idx+1}. {nombre}**"
                if desc: linea += f"\n   _{desc}_"
                precio_final, promo_txt = precio_f, ""
                if item_obj:
                    promos = promocion_service.obtener_promociones_aplicables_a_item(self.pyme_id_actual, item_obj, 1)
                    if promos:
                        mejor_promo = promos[0]; precio_final = mejor_promo.get('precio_con_descuento_unitario', precio_f)
                        promo_txt = f"🔥 ¡Oferta! {mejor_promo['nombre_promocion']}"
                        if mejor_promo.get('descripcion_publica') != mejor_promo['nombre_promocion']: promo_txt += f": {mejor_promo['descripcion_publica']}"

                linea += f"\n   Precio: ${precio_final if precio_final is not None else precio_f:,.2f} {moneda or 'ARS'}"
                if promo_txt and precio_f != precio_final : linea += f" (Antes: <s style='color:grey;'>${precio_f:,.2f}</s>)"

                promo_qdrant_txt = payload.get("promocion_texto")
                if promo_qdrant_txt and not promo_txt: linea += f"\n   ✨ *Promo: {promo_qdrant_txt}*"
                elif promo_txt: linea += f"\n   *{promo_txt}*"
                productos_formateados.append(linea)
                identificador_accion = payload.get("sku") or item_db_id or nombre
                botones_catalogo.append({"texto": f"Pedir {nombre[:20]}", "action": f"pedir_item_{identificador_accion}"})

            if productos_formateados:
                respuesta_texto = "Algunos productos que podrían interesarte:\n\n" + "\n\n".join(productos_formateados)
                respuesta_texto += "\n\nSi quieres alguno, usa los botones o dime (ej: 'quiero 2 [nombre]')."
                fuente_catalogo = "catalogo_qdrant_con_promos_v2"
        
        if not respuesta_texto:
            respuesta_texto = f"No encontré productos para '{pregunta}'. Intenta con otras palabras."
            # No product-specific buttons if nothing found
            botones_catalogo = []

        body = respuesta_texto
        options = []

        # Add "Pedir {nombre}" buttons from botones_catalogo (which were generated from resultados_qdrant)
        # Assuming botones_catalogo was populated correctly if resultados_qdrant had hits
        for btn_cat_original in botones_catalogo: # botones_catalogo was defined earlier in your original code
            # Original action: f"pedir_item_{identificador_accion}"
            # We need the identificador_accion part for the ID.
            action_str = btn_cat_original.get("action", "")
            id_suffix = action_str.replace("pedir_item_", "") if action_str.startswith("pedir_item_") else normalizar_texto(btn_cat_original.get("texto", ""))

            options.append({
                "id": f"pedir_item_pyme_{id_suffix}",
                "texto": btn_cat_original.get("texto", "Pedir producto")[:20] # Ensure text is suitable for button title
            })
            if len(options) >= 7 and self.context.get("channel") == 'whatsapp': # Limit Pedir buttons for WhatsApp to leave space for general ones
                break

        # Add general action buttons
        options.append({"id": "ver_catalogo_pyme_buscar_otra", "texto": "Buscar otra cosa"})

        if tiene_archivo_catalogo(self.pyme_id_actual):
            url_cat = url_descargar_catalogo_pyme(self.pyme_id_actual)
            if self.context.get("channel") == "whatsapp":
                body += f"\n\nTambién puedes descargar nuestro catálogo completo en: {url_cat}"
            else: # For web, add as a URL button
                options.append({
                    "id": "descargar_catalogo_pyme_pdf",
                    "texto": "Descargar Catálogo PDF",
                    "url": url_cat,
                    "type": "url" # For formatter to handle for web
                })

        options.append({"id": "hablar_con_agente_pyme_catalogo", "texto": "Hablar con un agente"})

        interactive_options_count = sum(1 for opt in options if opt.get("type") != "url")
        message_type = 'text'
        if interactive_options_count == 1: message_type = 'interactive_buttons'
        elif 1 < interactive_options_count <= 3: message_type = 'interactive_buttons'
        elif interactive_options_count > 3: message_type = 'interactive_list'

        if interactive_options_count == 0 and any(opt.get("type") == "url" for opt in options):
             message_type = 'text'

        return {
            "message_body": body,
            "options_list": options,
            "message_type": message_type,
            "fuente": fuente_catalogo
        }

class OfertasHandler(BaseHandler):
    def handle(self, pregunta):
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "ofertas_sin_pyme_id_v2"}
        promos = promocion_service.get_promociones_for_pyme(self.pyme_id_actual, activas_unicamente=True)

        options = [{"id": "ver_catalogo_pyme_ofertas", "texto": "Ver catálogo"}]
        message_type = 'interactive_buttons' # Only 1 option

        if promos:
            body = "¡Tenemos estas promociones activas!\n" + "\n".join([f"\n**{p.nombre_promocion}**: {p.descripcion_publica}" for p in promos[:3]])
            if len(promos) > 3: body += f"\n... y {len(promos) - 3} más!"
            body += "\n\n¿Te interesa alguna o quieres ver productos?"
            return {
                "message_body": body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "pyme_ofertas_con_promos_v2"
            }

        body_no_ofertas = "No tenemos ofertas especiales ahora, pero explora nuestro catálogo."
        return {
            "message_body": body_no_ofertas,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_ofertas_sin_promos_v2"
        }

class PedidoHandler(BaseHandler):
    def _sugerir_productos_complementarios(self, ultimo_producto_nombre: str, items_recien_agregados: list) -> tuple[str, list]:
        logger.debug(f"Sugerir complementarios (pyme_id: {self.pyme_id_actual}): refactorizar para nuevo carrito.")
        return "", [] # Simplificado por ahora

    def _extraer_item_para_modificar(self, texto_usuario: str) -> Optional[dict]:
        # Intenta extraer "nombre del producto" o "el último" o "el primero"
        texto_norm = texto_usuario.lower()
        resumen_carrito = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
        items_en_carrito = resumen_carrito.get("items_detalle", [])
        if not items_en_carrito: return None

        if "ultimo" in texto_norm or "último" in texto_norm: return items_en_carrito[-1]
        if "primero" in texto_norm: return items_en_carrito[0]

        # Quitar palabras clave de acción para aislar el nombre
        for kw in ["quitar", "sacar", "eliminar", "remover", "dejar", "cambiar cantidad de", "actualizar", "poner", "de", "a"]:
            texto_norm = texto_norm.replace(kw, "")

        nombre_buscado = texto_norm.strip()
        if not nombre_buscado: return None

        # Buscar por similitud en los nombres del carrito
        mejor_match_item = None; max_sim = 0.6 # Umbral mínimo
        from .common_utils import calcular_similitud_levenshtein # Asegurar importación
        for item_c in items_en_carrito:
            sim = calcular_similitud_levenshtein(nombre_buscado, item_c.get("nombre_producto","").lower())
            if sim > max_sim: max_sim = sim; mejor_match_item = item_c
        return mejor_match_item

    def handle(self, pregunta):
        estado_actual = deserialize_state(self.pyme_ctx.get("estado_conversacion")) or PymeConversationState.IDLE
        texto_usuario_lower = pregunta.lower()
        accion_payload = self.context.get("action_payload", texto_usuario_lower)
        intencion_actual = self.context.get("intencion")
        logger.info(f"[PedidoHandler-{self.pyme_id_actual}] Estado: {estado_actual}, Intención: {intencion_actual}, Payload: '{accion_payload}', Pregunta: '{pregunta}'")

        if not self.pyme_id_actual: return {"respuesta": "Error: Tienda no identificada.", "fuente": "error_pyme_id_pedido_handler"}

        # Acciones directas sobre el carrito
        if accion_payload == "vaciar_carrito_accion":
            cart_service.clear_pyme_cart(self.pyme_id_actual)
            self._actualizar_estado(PymeConversationState.ESPERANDO_PRODUCTO) # Quedarse en modo pedido
            options_vaciado = [{"id": "ver_catalogo_pyme_carrito_vaciado", "texto": "Ver Catálogo"}]
            return {
                "message_body": "Tu carrito ha sido vaciado. ¿Qué deseas pedir ahora?",
                "options_list": options_vaciado,
                "message_type": 'interactive_buttons',
                "fuente": "pyme_carrito_vaciado_v2"
            }

        if intencion_actual == "eliminar_del_carrito" or accion_payload.startswith("eliminar_item_"):
            item_a_eliminar_id = None
            if accion_payload.startswith("eliminar_item_"): # Botón con ID
                try: item_a_eliminar_id = int(accion_payload.replace("eliminar_item_", ""))
                except: pass
            else: # Texto, intentar extraer
                item_extraido = self._extraer_item_para_modificar(pregunta)
                if item_extraido: item_a_eliminar_id = item_extraido.get("catalogo_item_id")
            
            if item_a_eliminar_id:
                cart_service.remove_item_from_cart(self.pyme_id_actual, item_a_eliminar_id)
                resumen_tras_eliminar = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                self._actualizar_estado(PymeConversationState.ESPERANDO_PRODUCTO)
                options_eliminado = [
                    {"id": "agregar_mas_pedido_pyme", "texto": "Agregar más"},
                    {"id": "finalizar_pedido_pyme", "texto": "Finalizar pedido"}
                ]
                return {
                    "message_body": f"Item eliminado.\n{formatear_carrito_desde_summary(resumen_tras_eliminar, self.context)}",
                    "options_list": options_eliminado,
                    "message_type": 'interactive_buttons',
                    "fuente": "pyme_item_eliminado_v2"
                }
            options_no_id_elim = [{"id": "ver_carrito_pyme_no_id_elim", "texto": "Ver Carrito"}]
            return {
                "message_body": "No pude identificar qué producto quitar. Puedes ver tu carrito y reintentar.",
                "options_list": options_no_id_elim,
                "message_type": 'interactive_buttons',
                "fuente": "pyme_eliminar_item_no_id_v2"
            }
        
        if intencion_actual == "modificar_cantidad_carrito":
            items_extraidos_mod = extraer_productos_pedido(pregunta)
            if items_extraidos_mod:
                item_info = items_extraidos_mod[0]; nombre_prod_mod = item_info["nombre"]; nueva_cant = item_info["cantidad"]
                item_en_carrito = self._extraer_item_para_modificar(nombre_prod_mod) # Buscar por nombre en carrito
                if item_en_carrito and item_en_carrito.get("catalogo_item_id"):
                    cart_service.update_item_quantity_in_cart(self.pyme_id_actual, item_en_carrito["catalogo_item_id"], nueva_cant)
                    resumen_tras_modif = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                    self._actualizar_estado(PymeConversationState.ESPERANDO_PRODUCTO)
                    options_qty_upd = [
                        {"id": "agregar_mas_pedido_pyme_qty", "texto": "Agregar más"},
                        {"id": "finalizar_pedido_pyme_qty", "texto": "Finalizar pedido"}
                    ]
                    return {
                        "message_body": f"Cantidad actualizada.\n{formatear_carrito_desde_summary(resumen_tras_modif, self.context)}",
                        "options_list": options_qty_upd,
                        "message_type": 'interactive_buttons',
                        "fuente": "pyme_item_cantidad_actualizada_v2"
                    }
            return {"message_body": "No entendí qué producto o cantidad modificar. Ej: '3 vinos malbec' o 'cambiar manzanas a 5'.", "options_list": [], "message_type": "text", "fuente": "pyme_modificar_cantidad_ayuda_v2"}


        # Flujo principal de estados
        if estado_actual == PymeConversationState.IDLE and (intencion_actual == "iniciar_pedido" or accion_payload == "iniciar_pedido" or accion_payload == "limpiar_y_nuevo_pedido_saludo"):
            if accion_payload == "limpiar_y_nuevo_pedido_saludo": cart_service.clear_pyme_cart(self.pyme_id_actual)
            self._actualizar_estado(PymeConversationState.ESPERANDO_PRODUCTO)
            msg_ini = "¿Qué productos y cantidades te gustaría pedir? También puedes subir un archivo Excel."

            if pregunta not in ["iniciar_pedido", "limpiar_y_nuevo_pedido_saludo"] and intencion_actual == "iniciar_pedido":
                # Process current question as if it's an addition to the order
                return self.handle(pregunta) # Recursive call, will hit ESPERANDO_PRODUCTO

            options_ini = [{"id": "ver_catalogo_pyme_pedido_inicio", "texto": "Ver catálogo"}]
            return {
                "message_body": msg_ini,
                "options_list": options_ini,
                "message_type": 'interactive_buttons',
                "fuente": "pyme_iniciar_pedido_v2"
            }

        elif estado_actual == PymeConversationState.ESPERANDO_PRODUCTO:
            # (Manejo de CANCEL_KEYWORDS, ver_carrito, finalizar_pedido ya están arriba o en intenciones)
            if accion_payload == "agregar_mas_pedido": return {"respuesta": "¿Qué más quieres agregar?", "fuente": "pidiendo_mas_productos_v2"}

            texto_para_extraccion = pregunta
            items_extraidos = []
            if accion_payload.startswith("pedir_item_"):
                identificador_item_accion = accion_payload.replace("pedir_item_", "").replace("_", " ")
                items_extraidos = [{"nombre": identificador_item_accion, "cantidad": 1}] # Asumir cantidad 1
            elif texto_para_extraccion.strip():
                items_extraidos = extraer_productos_pedido(texto_para_extraccion)
            
            if not items_extraidos and not accion_payload.startswith("pedir_item_"): # Si no se extrajo nada y no fue un botón de pedir
                 resumen_vacio = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                 options_no_extr = [
                     {"id": "ver_catalogo_pyme_no_extr", "texto": "Ver catálogo"},
                     {"id": "finalizar_pedido_pyme_no_extr", "texto": "Finalizar pedido"}
                 ]
                 return {
                     "message_body": f"No entendí qué producto agregar. {formatear_carrito_desde_summary(resumen_vacio, self.context)}",
                     "options_list": options_no_extr,
                     "message_type": 'interactive_buttons',
                     "fuente": "pyme_producto_no_entendido_v2"
                 }

            items_agregados_ok_nombres = []
            productos_no_hallados_nombres = []

            for item_ext in items_extraidos:
                nombre_b = item_ext["nombre"]; cant_b = item_ext.get("cantidad",1)
                res_q = buscar_catalogo_qdrant(self.pyme_id_actual, nombre_b, limite=1, coleccion=self.context.get("coleccion_qdrant"))
                if res_q and res_q[0].payload:
                    p = res_q[0].payload
                    id_c = p.get("db_id") or (CatalogoItem.query.filter_by(user_id=self.pyme_id_actual, sku=p.get("sku")).first().id if p.get("sku") else None)
                    if id_c and p.get("precio_float") is not None:
                        info_p = {"catalogo_item_id": id_c, "nombre_producto": p.get("nombre"), "precio_unitario_original": p.get("precio_float"),
                                  "moneda": p.get("moneda","ARS"), "sku": p.get("sku"), "presentacion": p.get("unidad_descripcion")or p.get("unidad_original"), "imagen_url": p.get("imagen_url")}
                        cart_service.add_item_to_cart(self.pyme_id_actual, info_p, cant_b)
                        items_agregados_ok_nombres.append(info_p["nombre_producto"])
                        add_preference("productos", info_p["nombre_producto"])
                    else: productos_no_hallados_nombres.append(nombre_b + (" (sin precio)" if id_c else " (ID no hallado)"))
                else: productos_no_hallados_nombres.append(nombre_b + " (no en catálogo)")
            
            msg_res = ""
            if items_agregados_ok_nombres: msg_res += f"Agregado(s): {', '.join(items_agregados_ok_nombres)}. "
            if productos_no_hallados_nombres: msg_res += f"No pude agregar: {', '.join(productos_no_hallados_nombres)}. "
            
            resumen_obj = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
            msg_res += f"\n{formatear_carrito_desde_summary(resumen_obj, self.context)}"
            self.pyme_ctx["reintentos"] = 0; self._guardar_contexto_pyme()
            
            options_agregado = [
                {"id": "agregar_mas_pedido_pyme_agregado", "texto": "Agregar más"},
                {"id": "finalizar_pedido_pyme_agregado", "texto": "Finalizar pedido"},
                {"id": "vaciar_carrito_pyme_agregado", "texto": "Vaciar carrito"}
            ]
            return {
                "message_body": msg_res,
                "options_list": options_agregado,
                "message_type": 'interactive_buttons', # Could be list if options grow
                "fuente": "pyme_pedido_progreso_v2"
            }

        elif estado_actual == PymeConversationState.CONFIRMANDO_PEDIDO:
            if any(k in accion_payload for k in CANCEL_KEYWORDS):
                cart_service.clear_pyme_cart(self.pyme_id_actual)
                self._actualizar_estado(PymeConversationState.IDLE)
                options_cancelado_conf = [{"id": "ver_catalogo_pyme_cancel_conf", "texto": "Ver catálogo"}]
                return {
                    "message_body": "Pedido cancelado. ¿Necesitás otra cosa?",
                    "options_list": options_cancelado_conf,
                    "message_type": 'interactive_buttons',
                    "fuente": "pyme_pedido_cancelado_confirmacion_v2"
                }

            if accion_payload == "confirmar_pedido":
                viewer_user = db.session.get(User, self.cliente_id_actual) if self.cliente_id_actual else None
                if viewer_user:
                    self.pyme_ctx["nombre_cliente"] = self.pyme_ctx.get("nombre_cliente") or viewer_user.name
                    tel_val = validar_telefono(viewer_user.telefono)
                    if tel_val: self.pyme_ctx["telefono_cliente"] = self.pyme_ctx.get("telefono_cliente") or tel_val
                    if validar_email(viewer_user.email): self.pyme_ctx["email_cliente"] = self.pyme_ctx.get("email_cliente") or viewer_user.email

                campos_nec = ["nombre_cliente", "telefono_cliente", "direccion_cliente"]
                campos_falt = [c for c in campos_nec if not self.pyme_ctx.get(c)]
                if campos_falt:
                    next_f, estado_sig, preg_sig = (campos_falt[0], PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE, "¿Nombre completo para el pedido?") if campos_falt[0] == "nombre_cliente" else \
                                                 (campos_falt[0], PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO, "¿Teléfono (con cód. área)?") if campos_falt[0] == "telefono_cliente" else \
                                                 (campos_falt[0], PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION, "¿Dirección de entrega?")
                    self._actualizar_estado(estado_sig)
                    return {"respuesta": preg_sig, "fuente": f"solicitando_dato_{next_f}_v3"}
                else: # Todos los datos ok
                    self._actualizar_estado(PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS)
                    return self.handle(pregunta="internal_trigger_final_confirm") # Re-llamar
            
            elif accion_payload == "modificar_pedido":
                 self._actualizar_estado(PymeConversationState.ESPERANDO_PRODUCTO)
                 res_obj_mod = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                 options_mod_pedido = [
                     {"id": "agregar_mas_pedido_pyme_mod", "texto": "Agregar más"},
                     {"id": "finalizar_pedido_pyme_mod", "texto": "Finalizar"}
                 ]
                 return {
                     "message_body": f"Ok, volvemos a tu pedido. Carrito:\n{formatear_carrito_desde_summary(res_obj_mod, self.context)}\n¿Qué quieres hacer?",
                     "options_list": options_mod_pedido,
                     "message_type": 'interactive_buttons',
                     "fuente": "pyme_modificando_pedido_v2"
                 }
            else: # Repreguntar
                res_obj_reconf = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                options_reconf = [
                    {"id": "confirmar_pedido_reconf", "texto": "Sí"}, # Action will be 'confirmar_pedido'
                    {"id": "modificar_pedido_reconf", "texto": "Modificar"}, # Action 'modificar_pedido'
                    {"id": "cancelar_pedido_reconf", "texto": "Cancelar"} # Action 'cancelar_pedido'
                ]
                return {
                    "message_body": f"Tu pedido es:\n{formatear_carrito_desde_summary(res_obj_reconf, self.context)}\n\n¿Confirmas? (Sí/Modificar/Cancelar)",
                    "options_list": options_reconf,
                    "message_type": 'interactive_buttons',
                    "fuente": "pyme_reconfirmando_pedido_v2"
                }
        
        elif estado_actual in [PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE, PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO, PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION]:
            extracted = extract_multiple_contact_details_llm(pregunta, ['nombre_cliente', 'telefono_cliente', 'direccion_cliente', 'email_cliente'])
            if extracted.get("nombre_cliente"): self.pyme_ctx["nombre_cliente"] = extracted["nombre_cliente"]
            tel_ext = validar_telefono(extracted.get("telefono_cliente",""))
            if tel_ext: self.pyme_ctx["telefono_cliente"] = tel_ext
            if extracted.get("direccion_cliente"): self.pyme_ctx["direccion_cliente"] = extracted["direccion_cliente"]
            email_ext = extracted.get("email_cliente","")
            if validar_email(email_ext): self.pyme_ctx["email_cliente"] = email_ext

            if estado_actual == PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE and not self.pyme_ctx.get("nombre_cliente"):
                if pregunta.strip() and len(pregunta.strip().split()) >=2: self.pyme_ctx["nombre_cliente"] = pregunta.strip()
            elif estado_actual == PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO and not self.pyme_ctx.get("telefono_cliente"):
                tel_val_directo = validar_telefono(pregunta.strip())
                if tel_val_directo: self.pyme_ctx["telefono_cliente"] = tel_val_directo
            elif estado_actual == PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION and not self.pyme_ctx.get("direccion_cliente"):
                if pregunta.strip() and len(pregunta.strip()) >= 5: self.pyme_ctx["direccion_cliente"] = pregunta.strip()
            
            self._guardar_contexto_pyme()
            return self.handle(pregunta="confirmar_pedido") # Re-evaluar si faltan datos

        elif estado_actual == PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS or accion_payload == "internal_confirm_data_trigger":
            if accion_payload == "confirmar_final_con_datos" or accion_payload == "internal_confirm_data_trigger":
                res_cart_db = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                items_db = res_cart_db.get("items_detalle", []); monto_db = res_cart_db.get("total_final_con_descuento", 0.0)
                try:
                    np = PymePedido(pyme_id=self.pyme_id_actual, asunto=f"Pedido de {self.pyme_ctx.get('nombre_cliente', 'Cliente')}",
                                    detalles=json.dumps(items_db), rubro=self.context.get("rubro_nombre"),
                                    nombre_cliente=self.pyme_ctx.get("nombre_cliente"), email_cliente=self.pyme_ctx.get("email_cliente"),
                                    telefono_cliente=self.pyme_ctx.get("telefono_cliente"), user_id=self.cliente_id_actual,
                                    direccion=self.pyme_ctx.get("direccion_cliente"), monto_total=monto_db)
                    db.session.add(np); db.session.commit()
                    logger.info(f"PymePedido {np.nro_pedido} creado. PYME: {self.pyme_id_actual}, Cliente: {self.cliente_id_actual}")
                    # TODO: Asociar archivo si self.context.get("archivo_id_para_asociar")
                    cart_service.clear_pyme_cart(self.pyme_id_actual)
                    self._actualizar_estado(PymeConversationState.ESPERANDO_FEEDBACK)
                    self.pyme_ctx["nro_pedido_confirmado"] = np.nro_pedido; self._guardar_contexto_pyme()
                    options_pedido_finalizado = [
                        {"id": "dejar_feedback_pyme", "texto": "Sí"},
                        {"id": "no_feedback_pyme", "texto": "No"}
                    ]
                    return {
                        "message_body": f"¡Listo! Pedido **#{np.nro_pedido}** registrado. Nos comunicaremos. ¿Comentarios? (Sí/No)",
                        "options_list": options_pedido_finalizado,
                        "message_type": 'interactive_buttons',
                        "fuente": "pyme_pedido_finalizado_v2"
                    }
                except Exception as e:
                    logger.error(f"Error PymePedido: {e}"); db.session.rollback()
                    return {"message_body": "Error registrando pedido.", "options_list": [], "message_type": "text", "fuente": "pyme_error_db_pedido_v2"}
            
            elif accion_payload == "modificar_datos_cliente":
                for k in ["nombre_cliente", "telefono_cliente", "direccion_cliente", "email_cliente"]: self.pyme_ctx.pop(k, None)
                self._actualizar_estado(PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE)
                return {"message_body": "Ok, ingresemos tus datos de nuevo. ¿Nombre completo?", "options_list": [], "message_type": "text", "fuente": "pyme_mod_datos_cliente_v2"}
            elif accion_payload == "modificar_pedido":
                 self._actualizar_estado(PymeConversationState.ESPERANDO_PRODUCTO)
                 res_obj_mod_final_conf = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                 options_mod_pedido_final = [
                     {"id": "agregar_mas_pedido_pyme_mod_final", "texto": "Agregar más"},
                     {"id": "finalizar_pedido_pyme_mod_final", "texto": "Finalizar"}
                 ]
                 return {
                     "message_body": f"Volvemos a tu pedido. Carrito:\n{formatear_carrito_desde_summary(res_obj_mod_final_conf, self.context)}\n¿Qué hacer?",
                     "options_list": options_mod_pedido_final,
                     "message_type": 'interactive_buttons',
                     "fuente": "pyme_mod_pedido_final_conf_v2"
                 }
            else: # Repreguntar en ESPERANDO_CONFIRMACION_FINAL_CON_DATOS
                res_obj_reconf_final_datos = cart_service.get_cart_summary(self.pyme_id_actual, self.cliente_id_actual)
                res_ped_str_reconf_final_datos = formatear_carrito_desde_summary(res_obj_reconf_final_datos, self.context)
                res_datos_cli_reconf = f"Nombre: {self.pyme_ctx.get('nombre_cliente', 'N/A')}\nTel: {self.pyme_ctx.get('telefono_cliente', 'N/A')}\nDir: {self.pyme_ctx.get('direccion_cliente', 'N/A')}"
                options_reconf_final_datos = [
                    {"id": "confirmar_final_con_datos_reconf", "texto": "Sí, está correcto"},
                    {"id": "modificar_datos_cliente_reconf", "texto": "Modificar datos"},
                    {"id": "modificar_pedido_reconf", "texto": "Modificar pedido"}
                ]
                return {
                    "message_body": f"No entendí. Revisemos:\nPedido:\n{res_ped_str_reconf_final_datos}\nDatos:\n{res_datos_cli_reconf}\n¿Correcto? (Sí/Modificar datos/Modificar pedido)",
                    "options_list": options_reconf_final_datos,
                    "message_type": 'interactive_list', # 3 options, could be buttons
                    "fuente": "pyme_reconfirmando_final_datos_v2"
                }

        elif estado_actual == PymeConversationState.ESPERANDO_FEEDBACK:
            self._actualizar_estado(PymeConversationState.IDLE); self.pyme_ctx.pop("nro_pedido_confirmado", None); self._guardar_contexto_pyme()
            options_post_feedback = [
                {"id": "iniciar_pedido_pyme_post_fb", "texto": "Nuevo pedido"},
                {"id": "ver_catalogo_pyme_post_fb", "texto": "Ver catálogo"}
            ]
            if accion_payload == "dejar_feedback_pyme" or texto_usuario_lower in {"si", "sí"}:
                # Ideally, would ask for feedback text here if "Sí"
                return {
                    "message_body": "¡Gracias por tus comentarios! ¿Algo más?",
                    "options_list": options_post_feedback,
                    "message_type": 'interactive_buttons',
                    "fuente": "pyme_agradecimiento_feedback_v2"
                }
            return {
                "message_body": "Entendido. ¿Nuevo pedido o ver catálogo?",
                "options_list": options_post_feedback,
                "message_type": 'interactive_buttons',
                "fuente": "pyme_feedback_omitido_v2"
            }
        
        logger.error(f"[PYME_HANDLER_FALLBACK] Estado no manejado: {estado_actual}, Intención: {intencion_actual}, Payload: '{accion_payload}'")
        self._actualizar_estado(PymeConversationState.IDLE)
        options_fallback_error = [
            {"id": "iniciar_pedido_pyme_error_fallback", "texto": "Iniciar pedido"},
            {"id": "ver_catalogo_pyme_error_fallback", "texto": "Ver catálogo"}
        ]
        return {
            "message_body": "Hubo un inconveniente. ¿Empezamos de nuevo?",
            "options_list": options_fallback_error,
            "message_type": 'interactive_buttons',
            "fuente": "pyme_error_pedido_estado_desconocido_v2"
        }

# --- Resto de Handlers y función responder_pyme ---
# (Se asume que el resto del archivo sigue la estructura anterior, solo PedidoHandler y funciones relacionadas fueron modificadas extensamente)
# ... (FaqHandler, HumanHandler, UnclearHandler, TicketStatusHandler, FallbackHandler, ToolHandlerPyme) ...

# Temporal: Placeholder para coleccion_catalogo_para_rubro si no está definida globalmente
def coleccion_catalogo_para_rubro(rubro_nombre: str) -> str:
    # Lógica para determinar la colección basada en el rubro.
    # Podría ser una config, o una convención.
    # Ejemplo: return f"catalogo_{rubro_nombre.replace(' ', '_')}"
    return CATALOGO_PYME # Usar la constante global por ahora

# def responder_pyme(pregunta, owner_user, rubro_obj, viewer_user=None, anon_id=None, **kwargs):
def responder_pyme(pregunta, owner_user, rubro_obj, viewer_user=None, chat_db_context=None, anon_id=None, channel: str = "web", **kwargs):
    request_id = str(uuid.uuid4())
    # Añadir current_app al logger para acceso a config
    logger_actual = current_app.logger if current_app else logger # Asegurar que logger_actual esté definido
    logger_actual.info(f"[RESPONDER_PYME_START - {request_id}] Pregunta: '{pregunta}', UserPyme: {getattr(owner_user, 'id', 'N/A')}, ViewerCliente: {getattr(viewer_user, 'id', 'N/A')}, Anon: {anon_id}, Channel: {channel}, ChatSessionUUID: {kwargs.get('chat_session_uuid')}")

    # --- Contexto General ---
    pyme_id_para_servicios = getattr(owner_user, "id", None)
    nombre_pyme_display = getattr(owner_user, "nombre_empresa", "la tienda") if owner_user else "la tienda"
    rubro_nombre_actual = getattr(rubro_obj, "nombre", "general").lower() if rubro_obj else \
                          (getattr(owner_user.rubro, "nombre", "general").lower() if owner_user and hasattr(owner_user, "rubro") else "general")
    
    coleccion_qdrant_usar = coleccion_catalogo_para_rubro(rubro_nombre_actual)
    chat_session_uuid_actual = kwargs.get("chat_session_uuid")

    # --- Cargar Historial y Contexto PYME desde chat_db_context.context_data ---
    if chat_db_context.context_data is None:
        chat_db_context.context_data = {}

    historial_actual_sesion = chat_db_context.context_data.get(NOMBRE_HISTORIAL_SESION, [])
    if not historial_actual_sesion and chat_session_uuid_actual: # Solo intentar cargar desde DB si no hay nada en el contexto actual y hay session_id
        try:
            conversaciones_db = Conversacion.query.filter_by(session_id=chat_session_uuid_actual).order_by(Conversacion.timestamp.desc()).limit(MAX_HISTORIAL_CHAT).all()
            if conversaciones_db:
                conversaciones_db.reverse()
                historial_actual_sesion = [{"role": "USER" if i%2==0 else "CHATBOT", "content": conv.pregunta if i%2==0 else conv.respuesta} for i, conv in enumerate(conversaciones_db)]
                logger_actual.info(f"Historial reconstruido desde DB para {chat_session_uuid_actual}: {len(historial_actual_sesion)} mensajes.")
        except Exception as e_hist: logger_actual.error(f"Error cargando historial DB: {e_hist}")

    pyme_ctx_actual = chat_db_context.context_data.get(CONTEXTO_PYME, {})

    context_general = {
        "user_id": pyme_id_para_servicios, # ID de la PYME
        "nombre_pyme": nombre_pyme_display,
        "rubro_nombre": rubro_nombre_actual,
        "mensajes_previos": historial_actual_sesion, # Para el LLM
        CONTEXTO_PYME: pyme_ctx_actual, # Este es el diccionario que se modificará
        "cliente_id": getattr(viewer_user, "id", None), # ID del ChatUser/User final
        "viewer_user_obj": viewer_user, # <--- OBJETO USER COMPLETO DEL CLIENTE/VIEWER
        "anon_id": anon_id,
        "rubro_id": getattr(rubro_obj, "id", None) or (getattr(owner_user.rubro, "id", None) if owner_user and hasattr(owner_user, "rubro") else None),
        "coleccion_qdrant": coleccion_qdrant_usar,
        "chat_session_uuid": chat_session_uuid_actual,
        "datos_interpretados_archivo": kwargs.get("datos_interpretados_archivo"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"),
        "action_payload": kwargs.get("action_payload", pregunta.lower()), # Para botones
        "chat_db_context_data": chat_db_context.context_data # Pasar el dict de context_data para que los handlers lo modifiquen
    }

    # ---- INICIO: Lógica de sugerencia de registro PROACTIVA ----
    if not viewer_user and anon_id and current_app: # Solo para anónimos y si hay contexto de app
        pyme_ctx_para_sugerencia = context_general[CONTEXTO_PYME]

        estado_actual_sugerencia = deserialize_state(pyme_ctx_para_sugerencia.get("estado_conversacion"))
        # No necesitamos chequear historial_actual_sesion[-1] aquí, ya que la lógica de umbral y flag debería ser suficiente.

        # No deberíamos necesitar verificar 'ultima_fuente_bot' si el frontend maneja 'sugerencia_registro'
        # y no vuelve a llamar al backend inmediatamente. Pero como defensa:
        # if context_general[CONTEXTO_PYME].get("ultima_respuesta_tipo") == "sugerencia_registro":
        #    pass # No sugerir de nuevo si la última fue una sugerencia. Necesitaríamos guardar "ultima_respuesta_tipo".

        estados_evitar_sugerencia = [
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE,
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO,
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION,
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_EMAIL,
            PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS,
            PymeConversationState.CONFIRMANDO_PEDIDO, # Podría ser muy pronto si acaba de confirmar un ítem
            PymeConversationState.ESPERANDO_FEEDBACK # Ya terminó el flujo principal
        ]

        if estado_actual_sugerencia not in estados_evitar_sugerencia:
            interacciones_anon_sesion = pyme_ctx_para_sugerencia.get("interacciones_anon_sesion", 0)
            # Solo contar si la pregunta no es un simple "si", "no", "ok" (evitar contar respuestas a preguntas del bot)
            if len(pregunta.split()) > 1 or pregunta.lower() not in ["si", "no", "ok", "dale", "bueno"]:
                 interacciones_anon_sesion += 1
            pyme_ctx_para_sugerencia["interacciones_anon_sesion"] = interacciones_anon_sesion

            # Guardar pyme_ctx actualizado en chat_db_context.context_data ANTES de retornar la sugerencia
            chat_db_context.context_data[CONTEXTO_PYME] = pyme_ctx_para_sugerencia
            # No flask_session.modified = True

            umbral_sugerencia = current_app.config.get("PYME_UMBRAL_SUGERENCIA_REGISTRO", 3)

            if umbral_sugerencia and umbral_sugerencia > 0 and interacciones_anon_sesion >= umbral_sugerencia:
                # Verificar si ya se sugirió en esta "ronda" de interacciones para no ser repetitivo
                if not pyme_ctx_para_sugerencia.get("sugerencia_registro_emitida_ronda", False):
                    logger_actual.info(f"[RESPONDER_PYME - {request_id}] Anon {anon_id} alcanzó umbral de {umbral_sugerencia} interacciones. Sugiriendo registro.")
                    pyme_ctx_para_sugerencia["sugerencia_registro_emitida_ronda"] = True # Marcar como emitida
                    chat_db_context.context_data[CONTEXTO_PYME] = pyme_ctx_para_sugerencia # Guardar el flag
                    # No flask_session.modified = True

                    respuesta_sugerencia = construir_respuesta_sugerir_registro(
                        mensaje_personalizado="Hemos tenido una buena charla.",
                        tipo_entidad="pyme"
                    )
                    # Asegurar que el contexto_pyme devuelto es el actualizado
                    respuesta_sugerencia[CONTEXTO_PYME] = pyme_ctx_para_sugerencia
                    # Guardar la conversación ANTES de devolver la sugerencia
                    if pyme_id_para_servicios or anon_id:
                        try:
                            db.session.add(Conversacion(
                                user_id=context_general.get("cliente_id"), pregunta=pregunta,
                                respuesta=respuesta_sugerencia.get("respuesta", ""), fuente=respuesta_sugerencia.get("fuente", "sugerencia_registro_pyme"),
                                rubro=rubro_nombre_actual, session_id=chat_session_uuid_actual,
                                pyme_id=pyme_id_para_servicios
                            ))
                            db.session.commit()
                        except Exception as e_conv_sug: logger_actual.error(f"Error guardando Conversacion (sugerencia PYME): {e_conv_sug}")
                    return respuesta_sugerencia
            else: # Si no se alcanzó el umbral, resetear el flag de "emitida en ronda"
                 pyme_ctx_para_sugerencia.pop("sugerencia_registro_emitida_ronda", None)
                 chat_db_context.context_data[CONTEXTO_PYME] = pyme_ctx_para_sugerencia
                 # No flask_session.modified = True


            # Criterio 2: Si la pregunta del usuario implica querer guardar algo o ver historial (más adelante)
            # palabras_clave_historial = ["mi historial", "mis tickets", "guardar conversacion", "mis datos"]
            # if any(keyword in pregunta.lower() for keyword in palabras_clave_historial) and not pyme_ctx_para_sugerencia.get("sugerencia_registro_emitida_ronda", False):
            #     logger_actual.info(f"[RESPONDER_PYME - {request_id}] Anon {anon_id} preguntó por historial/guardar. Sugiriendo registro.")
            #     # ... (construir y devolver respuesta, marcar como emitida) ...
    # ---- FIN: Lógica de sugerencia de registro PROACTIVA ----

    # Clasificar intención usando el contexto (pyme_ctx_actual ya está en context_general)
    intencion_clasificada = _clasificar_intencion_pyme_con_llm(pregunta, context_general)
    context_general["intencion"] = intencion_clasificada # Actualizar la intención en el contexto general
    logger_actual.info(f"[PYME_INTENCION - {request_id}] Para '{pregunta}', Intención: {intencion_clasificada}, Estado Pyme Ctx: {context_general[CONTEXTO_PYME].get('estado_conversacion')}")

    # --- Cadena de Handlers ---
    # (SmallTalk y Saludo podrían ir primero si no dependen mucho del estado del PedidoHandler)
    # (ToolHandler también podría ir antes de PedidoHandler si las herramientas son genéricas)

    # Priorizar el PedidoHandler si ya está en un flujo activo de pedido
    estado_pyme_actual = deserialize_state(context_general[CONTEXTO_PYME].get("estado_conversacion"))

    respuesta_final_obj = None

    if estado_pyme_actual != PymeConversationState.IDLE and estado_pyme_actual is not None :
        # Si estamos en un flujo de pedido, dejar que PedidoHandler lo maneje primero
        # También si la intención es relacionada a pedido.
        if intencion_clasificada in ["iniciar_pedido", "agregar_al_carrito", "ver_carrito",
                                   "modificar_cantidad_carrito", "eliminar_del_carrito", "vaciar_carrito",
                                   "finalizar_pedido", "continuar_flujo"] or \
           estado_pyme_actual not in [PymeConversationState.IDLE, None]: # Si ya está en un estado de pedido

            logger.info(f"[PYME_ROUTER - {request_id}] Dirigiendo a PedidoHandler. Estado: {estado_pyme_actual}, Intención: {intencion_clasificada}")
            respuesta_final_obj = PedidoHandler(context_general).handle(pregunta)

    if not respuesta_final_obj: # Si PedidoHandler no manejó o no era su turno
        handler_map = {
            "saludo": SaludoHandler, "ver_catalogo": CatalogoHandler, "consultar_ofertas": OfertasHandler,
            "pregunta_faq": FaqHandler, "hablar_con_agente": HumanHandler,
            "consultar_estado_ticket": TicketStatusHandler, "descargar_catalogo": CatalogoHandler, # Reutilizar para mostrar el botón
            # "pregunta_ambigua": UnclearHandler # Unclear se maneja mejor con Fallback
        }
        handler_class = handler_map.get(intencion_clasificada)
        
        if handler_class:
            logger.info(f"[PYME_ROUTER - {request_id}] Usando handler: {handler_class.__name__}")
            respuesta_final_obj = handler_class(context_general).handle(pregunta)
        
        if not respuesta_final_obj and detectar_small_talk_con_llm(pregunta):
            logger.info(f"[PYME_ROUTER - {request_id}] Usando SmallTalkHandler.")
            respuesta_final_obj = SmallTalkHandler(context_general).handle(pregunta)

        if not respuesta_final_obj: # Si ningún handler específico respondió
            # Considerar ToolHandler antes del Fallback general
            logger.info(f"[PYME_ROUTER - {request_id}] Intentando con ToolHandlerPyme.")
            respuesta_final_obj = ToolHandlerPyme(context_general).handle(pregunta)

            if not respuesta_final_obj: # Si ToolHandler tampoco, usar Fallback
                logger.info(f"[PYME_ROUTER - {request_id}] Usando FallbackHandler.")
                respuesta_final_obj = FallbackHandler(context_general).handle(pregunta)

    if respuesta_final_obj is None: # Absolutamente ningún handler (no debería pasar con Fallback)
        logger.error(f"[PYME_ROUTER - {request_id}] CRITICAL: Ningún handler produjo respuesta para: '{pregunta}'")
        respuesta_final_obj = {"respuesta": "No pude procesar tu solicitud.", "fuente": "error_no_handler_pyme_v2"}

    # --- Guardar Conversación y Actualizar Contexto en DB ---
    historial_actual_sesion.append({"role": "USER", "content": pregunta})
    historial_actual_sesion.append({"role": "CHATBOT", "content": respuesta_final_obj.get("respuesta", "")})
    
    # Guardar historial y contexto pyme en chat_db_context.context_data
    # context_general[CONTEXTO_PYME] (que es pyme_ctx_actual) ya fue modificado por los handlers.
    # context_general["mensajes_previos"] (que es historial_actual_sesion) también.
    chat_db_context.context_data[NOMBRE_HISTORIAL_SESION] = historial_actual_sesion[-MAX_HISTORIAL_CHAT:]
    chat_db_context.context_data[CONTEXTO_PYME] = context_general[CONTEXTO_PYME]
    # La persistencia de chat_db_context.context_data se hace en routes/chat.py

    # Ensure 'estado_conversacion' within pyme_ctx_actual (which is context_general[CONTEXTO_PYME]) is a string
    pyme_context_to_save = context_general[CONTEXTO_PYME]
    if "estado_conversacion" in pyme_context_to_save and isinstance(pyme_context_to_save["estado_conversacion"], Enum):
        pyme_context_to_save["estado_conversacion"] = pyme_context_to_save["estado_conversacion"].name

    chat_db_context.context_data[CONTEXTO_PYME] = pyme_context_to_save


    try:
        if pyme_id_para_servicios or anon_id:
            db.session.add(Conversacion(
                user_id=context_general.get("cliente_id"), # ID del cliente final
                pregunta=pregunta,
                respuesta=respuesta_final_obj.get("message_body", ""), # Use message_body
                fuente=respuesta_final_obj.get("fuente", "desconocida_pyme_v2"), # Update fuente default
                rubro=rubro_nombre_actual,
                session_id=chat_session_uuid_actual,
                pyme_id=pyme_id_para_servicios # Guardar el ID de la pyme con la que se interactuó
            ))
            db.session.commit()
    except Exception as e_conv: logger.error(f"Error guardando Conversacion PYME: {e_conv}")

    # --- Preparar Respuesta Final ---
    # El contexto pyme para la respuesta debe ser serializado.
    # pyme_context_to_save ya tiene estado_conversacion como string.
    contexto_pyme_serializado_para_respuesta = serializar_enum(pyme_context_to_save)

    # Standardize the output from responder_pyme
    final_response_for_logic = {
        "message_body": respuesta_final_obj.get("message_body", "Error procesando su solicitud."),
        "options_list": respuesta_final_obj.get("options_list", []),
        "message_type": respuesta_final_obj.get("message_type", "text"),
        "header_text": respuesta_final_obj.get("header_text"),
        "footer_text": respuesta_final_obj.get("footer_text"),
        "fuente": respuesta_final_obj.get("fuente", "error_pyme_final_v2"),
        "estado_respuesta": respuesta_final_obj.get("estado_respuesta"),
        "ticket_id": respuesta_final_obj.get("ticket_id"),
        "contexto_pyme": contexto_pyme_serializado_para_respuesta,
        "contexto_actualizado": {CONTEXTO_PYME: contexto_pyme_serializado_para_respuesta}, # For consistency with municipios
        "adjuntos": []
    }

    # Añadir info de archivo subido si venía en kwargs (pasado desde logic.py)
    uploaded_file_info_kw = kwargs.get("uploaded_file_info")
    if uploaded_file_info_kw and isinstance(uploaded_file_info_kw, dict):
        if uploaded_file_info_kw.get("url") and uploaded_file_info_kw.get("name"):
            final_response_for_logic["adjuntos"].append({
                "nombre_original": uploaded_file_info_kw["name"],
                "url_descarga": uploaded_file_info_kw["url"],
                "tipo_mime": uploaded_file_info_kw.get("type", 'application/octet-stream')
            })

    # Use 'message_body' for logging and provide a fallback for None
    respuesta_log_pyme = (final_response_for_logic.get('message_body') or '')[:100]
    fuente_log_pyme = final_response_for_logic.get('fuente', 'desconocida_pyme')
    logger.info(f"[RESPONDER_PYME_END - {request_id}] Respuesta: '{respuesta_log_pyme}...', Fuente: {fuente_log_pyme}")
    return final_response_for_logic

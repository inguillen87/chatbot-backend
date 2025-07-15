import logging
import re
import random
import json
import uuid
from typing import Optional
from enum import Enum, auto
from sqlalchemy.orm.attributes import flag_modified
from models import db
try:
    from flask import session as flask_session, current_app, request # Añadir request
except Exception:
    flask_session = {}
    current_app = None
    request = None # Mock request si no hay contexto Flask

from services.cohere_ai import robust_chat, get_cohere_response
import models
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

# PROMPT_CLASIFICAR_INTENCION y _clasificar_intencion_pyme_con_llm eliminados.
# La intención vendrá de la llamada principal a Gemini.

def analizar_sentimiento_llm(texto: str) -> str:
    # Esta función aún usa robust_chat (Cohere). Se revisará en una fase posterior si debe migrar a Gemini
    # o si se mantiene para tareas específicas de análisis de sentimiento si Cohere es preferido para eso.
    # Por ahora, se deja como está, asumiendo que `robust_chat` sigue funcional o será mockeado en tests.
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

            # --- BEGIN: Consume datos_accion from main Gemini call ---
            datos_accion_llm = self.context.get("datos_accion")
            items_pre_extraidos_llm = []
            if datos_accion_llm and isinstance(datos_accion_llm, dict):
                # Assuming datos_accion_llm might have a "productos_pedido" list
                # or individual fields like "nombre_producto_mencionado", "cantidad_producto_mencionado"
                if datos_accion_llm.get("productos_pedido") and isinstance(datos_accion_llm["productos_pedido"], list):
                    items_pre_extraidos_llm = datos_accion_llm["productos_pedido"]
                    logger.info(f"[PedidoHandler] Productos pre-extraídos por LLM principal: {items_pre_extraidos_llm}")
                elif datos_accion_llm.get("nombre_producto_mencionado"):
                    items_pre_extraidos_llm.append({
                        "nombre": datos_accion_llm["nombre_producto_mencionado"],
                        "cantidad": datos_accion_llm.get("cantidad_producto_mencionado", 1)
                        # Unidad podría también venir de datos_accion_llm si el prompt de Gemini lo soporta
                    })
                    logger.info(f"[PedidoHandler] Producto individual pre-extraído por LLM principal: {items_pre_extraidos_llm}")

                # Pre-fill contact details from LLM if available
                if datos_accion_llm.get("nombre_usuario_detectado"): self.pyme_ctx["nombre_cliente"] = datos_accion_llm["nombre_usuario_detectado"]
                if datos_accion_llm.get("telefono_detectado") and validar_telefono(datos_accion_llm["telefono_detectado"]):
                    self.pyme_ctx["telefono_cliente"] = datos_accion_llm["telefono_detectado"] # Formateo se hará después si es necesario
                if datos_accion_llm.get("email_detectado") and validar_email(datos_accion_llm["email_detectado"]):
                    self.pyme_ctx["email_cliente"] = datos_accion_llm["email_detectado"]
                # Dirección podría ser más compleja, la dejamos para el flujo normal por ahora o si viene muy clara del LLM.
                if datos_accion_llm.get("ubicacion"): self.pyme_ctx["direccion_cliente"] = datos_accion_llm.get("ubicacion")
                self._guardar_contexto_pyme() # Guardar datos pre-llenados
            # --- END: Consume datos_accion ---

            if items_pre_extraidos_llm:
                # If LLM provided items, process them immediately (similar to ESPERANDO_PRODUCTO logic)
                # Pass 'items_extraidos' to avoid re-running extraction on the original 'pregunta' for these items.
                logger.info(f"[PedidoHandler] Procesando items pre-extraídos por LLM: {items_pre_extraidos_llm}")
                # Temporarily set pregunta to empty to signal that items are coming from pre-extraction
                return self._procesar_items_y_responder(items_pre_extraidos_llm, "productos_pre_extraidos_llm")

            # If no items from LLM, or if `pregunta` still contains more after LLM (e.g. "quiero pedir X y Z" where LLM got X but Z is still in `pregunta`)
            # The original `pregunta` might still be relevant.
            # If `pregunta` is a generic "iniciar pedido" and LLM found no items, then ask.
            if pregunta.strip() and pregunta not in ["iniciar_pedido", "limpiar_y_nuevo_pedido_saludo"] and intencion_actual == "iniciar_pedido":
                # Process current question as if it's an addition to the order (will hit ESPERANDO_PRODUCTO)
                # This could re-extract if LLM didn't clear pregunta or provide items.
                logger.info(f"[PedidoHandler] LLM no extrajo items, o pregunta '{pregunta}' es para procesamiento adicional. Llamando a _procesar_items_y_responder.")
                return self._procesar_items_y_responder(None, pregunta) # Let _procesar_items_y_responder call extraer_productos_pedido

            msg_ini = "¿Qué productos y cantidades te gustaría pedir? También puedes subir un archivo Excel."
            options_ini = [{"id": "ver_catalogo_pyme_pedido_inicio", "texto": "Ver catálogo"}]
            return {
                "message_body": msg_ini,
                "options_list": options_ini,
                "message_type": 'interactive_buttons',
                "fuente": "pyme_iniciar_pedido_v2_sin_preextraccion"
            }

        elif estado_actual == PymeConversationState.ESPERANDO_PRODUCTO:
            return self._procesar_items_y_responder(None, pregunta)

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

class FaqHandler(BaseHandler):
    def handle(self, pregunta):
        if not self.pyme_id_actual:
            return {"message_body": "No puedo buscar en las preguntas frecuentes sin identificar la tienda.", "fuente": "faq_sin_pyme_id_v2"}

        # Asumimos que el owner_user tiene un 'id' y un 'rubro' con 'nombre'
        owner_user_id = self.context.get("user_id")
        rubro_nombre = self.context.get("rubro_nombre", "general")

        if not owner_user_id:
             return {"message_body": "Error interno: no se pudo determinar el propietario para la búsqueda de FAQ.", "fuente": "faq_error_owner_id_v2"}

        # buscar_en_faq_spacy(pregunta, owner_id, rubro, top_n=1, umbral_similitud=0.7)
        resultados_faq = buscar_en_faq_spacy(pregunta, owner_user_id, rubro_nombre, top_n=1, umbral_similitud=0.7)

        if resultados_faq:
            mejor_match = resultados_faq[0]
            respuesta_faq = mejor_match.get("respuesta", "Encontré una respuesta relevante pero no puedo mostrarla ahora.")
            # Podríamos añadir botones si la respuesta_faq tiene acciones asociadas
            options = [{"id": "hablar_con_agente_pyme_faq", "texto": "Hablar con un agente"}]
            return {
                "message_body": respuesta_faq,
                "options_list": options,
                "message_type": "interactive_buttons",
                "fuente": f"pyme_faq_encontrada_v2 (sim: {mejor_match.get('similitud',0):.2f})"
            }
        else:
            options = [
                {"id": "intentar_otra_pregunta_pyme_faq", "texto": "Probar otra pregunta"},
                {"id": "hablar_con_agente_pyme_faq_no_encontrada", "texto": "Hablar con un agente"}
            ]
            return {
                "message_body": "No encontré una respuesta directa a tu pregunta en nuestra base de conocimiento. ¿Quieres intentar otra pregunta o hablar con un agente?",
                "options_list": options,
                "message_type": "interactive_list",
                "fuente": "pyme_faq_no_encontrada_v2"
            }

class HumanHandler(BaseHandler):
    def handle(self, pregunta):
        # Lógica para transferir a un humano o proveer info de contacto.
        # Por ahora, un placeholder.
        self._actualizar_estado(PymeConversationState.IDLE) # Resetear estado
        nombre_pyme = self.context.get("nombre_pyme", "la empresa")
        # TODO: Intentar obtener datos de contacto reales de la Pyme (owner_user)
        # pyme_user_obj = db.session.get(User, self.pyme_id_actual) if self.pyme_id_actual else None
        # telefono_pyme = getattr(pyme_user_obj, "telefono_contacto", "nuestro teléfono principal")
        # email_pyme = getattr(pyme_user_obj, "email_contacto", "nuestro email de soporte")

        body = f"Entendido. Para hablar con un representante de {nombre_pyme}, por favor contáctanos directamente."
        # Idealmente, aquí se crearía un ticket o se notificaría a alguien.
        # Por ahora, solo damos un mensaje.
        # Crear ticket si servicio_tickets está disponible
        ticket_creado_id = None
        if self.pyme_id_actual and self.cliente_id_actual:
            try:
                asunto = f"Solicitud de contacto desde chat: {pregunta[:50]}"
                descripcion = f"El cliente {self.cliente_id_actual} solicitó hablar con un agente. Última pregunta: '{pregunta}'."
                historial_chat_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in self.context.get("mensajes_previos", [])[-5:]])
                descripcion += f"\n\nÚltimos mensajes:\n{historial_chat_str}"

                ticket_creado_id = servicio_tickets.crear_ticket(
                    pyme_id=self.pyme_id_actual,
                    cliente_id=self.cliente_id_actual, # Puede ser None si es anónimo
                    asunto=asunto,
                    descripcion=descripcion,
                    fuente_ticket="CHATBOT_PYME",
                    # estado_ticket="ABIERTO", # El servicio lo maneja
                    # prioridad="MEDIA" # El servicio lo maneja
                )
                if ticket_creado_id:
                    body = f"He generado el ticket #{ticket_creado_id} para que un agente se ponga en contacto contigo. ¿Hay algo más en lo que pueda ayudarte mientras tanto?"
                    self.pyme_ctx["ultimo_ticket_creado"] = ticket_creado_id
                    self._guardar_contexto_pyme()
            except Exception as e:
                logger.error(f"Error creando ticket en HumanHandler: {e}")
                body += "\n(Hubo un problema al intentar generar un ticket automático)."


        options = [{"id": "ver_catalogo_pyme_post_human", "texto": "Ver catálogo"}]
        return {
            "message_body": body,
            "options_list": options,
            "message_type": "interactive_buttons", # O 'text' si no hay opciones
            "fuente": "pyme_human_handler_placeholder_v2",
            "ticket_id": ticket_creado_id
        }

class UnclearHandler(BaseHandler): # Aunque no está en handler_map, es bueno tenerlo
    def handle(self, pregunta):
        self.pyme_ctx["reintentos_ambigua"] = self.pyme_ctx.get("reintentos_ambigua", 0) + 1
        self._guardar_contexto_pyme()

        if self.pyme_ctx["reintentos_ambigua"] > 2:
            self.pyme_ctx["reintentos_ambigua"] = 0 # Reset
            self._guardar_contexto_pyme()
            return HumanHandler(self.context).handle("El usuario está teniendo dificultades para que lo entienda.")

        sugerencias = sugerencias_por_rubro(self.context.get("rubro_nombre"))
        msg = "No estoy seguro de cómo ayudarte con eso."
        if sugerencias:
            msg += "\nPuedes intentar preguntarme sobre:\n- " + "\n- ".join(sugerencias[:3])
        msg += "\n\nO puedes reformular tu pregunta."

        options = [{"id": "hablar_con_agente_pyme_unclear", "texto": "Hablar con un agente"}]
        if tiene_archivo_catalogo(self.pyme_id_actual):
             options.insert(0, {"id": "ver_catalogo_pyme_unclear", "texto": "Ver Catálogo"})


        return {
            "message_body": msg,
            "options_list": options,
            "message_type": "interactive_list" if len(options)>1 else "interactive_buttons",
            "fuente": "pyme_unclear_handler_v2"
        }

class TicketStatusHandler(BaseHandler):
    def handle(self, pregunta):
        estado_actual = deserialize_state(self.pyme_ctx.get("estado_conversacion"))

        if estado_actual == PymeConversationState.ESPERANDO_NUMERO_TICKET:
            numero_ticket_buscado = re.findall(r'\d+', pregunta)
            if numero_ticket_buscado:
                num_ticket = numero_ticket_buscado[0]
                # TODO: Buscar ticket en sistema de tickets usando servicio_tickets
                # ticket_info = servicio_tickets.consultar_ticket(self.pyme_id_actual, num_ticket, cliente_id=self.cliente_id_actual)
                ticket_info = None # Placeholder
                self._actualizar_estado(PymeConversationState.IDLE)
                if ticket_info:
                    # respuesta = f"El ticket #{num_ticket} está en estado: {ticket_info.get('estado','Desconocido')}. Última actualización: {ticket_info.get('ultima_actualizacion','N/A')}."
                    # if ticket_info.get('comentarios'):
                    #     respuesta += f"\nÚltimo comentario: {ticket_info['comentarios'][-1]['texto']}"
                    return {"message_body": f"Funcionalidad de consulta de ticket ({num_ticket}) aún no implementada.", "fuente": "pyme_ticket_status_found_placeholder_v2"}
                else:
                    return {"message_body": f"No encontré información para el ticket #{num_ticket}. Verifica el número e intenta de nuevo.", "fuente": "pyme_ticket_status_not_found_v2"}
            else:
                self.pyme_ctx["reintentos_numero_ticket"] = self.pyme_ctx.get("reintentos_numero_ticket", 0) + 1
                if self.pyme_ctx["reintentos_numero_ticket"] > 2:
                    self._actualizar_estado(PymeConversationState.IDLE)
                    self.pyme_ctx["reintentos_numero_ticket"] = 0
                    return {"message_body": "No pude identificar el número de ticket. Vuelvo al menú principal.", "fuente": "pyme_ticket_status_too_many_retries_v2"}
                else:
                    self._guardar_contexto_pyme()
                    return {"message_body": "No entendí el número. Por favor, dime solo el número de tu ticket.", "fuente": "pyme_ticket_status_reintentando_numero_v2"}
        else:
            self._actualizar_estado(PymeConversationState.ESPERANDO_NUMERO_TICKET)
            ultimo_ticket = self.pyme_ctx.get("ultimo_ticket_creado")
            msg = "¿Cuál es el número de ticket que quieres consultar?"
            options = []
            if ultimo_ticket:
                msg = f"¿Quieres consultar sobre tu último ticket (#{ultimo_ticket}) o ingresar otro número?"
                options.append({"id": f"consultar_ticket_numero_{ultimo_ticket}", "texto": f"Sí, Ticket #{ultimo_ticket}"})
                options.append({"id": "consultar_otro_ticket_numero", "texto": "Ingresar otro número"})

            return {
                "message_body": msg,
                "options_list": options,
                "message_type": "interactive_buttons" if options else "text",
                "fuente": "pyme_ticket_status_solicitando_numero_v2"
            }

class FallbackHandler(BaseHandler):
    def handle(self, pregunta):
        # Este es el último recurso. Intenta dar una respuesta genérica o escalar.
        logger.warning(f"[PYME_FALLBACK_HANDLER] Pregunta no manejada: '{pregunta}', Intención: {self.context.get('intencion')}, Estado: {self.pyme_ctx.get('estado_conversacion')}")
        return UnclearHandler(self.context).handle(pregunta)

class SmallTalkHandler(BaseHandler):
    def handle(self, pregunta: str):
        respuesta_small_talk = generar_respuesta_small_talk(pregunta, self.context.get("nombre_pyme", "la empresa"))
        if respuesta_small_talk:
            return {"message_body": respuesta_small_talk, "options_list": [], "message_type": "text", "fuente": "pyme_small_talk_handler_v2"}
        return None

class ToolHandlerPyme(BaseHandler):
    def handle(self, pregunta: str):
        match_webinfo = re.search(r"informaci[oó]n web de\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", pregunta, re.IGNORECASE)
        if match_webinfo:
            dominio = match_webinfo.group(1)
            info = obtener_info_web(dominio)
            if info: return {"message_body": f"Información de {dominio}:\n{info}", "fuente": "pyme_tool_webinfo_v2"}
            else: return {"message_body": f"No pude obtener información web para {dominio}.", "fuente": "pyme_tool_webinfo_no_data_v2"}
        return None

def coleccion_catalogo_para_rubro(rubro_nombre: str) -> str:
    return CATALOGO_PYME

from services.gemini_bridge import llamar_gemini # Importar llamar_gemini
from .chat_orchestrator import ChatOrchestrator # Importar el nuevo Orchestrator

def responder_pyme(pregunta_original, owner_user, rubro_obj, viewer_user=None, chat_db_context=None, anon_id=None, channel: str = "web", **kwargs):
    request_id = str(uuid.uuid4())
    logger_actual = current_app.logger if current_app else logger

    # --- 1. Procesamiento de Entrada y Carga de Contexto (simplificado) ---
    received_payload = {}
    pregunta_str = ""
    if isinstance(pregunta_original, dict):
        received_payload = pregunta_original
        pregunta_str = received_payload.get("pregunta", "")
    elif isinstance(pregunta_original, str):
        pregunta_str = pregunta_original
        received_payload["pregunta"] = pregunta_original
    if kwargs: received_payload.update(kwargs)

    if chat_db_context.context_data is None: chat_db_context.context_data = {}
    pyme_ctx_actual = chat_db_context.context_data.get(CONTEXTO_PYME, {})
    historial_chat_para_gemini = chat_db_context.context_data.get("mensajes_previos_gemini_formato", [])

    # --- 2. Construir Información de Usuario para Gemini ---
    nombre_pyme_display = getattr(owner_user, "nombre_empresa", "la tienda") if owner_user else "la tienda"
    usuario_info_for_gemini = {
        "nombre": getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None) or pyme_ctx_actual.get("nombre_cliente") or "Cliente",
        "tipo_entidad": "pyme",
        "pyme_info": {"nombre_pyme": nombre_pyme_display, "rubro": getattr(owner_user.rubro, "nombre", "general") if owner_user and hasattr(owner_user, "rubro") else "general"}
    }
    loc_usuario_texto = getattr(viewer_user, "direccion", None) or pyme_ctx_actual.get("direccion_cliente")
    if loc_usuario_texto: usuario_info_for_gemini["ubicacion_conocida"] = loc_usuario_texto

    # --- 3. Llamada Principal a Gemini ---
    mensaje_para_gemini = pregunta_str # Simplificado, podría añadir info de adjuntos si es relevante aquí
    # (Manejo de adjuntos y su análisis se delega a ActionHandlers si Gemini lo indica)

    llm_response_structured = llamar_gemini(
        mensaje_usuario=mensaje_para_gemini,
        usuario=usuario_info_for_gemini,
        historial=historial_chat_para_gemini
    )

    # Actualizar historial para la próxima llamada a Gemini
    if "mensajes_previos_gemini_formato" not in chat_db_context.context_data:
        chat_db_context.context_data["mensajes_previos_gemini_formato"] = []
    chat_db_context.context_data["mensajes_previos_gemini_formato"].append({"role": "user", "parts": [{"text": mensaje_para_gemini}]})
    # La respuesta del modelo al historial se añade después del ActionHandler

    # --- 4. Preparar Contexto Global para ChatOrchestrator y Action Handlers ---
    global_context_for_orchestrator = {
        CONTEXTO_PYME: pyme_ctx_actual,
        "user_id": getattr(owner_user, "id", None), # ID de la PYME (owner)
        "nombre_pyme": nombre_pyme_display,
        "rubro_nombre": getattr(owner_user.rubro, "nombre", "general").lower() if owner_user and hasattr(owner_user, "rubro") else "general",
        "viewer_user_obj": viewer_user,
        "cliente_id": getattr(viewer_user, "id", None), # ID del cliente final
        "anon_id": anon_id,
        "rubro_id": getattr(rubro_obj, "id", None) or (getattr(owner_user.rubro, "id", None) if owner_user and hasattr(owner_user, "rubro") else None),
        "coleccion_qdrant": coleccion_catalogo_para_rubro(getattr(owner_user.rubro, "nombre", "general").lower() if owner_user and hasattr(owner_user, "rubro") else "general"),
        "chat_session_uuid": kwargs.get("chat_session_uuid"),
        "chat_db_context_data": chat_db_context.context_data, # El dict vivo
        "channel": channel,
        "target_entity_type": "pyme", # Para DerivarHumanoAction
        # Pasar datos del payload que podrían ser útiles para handlers
        "pregunta_actual_usuario": pregunta_str,
        "action_button_payload": received_payload.get("action"),
        "uploaded_file_info": received_payload.get("uploaded_file_info") or received_payload.get("uploaded_file_info_whatsapp"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"), # Si ya se subió un archivo
    }

    # --- 5. Ejecutar Acción vía ChatOrchestrator ---
    orchestrator = ChatOrchestrator(global_context=global_context_for_orchestrator)
    action_handler_result = orchestrator.execute_action(llm_response_structured)

    # --- 6. Procesar Resultado del Action Handler y Formatear Respuesta ---
    respuesta_final_texto = action_handler_result.get("message_to_user")
    if not respuesta_final_texto:
        respuesta_final_texto = llm_response_structured.get("respuesta_usuario", "No estoy seguro de cómo proceder. ¿Podrías intentarlo de nuevo?")

    opciones_finales = llm_response_structured.get("botones", [])
    pedir_info_final = action_handler_result.get("pedir_info") or llm_response_structured.get("pedir_info")

    # Actualizar estado de conversación en pyme_ctx_actual (que es global_context_for_orchestrator[CONTEXTO_PYME])
    if action_handler_result.get("success") and not pedir_info_final:
        if pyme_ctx_actual.get("estado_conversacion") not in [None, PymeConversationState.IDLE.name]:
            logger_actual.info(f"Acción PYME '{llm_response_structured.get('accion_backend')}' exitosa y sin pedir_info. Limpiando estado PYME.")
            pyme_ctx_actual.clear() # Limpia el sub-diccionario
            # (preservar interacciones_anon_sesion si es necesario)
    elif pedir_info_final:
        # Mapear pedir_info_final a un PymeConversationState
        # Esta lógica de mapeo es crucial y debe ser exhaustiva.
        estado_objetivo_str_pyme = None
        if pedir_info_final == "productos_del_pedido": estado_objetivo_str_pyme = PymeConversationState.ESPERANDO_PRODUCTO.name
        elif pedir_info_final == "nombre_cliente_pedido": estado_objetivo_str_pyme = PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE.name
        # ... más mapeos para PYME ...
        if estado_objetivo_str_pyme:
            pyme_ctx_actual["estado_conversacion"] = estado_objetivo_str_pyme
        else:
            logger_actual.warning(f"No se pudo mapear pedir_info_pyme '{pedir_info_final}' a PymeConversationState.")

    # Guardar historial de chat_db_context con respuesta final
    chat_db_context.context_data["mensajes_previos_gemini_formato"].append({"role": "model", "parts": [{"text": respuesta_final_texto}]})
    if len(chat_db_context.context_data["mensajes_previos_gemini_formato"]) > 20:
        chat_db_context.context_data["mensajes_previos_gemini_formato"] = chat_db_context.context_data["mensajes_previos_gemini_formato"][-20:]

    # --- Lógica de sugerencia de registro PROACTIVA (simplificada, similar a municipio) ---
    if not viewer_user and anon_id and current_app and \
       action_handler_result.get("fuente","") != "sugerencia_registro_pyme_v2": # No sugerir si ya es una sugerencia
        
        estado_actual_str_sug_pyme = pyme_ctx_actual.get("estado_conversacion")
        estado_sug_pyme_enum = None
        if estado_actual_str_sug_pyme:
            try: estado_sug_pyme_enum = PymeConversationState[estado_actual_str_sug_pyme]
            except KeyError: pass
        
        estados_evitar_sug_pyme = [ # Definir estados donde no se debe interrumpir con sugerencia
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_NOMBRE, PymeConversationState.ESPERANDO_DATOS_CLIENTE_TELEFONO,
            PymeConversationState.ESPERANDO_DATOS_CLIENTE_DIRECCION, PymeConversationState.ESPERANDO_DATOS_CLIENTE_EMAIL,
            PymeConversationState.ESPERANDO_CONFIRMACION_FINAL_CON_DATOS, PymeConversationState.CONFIRMANDO_PEDIDO
        ]
        if not estado_sug_pyme_enum or estado_sug_pyme_enum not in estados_evitar_sug_pyme:
            interacciones_anon_pyme = pyme_ctx_actual.get("interacciones_anon_sesion", 0)
            if len(pregunta_str.split()) > 1 or pregunta_str.lower() not in ["si", "no", "ok"]:
                interacciones_anon_pyme += 1
            pyme_ctx_actual["interacciones_anon_sesion"] = interacciones_anon_pyme
            umbral_sug_pyme = current_app.config.get("PYME_UMBRAL_SUGERENCIA_REGISTRO", 3)

            if umbral_sug_pyme > 0 and interacciones_anon_pyme >= umbral_sug_pyme and \
               not pyme_ctx_actual.get("sugerencia_registro_emitida_ronda", False):
                logger_actual.info(f"Anon {anon_id} (PYME) alcanzó umbral. Añadiendo sugerencia de registro.")
                pyme_ctx_actual["sugerencia_registro_emitida_ronda"] = True
                sug_obj_pyme = construir_respuesta_sugerir_registro("Para una mejor experiencia de compra.", "pyme", channel)
                respuesta_final_texto += f"\n\n{sug_obj_pyme['respuesta']}"
                opciones_finales.extend(sug_obj_pyme.get('botones',[]))
    
    # Serializar y guardar contexto PYME final
    estado_final_pyme_str = pyme_ctx_actual.get("estado_conversacion")
    if isinstance(estado_final_pyme_str, PymeConversationState):
        pyme_ctx_actual["estado_conversacion"] = estado_final_pyme_str.name
    elif estado_final_pyme_str is None:
        pyme_ctx_actual.pop("estado_conversacion", None)

    contexto_pyme_serializado_para_db = serializar_enum(pyme_ctx_actual)
    chat_db_context.context_data[CONTEXTO_PYME] = contexto_pyme_serializado_para_db
    if chat_db_context:
        flag_modified(chat_db_context, "context_data")

    # Formatear respuesta
    message_type_pyme = "text"
    if opciones_finales:
        num_opt = len(opciones_finales)
        if 0 < num_opt <= 3: message_type_pyme = "interactive_buttons"
        elif num_opt > 3: message_type_pyme = "interactive_list"

    final_response_dict = {
        "message_body": respuesta_final_texto, "options_list": opciones_finales,
        "message_type": message_type_pyme,
        "contexto_actualizado": {CONTEXTO_PYME: contexto_pyme_serializado_para_db},
        "ticket_id": action_handler_result.get("data", {}).get("pedido_id"), # o ticket_id si es un reclamo pyme
        "fuente": action_handler_result.get("fuente") or llm_response_structured.get("accion_backend", "pyme_general_v4"),
        "adjuntos": [] # Manejar adjuntos si es necesario
    }

    # Log de conversación
    if anon_id and not viewer_user:
        try:
            db.session.add(Conversacion(
                session_id=kwargs.get("chat_session_uuid") or anon_id, pregunta=pregunta_str,
                respuesta=final_response_dict["message_body"], fuente=final_response_dict["fuente"],
                rubro=global_context_for_orchestrator["rubro_nombre"], user_id=None, pyme_id=global_context_for_orchestrator["user_id"]
            ))
            db.session.commit()
        except Exception as e_conv_pyme_final:
            logger_actual.error(f"Error guardando Conversacion final (PYME): {e_conv_pyme_final}", exc_info=True)
            db.session.rollback()

    logger.info(f"[RESPONDER_PYME_END_V4 - {request_id}] Respuesta: '{final_response_dict['message_body'][:100]}...', Fuente: {final_response_dict['fuente']}")
    return final_response_dict
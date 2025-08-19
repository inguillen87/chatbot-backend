import logging
import re
import random
import json
import uuid
from typing import Optional
from enum import Enum, auto
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from models import Conversacion, db, PymePedido
try:
    from flask import session as flask_session, current_app, request # Añadir request
except Exception:
    flask_session = {}
    current_app = None
    request = None # Mock request si no hay contexto Flask

import models
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    armar_respuesta_legible,
    CATALOGO_PYME
)
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.utils_placeholders import sugerencias_por_rubro
from services.logic import es_rubro_publico
from services.ticket_service import servicio_tickets
from services.webinfo import obtener_info_web
from .common_utils import construir_respuesta_sugerir_registro # <--- NUEVA IMPORTACIÓN
from services.preferences import add_preference
from services import cart as cart_service
from services.promocion_service import promocion_service
from .llm_utils import extract_multiple_contact_details_llm, resumir_descripcion_producto_llm
from .common_utils import validar_email, validar_telefono

logger = logging.getLogger(__name__)

class PymeConversationState(Enum):
    IDLE = auto()
    ESPERANDO_PRODUCTO = auto()
    ESPERANDO_DATOS_CLIENTE_NOMBRE = auto()
    ESPERANDO_DATOS_CLIENTE_TELEFONO = auto()
    ESPERANDO_DATOS_CLIENTE_DIRECCION = auto()
    ESPERANDO_DATOS_CLIENTE_EMAIL = auto()
    CONFIRMANDO_PEDIDO = auto()
    ESPERANDO_CONFIRMACION_FINAL_CON_DATOS = auto()

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

from services.actions.base_action_handler import BaseActionHandler

class BaseHandler(BaseActionHandler):
    def __init__(self, context):
        super().__init__(context)
        self.pyme_ctx = self.context.get(CONTEXTO_PYME, {})
        self.pyme_id_actual = self.context.get("user_id")
        self.cliente_id_actual = self.context.get("cliente_id")
        self.chat_session_uuid_actual = self.context.get("chat_session_uuid")

        # Get cart data from chat_db_context_data
        chat_db_context_data = self.context.get("chat_db_context_data", {})
        self.pyme_carts_data = chat_db_context_data.get(cart_service.SESSION_CARTS_KEY, {})

    def _guardar_contexto_pyme(self):
        if self.context.get("chat_db_context_data"):
            self.context["chat_db_context_data"][CONTEXTO_PYME] = self.pyme_ctx
            self.context["chat_db_context_data"][cart_service.SESSION_CARTS_KEY] = self.pyme_carts_data
        else:
            logger.error("[BaseHandler._guardar_contexto_pyme] chat_db_context_data no encontrado en self.context.")

    def _actualizar_estado(self, nuevo_estado: str, reintentos: int = 0):
        self.pyme_ctx["estado_conversacion"] = nuevo_estado
        self.pyme_ctx["reintentos"] = reintentos
        self._guardar_contexto_pyme()

    def _obtener_contexto_llm(self) -> dict:
        summary = {
            "estado_actual_flujo": self.pyme_ctx.get("estado_conversacion"),
            "ultima_intencion_registrada": self.context.get("intencion")
        }
        if self.pyme_id_actual:
            cart_summary = cart_service.get_cart_summary(self.pyme_carts_data, self.pyme_id_actual, self.cliente_id_actual)
            if cart_summary and cart_summary.get("items_detalle"):
                summary["items_carrito_nombres"] = [item.get("nombre_producto") for item in cart_summary["items_detalle"][:2]]
                summary["total_carrito_actual"] = cart_summary.get("total_final_con_descuento")

        for k in ["nombre_cliente", "producto_interes_previo", "productos_vistos_o_mencionados"]:
            if self.pyme_ctx.get(k):
                summary[k] = self.pyme_ctx[k]

        return {k: v for k, v in summary.items() if v is not None}

    def execute(self, action_data):
        raise NotImplementedError

class SaludoHandler(BaseHandler):
    def execute(self, action_data):
        nombre = self.context.get("nombre_pyme", "la empresa")
        body = f"¡Hola! Soy tu asistente para {nombre}. ¿En qué puedo ayudarte hoy?"
        options = [
            {"id": "ver_catalogo_pyme", "texto": "Ver catálogo"},
            {"id": "ver_ofertas_pyme", "texto": "Ver ofertas"}
        ]

        if self.pyme_id_actual:
            resumen_carrito_existente = cart_service.get_cart_summary(self.pyme_carts_data, self.pyme_id_actual, self.cliente_id_actual)
            if resumen_carrito_existente and resumen_carrito_existente.get("items_detalle"):
                body += "\n\nVeo que tienes algunos productos en tu carrito. ¿Quieres continuar con ese pedido o empezar uno nuevo?"
                options = [
                    {"id": "ver_carrito_pyme", "texto": "Continuar pedido"},
                    {"id": "limpiar_y_nuevo_pedido_saludo_pyme", "texto": "Nuevo pedido"},
                    {"id": "ver_catalogo_pyme_con_carrito", "texto": "Ver catálogo"}
                ]

        message_type = 'interactive_buttons'

        return {
            "message_body": body,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_saludo_interactivo_v2"
        }

class CatalogoHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "catalogo_sin_pyme_id_v2"}
        query_qdrant = pregunta
        if self.context.get("intencion") == "ver_catalogo" and len(pregunta.split()) < 3: query_qdrant = "productos populares"

        resultados_qdrant = buscar_catalogo_qdrant(self.pyme_id_actual, query_qdrant, self.context.get("rubro_nombre"), 3, self.context.get("coleccion_qdrant", CATALOGO_PYME))
        add_preference("busquedas", pregunta)
        
        respuesta_texto = ""; botones_catalogo = []; fuente_catalogo = "catalogo_qdrant_sin_resultados_v2"

        if resultados_qdrant:
            productos_formateados = []
            productos_formateados.append("| Producto | Precio | Cantidad |")
            productos_formateados.append("|---|---|---|")
            for idx, hit in enumerate(resultados_qdrant):
                payload = getattr(hit, "payload", {}); item_db_id = payload.get("db_id")
                item_obj = db.session.get(models.CatalogoItem, item_db_id) if item_db_id else None
                nombre = payload.get("nombre", "Producto")
                precio_s, precio_f, moneda = parse_precio_flexible(payload.get("precio_str", ""))
                cantidad = payload.get("cantidad", "")
                linea = f"| {nombre} | ${precio_f:,.2f} {moneda or 'ARS'} | {cantidad} |"
                productos_formateados.append(linea)
                identificador_accion = payload.get("sku") or item_db_id or nombre
                botones_catalogo.append({"texto": f"Pedir {nombre[:20]}", "action": f"pedir_item_{identificador_accion}"})

            if productos_formateados:
                respuesta_texto = "Algunos productos que podrían interesarte:\n\n" + "\n".join(productos_formateados)
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
    def execute(self, action_data):
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

class PedidoHandler(BaseActionHandler):
    def execute(self, action_data):
        # This handler will be simplified or removed, as the logic will be
        # handled by the LLM and other more specific action handlers.
        # For now, it returns a simple message.
        return {
            "success": True,
            "message_to_user": "Estoy procesando tu pedido.",
            "fuente": "pyme_pedido_handler_placeholder"
        }

class FaqHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
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
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
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
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
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

class TicketStatusHandler(BaseActionHandler):
    def execute(self, action_data):
        # This handler will be simplified or removed, as the logic will be
        # handled by the LLM and other more specific action handlers.
        # For now, it returns a simple message.
        return {
            "success": True,
            "message_to_user": "Estoy consultando el estado de tu ticket.",
            "fuente": "pyme_ticket_status_handler_placeholder"
        }

class FinalizarPedidoHandler(BaseHandler):
    def execute(self, action_data):
        if not self.pyme_id_actual:
            return {"message_to_user": "No puedo identificar la tienda para finalizar el pedido.", "fuente": "finalizar_pedido_sin_pyme_id"}

        cart_summary = cart_service.get_cart_summary(self.pyme_carts_data, self.pyme_id_actual, self.cliente_id_actual)

        if not cart_summary or not cart_summary.get("items_detalle"):
            return {"message_to_user": "Tu carrito está vacío. Agrega productos antes de finalizar el pedido.", "fuente": "finalizar_pedido_carrito_vacio"}

        detalles_pedido = json.dumps(cart_summary.get("items_detalle"))
        monto_total_pedido = cart_summary.get("total_final_con_descuento")

        # Get client data from context
        nombre_cliente = self.pyme_ctx.get("nombre_cliente")
        email_cliente = self.pyme_ctx.get("email_cliente")
        telefono_cliente = self.pyme_ctx.get("telefono_cliente")
        direccion_cliente = self.pyme_ctx.get("direccion_cliente")
        latitud_cliente = self.pyme_ctx.get("latitud_cliente")
        longitud_cliente = self.pyme_ctx.get("longitud_cliente")

        # Create the order
        nuevo_pedido = PymePedido(
            pyme_id=self.pyme_id_actual,
            asunto=f"Pedido de {nombre_cliente or 'cliente'}",
            detalles=detalles_pedido,
            monto_total=monto_total_pedido,
            nombre_cliente=nombre_cliente,
            email_cliente=email_cliente,
            telefono_cliente=telefono_cliente,
            direccion=direccion_cliente,
            latitud=latitud_cliente,
            longitud=longitud_cliente,
            user_id=self.cliente_id_actual
        )

        try:
            db.session.add(nuevo_pedido)
            db.session.commit()

            # Clear the cart
            cart_service.clear_pyme_cart(self.pyme_carts_data, self.pyme_id_actual)
            self._guardar_contexto_pyme()

            # TODO: Send email notification to logistics

            return {
                "success": True,
                "message_to_user": f"¡Gracias por tu compra! Tu pedido #{nuevo_pedido.nro_pedido} ha sido creado con éxito. Te mantendremos informado sobre el estado.",
                "fuente": "pyme_pedido_finalizado_exitosamente",
                "data": {"pedido_id": nuevo_pedido.id, "nro_pedido": nuevo_pedido.nro_pedido}
            }
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error al crear pedido final desde carrito para pyme {self.pyme_id_actual}: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al procesar tu pedido. Por favor, intenta de nuevo o contacta a un agente.",
                "fuente": "pyme_pedido_finalizado_error"
            }

from services.google_search import google_search

class FallbackHandler(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        logger.warning(f"[PYME_FALLBACK_HANDLER] Pregunta no manejada: '{pregunta}', Intención: {self.context.get('intencion')}, Estado: {self.pyme_ctx.get('estado_conversacion')}")

        search_results = google_search(pregunta)

        if not search_results:
            return UnclearHandler(self.context).execute({"pregunta": pregunta})

        search_items = []
        for result in search_results[:3]:
            search_items.append(f"- [{result.get('title')}]({result.get('link')})\n{result.get('snippet')}")

        return {
            "message_body": "No estoy seguro de cómo ayudarte con eso, pero encontré esto en la web:\n\n" + "\n\n".join(search_items),
            "options_list": [],
            "message_type": "text",
            "fuente": "pyme_fallback_google_search"
        }


class ToolHandlerPyme(BaseHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        match_webinfo = re.search(r"informaci[oó]n web de\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", pregunta, re.IGNORECASE)
        if match_webinfo:
            dominio = match_webinfo.group(1)
            info = obtener_info_web(dominio)
            if info: return {"message_body": f"Información de {dominio}:\n{info}", "fuente": "pyme_tool_webinfo_v2"}
            else: return {"message_body": f"No pude obtener información web para {dominio}.", "fuente": "pyme_tool_webinfo_no_data_v2"}
        return None

class AnalizarImagenHandler(BaseHandler):
    def execute(self, action_data):
        if self.context.get("intencion") != "analizar_imagen":
            return None

        from services.google_vision_service import GoogleVisionService
        import requests

        memoria = self.context[CONTEXTO_PYME]
        foto_url = memoria.get("foto_url")

        if not foto_url:
            return {"respuesta": "No se encontró una imagen para analizar."}

        try:
            response = requests.get(foto_url)
            response.raise_for_status()
            image_content = response.content

            vision_service = GoogleVisionService()
            analysis_result = vision_service.analyze_image(image_content)

            memoria["analisis_imagen"] = analysis_result
            memoria["estado_conversacion"] = PymeConversationState.ESPERANDO_PRODUCTO.name

            return {
                "respuesta": f"He analizado la imagen y detecté lo siguiente: {', '.join(analysis_result['labels'])}. Para continuar, ¿qué producto te gustaría pedir?",
            }
        except Exception as e:
            logger.error(f"Error al analizar la imagen: {e}")
            return {"respuesta": "Hubo un error al analizar la imagen. Por favor, intentá de nuevo."}

class SolicitarUbicacionHandler(BaseHandler):
    def execute(self, action_data):
        if self.context.get("intencion") != "solicitar_ubicacion":
            return None

        return {
            "respuesta": "Para poder ayudarte mejor, necesito tu ubicación. ¿Podrías compartirla?",
            "botones": [
                {"texto": "Compartir ubicación", "action": "compartir_ubicacion"},
                {"texto": "No, gracias", "action": "cancelar"},
            ],
        }

def coleccion_catalogo_para_rubro(rubro_nombre: str) -> str:
    return CATALOGO_PYME

def get_or_create_user_by_phone(phone_number: str, owner_user: models.User) -> Optional[models.User]:
    """
    Busca un usuario por su número de teléfono. Si no existe, crea uno nuevo
    asociado al `owner_user` (la pyme o municipio).
    """
    if not phone_number or not owner_user:
        return None

    # Intentar encontrar el usuario existente por teléfono
    user = models.User.query.filter_by(telefono=phone_number, empresa_id=owner_user.id).first()
    if user:
        return user

    # Si no existe, crear uno nuevo
    logger.info(f"No se encontró un usuario para el teléfono '{phone_number}'. Creando uno nuevo.")

    nuevo_usuario = models.User(
        telefono=phone_number,
        email=f"{phone_number}@whatsapp.chatboc.com", # Email de marcador de posición
        rubro_id=owner_user.rubro_id,
        empresa_id=owner_user.id,
        rol='usuario',
        tipo_chat=owner_user.tipo_chat,
        plan='gratis',
        acepto_terminos=True, # Asumimos aceptación para que el sistema funcione
        fecha_aceptacion_terminos=datetime.utcnow()
    )
    nuevo_usuario.name = f"Usuario de WhatsApp {phone_number[-4:]}"
    nuevo_usuario.set_password(str(uuid.uuid4())) # Contraseña aleatoria y segura

    try:
        db.session.add(nuevo_usuario)
        db.session.commit()
        logger.info(f"Nuevo usuario de WhatsApp creado con ID {nuevo_usuario.id} para el teléfono '{phone_number}'")
        return nuevo_usuario
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error al crear el usuario de WhatsApp para el teléfono '{phone_number}': {e}", exc_info=True)
        return None

from datetime import datetime

def get_or_create_pyme_user_by_token(token: str) -> Optional[models.User]:
    """
    Busca un usuario PYME por su token. Si no existe, crea uno nuevo
    con valores predeterminados y un rubro genérico.
    """
    if not token:
        return None

    # Intentar encontrar el usuario existente
    user = models.User.query.filter_by(token=token).first()
    if user:
        return user

    # Si no existe, crear uno nuevo
    logger.info(f"No se encontró un usuario para el token '{token[:10]}...'. Creando uno nuevo.")

    # Asegurarse de que el rubro "General" exista
    rubro_general = models.Rubro.query.filter(func.lower(models.Rubro.nombre) == "general").first()
    if not rubro_general:
        logger.info("No se encontró el rubro 'General', creándolo...")
        rubro_general = models.Rubro(nombre="General", clave="general", es_publico=False)
        db.session.add(rubro_general)
        db.session.commit()
        logger.info(f"Rubro 'General' creado con ID: {rubro_general.id}")

    # Crear el nuevo usuario (Pyme)
    nuevo_pyme_user = models.User(
        token=token,
        email=f"pyme_{token[:8]}@chatboc.com", # Email de marcador de posición
        nombre_empresa=f"Empresa {token[:8]}",
        rubro_id=rubro_general.id,
        rol='admin', # El dueño de la Pyme es admin de su propia entidad
        tipo_chat='pyme',
        plan='gratis', # O el plan por defecto que corresponda
        acepto_terminos=True, # Asumimos aceptación para que el sistema funcione
        fecha_aceptacion_terminos=datetime.utcnow()
    )
    nuevo_pyme_user.name = f"Empresa {token[:8]}"
    nuevo_pyme_user.set_password(str(uuid.uuid4())) # Contraseña aleatoria y segura

    try:
        db.session.add(nuevo_pyme_user)
        db.session.commit()
        logger.info(f"Nuevo usuario Pyme creado con ID {nuevo_pyme_user.id} para el token '{token[:10]}...'")
        return nuevo_pyme_user
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error al crear el usuario Pyme para el token '{token[:10]}...': {e}", exc_info=True)
        return None

from services.gemini_bridge import llamar_gemini # Importar llamar_gemini
from .chat_orchestrator import ChatOrchestrator # Importar el nuevo Orchestrator

def responder_pyme(pregunta_original, owner_user, rubro_obj, viewer_user=None, chat_db_context=None, anon_id=None, channel: str = "web", **kwargs):
    request_id = str(uuid.uuid4())
    logger_actual = current_app.logger if current_app else logger

    if not owner_user:
        logger.error("[responder_pyme] Critical error: owner_user is None. Cannot proceed.")
        return {
            "message_body": "Error de configuración: No se pudo identificar la empresa. Por favor, contacte al administrador.",
            "options_list": [],
            "message_type": "text",
            "fuente": "error_no_owner_user"
        }

    if chat_db_context is None:
        chat_db_context = models.ChatSessionContext()
        chat_db_context.context_data = {}

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

    if chat_db_context.context_data is None:
        chat_db_context.context_data = {}

    if "mensajes_previos_gemini_formato" not in chat_db_context.context_data:
        chat_db_context.context_data["mensajes_previos_gemini_formato"] = []

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
        historial=historial_chat_para_gemini,
        chat_session_id=kwargs.get("chat_session_uuid")
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
        "empresa_token": getattr(owner_user, "token", None),
        # Pasar datos del payload que podrían ser útiles para handlers
        "pregunta_actual_usuario": pregunta_str,
        "action_button_payload": received_payload.get("action"),
        "uploaded_file_info": received_payload.get("uploaded_file_info") or received_payload.get("uploaded_file_info_whatsapp"),
        "archivo_id_para_asociar": kwargs.get("archivo_id_para_asociar"), # Si ya se subió un archivo
    }

    # --- 5. Ejecutar Acción vía ChatOrchestrator ---
    if llm_response_structured.get("accion_backend") == "saludar":
        handler = SaludoHandler(global_context_for_orchestrator)
        action_handler_result = handler.execute(pregunta_str)
    else:
        orchestrator = ChatOrchestrator(global_context=global_context_for_orchestrator)
        action_handler_result = orchestrator.execute_action(llm_response_structured)

    # --- 6. Procesar Resultado del Action Handler y Formatear Respuesta ---
    respuesta_final_texto = action_handler_result.get("message_to_user")
    if not respuesta_final_texto:
        respuesta_final_texto = llm_response_structured.get("message_body", "No estoy seguro de cómo proceder. ¿Podrías intentarlo de nuevo?")

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

    # Proactive suggestions
    sugerencia_proactiva = sugerir_productos_relacionados(historial_chat_para_gemini, owner_user.id)
    if sugerencia_proactiva:
        final_response_dict["message_body"] += f"\n\n{sugerencia_proactiva}"

    logger.info(f"[RESPONDER_PYME_END_V4 - {request_id}] Respuesta: '{final_response_dict['message_body'][:100]}...', Fuente: {final_response_dict['fuente']}")
    return final_response_dict

def sugerir_productos_relacionados(historial_chat: list, pyme_id: int) -> Optional[str]:
    """
    Analiza el historial de chat para sugerir productos relacionados o promociones.
    """
    if not historial_chat:
        return None

    last_user_message = ""
    for msg in reversed(historial_chat):
        if msg.get("role") == "user":
            last_user_message = msg.get("parts", [{}])[0].get("text", "")
            break

    if not last_user_message:
        return None

    # Simple keyword-based suggestion for now
    if "vino" in last_user_message.lower():
        return "Veo que te interesa el vino. ¿Te gustaría probar nuestra selección de quesos para acompañar?"
    elif "queso" in last_user_message.lower():
        return "El queso es una excelente elección. ¿Qué tal un vino Malbec para maridar?"

    return None
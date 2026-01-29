import logging
from typing import Dict, Any, List
from .pyme_base_handler import BasePymeHandler
from services.ticket_service import servicio_tickets
from services.pymes import PymeConversationState, tiene_archivo_catalogo, url_descargar_catalogo_pyme
from services.utils_placeholders import sugerencias_por_rubro
from services.google_search import google_search
from services.promocion_service import promocion_service
from services.qdrant_search import buscar_catalogo_qdrant, CATALOGO_PYME
from services.preferences import add_preference
from services.config_loader import cargar_configuracion_pyme
from services.live_chat_schedule import build_live_chat_status
from models import db
import models
from services.common_utils import parse_precio_flexible
from socket_service import emit_new_ticket
from routes.ticket import serialize_ticket_to_json
from services.pyme_menu import get_pyme_menu_payload
from services.pyme_multimodal import load_catalog, match_catalog_items

logger = logging.getLogger(__name__)

class SaludoHandler(BasePymeHandler):
    def execute(self, action_data):
        channel = self.context.get("channel", "web")
        menu_payload = get_pyme_menu_payload(self.context, channel=channel)
        menu_payload.setdefault("fuente", "pyme_saludo_menu_principal_v4")
        menu_payload.setdefault("success", True)
        return menu_payload

class CatalogoHandler(BasePymeHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "catalogo_sin_pyme_id_v2"}

        # 1. Try Deterministic/Fuzzy Search first (better for specific varietals like "Malbec")
        rubro_slug = self.context.get("rubro_slug") or self.pyme_ctx.get("rubro_slug") or "default"
        catalog_items_memory = load_catalog(self.pyme_id_actual, rubro_slug)

        # Filter query to remove common stopwords if needed, but match_catalog_items does token matching
        deterministic_matches = match_catalog_items(pregunta, catalog_items_memory, max_results=5)

        resultados_qdrant: List[Any] = []
        fuente_catalogo = "catalogo_qdrant_sin_resultados_v2"
        respuesta_texto = ""
        botones_catalogo = []

        if deterministic_matches:
             # Use deterministic matches
             fuente_catalogo = "catalogo_deterministic_match"
             productos_formateados = []
             for item in deterministic_matches:
                 nombre = item.get("nombre", "Producto")
                 precio = item.get("precio", 0.0)
                 moneda = item.get("moneda", "ARS")
                 presentacion = item.get("presentacion", "")

                 from services.pyme_multimodal import _format_money # Import locally to avoid circular if at top
                 precio_txt = f"${_format_money(precio, moneda)}"
                 linea = f"• *{nombre}* ({presentacion}) — {precio_txt}"
                 productos_formateados.append(linea)

                 identificador = item.get("sku") or item.get("nombre")
                 botones_catalogo.append({"texto": f"Pedir {nombre[:15]}", "action": f"pedir_item_{identificador}"})

             if productos_formateados:
                respuesta_texto = "Encontré estos productos para tu búsqueda:\n\n" + "\n".join(productos_formateados)
                respuesta_texto += "\n\n¿Te gustaría encargar alguno? Respondé con el nombre o usá los botones."

        # 2. If no deterministic matches, try Qdrant
        if not respuesta_texto:
            query_qdrant = pregunta
            if self.context.get("intencion") == "ver_catalogo" and len(pregunta.split()) < 3: query_qdrant = "productos populares"

            try:
                resultados_qdrant = buscar_catalogo_qdrant(
                    self.pyme_id_actual,
                    query_qdrant,
                    self.context.get("rubro_nombre"),
                    3,
                    self.context.get("coleccion_qdrant", CATALOGO_PYME),
                )
            except Exception as exc:
                logger.warning(
                    "[PYME][CatalogoHandler] Error consultando Qdrant: %s",
                    exc,
                    exc_info=True,
                )
                resultados_qdrant = []

            if resultados_qdrant:
                productos_formateados = []
                for idx, hit in enumerate(resultados_qdrant):
                    payload = getattr(hit, "payload", {}); item_db_id = payload.get("db_id")
                    nombre = payload.get("nombre", "Producto")
                    precio_s, precio_f, moneda = parse_precio_flexible(payload.get("precio_str", ""))
                    cantidad = payload.get("cantidad", "")

                    precio_txt = f"${precio_f:,.0f} {moneda or 'ARS'}" if precio_f else "Consultar precio"
                    linea = f"• *{nombre}* ({precio_txt})"
                    if cantidad:
                        linea += f" - Stock: {cantidad}"

                    productos_formateados.append(linea)
                    identificador_accion = payload.get("sku") or item_db_id or nombre
                    botones_catalogo.append({"texto": f"Pedir {nombre[:15]}", "action": f"pedir_item_{identificador_accion}"})

                if productos_formateados:
                    respuesta_texto = "¡Claro! Aquí tienes algunos productos relacionados que encontré:\n\n" + "\n".join(productos_formateados)
                    respuesta_texto += "\n\n¿Te gustaría encargar alguno? Puedes usar los botones o decírmelo."
                    fuente_catalogo = "catalogo_qdrant_con_promos_v2"

        chat_ctx = self.context.setdefault("chat_db_context_data", {})
        add_preference(chat_ctx, "busquedas", pregunta)

        if not respuesta_texto:
            fallback_items: List[Dict[str, Any]] = []
            static_bundle = self.pyme_ctx.get("static_data_cache") or {}
            raw_catalog = static_bundle.get("catalogo_destacado")
            if not isinstance(raw_catalog, list):
                raw_catalog = None
            if not raw_catalog:
                rubro_slug = (
                    self.context.get("rubro_nombre")
                    or self.pyme_ctx.get("rubro_slug")
                    or "default"
                )
                raw_catalog = cargar_configuracion_pyme(rubro_slug, "catalogo_destacado.json")
                if not raw_catalog:
                    raw_catalog = cargar_configuracion_pyme("default", "catalogo_destacado.json")
            if isinstance(raw_catalog, list):
                for raw in raw_catalog:
                    if isinstance(raw, dict):
                        fallback_items.append(raw)
                        if len(fallback_items) >= 5:
                            break

            if fallback_items:
                lines: List[str] = [
                    "Estos son algunos destacados de nuestra selección:"
                ]
                for idx, item in enumerate(fallback_items, 1):
                    nombre = item.get("nombre") or item.get("sku") or "Producto"
                    presentacion = item.get("presentacion") or "Presentación estándar"
                    precio = item.get("precio")
                    moneda = item.get("moneda") or "ARS"
                    descripcion = item.get("descripcion") or ""
                    precio_txt = ""
                    try:
                        if precio is not None:
                            precio_txt = f" — ${float(precio):,.0f} {moneda}"
                    except (TypeError, ValueError):
                        precio_txt = ""
                    line = f"*{idx}. {nombre}* ({presentacion}){precio_txt}"
                    if descripcion:
                        line += f"\n   _{descripcion}_"
                    lines.append(line)
                respuesta_texto = "\n\n".join(lines)
                fuente_catalogo = "pyme_catalogo_destacado_static_v1"
            else:
                respuesta_texto = (
                    "No encontré productos específicos para esa búsqueda por el momento."
                    " ¿Te gustaría que te derive con un asesor para una atención personalizada?"
                )
                botones_catalogo = []

        body = respuesta_texto
        options: List[Dict[str, Any]] = []

        for btn_cat_original in botones_catalogo:
            action_str = btn_cat_original.get("action", "")
            id_suffix = action_str.replace("pedir_item_", "") if action_str.startswith("pedir_item_") else btn_cat_original.get("texto", "")

            options.append({
                "id": f"pedir_item_pyme_{id_suffix}",
                "texto": btn_cat_original.get("texto", "Pedir producto")[:20]
            })
            if len(options) >= 5 and self.context.get("channel") == 'whatsapp':
                break

        if not botones_catalogo and respuesta_texto:
            options.append({"id": "pyme_hacer_pedido", "texto": "Cargar pedido"})

        options.append({"id": "ver_catalogo_pyme_buscar_otra", "texto": "Buscar otra cosa"})

        if tiene_archivo_catalogo(self.pyme_id_actual):
            url_cat = url_descargar_catalogo_pyme(self.pyme_id_actual)
            if self.context.get("channel") == "whatsapp":
                body += f"\n\n📂 También puedes descargar nuestro catálogo completo aquí: {url_cat}"
            else:
                options.append({
                    "id": "descargar_catalogo_pyme_pdf",
                    "texto": "Descargar Catálogo PDF",
                    "url": url_cat,
                    "type": "url"
                })

        options.append({"id": "pyme_promociones", "texto": "Ver promociones"})
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

class OfertasHandler(BasePymeHandler):
    def execute(self, action_data):
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "ofertas_sin_pyme_id_v2"}
        promos = promocion_service.get_promociones_for_pyme(self.pyme_id_actual, activas_unicamente=True)

        options = [
            {"id": "ver_catalogo_pyme_ofertas", "texto": "Ver catálogo"},
            {"id": "pyme_hacer_pedido", "texto": "Cargar pedido"},
        ]
        message_type = 'interactive_buttons'

        if promos:
            body = "🎉 **¡Aprovecha nuestras promociones actuales!**\n" + "\n".join([
                f"\n✨ **{p.nombre_promocion}**: {p.descripcion_publica}" for p in promos[:3]
            ])
            if len(promos) > 3:
                body += f"\n... ¡y {len(promos) - 3} más!"
            body += "\n\n¿Te gustaría aprovechar alguna de estas ofertas o prefieres ver el catálogo general?"
            return {
                "message_body": body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "pyme_ofertas_con_promos_v2"
            }

        static_bundle = self.pyme_ctx.get("static_data_cache") or {}
        raw_promos = static_bundle.get("promociones")
        if not isinstance(raw_promos, list):
            raw_promos = cargar_configuracion_pyme(
                self.context.get("rubro_nombre") or self.pyme_ctx.get("rubro_slug") or "default",
                "promociones.json",
            ) or []
        formatted_promos: List[str] = []
        for promo in raw_promos[:3]:
            if not isinstance(promo, dict):
                continue
            nombre = promo.get("nombre") or "Promoción"
            canal = promo.get("canal")
            validez = promo.get("validez")
            descripcion = promo.get("descripcion") or promo.get("descripcion_publica") or ""
            header = f"✨ **{nombre}**"
            if canal:
                header += f" · {canal}"
            if validez:
                header += f" (vigente hasta {validez})"
            block = header
            if descripcion:
                block += f"\n   _{descripcion}_"
            formatted_promos.append(block)

        if formatted_promos:
            body = "🚀 **Promociones destacadas:**\n\n" + "\n\n".join(formatted_promos)
            body += "\n\n¿Alguna te llama la atención? También puedes hacer un pedido directo."
            return {
                "message_body": body,
                "options_list": options,
                "message_type": message_type,
                "fuente": "pyme_promos_estaticas_v1",
            }

        body_no_ofertas = "Por el momento no tenemos ofertas especiales activas, pero te invito a explorar nuestro catálogo completo donde encontrarás excelentes productos."
        return {
            "message_body": body_no_ofertas,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_ofertas_sin_promos_v2"
        }

class HumanHandler(BasePymeHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        self.pyme_ctx["estado_conversacion"] = PymeConversationState.IDLE.name
        self._guardar_contexto_pyme()
        nombre_pyme = self.context.get("nombre_pyme", "la empresa")

        body = f"Entendido. Para hablar con un representante de {nombre_pyme}, por favor contáctanos directamente."
        ticket_creado_id = None
        if self.pyme_id_actual and self.cliente_id_actual:
            try:
                asunto = f"Solicitud de contacto desde chat: {pregunta[:50]}"
                descripcion = f"El cliente {self.cliente_id_actual} solicitó hablar con un agente. Última pregunta: '{pregunta}'."
                historial_chat_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in self.context.get("mensajes_previos", [])[-5:]])
                descripcion += f"\n\nÚltimos mensajes:\n{historial_chat_str}"

                ticket_creado_id = servicio_tickets.crear_ticket(
                    pyme_id=self.pyme_id_actual,
                    cliente_id=self.cliente_id_actual,
                    asunto=asunto,
                    descripcion=descripcion,
                    fuente_ticket="CHATBOT_PYME",
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
            "message_type": "interactive_buttons",
            "fuente": "pyme_human_handler_placeholder_v2",
            "ticket_id": ticket_creado_id
        }

class UnclearHandler(BasePymeHandler):
    def execute(self, action_data):
        pregunta = action_data.get("pregunta", "")
        self.pyme_ctx["reintentos_ambigua"] = self.pyme_ctx.get("reintentos_ambigua", 0) + 1
        self._guardar_contexto_pyme()

        if self.pyme_ctx["reintentos_ambigua"] > 2:
            self.pyme_ctx["reintentos_ambigua"] = 0
            self._guardar_contexto_pyme()
            return HumanHandler(self.context).execute({"pregunta": "El usuario está teniendo dificultades para que lo entienda."})

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

class FallbackHandler(BasePymeHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
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

class DerivarHumanoActionHandlerPyme(BasePymeHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Crea un ticket real de chat en vivo y devuelve su identificador."""
        logger.info(f"Executing DerivarHumanoActionHandlerPyme with data: {action_data}")

        try:
            viewer_user = self.context.get("viewer_user_obj")
            owner_user = self.context.get("user_obj")
            pregunta_original = self.context.get("pregunta_actual_usuario", "")

            nombre = (getattr(viewer_user, "name", None) or action_data.get("nombre"))
            telefono = (getattr(viewer_user, "telefono", None) or action_data.get("telefono"))
            email = (getattr(viewer_user, "email", None) or action_data.get("email"))

            ticket_data = {
                "asunto": f"Solicitud de Chat en Vivo por: {nombre or 'Usuario'}",
                "categoria": "Atención en Vivo",
                "pregunta": pregunta_original,
                "detalles": action_data.get("motivo_derivacion", "Solicitud de agente"),
                "user_id": self.context.get("cliente_id"),
                "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                "estado": "esperando_agente_en_vivo",
                "nombre_cliente": nombre,
                "telefono_cliente": telefono,
                "email_cliente": email,
                "pyme_id": getattr(owner_user, "id", None)
            }

            ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}

            sala = servicio_tickets.crear_nuevo_ticket(tipo_ticket="pyme", ticket_data=ticket_data_cleaned)
            if not sala:
                raise Exception("crear_nuevo_ticket devolvió None")

            try:
                ticket_json = serialize_ticket_to_json(sala, "pyme")
                emit_new_ticket(ticket_json)
            except Exception as e_notify:
                logger.error(f"Error enviando notificación en tiempo real para ticket #{sala.nro_ticket}: {e_notify}", exc_info=True)

            servicio_tickets.crear_comentario(
                ticket_id=sala.id,
                tipo_ticket="pyme",
                comentario_data={
                    "comentario": pregunta_original,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                    "es_admin": False,
                },
            )

            chat_id = f"P-{sala.nro_ticket}"

            user_message = (
                "En breve un representante se pondrá en contacto contigo. "
                f"Tu número de chat es {chat_id}."
            )
            live_chat_status = build_live_chat_status()
            if not live_chat_status.get("available"):
                schedule_text = live_chat_status.get("description")
                if schedule_text:
                    user_message = (
                        f"{user_message}\n\n"
                        f"⏰ Nuestro horario de atención en vivo es {schedule_text}."
                    )
                else:
                    user_message = f"{user_message}\n\n⏰ Ahora mismo no hay agentes disponibles."
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {
                    "ticket_id": sala.id,
                    "chat_id": chat_id,
                    "status": "esperando_agente_en_vivo",
                    "live_chat": live_chat_status,
                },
            }
        except Exception as e:
            logger.error(f"Error en DerivarHumanoActionHandlerPyme: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Ocurrió un problema al crear el chat en vivo. ¿Podés intentar de nuevo más tarde?",
                "error_details": str(e),
            }

class OtrasConsultasHandler(BasePymeHandler):
    def execute(self, action_data):
        body = "¿Con cuál de estas opciones puedo ayudarte?"
        options = [
            {"id": "pyme_factura", "texto": "Factura"},
            {"id": "pyme_trabajar", "texto": "Trabajar en la empresa"}
        ]
        message_type = 'interactive_list'
        return {
            "message_body": body,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_otras_consultas_submenu_v1"
        }

class FacturaHandler(BasePymeHandler):
    def execute(self, action_data):
        body = "Por el momento no emitimos Factura A"
        options = [
            {"id": "pyme_no_recibi_factura", "texto": "No recibi mi factura"},
            {"id": "pyme_error_detalle_boleta", "texto": "Error en el detalle de la boleta"}
        ]
        message_type = 'interactive_list'
        return {
            "message_body": body,
            "options_list": options,
            "message_type": message_type,
            "fuente": "pyme_factura_submenu_v1"
        }

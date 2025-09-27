import logging
from typing import Dict, Any
from .pyme_base_handler import BasePymeHandler
from services.ticket_service import servicio_tickets
from services.pymes import PymeConversationState, tiene_archivo_catalogo, url_descargar_catalogo_pyme
from services.utils_placeholders import sugerencias_por_rubro
from services.google_search import google_search
from services.promocion_service import promocion_service
from services.qdrant_search import buscar_catalogo_qdrant, CATALOGO_PYME
from services.preferences import add_preference
from models import db
import models
from services.common_utils import parse_precio_flexible
from socket_service import emit_ticket_update
from routes.ticket import serialize_ticket_to_json
from services.pyme_menu import get_pyme_menu_payload

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
        query_qdrant = pregunta
        if self.context.get("intencion") == "ver_catalogo" and len(pregunta.split()) < 3: query_qdrant = "productos populares"

        resultados_qdrant = buscar_catalogo_qdrant(self.pyme_id_actual, query_qdrant, self.context.get("rubro_nombre"), 3, self.context.get("coleccion_qdrant", CATALOGO_PYME))
        add_preference(self.pyme_id_actual, "busquedas", pregunta)

        if not resultados_qdrant:
            logger.warning("Qdrant search failed or returned no results. Falling back to static catalog.")
            pyme_config = (self.context.get("pyme_config_full") or {}).get("config", {})
            static_catalog = pyme_config.get("catalogo_destacado", [])

            resultados_qdrant = []
            for item in static_catalog:
                resultados_qdrant.append(type('obj', (object,), {'payload': item})())

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
            botones_catalogo = []

        body = respuesta_texto
        options = []

        for btn_cat_original in botones_catalogo:
            action_str = btn_cat_original.get("action", "")
            id_suffix = action_str.replace("pedir_item_", "") if action_str.startswith("pedir_item_") else btn_cat_original.get("texto", "")

            options.append({
                "id": f"pedir_item_pyme_{id_suffix}",
                "texto": btn_cat_original.get("texto", "Pedir producto")[:20]
            })
            if len(options) >= 7 and self.context.get("channel") == 'whatsapp':
                break

        options.append({"id": "ver_catalogo_pyme_buscar_otra", "texto": "Buscar otra cosa"})

        if tiene_archivo_catalogo(self.pyme_id_actual):
            url_cat = url_descargar_catalogo_pyme(self.pyme_id_actual)
            if self.context.get("channel") == "whatsapp":
                body += f"\n\nTambién puedes descargar nuestro catálogo completo en: {url_cat}"
            else:
                options.append({
                    "id": "descargar_catalogo_pyme_pdf",
                    "texto": "Descargar Catálogo PDF",
                    "url": url_cat,
                    "type": "url"
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

class OfertasHandler(BasePymeHandler):
    def execute(self, action_data):
        if not self.pyme_id_actual: return {"respuesta": "No puedo identificar la tienda.", "fuente": "ofertas_sin_pyme_id_v2"}
        promos = promocion_service.get_promociones_for_pyme(self.pyme_id_actual, activas_unicamente=True)

        options = [{"id": "pyme_productos_stock", "texto": "Ver catálogo"}]
        message_type = 'interactive_buttons'

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
                emit_ticket_update(ticket_json)
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

            user_message = f"En breve un representante se pondrá en contacto contigo. Tu número de chat es {chat_id}."
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"ticket_id": sala.id, "chat_id": chat_id, "status": "esperando_agente_en_vivo"},
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

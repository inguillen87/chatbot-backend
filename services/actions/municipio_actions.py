# services/actions/municipio_actions.py
import logging
import os
import re
import sys
from urllib.parse import urlparse
from typing import Dict, Any, Optional
import random

from services.ticket_service import servicio_tickets
from services.notifications import enviar_notificacion_whatsapp_con_plantilla, enviar_notificacion_sms
from services.herramientas_municipio import (
    parse_direccion_completa as parse_direccion,
    direccion_es_valida,
    normalizar_texto,
    obtener_direccion_de_coordenadas,
    consultar_ocupacion, # Imported here
)
from services.ticket_utils import formatear_ticket_respuesta, remove_buttons_with_urls_in_message
from services.common_utils import validar_telefono, formatear_telefono_e164, validar_email, _get_main_menu_payload
from services.config_loader import cargar_configuracion_municipio
from models import MunicipioTicket, TenantProfile
from services import promo_service

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

# Define BaseActionHandler here to ensure it exists
class BaseActionHandler:
    action_name = None

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        raise NotImplementedError

def _normalize_url_for_comparison(raw_url: str) -> tuple[str, str]:
    """Return normalized (domain, path) for URL comparison."""

    if not raw_url:
        return "", ""

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return "", ""

    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    path = (parsed.path or "").strip()
    if path:
        if not path.startswith("/"):
            path = f"/{path}"
        path = path.rstrip("/")

    return domain, path

def _address_seems_generic(address: str | None) -> bool:
    if not address:
        return True

    normalized = normalizar_texto(address)
    if not normalized:
        return True

    if any(char.isdigit() for char in normalized):
        return False

    generic_tokens = {
        "argentina",
        "provincia",
        "provincia de mendoza",
        "mendoza",
        "ciudad",
        "municipio",
    }

    tokens = set(normalized.split())
    if len(tokens) <= 2 and tokens.issubset(generic_tokens):
        return True

    return False


def _resolve_municipio_tenant_ids(owner_user, context: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """Resolve tenant_id and municipio_id for municipal tickets."""

    municipio_config = (context or {}).get("municipio_config_actual", {}) or {}
    tenant_slug = (
        municipio_config.get("tenant_slug")
        or municipio_config.get("slug")
        or getattr(owner_user, "tenant_slug", None)
    )
    owner_id = (
        getattr(owner_user, "municipio_id", None)
        or getattr(owner_user, "id", None)
    )

    tenant = None
    if tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=str(tenant_slug).strip()).first()
    if not tenant and owner_id:
        tenant = TenantProfile.query.filter_by(municipio_id=owner_id).first()

    tenant_id = getattr(tenant, "id", None)
    municipio_id = getattr(tenant, "municipio_id", None) or owner_id

    if not municipio_id:
        logger.warning(
            "[tickets] municipio_id missing while resolving tenant. tenant_slug=%s owner_id=%s",
            tenant_slug,
            owner_id,
        )

    return tenant_id, municipio_id


def _render_closing_caption_template(template: str | None, values: Dict[str, Any]) -> str:
    """Render a caption template using {{placeholder}} or {placeholder} tokens."""

    message_body = str(values.get("message_body") or "").strip()
    if not template:
        return message_body

    rendered = str(template)
    for key, value in values.items():
        token_value = str(value) if value is not None else ""
        rendered = rendered.replace(f"{{{{{key}}}}}", token_value)
        rendered = rendered.replace(f"{{{key}}}", token_value)

    return rendered


def _apply_whatsapp_closing_promo(
    payload: Dict[str, Any],
    *,
    context: Dict[str, Any],
    caption_values: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach WhatsApp hero media for closing flows when enabled."""

    channel = (context.get("channel") or "").strip().lower()
    if not channel.startswith("whatsapp"):
        return payload

    municipio_config = context.get("municipio_config_actual", {}) or {}
    if not municipio_config.get("closing_promo_enabled"):
        return payload

    image_url = (
        municipio_config.get("closing_promo_image_url")
        or municipio_config.get("promo_image_url")
        or payload.get("image_url")
    )
    if not image_url:
        return payload

    caption_template = municipio_config.get("closing_promo_caption_template")
    caption = _render_closing_caption_template(caption_template, caption_values)

    pre_messages = payload.get("_twilio_pre_messages")
    if not isinstance(pre_messages, list):
        pre_messages = [] if pre_messages is None else [pre_messages]

    pre_messages.append(
        {
            "channels": ["whatsapp"],
            "body": caption,
            "media_urls": [image_url],
        }
    )
    payload["_twilio_pre_messages"] = pre_messages

    if payload.get("options_list"):
        payload["message_body"] = municipio_config.get(
            "closing_promo_followup_text",
            "Seleccioná una opción para continuar.",
        )

    payload.pop("image_url", None)
    return payload


class BuscarEstacionamientoActionHandler(BaseActionHandler):
    action_name = "buscar_estacionamiento"

    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        logger.info(f"Executing BuscarEstacionamientoActionHandler")

        # La ubicación puede venir de la acción del LLM o del contexto si se pidió antes
        ubicacion = context.get("ubicacion_usuario")

        if not ubicacion:
            # Si no hay ubicación, la pedimos.
            context["estado_conversacion"] = "ESPERANDO_UBICACION_GENERAL"
            context["accion_pendiente_tras_ubicacion"] = "buscar_estacionamiento"

            return {
                "success": False,
                "message_body": "Para encontrar estacionamiento, por favor compartí tu ubicación o escribí una dirección (ej: San Martín 1200).",
                "pedir_info": "ubicacion"
            }

        resultado = consultar_ocupacion(ubicacion) # resultado es un dict {"texto": "..."}

        # Limpiar el estado de espera si existía
        if context.get("accion_pendiente_tras_ubicacion") == "buscar_estacionamiento":
            context.pop("accion_pendiente_tras_ubicacion")
            if "estado_conversacion" in context:
                 context.pop("estado_conversacion")


        return {
            "success": True,
            "message_body": resultado["texto"],
            "data": resultado
        }

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoActionHandler")

        # Normally 'action_data' comes from the caller, but here we access context directly
        # or assume responder_chatboc populated context

        datos_parciales_llm = context.get("datos_parciales_llm_reclamo", {})
        contacto_ctx = context.get("contacto_usuario", {})
        viewer_user = current_user # Alias

        # Fusionar datos
        categoria = datos_parciales_llm.get("categoria")
        if categoria:
            categoria = re.sub(r"^[^\w]+", "", str(categoria)).strip()
        descripcion = datos_parciales_llm.get("descripcion")
        ubicacion_llm = datos_parciales_llm.get("ubicacion")
        distrito_llm = datos_parciales_llm.get("distrito")
        coordenadas_llm = datos_parciales_llm.get("coordenadas")
        foto_url_llm = datos_parciales_llm.get("foto_url")

        lat_coord = lon_coord = None
        if isinstance(coordenadas_llm, dict):
            lat_raw = (
                coordenadas_llm.get("lat")
                or coordenadas_llm.get("latitude")
                or coordenadas_llm.get("latitud")
            )
            lon_raw = (
                coordenadas_llm.get("lon")
                or coordenadas_llm.get("lng")
                or coordenadas_llm.get("longitude")
                or coordenadas_llm.get("longitud")
            )
            try:
                if lat_raw is not None and lon_raw is not None:
                    lat_coord = float(lat_raw)
                    lon_coord = float(lon_raw)
            except (TypeError, ValueError):
                lat_coord = lon_coord = None

            if lat_coord is not None and lon_coord is not None:
                coordenadas_llm = {"lat": lat_coord, "lng": lon_coord}
            else:
                coordenadas_llm = None

        geocoded_from_coords = None
        enrichment_disabled = (
            os.getenv("CHATBOC_DISABLE_COORD_ENRICHMENT") == "1"
            or bool(os.getenv("PYTEST_CURRENT_TEST"))
            or "pytest" in sys.modules
        )
        if lat_coord is not None and lon_coord is not None and not enrichment_disabled:
            geocoded_from_coords = obtener_direccion_de_coordenadas(lat_coord, lon_coord)
            if geocoded_from_coords and geocoded_from_coords.get("formatted_address"):
                if not ubicacion_llm or _address_seems_generic(ubicacion_llm):
                    ubicacion_llm = geocoded_from_coords.get("formatted_address")

        municipio_config = {} # Would need to be passed in context or loaded

        # Contact Info - Name
        def _sanitize_nombre(valor: Any) -> str | None:
            if not isinstance(valor, str):
                return None
            cleaned = valor.strip().strip("\"'")
            cleaned = re.sub(r"\s+", " ", cleaned)
            cleaned = cleaned.strip(".,:;!¡¿?-")
            if not cleaned:
                return None
            cleaned_lower = cleaned.lower()
            if cleaned_lower in {"vecino", "vecina", "vecine", "vecino/a"}:
                return None
            forbidden_tokens = {
                "quiero",
                "necesito",
                "solicito",
                "reclamo",
                "problema",
                "pido",
                "favor",
                "hola",
                "buenas",
                "tengo",
                "hay",
            }
            if any(token in cleaned_lower for token in forbidden_tokens):
                return None
            if any(char.isdigit() for char in cleaned_lower):
                return None
            if len(cleaned.split()) > 6:
                return None
            return cleaned

        trusted_candidates = [
            getattr(viewer_user, "name", None) if viewer_user else None,
            getattr(viewer_user, "nombre", None) if viewer_user else None,
            contacto_ctx.get("nombre"),
        ]

        llm_candidates = [
            datos_parciales_llm.get("usuario"),
            datos_parciales_llm.get("nombre"),
            datos_parciales_llm.get("nombre_usuario_detectado"),
            datos_parciales_llm.get("nombre_detectado"),
        ]

        candidate_names = trusted_candidates + llm_candidates

        nombre_vecino_final = next(
            (clean for clean in map(_sanitize_nombre, candidate_names) if clean),
            "Vecino/a",
        )

        telefono_final = None
        phone_sources = [
            datos_parciales_llm.get("telefono"),
            datos_parciales_llm.get("telefono_detectado"),
            getattr(viewer_user, "telefono", None),
            contacto_ctx.get("telefono")
        ]
        for phone in phone_sources:
            if phone and validar_telefono(str(phone)):
                telefono_final = formatear_telefono_e164(str(phone))
                break

        # Contact Info - Email
        email_final = None
        email_sources = [
            datos_parciales_llm.get("email"),
            datos_parciales_llm.get("email_detectado"),
            getattr(viewer_user, "email", None),
            contacto_ctx.get("email")
        ]
        for email in email_sources:
            if email and validar_email(str(email)):
                email_final = str(email).lower()
                break

        # Contact Info - DNI
        dni_final = None
        dni_sources = [
            datos_parciales_llm.get("dni"),
            getattr(viewer_user, "dni", None),
            contacto_ctx.get("dni")
        ]
        for dni in dni_sources:
            dni_str = str(dni).strip() if dni else ""
            if dni_str.isdigit():
                dni_final = dni_str
                break

        # Optional contact address
        direccion_contacto = (
            datos_parciales_llm.get("direccion_contacto")
            or datos_parciales_llm.get("direccion")
        )
        if not direccion_contacto and viewer_user:
            direccion_contacto = getattr(viewer_user, "direccion", None)


        # Actualizar el contexto con los datos más recientes para persistencia
        for key, value in [
            ("categoria_reclamo", categoria),
            ("descripcion_reclamo", descripcion),
            ("direccion_reclamo", ubicacion_llm),
            ("coordenadas_reclamo", coordenadas_llm),
            ("nombre_vecino", nombre_vecino_final),
            ("telefono_vecino", telefono_final),
            ("email_vecino", email_final),
            ("dni_vecino", dni_final),
            ("direccion_contacto", direccion_contacto),
            ("foto_url", foto_url_llm),
        ]:
            if value:
                context[key] = value

        # Validación de datos esenciales para la creación del ticket
        campos_requeridos = ['descripcion', 'ubicacion', 'nombre', 'telefono', 'email']

        datos_finales_reclamo = {
            "categoria": categoria,
            "descripcion": descripcion,
            "ubicacion": ubicacion_llm or coordenadas_llm,
            "nombre": nombre_vecino_final if nombre_vecino_final != "Vecino/a" else None,
            "telefono": telefono_final,
            "email": email_final,
            "dni": dni_final,
        }

        campos_faltantes = [campo for campo in campos_requeridos if not datos_finales_reclamo.get(campo)]

        if campos_faltantes:
            # Eliminar duplicados
            campos_faltantes = sorted(list(set(campos_faltantes)))

            # Mensaje más amigable y botones de acción
            mensaje = f"Para continuar con tu reclamo, necesito algunos datos más: **{', '.join(campos_faltantes)}**. Por favor, indícamelos."

            return {
                "success": False,
                "message_body": mensaje,
                "pedir_info": campos_faltantes,
                "message_type": "text"
            }

        # --- Handle PIN (generate if missing) ---
        pin_llm = (
            datos_parciales_llm.get("pin")
            or datos_parciales_llm.get("consulta_pin")
        )
        pin_str = str(pin_llm).strip() if pin_llm else ""
        if pin_str.isdigit() and len(pin_str) == 6:
            pin_final = pin_str
        else:
            pin_final = f"{random.randint(0, 999999):06d}"

        context["pin_ticket"] = pin_final

        # Recopilación final de datos y creación del ticket

        # We need to resolve tenant/municipio IDs.
        # Since `responder_chatboc` calls `execute(owner_user, ...)`, we use owner_user.
        tenant_id, municipio_id = _resolve_municipio_tenant_ids(owner_user, {"municipio_config_actual": {}}) # Context dict mock

        ticket_data = {
            "asunto": f"Reclamo (LLM): {categoria or 'General'}",
            "categoria": categoria or "Reclamo General",
            "detalles": descripcion,
            "direccion": ubicacion_llm,
            "distrito": distrito_llm,
            "nombre_vecino": nombre_vecino_final,
            "telefono_vecino": telefono_final,
            "email_vecino": email_final,
            "dni_vecino": dni_final,
            "direccion_contacto": direccion_contacto,
            "estado": "nuevo",
            "user_id": getattr(viewer_user, "id", None) if viewer_user else None,
            "municipio_id": municipio_id,
            "tenant_id": tenant_id,
            "latitud": coordenadas_llm.get("lat") if isinstance(coordenadas_llm, dict) else None,
            "longitud": (
                coordenadas_llm.get("lng") if isinstance(coordenadas_llm, dict) else None
            ),
            "origen_reclamo": "LLM_CHATBOT",
            "foto_url_directa": foto_url_llm,
            "canal_ingreso": channel,
            "consulta_pin": pin_final,
        }

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
        logger.info(f"Data for servicio_tickets.crear_nuevo_ticket: {ticket_data_cleaned}")

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            # 'ticket_creado' is now always a dict.
            ticket_nro = ticket_creado.get('nro_ticket')
            if not ticket_nro:
                raise ValueError("El ticket creado no tiene un 'nro_ticket'.")
            nro_ticket_str = f"M-{ticket_nro}"
            logger.info(f"Ticket {nro_ticket_str} creado exitosamente.")

            # Limpiar contexto de reclamo después de la creación exitosa
            context.clear()
            # Restore essential info
            if nombre_vecino_final: context.setdefault("contacto_usuario", {})["nombre"] = nombre_vecino_final
            if telefono_final: context.setdefault("contacto_usuario", {})["telefono"] = telefono_final

            from services.municipio_responder import ConversationState
            context['estado_conversacion'] = "CONVERSACION_GENERAL_LLM" # Hardcoded enum value string

            # Formatear respuesta y obtener el botón de contacto
            mensaje_respuesta, botones_finales = formatear_ticket_respuesta(
                "reclamo",
                ticket_data_cleaned.get("nombre_vecino", "Vecino/a"),
                descripcion,
                categoria,
                nro_ticket_str,
                {}, # Contacto especializado placeholder
                "https://chatboc.ar/chat", # Base url placeholder
                dni=ticket_data_cleaned.get("dni_vecino"),
                consulta_pin=pin_final,
                include_links_in_message=True,
            )
            if botones_finales is None:
                botones_finales = []

            response_payload = {
                "success": True,
                "message_body": mensaje_respuesta,
                "options_list": botones_finales,
                "message_type": "interactive_buttons" if botones_finales else "text",
                "delayed_payload": _get_main_menu_payload({"channel": channel}), # Simplified context
                "delay_seconds": 20,
                "data": {
                    "ticket_id": ticket_creado.get('id'),
                    "nro_ticket": nro_ticket_str,
                    "status": "creado",
                    "consulta_pin": pin_final,
                }
            }
            return response_payload
        except Exception as e:
            logger.error(f"Error en CrearReclamoActionHandler: {e}", exc_info=True)
            response = {
                "success": False,
                "message_body": "Hubo un problema al registrar tu reclamo. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }
            return response

class ConsultarEstadoTicketActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoTicketActionHandler")
        # Logic to check ticket status
        # This is a simplified restore.
        return {
            "message_body": "Por favor, decime el número de tu reclamo (ej: M-1234).",
            "pedir_info": "id_ticket_mencionado",
            "message_type": "text"
        }

class ConsultarReclamoActionHandler(ConsultarEstadoTicketActionHandler):
    pass

class InfoLicenciaConducirActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        municipio_config = context.get("municipio_config_actual") or {}
        url = municipio_config.get("url_licencia") or "https://www.juninmendoza.gov.ar/licencia-de-conducir-junin/"

        return {
            "message_body": "Solicitá turno, revisá requisitos y encontrá información sobre la Licencia de Conducir.\n\n¿En qué más puedo ayudarte?",
            "options_list": [
                {"texto": "Pedir turno", "url": "https://tlc.mendoza.gov.ar/turnos", "type": "url"},
                {"texto": "Requisitos y costos", "url": url, "type": "url"},
            ],
            "message_type": "interactive_buttons",
            "fuente": "info_licencia_de_conducir_json"
        }

class SolicitarTurnoActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "Podés solicitar turnos para diversas áreas a través de nuestro portal web.",
            "options_list": [
                {"texto": "Solicitar Turno Web", "url": "https://www.juninmendoza.gov.ar/turnos", "type": "url"},
            ],
            "message_type": "interactive_buttons"
        }

class PagarTasasActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "Pagá tus tasas municipales de forma online, rápida y segura.",
            "options_list": [
                {"texto": "Pagar Online", "url": "https://www.juninmendoza.gov.ar/rentas", "type": "url"},
            ],
            "message_type": "interactive_buttons"
        }

class VerCatalogoActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        tenant_id, municipio_id = _resolve_municipio_tenant_ids(owner_user, {"municipio_config_actual": {}})

        # Get base URL for catalog
        # Assuming defaults if config missing in context
        catalog_url = None

        if not catalog_url:
            # Fallback to constructing it if we have a slug
            tenant_slug = getattr(owner_user, "tenant_slug", "municipio")
            app_base_url = os.environ.get("APP_BASE_URL", "https://chatboc.ar")
            catalog_url = f"{app_base_url}/store/{tenant_slug}"

        return {
            "message_body": "Explorá nuestro catálogo de productos y servicios, canjeá puntos y más.",
            "options_list": [
                {"texto": "🛍️ Ver Productos", "url": catalog_url, "type": "url"},
                {"texto": "🎁 Canje de Puntos", "action_id": "catalogo_canje_puntos"},
                {"texto": "❤️ Donaciones", "action_id": "catalogo_donaciones"}
            ],
            "message_type": "interactive_buttons",
            "fuente": "ver_catalogo_handler"
        }

class VerProductosActionHandler(VerCatalogoActionHandler):
    pass

class CanjearPuntosActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "El sistema de canje de puntos estará disponible próximamente.",
            "message_type": "text"
        }

class ComprarProductosActionHandler(VerCatalogoActionHandler):
    pass

class DonacionesActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
         return {
            "message_body": "Para realizar donaciones, por favor contactate con Desarrollo Social al 2634-123456.",
            "message_type": "text"
        }

class HacerSugerenciaActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "¿Qué sugerencia te gustaría dejarnos? Te escuchamos.",
            "pedir_info": "descripcion_sugerencia",
            "message_type": "text"
        }

class AgendaCulturalActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
         return {
            "message_body": "Consultá la agenda cultural y enterate de todos los eventos.",
            "options_list": [
                {"texto": "Ver Agenda", "url": "https://www.juninmendoza.gov.ar/agenda", "type": "url"}
            ],
            "message_type": "interactive_buttons"
        }

class VeterinariaBromatologiaActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "Información sobre castraciones, vacunación y control bromatológico.",
            "options_list": [
                {"texto": "Turnos Veterinaria", "url": "https://www.juninmendoza.gov.ar/veterinaria", "type": "url"}
            ],
            "message_type": "interactive_buttons"
        }

class ObrasActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
         return {
            "message_body": "Conocé las obras que estamos realizando en el municipio.",
            "options_list": [
                {"texto": "Mapa de Obras", "url": "https://www.juninmendoza.gov.ar/obras", "type": "url"}
            ],
            "message_type": "interactive_buttons"
        }

class ContactosUtilesActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "Acá tenés algunos contactos útiles:\n\n*Policía:* 911\n*Bomberos:* 100\n*Defensa Civil:* 103\n*Atención al Vecino:* 0800-222-5864",
            "message_type": "text"
        }

class PuntoLimpioActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "El programa Punto Limpio recolecta plásticos para reciclaje. Podés llevar tus botellas a los puntos habilitados en plazas y delegaciones.",
            "message_type": "text",
             "options_list": [
                {"texto": "Ver Puntos", "url": "https://www.juninmendoza.gov.ar/puntolimpio", "type": "url"}
            ]
        }

class EncuestasActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
         return {
            "message_body": "No hay encuestas activas en este momento. ¡Gracias por querer participar!",
            "message_type": "text"
        }

class AyudaActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        return {
            "message_body": "Soy el asistente virtual del municipio. Puedo ayudarte a realizar reclamos, consultar trámites, ver la agenda cultural y más. Simplemente escribí lo que necesitás o elegí una opción del menú.",
            "options_list": [{"texto": "Ver Menú", "action_id": "menu_principal"}],
            "message_type": "interactive_buttons"
        }

class UnknownIntentHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
         return {
            "message_body": "No estoy seguro de haber entendido. ¿Podrías reformular tu consulta o elegir una opción del menú?",
            "options_list": [{"texto": "Ver Menú", "action_id": "menu_principal"}],
            "message_type": "interactive_buttons"
        }

# Restored DerivarHumanoActionHandler
from socket_service import socketio, emit_new_ticket
from routes.ticket import serialize_ticket_to_json

class DerivarHumanoActionHandler(BaseActionHandler):
    def execute(self, owner_user, current_user, context, text, channel="web") -> Dict[str, Any]:
        """Crea un ticket real de chat en vivo y devuelve su identificador."""
        logger.info(f"Executing DerivarHumanoActionHandler")

        try:
            viewer_user = current_user
            pregunta_original = text or "Solicitud de agente"

            nombre = (getattr(viewer_user, "name", None) if viewer_user else "Vecino")

            # Simple resolve
            tenant_id, municipio_id = _resolve_municipio_tenant_ids(owner_user, {})

            ticket_data = {
                "asunto": f"Solicitud de Chat en Vivo por: {nombre}",
                "categoria": "Atención en Vivo",
                "pregunta": pregunta_original,
                "detalles": "Solicitud de agente desde el bot",
                "user_id": getattr(viewer_user, "id", None) if viewer_user else None,
                "municipio_id": municipio_id,
                "estado": "esperando_agente_en_vivo",
                "nombre_vecino": nombre,
            }
            ticket_type = "municipio"

            ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
            ticket_data_cleaned['tipo_ticket'] = ticket_type
            sala_dict = servicio_tickets.crear_nuevo_ticket(tipo_ticket=ticket_type, ticket_data=ticket_data_cleaned)
            if not sala_dict:
                raise Exception("crear_nuevo_ticket devolvió None")

            chat_id = f"M-{sala_dict['nro_ticket']}"

            user_message, _ = formatear_ticket_respuesta("chat", nombre, pregunta_original, "Atención en Vivo", chat_id)
            return {
                "success": True,
                "message_body": user_message,
                "data": {"ticket_id": sala_dict['id'], "chat_id": chat_id, "status": "esperando_agente_en_vivo"},
            }
        except Exception as e:
            logger.error(f"Error en DerivarHumanoActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_body": "Ocurrió un problema al crear el chat en vivo. ¿Podés intentar de nuevo más tarde?",
                "error_details": str(e),
            }

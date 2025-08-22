import logging
from services.conversation_state import ConversationState
from services.config_loader import cargar_configuracion_municipio
from services.flows.reclamo_handler import _get_reclamos_menu
from services.handlers.news_handler import NewsHandler
from services.handlers.poi_handler import PointsOfInterestHandler

logger = logging.getLogger(__name__)

MUNICIPIO_ID = "default" # This should probably be passed in context

def get_tramites_info():
    # This function is in municipio_responder.py, it should be moved to a more common place
    # For now, let's duplicate it here to avoid circular dependency
    from services.municipio_responder import get_tramites_info as original_get_tramites_info
    return original_get_tramites_info()

MENU_KEYWORDS = {
    "mostrar_menu_reclamos": ["reclamo", "reclamos", "iniciar", "problema"],
    "licencia_de_conducir": ["licencia", "conducir", "licencias", "carnet"],
    "pago_de_tasas_vigentes": ["pagar", "pago", "tasas", "tasa", "boleta", "boletas"],
    "consultar_otros_tramites": ["consultar", "consulta", "tramites", "tramite", "otros"],
    "veterinaria_y_bromatologia": ["veterinaria", "animales", "perro", "gato", "mascotas", "bromatologia", "bromatología"],
    "solicitar_turnos": ["turnos", "turno", "solicitar"],
    "agenda_cultural_y_turistica": ["agenda", "cultural", "turistica", "turismo", "eventos"],
    "ultimas_novedades": ["novedades", "noticias", "ultimas"],
    "estacionamiento": ["estacionamiento", "estacionar", "auto", "coche"],
    "contactos_utiles": ["contactos", "utiles", "emergencia", "bomberos", "policia"]
}

def handle_main_menu_action(action_id: str, context: dict, chat_db_context) -> dict:
    """
    Handles actions from the new categorized main menu.
    """
    if action_id == "mostrar_menu_reclamos":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault("contexto_municipio_v2", {})
        logger.info("[MENU_ACTION] Clearing previous claim context for new claim.")
        contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
        contexto_municipio_actual.pop("historial_llm_reclamo", None)
        return _get_reclamos_menu()

    tramites_info = get_tramites_info()
    if action_id in tramites_info:
        data = tramites_info[action_id] or {}
        botones = data.get("botones", [])
        for btn in botones:
            if btn.get("url") and not btn.get("type"):
                btn["type"] = "url"
        return {
            "message_body": data.get("descripcion", ""),
            "options_list": botones,
            "message_type": "interactive_buttons" if botones else "text",
            "fuente": f"info_{action_id}_json",
        }
    if action_id == "veterinaria_y_bromatologia":
        contactos_info = cargar_configuracion_municipio(MUNICIPIO_ID, "contactos_especializados.json")
        contacto_data = contactos_info.get("Veterinaria y Bromatologia", {})

        if not contacto_data:
            return {
                "message_body": "No se encontró la información de contacto para Veterinaria y Bromatología en este momento.",
                "message_type": "text"
            }

        nombre = contacto_data.get("nombre")
        telefono = contacto_data.get("telefono")
        horario = contacto_data.get("horario")

        message_body = f"🐾 *Información de Veterinaria y Bromatología*\n\n"
        if nombre:
            message_body += f"Encargado/a: *{nombre}*\n"
        if telefono:
            telefono_numerico = ''.join(filter(str.isdigit, telefono))
            link_whatsapp = f"https://wa.me/{telefono_numerico}"
            message_body += f"Teléfono: *{telefono}* (WhatsApp: {link_whatsapp})\n"
        if horario:
            message_body += f"Horario de atención: *{horario}*\n"

        botones = []
        if telefono:
            telefono_numerico = ''.join(filter(str.isdigit, telefono))
            link_whatsapp = f"https://wa.me/{telefono_numerico}"
            botones.append({
                "texto": "Contactar por WhatsApp",
                "url": link_whatsapp,
                "type": "url"
            })

        return {
            "message_body": message_body.strip(),
            "options_list": botones,
            "message_type": "interactive_buttons" if botones else "text",
            "fuente": "info_veterinaria_json"
        }

    if action_id == "ultimas_novedades":
        return NewsHandler(context=context).handle({})

    if action_id == "consultar_otros_tramites":
        from services.actions.municipio_actions import ConsultarInfoTramiteActionHandler
        handler_response = ConsultarInfoTramiteActionHandler(context).execute({})

        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault("contexto_municipio_v2", {})

        if not handler_response.get('success', True):
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_TRAMITE.name
            if chat_db_context:
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(chat_db_context, "context_data")

            return {
                "message_body": handler_response.get('message_to_user', '¿Sobre qué trámite necesitas información?'),
                "message_type": "text",
                "options_list": [],
                "fuente": "fix_tramites_bug_wrapper_v2"
            }
        else:
            contexto_municipio_actual['estado_conversacion'] = None
            if chat_db_context:
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(chat_db_context, "context_data")
            return handler_response

    if action_id == "agenda_cultural_y_turistica":
        from services.herramientas_municipio import consultar_eventos_culturales
        eventos_hoy = consultar_eventos_culturales(fecha="hoy")
        return {
            "message_body": f"Aquí tienes la agenda para hoy:\n\n{eventos_hoy}",
            "options_list": [],
            "message_type": "text",
            "fuente": "agenda_cultural_hoy"
        }

    if action_id == "estacionamiento":
        contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault("contexto_municipio_v2", {})
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_UBICACION_GENERAL.name
        contexto_municipio_actual['consulta_pendiente_ubicacion'] = 'estacionamiento'
        if chat_db_context:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(chat_db_context, "context_data")
        return {
            "message_body": "Para encontrar estacionamiento, por favor compartí tu ubicación o escribí una dirección.",
            "options_list": [{"texto": "Compartir ubicación", "action": "compartir_ubicacion"}, {"texto": "Cancelar", "action": "cancelar"}],
            "message_type": "interactive_buttons",
            "fuente": "pedir_ubicacion_estacionamiento"
        }

    if action_id == "solicitar_turnos":
        return {
            "message_body": "📅 Para solicitar turnos para la Licencia de Conducir, por favor ingresá al siguiente enlace:",
            "options_list": [{"texto": "Solicitar Turno", "url": "https://tlc.mendoza.gov.ar/turnos", "type": "url"}],
            "message_type": "interactive_buttons",
            "fuente": "info_solicitar_turnos_licencia"
        }

    if action_id == "contactos_utiles":
        contactos_utiles_data = cargar_configuracion_municipio(MUNICIPIO_ID, "contactos_utiles.json")
        if contactos_utiles_data:
            categorias = [cat.get("nombre_categoria") for cat in contactos_utiles_data.get("categorias", [])]
            botones = [{"texto": cat} for cat in categorias]

            contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault("contexto_municipio_v2", {})
            contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_CATEGORIA_CONTACTO.name
            if chat_db_context:
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(chat_db_context, "context_data")

            return {
                "message_body": "Por favor, elegí una categoría de contactos:",
                "options_list": botones,
                "message_type": "interactive_buttons",
                "fuente": "contactos_utiles_categorias"
            }

    return {
        "message_body": "Esta función no está implementada en este momento. Por favor, intentá con otra opción.",
        "options_list": [],
        "message_type": "text",
        "fuente": f"unimplemented_{action_id}"
    }


def handle_contact_category_selection(pregunta_str: str, context: dict, chat_db_context) -> dict:
    contactos_utiles_data = cargar_configuracion_municipio(MUNICIPIO_ID, "contactos_utiles.json")
    if contactos_utiles_data:
        for categoria in contactos_utiles_data.get("categorias", []):
            if categoria.get("nombre_categoria").lower() == pregunta_str.lower():
                mensaje = f"Contactos para *{categoria.get('nombre_categoria')}*:\n\n"
                for contacto in categoria.get("contactos", []):
                    mensaje += f"*{contacto.get('nombre')}*: {contacto.get('descripcion')}\n"
                    if contacto.get('telefono'):
                        mensaje += f"Teléfono: {contacto.get('telefono')}\n"
                    if contacto.get('url'):
                        mensaje += f"Web: {contacto.get('url')}\n"
                    mensaje += "\n"

                contexto_municipio_actual = context.get("chat_db_context_data", {}).setdefault("contexto_municipio_v2", {})
                contexto_municipio_actual['estado_conversacion'] = None
                if chat_db_context:
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(chat_db_context, "context_data")

                return {
                    "message_body": mensaje,
                    "options_list": [],
                    "message_type": "text",
                    "fuente": "contactos_utiles_detalle"
                }

    return {
        "message_body": "No encontré esa categoría. Por favor, intentá de nuevo.",
        "options_list": [],
        "message_type": "text",
        "fuente": "contactos_utiles_categoria_no_encontrada"
    }

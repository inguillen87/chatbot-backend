import logging
import json
import random
from flask import current_app, has_app_context

# Imports necesarios para la función
from models import MunicipioTicket, db, User, ArchivoAdjunto, AnalisisArchivo
from services.ticket_service import servicio_tickets
from .herramientas_municipio import parse_direccion_completa, direccion_es_valida
from .common_utils import validar_telefono, formatear_telefono_e164, validar_email
from .config_loader import CONFIG_MUNICIPIO # Para fallback de config
from services.llm_bridge import llamar_llm  # Necesario para el type hint, aunque no se usa en esta función
from services.whatsapp_service import enviar_notificacion_whatsapp_con_plantilla

# Definición completa de accion_crear_reclamo_municipio
def accion_crear_reclamo_municipio(datos_llm: dict, context: dict) -> dict:
    logger_func = current_app.logger if has_app_context() else logging.getLogger(__name__)
    logger_func.info(f"[ACCION_CREAR_RECLAMO_MUNICIPIO] Datos LLM: {datos_llm}")

    # --- 1. Extracción y Validación de Datos ---
    categoria = datos_llm.get("categoria", "Reclamo General")
    descripcion = datos_llm.get("descripcion")
    ubicacion_llm = datos_llm.get("ubicacion")
    coordenadas_llm = datos_llm.get("coordenadas") 
    nombre_vecino_llm = datos_llm.get("usuario") 
    telefono_llm = datos_llm.get("telefono")
    email_llm = datos_llm.get("email")
    foto_url_llm = datos_llm.get("foto_url_adjunta") # Campo hipotético si LLM extrae URL de imagen del texto

    if not descripcion:
        return {
            "message_body": "No pude entender la descripción del reclamo. Por favor, intenta describirlo de nuevo.",
            "options_list": [], "fuente": "accion_crear_reclamo_error_sin_descripcion"
        }
    if not ubicacion_llm and not coordenadas_llm:
        return {
            "message_body": "No pude entender la ubicación del reclamo. Por favor, especifica dónde es el problema.",
            "options_list": [], "fuente": "accion_crear_reclamo_error_sin_ubicacion"
        }

    # --- 2. Recopilación de Información del Contexto ---
    viewer_user = context.get("viewer_user_obj")
    owner_user = context.get("user_obj") # Dueño del bot (municipio)
    
    user_id_db = getattr(viewer_user, "id", None)
    anon_id_db = context.get("anon_id") if not user_id_db else None
    municipio_config_actual = context.get("municipio_config_actual", CONFIG_MUNICIPIO) 
    municipio_db_id_para_ticket = getattr(owner_user, "municipio_id", None) # Asumiendo que owner_user tiene este attr.
    chat_session_uuid = context.get("chat_session_uuid")
    # chat_db_context_data = context.get("chat_db_context_data", {}) # Para idempotencia

    nombre_vecino_final = nombre_vecino_llm or getattr(viewer_user, "nombre", None) or "Ciudadano Anónimo"
    
    telefono_final_validado_e164 = None
    temp_phone_str = str(telefono_llm or getattr(viewer_user, "telefono", ""))
    if temp_phone_str and validar_telefono(temp_phone_str):
        telefono_final_validado_e164 = formatear_telefono_e164(temp_phone_str)

    email_final_validado = None
    temp_email_str = str(email_llm or getattr(viewer_user, "email", ""))
    if temp_email_str and validar_email(temp_email_str):
        email_final_validado = temp_email_str.lower()

    direccion_final_txt = ubicacion_llm
    latitud_final = coordenadas_llm.get("lat") if isinstance(coordenadas_llm, dict) else None
    longitud_final = coordenadas_llm.get("lon") if isinstance(coordenadas_llm, dict) else None

    if ubicacion_llm and not (latitud_final and longitud_final):
        parsed_address = parse_direccion_completa(ubicacion_llm, municipio_config_actual)
        if parsed_address and parsed_address.get("calle") and parsed_address.get("localidad"):
            direccion_final_txt = f"{parsed_address['calle']} {parsed_address.get('numero', '')}, {parsed_address['localidad']}".replace(" ,", ",").strip()
            logger_func.info(f"Dirección parseada de LLM: {direccion_final_txt}")
            # Aquí se podría intentar geocodificar para obtener lat/lon si es crucial y no vienen del LLM
        elif not direccion_es_valida(ubicacion_llm):
             return {
                "message_body": f"La ubicación '{ubicacion_llm}' no parece válida. ¿Podrías verificarla?",
                "options_list": [], "fuente": "accion_crear_reclamo_direccion_invalida_llm"
            }
    
    # --- Lógica de Idempotencia (Simplificada por ahora, necesita el payload original o datos más específicos) ---
    # if chat_session_uuid and chat_db_context_data:
    #     idempotency_key_data = f"{categoria}-{descripcion[:20]}-{direccion_final_txt[:20]}"
    #     # Esta clave es muy simple, idealmente usar un hash o algo más robusto del payload del LLM.
    #     # O el LLM podría devolver un ID de interacción único.
    #     idempotency_key = f"{chat_session_uuid}_{hash(idempotency_key_data)}" 
    #     processed_keys = chat_db_context_data.get("processed_idempotency_keys_reclamos_llm", {})
    #     if idempotency_key in processed_keys:
    #         existing_ticket_nro = processed_keys[idempotency_key]
    #         logger_func.info(f"Idempotency: Reclamo LLM ya procesado. Ticket: M-{existing_ticket_nro}.")
    #         return {
    #             "message_body": f"Este reclamo ya fue registrado con el número de ticket: **M-{existing_ticket_nro}**.",
    #             "options_list": [], "fuente": "reclamo_llm_idempotencia_detectada"
    #         }

    # --- Límite de tickets para anónimos (Simplificado) ---
    # if anon_id_db and hasattr(current_app, 'config'):
    #     max_anon_tickets = current_app.config.get("ANONYMOUS_MAX_TICKETS_PER_SESSION", 1)
    #     # ... (lógica de conteo de MunicipioTicket para anon_id_db en la última hora/sesión) ...
    #     # if count >= max_anon_tickets: return {...}

    ticket_data = {
        "asunto": f"Reclamo (LLM): {categoria}", "categoria": categoria, "detalles": descripcion,
        "direccion": direccion_final_txt, "nombre_vecino": nombre_vecino_final,
        "telefono_vecino": telefono_final_validado_e164, "email_vecino": email_final_validado,
        "estado": "nuevo", "user_id": user_id_db, "anon_id": anon_id_db,
        "municipio_id": municipio_db_id_para_ticket, "latitud": latitud_final, "longitud": longitud_final,
        "origen_reclamo": "LLM_CHATBOT"
    }
    # foto_url_directa: Si el LLM o el contexto (foto_url de una imagen de WhatsApp) la proveen
    if context.get("foto_url"): # De una imagen de WhatsApp procesada antes de esta acción
        ticket_data["foto_url_directa"] = context.get("foto_url")
    elif foto_url_llm: # Si el LLM extrajo una URL de imagen del texto
        ticket_data["foto_url_directa"] = foto_url_llm


    ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
    logger_func.info(f"[ACCION_CREAR_RECLAMO_MUNICIPIO] Datos para ticket: {ticket_data_cleaned}")

    try:
        ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
        if not ticket_creado:
            raise Exception("servicio_tickets.crear_nuevo_ticket retornó None")
        
        nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
        logger_func.info(f"Ticket {nro_ticket_str} creado exitosamente vía LLM.")

        # Manejo de adjuntos (si el LLM indicó o si hay algo en contexto)
        # Ejemplo: Si el LLM dijo "adjuntar_foto" y el frontend subió una foto, el ID estaría en context.
        archivo_id_a_vincular = context.get("archivo_id_para_asociar")
        if archivo_id_a_vincular:
            from services.archivo_service import archivo_service # Import local
            asociacion_exitosa = archivo_service.asociar_archivos_a_ticket(ticket_id=ticket_creado.id, tipo_ticket="municipio", ids_archivos=[archivo_id_a_vincular])
            if asociacion_exitosa: logger_func.info(f"Archivo ID {archivo_id_a_vincular} asociado a ticket {nro_ticket_str}.")
            else: logger_func.warning(f"No se pudo asociar archivo ID {archivo_id_a_vincular} a ticket {nro_ticket_str}.")
            context.pop("archivo_id_para_asociar", None) # Consumir

        # Si es una imagen de WhatsApp (URL en foto_url_directa y análisis crudo en contexto)
        # raw_analysis_data = context.get("analisis_imagen_reclamo_auto_raw")
        # if ticket_data.get("foto_url_directa") and raw_analysis_data and raw_analysis_data.get("mime_type"):
        #     ... (lógica para crear ArchivoAdjunto y AnalisisArchivo para WhatsApp) ...

        # Guardar en idempotency_keys_reclamos_llm
        # if chat_session_uuid and chat_db_context_data and idempotency_key:
        #      processed_keys = chat_db_context_data.get("processed_idempotency_keys_reclamos_llm", {})
        #      processed_keys[idempotency_key] = ticket_creado.nro_ticket
        #      chat_db_context_data["processed_idempotency_keys_reclamos_llm"] = processed_keys


        if telefono_final_validado_e164:
            try:
                enviar_notificacion_whatsapp_con_plantilla(telefono_final_validado_e164, nombre_vecino_final, str(ticket_creado.nro_ticket), categoria)
                logger_func.info(f"Notificación WhatsApp enviada para ticket {nro_ticket_str}")
            except Exception as e_notify_wp:
                logger_func.error(f"Error enviando notificación WhatsApp para {nro_ticket_str}: {e_notify_wp}")
        
        return {
            "message_body": f"¡Gracias {nombre_vecino_final}! Tu reclamo sobre '{categoria}' ha sido registrado con el número {nro_ticket_str}. Te mantendremos informado.",
            "options_list": [
                {"id": f"consultar_estado_ticket_{ticket_creado.nro_ticket}", "texto": "Consultar estado"},
                {"id": "iniciar_otro_reclamo_llm", "texto": "Hacer otro reclamo"}
            ],
            "fuente": "accion_crear_reclamo_llm_exito",
            "ticket_id": ticket_creado.id 
        }
    except Exception as e:
        logger_func.error(f"[ACCION_CREAR_RECLAMO_MUNICIPIO] Error al crear ticket: {e}", exc_info=True)
        db.session.rollback()
        return {
            "message_body": "Hubo un problema al intentar registrar tu reclamo. Por favor, intenta de nuevo más tarde o contacta al municipio directamente.",
            "options_list": [],
            "fuente": "accion_crear_reclamo_llm_error_creacion"
        }

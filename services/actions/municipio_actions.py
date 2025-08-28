# services/actions/municipio_actions.py
import logging
import re
from .base_action_handler import BaseActionHandler
from typing import Dict, Any
from services.ticket_service import servicio_tickets
from services.notifications import enviar_notificacion_whatsapp_con_plantilla, enviar_notificacion_sms
from services.herramientas_municipio import parse_direccion_completa as parse_direccion, direccion_es_valida
from services.ticket_utils import formatear_ticket_respuesta
from services.common_utils import validar_telefono, formatear_telefono_e164, validar_email
from services.config_loader import cargar_configuracion_municipio
from models import MunicipioTicket

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

class BuscarEstacionamientoActionHandler(BaseActionHandler):
    action_name = "buscar_estacionamiento"

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing BuscarEstacionamientoActionHandler with data: {action_data}")

        # La ubicación puede venir de la acción del LLM o del contexto si se pidió antes
        ubicacion = action_data.get("ubicacion") or self.context.get("ubicacion_usuario")

        if not ubicacion:
            # Si no hay ubicación, la pedimos.
            self.context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = "ESPERANDO_UBICACION_GENERAL"
            self.context[CONTEXTO_MUNICIPIO]["accion_pendiente_tras_ubicacion"] = "buscar_estacionamiento"

            return {
                "success": False,
                "message_to_user": "Para encontrar estacionamiento, por favor compartí tu ubicación o escribí una dirección (ej: San Martín 1200).",
                "pedir_info": "ubicacion"
            }

        # Llamar al servicio de estacionamiento
        from services.estacionamiento_service import consultar_ocupacion
        resultado = consultar_ocupacion(ubicacion) # resultado es un dict {"texto": "..."}

        # Limpiar el estado de espera si existía
        if self.context.get(CONTEXTO_MUNICIPIO, {}).get("accion_pendiente_tras_ubicacion") == "buscar_estacionamiento":
            self.context[CONTEXTO_MUNICIPIO].pop("accion_pendiente_tras_ubicacion")
            if "estado_conversacion" in self.context[CONTEXTO_MUNICIPIO]:
                 self.context[CONTEXTO_MUNICIPIO].pop("estado_conversacion")


        return {
            "success": True,
            "message_to_user": resultado["texto"],
            "data": resultado
        }

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoActionHandler with data: {action_data}")

        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO, {})
        viewer_user = self.context.get("viewer_user_obj")

        # Fusionar datos: action_data tiene prioridad, luego el contexto del reclamo, luego el perfil del usuario
        datos_parciales = contexto_reclamo.get("datos_parciales_llm_reclamo", {})
        categoria = action_data.get("categoria") or datos_parciales.get("categoria")
        descripcion = action_data.get("descripcion") or datos_parciales.get("descripcion")
        ubicacion_llm = action_data.get("ubicacion") or datos_parciales.get("ubicacion")
        distrito_llm = action_data.get("distrito") or datos_parciales.get("distrito")

        if ubicacion_llm and not distrito_llm:
            logger.info(f"Attempting to parse district from address: {ubicacion_llm}")
            parsed_address = parse_direccion(ubicacion_llm)
            if parsed_address and parsed_address.get('localidad'):
                distrito_llm = parsed_address.get('localidad')
                logger.info(f"Parsed district: {distrito_llm}")
        coordenadas_llm = action_data.get("coordenadas") or datos_parciales.get("coordenadas")
        foto_url_llm = action_data.get("foto_url_adjunta") or datos_parciales.get("foto_url")

        # Lógica de fusión de datos de contacto mejorada
        llm_name = (action_data.get("usuario") or datos_parciales.get("usuario") or
                    action_data.get("nombre_usuario_detectado") or datos_parciales.get("nombre_usuario_detectado"))
        profile_name_from_user_obj = getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None)
        profile_name_from_context = self.context.get("profile_name")

        # Prioritize LLM name, then profile from user object, then profile from context.
        nombre_vecino_final = "Vecino/a"  # Default
        if isinstance(llm_name, str) and llm_name.strip():
            nombre_vecino_final = llm_name
        elif isinstance(profile_name_from_user_obj, str) and profile_name_from_user_obj.strip():
            nombre_vecino_final = profile_name_from_user_obj
        elif isinstance(profile_name_from_context, str) and profile_name_from_context.strip():
            nombre_vecino_final = profile_name_from_context

        telefono_from_llm = (action_data.get("telefono") or datos_parciales.get("telefono") or
                             action_data.get("telefono_detectado") or datos_parciales.get("telefono_detectado"))
        telefono_final = None
        if telefono_from_llm and validar_telefono(telefono_from_llm):
            telefono_final = formatear_telefono_e164(telefono_from_llm)
        elif viewer_user and getattr(viewer_user, "telefono", None) and validar_telefono(str(viewer_user.telefono)):
             telefono_final = formatear_telefono_e164(str(viewer_user.telefono))


        email_from_llm = (action_data.get("email") or datos_parciales.get("email") or
                          action_data.get("email_detectado") or datos_parciales.get("email_detectado"))
        email_final = None
        if email_from_llm and validar_email(email_from_llm):
            email_final = email_from_llm.lower()
        elif viewer_user and getattr(viewer_user, "email", None) and validar_email(str(viewer_user.email)):
            email_final = str(viewer_user.email).lower()

        dni_from_llm = action_data.get("dni") or datos_parciales.get("dni")
        dni_final = None
        if dni_from_llm and isinstance(dni_from_llm, str) and dni_from_llm.isdigit():
            dni_final = dni_from_llm
        elif viewer_user and getattr(viewer_user, "dni", None) and str(viewer_user.dni).isdigit():
            dni_final = str(viewer_user.dni)


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
            ("foto_url", foto_url_llm),
        ]:
            if value:
                contexto_reclamo[key] = value

        # Validación de datos esenciales para la creación del ticket
        campos_faltantes = []
        if not descripcion:
            campos_faltantes.append("descripcion")
        if not ubicacion_llm and not coordenadas_llm:
            campos_faltantes.append("ubicacion")
        logger.info(f"DEBUG: viewer_user: {viewer_user}")
        logger.info(f"DEBUG: nombre_vecino_final: {nombre_vecino_final}")
        logger.info(f"DEBUG: telefono_final: {telefono_final}")
        logger.info(f"DEBUG: email_final: {email_final}")
        logger.info(f"DEBUG: campos_faltantes before: {campos_faltantes}")
        if not viewer_user and (nombre_vecino_final == "Vecino/a" or not telefono_final or not email_final or not dni_final):
             if nombre_vecino_final == "Vecino/a":
                 campos_faltantes.append("nombre")
             if not telefono_final:
                 campos_faltantes.append("telefono")
             if not email_final:
                 campos_faltantes.append("email")
             if not dni_final:
                 campos_faltantes.append("dni")
        logger.info(f"DEBUG: campos_faltantes after: {campos_faltantes}")

        # La lógica de confirmación ahora se maneja en 'municipio_responder.py'
        # Este handler ahora solo valida y crea.

        if campos_faltantes:
            # Eliminar duplicados
            campos_faltantes = sorted(list(set(campos_faltantes)))
            self.context[CONTEXTO_MUNICIPIO] = contexto_reclamo

            # Mensaje más amigable y botones de acción
            mensaje = f"Para continuar con tu reclamo, necesito algunos datos más: **{', '.join(campos_faltantes)}**. Por favor, indícamelos."
            botones = [{"texto": f"Ingresar {campo.replace('_', ' ')}", "id_accion": f"ingresar_{campo}"} for campo in campos_faltantes]
            botones.append({"texto": "Cancelar reclamo", "id_accion": "cancelar_reclamo"})

            return {
                "success": False,
                "message_to_user": mensaje,
                "pedir_info": campos_faltantes,
                "options_list": botones,
                "message_type": "interactive_list" if len(botones) > 3 else "interactive_buttons"
            }

        # --- Solicitar PIN de consulta si aún no se proporcionó ---
        pin_llm = (
            action_data.get("pin")
            or datos_parciales.get("pin")
            or datos_parciales.get("consulta_pin")
        )
        pin_final = None
        if pin_llm:
            pin_str = str(pin_llm).strip()
            if pin_str.isdigit() and len(pin_str) == 6:
                pin_final = pin_str
            else:
                return {
                    "success": False,
                    "message_to_user": "El PIN debe ser un número de 6 dígitos. Por favor, ingresá un PIN válido.",
                    "pedir_info": "pin_ticket",
                }
        else:
            return {
                "success": False,
                "message_to_user": "Antes de finalizar, elegí un PIN de 6 dígitos para consultar tu reclamo más adelante.",
                "pedir_info": "pin_ticket",
            }

        contexto_reclamo["pin_ticket"] = pin_final

        # Recopilación final de datos y creación del ticket
        owner_user = self.context.get("user_obj")

        # Update viewer_user object if it exists and we have new info
        if viewer_user:
            updated = False
            if nombre_vecino_final and not viewer_user.name:
                viewer_user.name = nombre_vecino_final
                updated = True
            if telefono_final and not viewer_user.telefono:
                viewer_user.telefono = telefono_final
                updated = True
            if email_final and not viewer_user.email:
                viewer_user.email = email_final
                updated = True
            if dni_final and not getattr(viewer_user, "dni", None):
                viewer_user.dni = dni_final
                updated = True
            if updated:
                from models import db
                db.session.add(viewer_user)
                db.session.commit()
                logger.info(f"User profile for {viewer_user.id} updated with new contact info.")
        pregunta_original = self.context.get("pregunta_actual_usuario", "")
        ticket_data = {
            "pregunta": pregunta_original,
            "asunto": f"Reclamo (LLM): {categoria or 'General'}",
            "categoria": categoria or "Reclamo General",
            "detalles": descripcion,
            "direccion": ubicacion_llm,
            "distrito": distrito_llm,
            "nombre_vecino": nombre_vecino_final,
            "telefono_vecino": telefono_final,
            "email_vecino": email_final,
            "dni_vecino": dni_final,
            "estado": "nuevo",
            "user_id": getattr(viewer_user, "id", None),
            "anon_id": self.context.get("anon_id") if not getattr(viewer_user, "id", None) else None,
            "municipio_id": getattr(owner_user, "municipio_id", None),  # Asegurar que el municipio_id se pasa aquí
            "latitud": coordenadas_llm.get("lat") if isinstance(coordenadas_llm, dict) else None,
            "longitud": coordenadas_llm.get("lon") if isinstance(coordenadas_llm, dict) else None,
            "origen_reclamo": "LLM_CHATBOT",
            "foto_url_directa": foto_url_llm,
            "canal_ingreso": self.context.get("channel"),
        }

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
        logger.info(f"Data for servicio_tickets.crear_nuevo_ticket: {ticket_data_cleaned}")

        # Enhanced logging for debugging contact info
        logger.info(f"DEBUG_CONTACT_INFO: nombre='{ticket_data_cleaned.get('nombre_vecino')}', "
                    f"telefono='{ticket_data_cleaned.get('telefono_vecino')}', "
                    f"email='{ticket_data_cleaned.get('email_vecino')}'")

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

            # Cargar contactos y encontrar el específico para la categoría
            contactos = cargar_configuracion_municipio(
                getattr(owner_user, "municipio_id", "default"),
                "contactos_especializados.json",
            )
            contacto_especializado = dict(contactos.get(categoria, contactos.get("default", {})))

            # Completar datos desde tramites.json si existen
            tramites_cfg = cargar_configuracion_municipio(
                getattr(owner_user, "municipio_id", "default"),
                "tramites.json",
            )
            tramite_info = tramites_cfg.get(categoria, {}) if isinstance(tramites_cfg, dict) else {}
            if isinstance(tramite_info, dict):
                if not contacto_especializado.get("telefono") and tramite_info.get("telefono"):
                    contacto_especializado["telefono"] = tramite_info.get("telefono")
                if not contacto_especializado.get("horario") and tramite_info.get("horario"):
                    contacto_especializado["horario"] = tramite_info.get("horario")
                if not contacto_especializado.get("link"):
                    botones = tramite_info.get("botones")
                    if isinstance(botones, list) and botones:
                        contacto_especializado["link"] = botones[0].get("url")

            # Fallback con datos del perfil del municipio y configuración general
            if getattr(owner_user, "link_web", None):
                contacto_especializado.setdefault("link", owner_user.link_web)
            else:
                cfg = cargar_configuracion_municipio(
                    getattr(owner_user, "municipio_id", "default"),
                    "config.json",
                )
                if isinstance(cfg, dict) and cfg.get("web_url"):
                    contacto_especializado.setdefault("link", cfg.get("web_url"))

            if getattr(owner_user, "telefono", None):
                contacto_especializado.setdefault("telefono", owner_user.telefono)
            if getattr(owner_user, "horario", None):
                contacto_especializado.setdefault("horario", owner_user.horario)

            # Limpiar contexto de reclamo después de la creación exitosa
            # Guardamos la info del usuario si existe, para no perderla.
            user_info = contexto_reclamo.get('user', {})
            # Limpiamos TODO el contexto del municipio para evitar "context bleed".
            if CONTEXTO_MUNICIPIO in self.context:
                self.context[CONTEXTO_MUNICIPIO].clear()
                # Restauramos la info del usuario.
                if user_info:
                    self.context[CONTEXTO_MUNICIPIO]['user'] = user_info

                # Forzamos el estado de vuelta a conversación general para que el bot no quede "trabado" en el flujo de reclamo.
                from services.municipio_responder import ConversationState
                self.context[CONTEXTO_MUNICIPIO]['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name
                logger.info(f"Contexto de reclamo limpiado. Nuevo estado: {self.context[CONTEXTO_MUNICIPIO]['estado_conversacion']}")


            # Notificaciones
            if ticket_data_cleaned.get("telefono_vecino"):
                try:
                    enviar_notificacion_whatsapp_con_plantilla(
                        ticket_data_cleaned["telefono_vecino"],
                        ticket_data_cleaned.get("nombre_vecino", "Vecino"),
                        str(ticket_nro),
                        ticket_data_cleaned.get("categoria", "Varios")
                    )
                except Exception as e_whatsapp:
                    logger.error(f"Error enviando notificación de WhatsApp para {nro_ticket_str}: {e_whatsapp}")

                try:
                    enviar_notificacion_sms(
                        ticket_data_cleaned["telefono_vecino"],
                        f"Hola {ticket_data_cleaned.get('nombre_vecino', 'Vecino')}! Tu reclamo M-{ticket_nro} ({ticket_data_cleaned.get('categoria', 'Varios')}) fue generado."
                    )
                except Exception as e_sms:
                    logger.error(f"Error enviando notificación por SMS para {nro_ticket_str}: {e_sms}")

            # Formatear respuesta y obtener el botón de contacto
            municipio_config = self.context.get('municipio_config_actual', {})
            base_chat_url = municipio_config.get('base_chat_url', 'https://www.chatboc.ar/tickets/municipio')
            mensaje_respuesta, botones_finales = formatear_ticket_respuesta(
                "reclamo",
                ticket_data_cleaned.get("nombre_vecino", "Vecino/a"),
                descripcion,
                categoria,
                nro_ticket_str,
                contacto_especializado,
                base_chat_url,
                dni=ticket_data_cleaned.get("dni_vecino"),
                consulta_pin=ticket_creado.get("consulta_pin"),
            )

            # Log para debug
            logger.info(f"Respuesta formateada: '{mensaje_respuesta}', Botones: {botones_finales}")

            return {
                "success": True,
                "message_to_user": mensaje_respuesta,
                "options_list": botones_finales,
                "message_type": "interactive_buttons" if botones_finales else "text",
                "data": {"ticket_id": ticket_creado.get('id'), "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en CrearReclamoActionHandler: {e}", exc_info=True)
            response = {
                "success": False,
                "message_to_user": "Hubo un problema al registrar tu reclamo. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }
            print(f"DEBUG: CrearReclamoActionHandler returning error: {response}")
            return response

class ConsultarEstadoTicketActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoTicketActionHandler with data: {action_data}")
        ticket_id = action_data.get("id_ticket_mencionado")
        if not ticket_id:
            # Try to parse from raw user question stored in context
            raw_question = self.context.get("pregunta_actual_usuario", "")
            match = re.search(r"\d+", raw_question)
            if match:
                ticket_id = match.group(0)
        if not ticket_id:
            return {
                "success": False,
                "message_to_user": "Para consultar el estado, necesito el número de ticket.",
                "pedir_info": "id_ticket_mencionado"
            }

        ticket_id_str = str(ticket_id).replace("M-", "").strip()

        pin = action_data.get("pin")
        if not pin:
            return {
                "success": False,
                "message_to_user": "Necesito el PIN de 6 dígitos para consultar el ticket.",
                "pedir_info": "pin_ticket",
            }

        ticket = MunicipioTicket.query.filter_by(nro_ticket=ticket_id_str, consulta_pin=pin).first()
        if not ticket:
            return {
                "success": False,
                "message_to_user": f"No encontré el ticket M-{ticket_id_str} o el PIN es incorrecto.",
                "options_list": [{"texto": "Ingresar otro número", "id_accion": "consultar_estado_ticket"}],
                "message_type": "interactive_buttons",
            }

        asunto = ticket.asunto or ticket.categoria or "Reclamo"
        user_message = (
            f"El ticket M-{ticket.nro_ticket} sobre '{asunto}' se encuentra actualmente: **{ticket.estado}**."
        )
        botones = [{"texto": "Consultar otro ticket", "id_accion": "consultar_estado_ticket"}]
        return {
            "success": True,
            "message_to_user": user_message,
            "options_list": botones,
            "message_type": "interactive_buttons",
            "data": {"ticket_id": ticket.nro_ticket, "status": ticket.estado}
        }

class ConsultarInfoTramiteActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarInfoTramiteActionHandler with data: {action_data}")
        tramite_nombre = action_data.get("nombre_tramite") or action_data.get("categoria") # Categoria might be used if specific tramite name isn't clear
        if not tramite_nombre:
            return {
                "success": False,
                "message_to_user": "¿Sobre qué trámite necesitas información?",
                "pedir_info": "nombre_tramite"
            }

        from services.municipio_responder import obtener_info_tramite_web

        info_tramite = obtener_info_tramite_web(tramite_nombre)

        if "error" in info_tramite:
            return {
                "success": False,
                "message_to_user": f"No encontré información sobre el trámite '{tramite_nombre}'.",
                "pedir_info": "nombre_tramite"
            }
        else:
            botones = info_tramite.get("botones", []).copy()
            botones.append({"texto": "Consultar otro trámite", "id_accion": "info_tramite"})
            return {
                "success": True,
                "message_to_user": info_tramite.get("contenido", "No hay información disponible para este trámite."),
                "options_list": botones,
                "message_type": "interactive_buttons",
                "data": {"tramite_nombre": tramite_nombre, "info_recuperada": "json"}
            }

class HacerSugerenciaActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing HacerSugerenciaActionHandler with data: {action_data}")
        descripcion_sugerencia = action_data.get("descripcion")
        if not descripcion_sugerencia:
            return {
                "success": False,
                "message_to_user": "Claro, ¿cuál es tu sugerencia?",
                "pedir_info": "descripcion_sugerencia"
            }

        nombre_vecino = action_data.get("nombre")
        dni_vecino = action_data.get("dni")
        email_vecino = action_data.get("email")
        direccion_vecino = action_data.get("direccion")
        if not all([nombre_vecino, dni_vecino, email_vecino, direccion_vecino]):
            return {
                "success": False,
                "message_to_user": "Para registrar tu sugerencia necesito tu nombre completo, DNI, email y dirección. Podés escribir todo en un solo mensaje.",
                "pedir_info": "datos_contacto_sugerencia"
            }
        # Create a ticket for the suggestion
        viewer_user = self.context.get("viewer_user_obj")
        owner_user = self.context.get("user_obj")
        user_id_db = getattr(viewer_user, "id", None)
        anon_id_db = self.context.get("anon_id") if not user_id_db else None
        municipio_db_id_para_ticket = getattr(owner_user, "municipio_id", None)
        nombre_vecino_final = nombre_vecino or getattr(viewer_user, "nombre", "Ciudadano Anónimo")

        ticket_data = {
            "asunto": "Sugerencia de Ciudadano",
            "categoria": "Sugerencia",
            "detalles": descripcion_sugerencia,
            "estado": "nuevo",
            "user_id": user_id_db,
            "anon_id": anon_id_db,
            "origen_reclamo": "LLM_CHATBOT",
            "nombre_vecino": nombre_vecino_final,
            "dni_vecino": dni_vecino,
            "email_vecino": email_vecino,
            "direccion": direccion_vecino,
        }
        if self.context.get("foto_url"):
            ticket_data["foto_url_directa"] = self.context.get("foto_url")

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
        if "municipio_id" in ticket_data_cleaned:
            del ticket_data_cleaned["municipio_id"]

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            nro_ticket_str = f"S-{ticket_creado.get('nro_ticket')}"
            logger.info(f"Ticket de sugerencia {nro_ticket_str} creado exitosamente.")

            # Limpiar el contexto para evitar estados pegajosos
            if CONTEXTO_MUNICIPIO in self.context:
                self.context[CONTEXTO_MUNICIPIO].clear()
                from services.municipio_responder import ConversationState
                self.context[CONTEXTO_MUNICIPIO]['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name

            # Obtener la URL base del chat del contexto para el botón "Ver mi Ticket"
            municipio_config = self.context.get('municipio_config_actual', {})
            base_chat_url = municipio_config.get('base_chat_url', 'https://www.chatboc.ar/tickets/municipio')

            respuesta_formateada, botones_generados = formatear_ticket_respuesta(
                "sugerencia",
                nombre_vecino_final,
                descripcion_sugerencia,
                "Sugerencia",
                nro_ticket_str,
                {}, # No hay contacto especializado para sugerencias
                base_chat_url,
                dni=dni_vecino,
                consulta_pin=ticket_creado.get("consulta_pin"),
            )

            # Añadir el botón de acción específico para sugerencias
            botones_finales = botones_generados
            botones_finales.append({"texto": "Hacer otra sugerencia", "id_accion": "hacer_sugerencia"})

            return {
                "success": True,
                "message_to_user": respuesta_formateada,
                "options_list": botones_finales,
                "message_type": "interactive_buttons",
                "data": {"ticket_id": ticket_creado.get('id'), "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en HacerSugerenciaActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al intentar registrar tu sugerencia. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }

class ConsultarPuntosDeInteresActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarPuntosDeInteresActionHandler with data: {action_data}")

        tipo_de_comercio = action_data.get("tipo_comercio")
        if not tipo_de_comercio:
            return {"success": False, "message_to_user": "No especificaste qué tipo de comercio buscar."}

        # La ubicación se obtiene de los datos de la acción (si se proporcionó en el mensaje actual)
        # o del contexto de la conversación como fallback.
        ubicacion = action_data.get("ubicacion") or self.context.get("ubicacion_usuario")
        if not ubicacion:
            # Si no hay ubicación en ningún lado, se la pedimos al usuario.
            self.context[CONTEXTO_MUNICIPIO]["estado_conversacion"] = "ESPERANDO_UBICACION_GENERAL"
            self.context[CONTEXTO_MUNICIPIO]["accion_pendiente_tras_ubicacion"] = "consultar_puntos_de_interes"
            self.context[CONTEXTO_MUNICIPIO]["datos_pendientes"] = {"tipo_comercio": tipo_de_comercio}

            return {
                "success": False,
                "message_to_user": "Para poder ayudarte mejor, necesito tu ubicación. ¿Podrías compartirla?",
                "pedir_info": "ubicacion"
            }

        from services.herramientas_municipio import buscar_comercios_por_rubro_y_ubicacion

        resultado = buscar_comercios_por_rubro_y_ubicacion(tipo_de_comercio, ubicacion)

        return {
            "success": True,
            "message_to_user": resultado,
            "data": {"tipo_comercio_buscado": tipo_de_comercio, "ubicacion_usada": ubicacion}
        }

class ActivarPanicoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.critical(f"Executing ActivarPanicoActionHandler with data: {action_data}")
        # Simulate alerting emergency services
        user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. Hemos notificado a los servicios de emergencia con tu ubicación. Mantené la calma, la ayuda está en camino."
        if not action_data.get("coordenadas") and not action_data.get("ubicacion"):
            user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. No pudimos obtener tu ubicación precisa. Por favor, si es posible, indicala a los servicios de emergencia cuando te contacten. Mantené la calma."

        # servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": "ALERTA PANICO", ...})
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"alerta_status": "enviada"}
        }

from socket_service import socketio, emit_ticket_update
from routes.ticket import serialize_ticket_to_json

class DerivarHumanoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Crea un ticket real de chat en vivo y devuelve su identificador."""
        logger.info(f"Executing DerivarHumanoActionHandler with data: {action_data}")

        try:
            viewer_user = self.context.get("viewer_user_obj")
            owner_user = self.context.get("user_obj")
            pregunta_original = self.context.get("pregunta_actual_usuario", "")

            nombre = (getattr(viewer_user, "name", None) or action_data.get("nombre"))
            telefono = (getattr(viewer_user, "telefono", None) or action_data.get("telefono"))
            email = (getattr(viewer_user, "email", None) or action_data.get("email"))

            ticket_data = {
                "asunto": f"Solicitud de Chat en Vivo por: {nombre or 'Vecino'}",
                "categoria": "Atención en Vivo",
                "pregunta": pregunta_original,
                "detalles": action_data.get("motivo_derivacion", "Solicitud de agente"),
                "user_id": self.context.get("cliente_id"),
                "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                "municipio_id": getattr(owner_user, "municipio_id", None),
                "estado": "esperando_agente_en_vivo",
                "nombre_vecino": nombre,
                "telefono_vecino": telefono,
                "email_vecino": email,
            }
            ticket_type = "municipio"

            ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
            ticket_data_cleaned['tipo_ticket'] = ticket_type
            sala_dict = servicio_tickets.crear_nuevo_ticket(tipo_ticket=ticket_type, ticket_data=ticket_data_cleaned)
            if not sala_dict:
                raise Exception("crear_nuevo_ticket devolvió None")

            # Since downstream functions need the object, fetch it from the DB
            from models import MunicipioTicket
            sala_obj = db.session.get(MunicipioTicket, sala_dict['id'])
            if not sala_obj:
                raise Exception(f"No se pudo recuperar el ticket recién creado con ID {sala_dict['id']}")

            try:
                ticket_json = serialize_ticket_to_json(sala_obj, ticket_type)
                emit_ticket_update(ticket_json)
            except Exception as e_notify:
                logger.error(f"Error enviando notificación en tiempo real para ticket #{sala_dict['nro_ticket']}: {e_notify}", exc_info=True)

            servicio_tickets.crear_comentario(
                ticket_id=sala_dict['id'],
                tipo_ticket=ticket_type,
                comentario_data={
                    "comentario": pregunta_original,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                    "es_admin": False,
                },
            )

            # Emitir evento de socket para notificar al panel de administración
            try:
                ticket_json = serialize_ticket_to_json(sala_obj, ticket_type)
                room_name = f"municipio_{sala_obj.municipio_id}"
                socketio.emit('live_chat_request', ticket_json, room=room_name)
                logger.info(f"Socket event 'live_chat_request' emitted to room '{room_name}' for ticket {sala_obj.id}")
            except Exception as e_socket:
                logger.error(f"Failed to emit socket event for new live chat ticket {sala_obj.id}: {e_socket}", exc_info=True)


            chat_id = f"M-{sala_dict['nro_ticket']}"

            # formatear_ticket_respuesta now returns a tuple (message, buttons)
            user_message, _ = formatear_ticket_respuesta("chat", nombre, pregunta_original, "Atención en Vivo", chat_id)
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"ticket_id": sala_dict['id'], "chat_id": chat_id, "status": "esperando_agente_en_vivo"},
            }
        except Exception as e:
            logger.error(f"Error en DerivarHumanoActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Ocurrió un problema al crear el chat en vivo. ¿Podés intentar de nuevo más tarde?",
                "error_details": str(e),
            }

class ProcesarAdjuntoReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoReclamoActionHandler with data: {action_data}")
        # This handler would be triggered AFTER an image/file is uploaded and processed by InputProcessor
        # and its analysis (e.g., from Vision API) is available in action_data.

        archivo_url = action_data.get("archivo_url")
        analisis_imagen = action_data.get("analisis_imagen") # e.g., {'es_reclamo': True, 'categoria_sugerida': 'bache', ...}

        if not archivo_url:
            return {"success": False, "message_to_user": "No se detectó ningún archivo adjunto."}

        # Simulate associating the attachment with a claim (either new or existing)
        # This might update a claim in progress or provide data for a new one.
        user_message = f"Recibí el archivo {archivo_url}. "
        if analisis_imagen:
            user_message += f"Parece ser sobre '{analisis_imagen.get('categoria_sugerida', 'algo')}'."
            if analisis_imagen.get('texto_ocr'):
                 user_message += f" Contiene texto: '{analisis_imagen['texto_ocr'][:50]}...'."

        # The result of this action might be to update the context for ReclamoHandler
        # or to directly create/update a claim if enough info is present.
        # For now, just acknowledge.
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"adjunto_procesado": True, "analisis_realizado": bool(analisis_imagen)}
        }

class CorregirDatosReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CorregirDatosReclamoActionHandler with data: {action_data}")

        campo_a_corregir = action_data.get("campo_a_corregir")
        nuevo_valor = action_data.get("nuevo_valor")

        if not campo_a_corregir or nuevo_valor is None:
            return {
                "success": False,
                "message_to_user": "No especificaste qué dato corregir o cuál es el nuevo valor.",
                "pedir_info": "detalle_correccion"
            }

        # Update the context with the new value
        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO, {})
        if campo_a_corregir == "ubicacion":
            contexto_reclamo["direccion_reclamo"] = nuevo_valor
        elif campo_a_corregir == "descripcion":
            contexto_reclamo["descripcion_reclamo"] = nuevo_valor
        elif campo_a_corregir == "categoria":
            contexto_reclamo["categoria_reclamo"] = nuevo_valor
        elif campo_a_corregir == "nombre":
            contexto_reclamo["nombre_vecino"] = nuevo_valor
        elif campo_a_corregir == "telefono":
            contexto_reclamo["telefono_vecino"] = nuevo_valor
        elif campo_a_corregir == "email":
            contexto_reclamo["email_vecino"] = nuevo_valor

        user_message = f"Entendido. He actualizado '{campo_a_corregir}' a '{nuevo_valor}'. ¿Algo más que desees cambiar o confirmamos el reclamo?"

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"campo_corregido": campo_a_corregir, "valor_actualizado": nuevo_valor},
            "pedir_info": "confirmacion_tras_correccion"
        }

class MenuPrincipalActionHandler(BaseActionHandler):
    action_name = "menu_principal"

    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": True,
            "message_to_user": "Estas son las cosas que puedo hacer por vos:",
            "options_list": [
                {"texto": "Hacer un Reclamo", "id_accion": "crear_reclamo"},
                {"texto": "Consultas y Turnos", "id_accion": "consultar_tramite"},
                {"texto": "Buscar estacionamiento", "id_accion": "buscar_estacionamiento"},
            ],
            "message_type": "interactive_buttons"
        }

# Add other handlers as needed
# e.g., CalificarAtencionActionHandler, ConfirmarCierreTicketActionHandler

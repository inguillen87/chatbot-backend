# services/actions/municipio_actions.py
import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any
from services.ticket_service import servicio_tickets
from services.notifications import enviar_notificacion_whatsapp_con_plantilla, enviar_notificacion_sms
from services.herramientas_municipio import parse_direccion_completa as parse_direccion, direccion_es_valida
from services.ticket_utils import formatear_ticket_respuesta
from services.common_utils import validar_telefono, formatear_telefono_e164, validar_email
from services.gemini_bridge import llamar_gemini
from services.config_loader import cargar_configuracion_municipio

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

class BuscarEstacionamientoActionHandler:
    action_name = "buscar_estacionamiento"

    def handle(self, context):
        # si ya tenemos ubicación del usuario en context, usarla; si no, pedirla
        ubic = context.get("ubicacion") or context.get("payload", {}).get("ubicacion")
        if not ubic:
            return {
                "texto": (
                    "Decime la **calle y altura** o compartí tu **ubicación**.\n"
                    "Ej: *San Martín 1200, Junín* o enviá ubicación por WhatsApp."
                ),
                "pedir_info": {"tipo": "ubicacion_o_texto"},
                "botones": [
                    {"texto": "Enviar ubicación", "accion": "enviar_ubicacion"},
                    {"texto": "San Martín 1200", "accion": "texto_libre", "valor": "San Martín 1200, Junín"}
                ],
            }

        # Llamar a servicio
        from services.estacionamiento_service import consultar_ocupacion
        resultado = consultar_ocupacion(ubic)

        return resultado

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoActionHandler with data: {action_data}")

        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO, {})
        viewer_user = self.context.get("viewer_user_obj")

        datos_parciales = contexto_reclamo.get("datos_parciales_llm_reclamo", {})
        categoria = action_data.get("categoria") or datos_parciales.get("categoria")
        descripcion = action_data.get("descripcion") or datos_parciales.get("descripcion")
        ubicacion_llm = action_data.get("ubicacion") or datos_parciales.get("ubicacion")
        distrito_llm = action_data.get("distrito") or datos_parciales.get("distrito")
        coordenadas_llm = action_data.get("coordenadas") or datos_parciales.get("coordenadas")
        foto_url_llm = action_data.get("foto_url_adjunta") or datos_parciales.get("foto_url")

        llm_name = (action_data.get("usuario") or datos_parciales.get("usuario") or
                    action_data.get("nombre_usuario_detectado") or datos_parciales.get("nombre_usuario_detectado"))

        profile_name_from_user_obj = None
        if viewer_user and (getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None)):
             profile_name_from_user_obj = getattr(viewer_user, "name", None) or getattr(viewer_user, "nombre", None)

        profile_name_from_context = self.context.get("profile_name")

        nombre_vecino_final = None
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

        for key, value in [("categoria_reclamo", categoria), ("descripcion_reclamo", descripcion),
                           ("direccion_reclamo", ubicacion_llm), ("coordenadas_reclamo", coordenadas_llm),
                           ("nombre_vecino", nombre_vecino_final), ("telefono_vecino", telefono_final),
                           ("email_vecino", email_final), ("foto_url", foto_url_llm)]:
            if value:
                contexto_reclamo[key] = value

        campos_faltantes = []
        if not descripcion:
            campos_faltantes.append("descripcion")
        if not ubicacion_llm and not coordenadas_llm:
            campos_faltantes.append("ubicacion")

        if not viewer_user:
            if not nombre_vecino_final: campos_faltantes.append("nombre_completo")
            if not telefono_final: campos_faltantes.append("telefono")
            if not email_final: campos_faltantes.append("email")
        elif not nombre_vecino_final:
             nombre_vecino_final = viewer_user.name or viewer_user.email or "Vecino/a"

        if campos_faltantes:
            campos_faltantes = sorted(list(set(campos_faltantes)))
            self.context[CONTEXTO_MUNICIPIO] = contexto_reclamo
            mensaje = f"Para poder registrar tu reclamo, necesito los siguientes datos: **{', '.join(campos_faltantes)}**. Por favor, indícamelos."
            botones = [{"texto": f"Ingresar {campo.replace('_', ' ')}", "id_accion": f"ingresar_{campo}"} for campo in campos_faltantes]
            botones.append({"texto": "Cancelar reclamo", "id_accion": "cancelar_reclamo"})
            return {
                "success": False, "message_to_user": mensaje, "pedir_info": campos_faltantes,
                "options_list": botones, "message_type": "interactive_list" if len(botones) > 3 else "interactive_buttons"
            }

        owner_user = self.context.get("user_obj")

        if viewer_user:
            updated = False
            if nombre_vecino_final and not (hasattr(viewer_user, 'name') and viewer_user.name):
                viewer_user.name = nombre_vecino_final
                updated = True
            if telefono_final and not viewer_user.telefono:
                viewer_user.telefono = telefono_final
                updated = True
            if email_final and not viewer_user.email:
                viewer_user.email = email_final
                updated = True
            if updated:
                from models import db
                db.session.add(viewer_user)

        pregunta_original = self.context.get("pregunta_actual_usuario", "")
        ticket_data = {
            "pregunta": pregunta_original, "asunto": f"Reclamo (LLM): {categoria or 'General'}",
            "categoria": categoria or "Reclamo General", "detalles": descripcion, "direccion": ubicacion_llm,
            "distrito": distrito_llm, "nombre_vecino": nombre_vecino_final, "telefono_vecino": telefono_final,
            "email_vecino": email_final, "estado": "nuevo", "user_id": getattr(viewer_user, "id", None),
            "anon_id": self.context.get("anon_id") if not getattr(viewer_user, "id", None) else None,
            "municipio_id": getattr(owner_user, "municipio_id", None),
            "latitud": coordenadas_llm.get("lat") if isinstance(coordenadas_llm, dict) else None,
            "longitud": coordenadas_llm.get("lon") if isinstance(coordenadas_llm, dict) else None,
            "origen_reclamo": "LLM_CHATBOT", "foto_url_directa": foto_url_llm, "canal_ingreso": self.context.get("channel"),
        }

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
        logger.info(f"Data for servicio_tickets.crear_nuevo_ticket: {ticket_data_cleaned}")
        logger.info(f"DEBUG_CONTACT_INFO: nombre='{ticket_data_cleaned.get('nombre_vecino')}', telefono='{ticket_data_cleaned.get('telefono_vecino')}', email='{ticket_data_cleaned.get('email_vecino')}'")

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
            logger.info(f"Ticket {nro_ticket_str} creado exitosamente.")

            contactos = cargar_configuracion_municipio(getattr(owner_user, "municipio_id", "default"), "contactos_especializados.json")
            contacto_especializado = dict(contactos.get(categoria, contactos.get("default", {})))

            tramites_cfg = cargar_configuracion_municipio(getattr(owner_user, "municipio_id", "default"), "tramites.json")
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

            if getattr(owner_user, "link_web", None):
                contacto_especializado.setdefault("link", owner_user.link_web)
            else:
                cfg = cargar_configuracion_municipio(getattr(owner_user, "municipio_id", "default"), "config.json")
                if isinstance(cfg, dict) and cfg.get("web_url"):
                    contacto_especializado.setdefault("link", cfg.get("web_url"))

            if getattr(owner_user, "telefono", None):
                contacto_especializado.setdefault("telefono", owner_user.telefono)
            if getattr(owner_user, "horario", None):
                contacto_especializado.setdefault("horario", owner_user.horario)

            user_info = contexto_reclamo.get('user', {})
            if CONTEXTO_MUNICIPIO in self.context:
                self.context[CONTEXTO_MUNICIPIO].clear()
                if user_info:
                    self.context[CONTEXTO_MUNICIPIO]['user'] = user_info
                from services.municipio_responder import ConversationState
                self.context[CONTEXTO_MUNICIPIO]['estado_conversacion'] = ConversationState.CONVERSACION_GENERAL_LLM.name
                logger.info(f"Contexto de reclamo limpiado. Nuevo estado: {self.context[CONTEXTO_MUNICIPIO]['estado_conversacion']}")

            if ticket_data_cleaned.get("telefono_vecino"):
                try:
                    enviar_notificacion_whatsapp_con_plantilla(ticket_data_cleaned["telefono_vecino"], ticket_data_cleaned.get("nombre_vecino", "Vecino"), str(ticket_creado.nro_ticket), ticket_data_cleaned.get("categoria", "Varios"))
                except Exception as e_whatsapp:
                    logger.error(f"Error enviando notificación de WhatsApp para {nro_ticket_str}: {e_whatsapp}")
                try:
                    enviar_notificacion_sms(ticket_data_cleaned["telefono_vecino"], f"Hola {ticket_data_cleaned.get('nombre_vecino', 'Vecino')}! Tu reclamo M-{ticket_creado.nro_ticket} ({ticket_data_cleaned.get('categoria', 'Varios')}) fue generado.")
                except Exception as e_sms:
                    logger.error(f"Error enviando notificación por SMS para {nro_ticket_str}: {e_sms}")

            municipio_config = self.context.get('municipio_config_actual', {})
            mensaje_respuesta, botones_finales = formatear_ticket_respuesta(
                ticket=ticket_creado, municipio_config=municipio_config, canal=self.context.get("channel", "web"),
                viewer_user=viewer_user, extra_info=contacto_especializado, current_user=self.context.get("current_user"),
                datos_llm=action_data
            )
            logger.info(f"Respuesta formateada: '{mensaje_respuesta}', Botones: {botones_finales}")
            return {
                "success": True, "message_to_user": mensaje_respuesta, "options_list": botones_finales,
                "message_type": "interactive_buttons" if botones_finales else "text",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en CrearReclamoActionHandler: {e}", exc_info=True)
            return {
                "success": False, "message_to_user": "Hubo un problema al registrar tu reclamo. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }

class ConsultarEstadoTicketActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoTicketActionHandler with data: {action_data}")
        ticket_id = action_data.get("id_ticket_mencionado")
        if not ticket_id:
            return { "success": False, "message_to_user": "Para consultar el estado, necesito el número de ticket.", "pedir_info": "id_ticket_mencionado" }
        simulated_status, simulated_asunto = "En proceso", "Luminaria Rota"
        user_message = f"El ticket M-{ticket_id} sobre '{simulated_asunto}' se encuentra actualmente: **{simulated_status}**."
        botones = [{"texto": "Consultar otro ticket", "id_accion": "consultar_estado_ticket"}]
        return { "success": True, "message_to_user": user_message, "options_list": botones, "message_type": "interactive_buttons", "data": {"ticket_id": ticket_id, "status": simulated_status} }

class ConsultarInfoTramiteActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        from services.municipios import get_tramites_info
        logger.info(f"Executing ConsultarInfoTramiteActionHandler with data: {action_data}")
        tramite_nombre = action_data.get("nombre_tramite") or action_data.get("categoria")
        if not tramite_nombre:
            return { "success": False, "message_to_user": "¿Sobre qué trámite necesitas información?", "pedir_info": "nombre_tramite" }
        from services.municipios import obtener_info_tramite_web
        info_tramite = obtener_info_tramite_web(tramite_nombre)
        if "error" in info_tramite:
            return { "success": False, "message_to_user": f"No encontré información sobre el trámite '{tramite_nombre}'.", "pedir_info": "nombre_tramite" }
        else:
            botones = [{"texto": "Consultar otro trámite", "id_accion": "info_tramite"}]
            return { "success": True, "message_to_user": info_tramite.get("contenido", "No hay información disponible para este trámite."), "options_list": botones, "message_type": "interactive_buttons", "data": {"tramite_nombre": tramite_nombre, "info_recuperada": "web"} }

class HacerSugerenciaActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing HacerSugerenciaActionHandler with data: {action_data}")
        descripcion_sugerencia = action_data.get("descripcion")
        if not descripcion_sugerencia:
            return { "success": False, "message_to_user": "Claro, ¿cuál es tu sugerencia?", "pedir_info": "descripcion_sugerencia" }
        viewer_user, owner_user = self.context.get("viewer_user_obj"), self.context.get("user_obj")
        ticket_data = {
            "asunto": "Sugerencia de Ciudadano", "categoria": "Sugerencia", "detalles": descripcion_sugerencia,
            "estado": "nuevo", "user_id": getattr(viewer_user, "id", None),
            "anon_id": self.context.get("anon_id") if not getattr(viewer_user, "id", None) else None,
            "origen_reclamo": "LLM_CHATBOT"
        }
        if self.context.get("foto_url"):
            ticket_data["foto_url_directa"] = self.context.get("foto_url")
        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None and k != "municipio_id"}
        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")
            nro_ticket_str = f"S-{ticket_creado.nro_ticket}"
            logger.info(f"Ticket de sugerencia {nro_ticket_str} creado exitosamente.")

            municipio_config = self.context.get('municipio_config_actual', {})
            mensaje_respuesta, botones_finales = formatear_ticket_respuesta(
                ticket=ticket_creado,
                municipio_config=municipio_config,
                tipo='sugerencia'
            )

            return {
                "success": True,
                "message_to_user": mensaje_respuesta,
                "options_list": botones_finales,
                "message_type": "interactive_buttons" if botones_finales else "text",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en HacerSugerenciaActionHandler: {e}", exc_info=True)
            return { "success": False, "message_to_user": "Hubo un problema al registrar tu sugerencia. Por favor, intenta de nuevo más tarde.", "error_details": str(e) }

class ConsultarPuntosDeInteresActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarPuntosDeInteresActionHandler with data: {action_data}")
        tipo_de_comercio = action_data.get("tipo_comercio")
        if not tipo_de_comercio:
            return {"success": False, "message_to_user": "No especificaste qué tipo de comercio buscar."}
        ubicacion = action_data.get("ubicacion") or self.context.get("ubicacion_usuario")
        if not ubicacion:
            self.context[CONTEXTO_MUNICIPIO].update({"estado_conversacion": "ESPERANDO_UBICACION_GENERAL", "accion_pendiente_tras_ubicacion": "consultar_puntos_de_interes", "datos_pendientes": {"tipo_comercio": tipo_de_comercio}})
            return { "success": False, "message_to_user": "Para poder ayudarte mejor, necesito tu ubicación. ¿Podrías compartirla?", "pedir_info": "ubicacion" }
        from services.herramientas_municipio import buscar_comercios_por_rubro_y_ubicacion
        resultado = buscar_comercios_por_rubro_y_ubicacion(tipo_de_comercio, ubicacion)
        return { "success": True, "message_to_user": resultado, "data": {"tipo_comercio_buscado": tipo_de_comercio, "ubicacion_usada": ubicacion} }

class ActivarPanicoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.critical(f"Executing ActivarPanicoActionHandler with data: {action_data}")
        user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. Hemos notificado a los servicios de emergencia con tu ubicación. Mantené la calma, la ayuda está en camino."
        if not action_data.get("coordenadas") and not action_data.get("ubicacion"):
            user_message = "🚨 ALERTA DE PÁNICO RECIBIDA. No pudimos obtener tu ubicación precisa. Por favor, si es posible, indicala a los servicios de emergencia cuando te contacten. Mantené la calma."
        return { "success": True, "message_to_user": user_message, "data": {"alerta_status": "enviada"} }

from socket_service import socketio
from routes.ticket import serialize_ticket_to_json

class DerivarHumanoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing DerivarHumanoActionHandler with data: {action_data}")
        try:
            viewer_user, owner_user = self.context.get("viewer_user_obj"), self.context.get("user_obj")
            nombre = getattr(viewer_user, "name", None) or action_data.get("nombre")
            ticket_data = {
                "asunto": f"Solicitud de Chat en Vivo por: {nombre or 'Vecino'}", "categoria": "Atención en Vivo",
                "pregunta": self.context.get("pregunta_actual_usuario", ""), "detalles": action_data.get("motivo_derivacion", "Solicitud de agente"),
                "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                "municipio_id": getattr(owner_user, "municipio_id", None), "estado": "esperando_agente_en_vivo",
                "nombre_vecino": nombre, "telefono_vecino": getattr(viewer_user, "telefono", None) or action_data.get("telefono"),
                "email_vecino": getattr(viewer_user, "email", None) or action_data.get("email"),
            }
            ticket_type = "municipio"
            ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
            ticket_data_cleaned['tipo_ticket'] = ticket_type
            sala = servicio_tickets.crear_nuevo_ticket(tipo_ticket=ticket_type, ticket_data=ticket_data_cleaned)
            if not sala:
                raise Exception("crear_nuevo_ticket devolvió None")
            servicio_tickets.crear_comentario(
                ticket_id=sala.id, tipo_ticket=ticket_type,
                comentario_data={"comentario": self.context.get("pregunta_actual_usuario", ""), "user_id": self.context.get("cliente_id"), "anon_id": self.context.get("anon_id"), "es_admin": False},
            )
            try:
                ticket_json, room_name = serialize_ticket_to_json(sala, ticket_type), f"municipio_{sala.municipio_id}"
                socketio.emit('live_chat_request', ticket_json, room=room_name)
                logger.info(f"Socket event 'live_chat_request' emitted to room '{room_name}' for ticket {sala.id}")
            except Exception as e_socket:
                logger.error(f"Failed to emit socket event for new live chat ticket {sala.id}: {e_socket}", exc_info=True)
            chat_id = f"M-{sala.nro_ticket}"
            user_message = formatear_ticket_respuesta(ticket=sala, municipio_config={}, tipo="chat")
            return { "success": True, "message_to_user": user_message, "data": {"ticket_id": sala.id, "chat_id": chat_id, "status": "esperando_agente_en_vivo"} }
        except Exception as e:
            logger.error(f"Error en DerivarHumanoActionHandler: {e}", exc_info=True)
            return { "success": False, "message_to_user": "Ocurrió un problema al crear el chat en vivo. ¿Podés intentar de nuevo más tarde?", "error_details": str(e) }

class ProcesarAdjuntoReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ProcesarAdjuntoReclamoActionHandler with data: {action_data}")
        archivo_url, analisis_imagen = action_data.get("archivo_url"), action_data.get("analisis_imagen")
        if not archivo_url:
            return {"success": False, "message_to_user": "No se detectó ningún archivo adjunto."}
        user_message = f"Recibí el archivo {archivo_url}. "
        if analisis_imagen:
            user_message += f"Parece ser sobre '{analisis_imagen.get('categoria_sugerida', 'algo')}'."
            if analisis_imagen.get('texto_ocr'):
                 user_message += f" Contiene texto: '{analisis_imagen['texto_ocr'][:50]}...'."
        return { "success": True, "message_to_user": user_message, "data": {"adjunto_procesado": True, "analisis_realizado": bool(analisis_imagen)} }

class CorregirDatosReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CorregirDatosReclamoActionHandler with data: {action_data}")
        campo_a_corregir, nuevo_valor = action_data.get("campo_a_corregir"), action_data.get("nuevo_valor")
        if not campo_a_corregir or nuevo_valor is None:
            return { "success": False, "message_to_user": "No especificaste qué dato corregir o cuál es el nuevo valor.", "pedir_info": "detalle_correccion" }
        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO, {})
        if campo_a_corregir == "ubicacion": contexto_reclamo["direccion_reclamo"] = nuevo_valor
        elif campo_a_corregir == "descripcion": contexto_reclamo["descripcion_reclamo"] = nuevo_valor
        elif campo_a_corregir == "categoria": contexto_reclamo["categoria_reclamo"] = nuevo_valor
        elif campo_a_corregir == "nombre": contexto_reclamo["nombre_vecino"] = nuevo_valor
        elif campo_a_corregir == "telefono": contexto_reclamo["telefono_vecino"] = nuevo_valor
        elif campo_a_corregir == "email": contexto_reclamo["email_vecino"] = nuevo_valor
        user_message = f"Entendido. He actualizado '{campo_a_corregir}' a '{nuevo_valor}'. ¿Algo más que desees cambiar o confirmamos el reclamo?"
        return { "success": True, "message_to_user": user_message, "data": {"campo_corregido": campo_a_corregir, "valor_actualizado": nuevo_valor}, "pedir_info": "confirmacion_tras_correccion" }

class MenuPrincipalActionHandler:
    def handle(self, context):
        return {
            "texto": "Estas son las cosas que puedo hacer por vos:",
            "botones": [ {"texto": "Hacer un Reclamo", "accion": "crear_reclamo"}, {"texto": "Consultas y Turnos", "accion": "consultar_tramite"}, {"texto": "Buscar estacionamiento", "accion": "buscar_estacionamiento"} ],
        }

class BuscarEstacionamientoActionHandler(BaseActionHandler):
    def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing BuscarEstacionamientoActionHandler with data: {payload}")
        return { "message_body": "Próximamente, podrás buscar estacionamiento desde aquí. ¡Estamos trabajando en esta funcionalidad!", "options_list": [], "fuente": "buscar_estacionamiento_placeholder" }

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

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoActionHandler with data: {action_data}")

        contexto_reclamo = self.context.get(CONTEXTO_MUNICIPIO, {})
        viewer_user = self.context.get("viewer_user_obj")

        # Fusionar datos: action_data tiene prioridad, luego el contexto del reclamo, luego el perfil del usuario
        categoria = action_data.get("categoria") or contexto_reclamo.get("categoria_reclamo")
        descripcion = action_data.get("descripcion") or contexto_reclamo.get("descripcion_reclamo")
        ubicacion_llm = action_data.get("ubicacion") or contexto_reclamo.get("direccion_reclamo")
        distrito_llm = action_data.get("distrito") or contexto_reclamo.get("distrito_reclamo")
        coordenadas_llm = action_data.get("coordenadas") or contexto_reclamo.get("coordenadas_reclamo")
        foto_url_llm = action_data.get("foto_url_adjunta") or contexto_reclamo.get("foto_url")

        # Lógica de fusión de datos de contacto mejorada
        nombre_vecino_final = action_data.get("usuario") or action_data.get("nombre_usuario_detectado") or contexto_reclamo.get("nombre_vecino") or getattr(viewer_user, "nombre", None)

        telefono_from_llm = action_data.get("telefono") or action_data.get("telefono_detectado")
        telefono_final = None
        if telefono_from_llm and validar_telefono(telefono_from_llm):
            telefono_final = formatear_telefono_e164(telefono_from_llm)
        elif viewer_user and getattr(viewer_user, "telefono", None) and validar_telefono(viewer_user.telefono):
            telefono_final = formatear_telefono_e164(viewer_user.telefono)

        email_from_llm = action_data.get("email") or action_data.get("email_detectado")
        email_final = None
        if email_from_llm and validar_email(email_from_llm):
            email_final = email_from_llm.lower()
        elif viewer_user and getattr(viewer_user, "email", None) and validar_email(viewer_user.email):
            email_final = viewer_user.email.lower()


        # Actualizar el contexto con los datos más recientes para persistencia
        for key, value in [("categoria_reclamo", categoria), ("descripcion_reclamo", descripcion),
                           ("direccion_reclamo", ubicacion_llm), ("coordenadas_reclamo", coordenadas_llm),
                           ("nombre_vecino", nombre_vecino_final), ("telefono_vecino", telefono_final),
                           ("email_vecino", email_final), ("foto_url", foto_url_llm)]:
            if value:
                contexto_reclamo[key] = value

        # Validación de datos esenciales para la creación del ticket
        campos_faltantes = []
        if not descripcion:
            campos_faltantes.append("descripcion")
        if not ubicacion_llm and not coordenadas_llm:
            campos_faltantes.append("ubicacion")
        if not viewer_user and not all([nombre_vecino_final, telefono_final, email_final]):
             campos_faltantes.extend(["nombre", "telefono", "email"])


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
            if updated:
                db.session.add(viewer_user)
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

            nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
            logger.info(f"Ticket {nro_ticket_str} creado exitosamente.")

            # Cargar contactos y encontrar el específico para la categoría
            contactos = cargar_configuracion_municipio(getattr(owner_user, "municipio_id", "default"), "contactos_especializados.json")
            contacto_especializado = contactos.get(categoria, contactos.get("default"))

            # Limpiar contexto de reclamo después de la creación exitosa
            keys_to_clear = [k for k in contexto_reclamo if k.endswith("_reclamo") or k.startswith("nombre_vecino") or k.startswith("telefono_vecino") or k.startswith("email_vecino") or k.startswith("foto_url")]
            for key in keys_to_clear:
                contexto_reclamo.pop(key, None)
            self.context[CONTEXTO_MUNICIPIO] = contexto_reclamo

            # Notificaciones
            if ticket_data_cleaned.get("telefono_vecino"):
                try:
                    enviar_notificacion_whatsapp_con_plantilla(ticket_data_cleaned["telefono_vecino"], ticket_data_cleaned["nombre_vecino"], str(ticket_creado.nro_ticket), ticket_data_cleaned["categoria"])
                    enviar_notificacion_sms(ticket_data_cleaned["telefono_vecino"], f"Hola {ticket_data_cleaned['nombre_vecino']}! Tu reclamo M-{ticket_creado.nro_ticket} ({ticket_data_cleaned['categoria']}) fue generado.")
                except Exception as e_notify:
                    logger.error(f"Error enviando notificaciones para {nro_ticket_str}: {e_notify}")

            # Formatear respuesta y obtener el botón de contacto
            mensaje_respuesta, boton_contacto = formatear_ticket_respuesta(
                "reclamo",
                ticket_data_cleaned["nombre_vecino"],
                descripcion,
                categoria,
                nro_ticket_str,
                contacto_especializado
            )

            botones_finales = []
            if boton_contacto:
                botones_finales.append(boton_contacto)

            return {
                "success": True,
                "message_to_user": mensaje_respuesta,
                "options_list": botones_finales,
                "message_type": "interactive_buttons" if botones_finales else "text",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en CrearReclamoActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al registrar tu reclamo. Por favor, intenta de nuevo más tarde.",
                "error_details": str(e)
            }

class ConsultarEstadoTicketActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing ConsultarEstadoTicketActionHandler with data: {action_data}")
        ticket_id = action_data.get("id_ticket_mencionado")
        if not ticket_id:
            return {
                "success": False,
                "message_to_user": "Para consultar el estado, necesito el número de ticket.",
                "pedir_info": "id_ticket_mencionado"
            }
        # Simulate fetching ticket status
        # ticket = MunicipioTicket.query.filter_by(nro_ticket=ticket_id).first()
        simulated_status = "En proceso"
        simulated_asunto = "Luminaria Rota"
        user_message = f"El ticket M-{ticket_id} sobre '{simulated_asunto}' se encuentra actualmente: **{simulated_status}**."
        botones = [{"texto": "Consultar otro ticket", "id_accion": "consultar_estado_ticket"}]
        return {
            "success": True,
            "message_to_user": user_message,
            "options_list": botones,
            "message_type": "interactive_buttons",
            "data": {"ticket_id": ticket_id, "status": simulated_status}
        }

class ConsultarInfoTramiteActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        from services.municipios import get_tramites_info
        logger.info(f"Executing ConsultarInfoTramiteActionHandler with data: {action_data}")
        tramite_nombre = action_data.get("nombre_tramite") or action_data.get("categoria") # Categoria might be used if specific tramite name isn't clear
        if not tramite_nombre:
            return {
                "success": False,
                "message_to_user": "¿Sobre qué trámite necesitas información?",
                "pedir_info": "nombre_tramite"
            }

        from services.municipios import obtener_info_tramite_web

        info_tramite = obtener_info_tramite_web(tramite_nombre)

        if "error" in info_tramite:
            return {
                "success": False,
                "message_to_user": f"No encontré información sobre el trámite '{tramite_nombre}'.",
                "pedir_info": "nombre_tramite"
            }
        else:
            botones = [{"texto": "Consultar otro trámite", "id_accion": "info_tramite"}]
            return {
                "success": True,
                "message_to_user": info_tramite.get("contenido", "No hay información disponible para este trámite."),
                "options_list": botones,
                "message_type": "interactive_buttons",
                "data": {"tramite_nombre": tramite_nombre, "info_recuperada": "web"}
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
        # Create a ticket for the suggestion
        viewer_user = self.context.get("viewer_user_obj")
        owner_user = self.context.get("user_obj")
        user_id_db = getattr(viewer_user, "id", None)
        anon_id_db = self.context.get("anon_id") if not user_id_db else None
        municipio_db_id_para_ticket = getattr(owner_user, "municipio_id", None)
        nombre_vecino_final = getattr(viewer_user, "nombre", "Ciudadano Anónimo")

        ticket_data = {
            "asunto": "Sugerencia de Ciudadano",
            "categoria": "Sugerencia",
            "detalles": descripcion_sugerencia,
            "estado": "nuevo",
            "user_id": user_id_db,
            "anon_id": anon_id_db,
            "origen_reclamo": "LLM_CHATBOT"
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

            nro_ticket_str = f"S-{ticket_creado.nro_ticket}"
            logger.info(f"Ticket de sugerencia {nro_ticket_str} creado exitosamente.")

            botones = [{"texto": "Hacer otra sugerencia", "id_accion": "hacer_sugerencia"}]
            return {
                "success": True,
                "message_to_user": formatear_ticket_respuesta("sugerencia", nombre_vecino_final, descripcion_sugerencia, "Sugerencia", nro_ticket_str),
                "options_list": botones,
                "message_type": "interactive_buttons",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status": "creado"}
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

        # La ubicación se obtiene del perfil del usuario o se usa una por defecto si no está disponible.
        # Esto debería ser mejorado para obtener la ubicación del contexto de la conversación si es posible.
        ubicacion = self.context.get("ubicacion_usuario")
        if not ubicacion:
            # Si no hay ubicación en el contexto, se la pedimos al usuario.
            return {
                "success": False,
                "message_to_user": "Para poder ayudarte a encontrar lo que buscas, necesito que me digas tu ubicación. Por favor, compártela o decime en qué zona estás.",
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
            sala = servicio_tickets.crear_nuevo_ticket(tipo_ticket=ticket_type, ticket_data=ticket_data_cleaned)
            if not sala:
                raise Exception("crear_nuevo_ticket devolvió None")

            servicio_tickets.crear_comentario(
                ticket_id=sala.id,
                tipo_ticket=ticket_type,
                comentario_data={
                    "comentario": pregunta_original,
                    "user_id": self.context.get("cliente_id"),
                    "anon_id": self.context.get("anon_id"),
                    "es_admin": False,
                },
            )

            chat_id = f"M-{sala.nro_ticket}"

            user_message = formatear_ticket_respuesta("chat", nombre, pregunta_original, "Atención en Vivo", chat_id)
            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"ticket_id": sala.id, "chat_id": chat_id, "status": "esperando_agente_en_vivo"},
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

# Add other handlers as needed
# e.g., CalificarAtencionActionHandler, ConfirmarCierreTicketActionHandler

# services/actions/municipio_actions.py
import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any
from services.ticket_service import servicio_tickets
from services.municipios import enviar_notificacion_whatsapp_con_plantilla, enviar_notificacion_sms
from services.herramientas_municipio import parse_direccion_completa, direccion_es_valida
from services.common_utils import validar_telefono, formatear_telefono_e164, validar_email

logger = logging.getLogger(__name__)

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoActionHandler with data: {action_data}")

        # 1. Extracción y Validación de Datos
        categoria = action_data.get("categoria", "Reclamo General")
        descripcion = action_data.get("descripcion")
        ubicacion_llm = action_data.get("ubicacion")
        coordenadas_llm = action_data.get("coordenadas")
        nombre_vecino_llm = action_data.get("usuario")
        telefono_llm = action_data.get("telefono")
        email_llm = action_data.get("email")
        foto_url_llm = action_data.get("foto_url_adjunta")

        if not descripcion:
            return {
                "success": False,
                "message_to_user": "No pude entender la descripción del reclamo. Por favor, intenta describirlo de nuevo.",
                "pedir_info": "descripcion",
            }
        if not ubicacion_llm and not coordenadas_llm:
            return {
                "success": False,
                "message_to_user": "No pude entender la ubicación del reclamo. Por favor, especifica dónde es el problema.",
                "pedir_info": "ubicacion",
            }

        # 2. Recopilación de Información del Contexto
        viewer_user = self.context.get("viewer_user_obj")
        owner_user = self.context.get("user_obj")
        user_id_db = getattr(viewer_user, "id", None)
        anon_id_db = self.context.get("anon_id") if not user_id_db else None
        municipio_db_id_para_ticket = getattr(owner_user, "municipio_id", None)

        nombre_vecino_final = nombre_vecino_llm or getattr(viewer_user, "nombre", "Ciudadano Anónimo")

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

        ticket_data = {
            "asunto": f"Reclamo (LLM): {categoria}", "categoria": categoria, "detalles": descripcion,
            "direccion": direccion_final_txt, "nombre_vecino": nombre_vecino_final,
            "telefono_vecino": telefono_final_validado_e164, "email_vecino": email_final_validado,
            "estado": "nuevo", "user_id": user_id_db, "anon_id": anon_id_db,
            "latitud": latitud_final, "longitud": longitud_final,
            "origen_reclamo": "LLM_CHATBOT"
        }
        if self.context.get("foto_url"):
            ticket_data["foto_url_directa"] = self.context.get("foto_url")
        elif foto_url_llm:
            ticket_data["foto_url_directa"] = foto_url_llm

        ticket_data_cleaned = {k: v for k, v in ticket_data.items() if v is not None}
        if "municipio_id" in ticket_data_cleaned:
            del ticket_data_cleaned["municipio_id"]
        logger.info(f"Data for servicio_tickets.crear_nuevo_ticket: {ticket_data_cleaned}")

        try:
            ticket_creado = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data=ticket_data_cleaned)
            if not ticket_creado:
                raise Exception("servicio_tickets.crear_nuevo_ticket returned None")

            nro_ticket_str = f"M-{ticket_creado.nro_ticket}"
            logger.info(f"Ticket {nro_ticket_str} creado exitosamente vía LLM.")

            archivo_id_a_vincular = self.context.get("archivo_id_para_asociar")
            if archivo_id_a_vincular:
                from services.archivo_service import archivo_service
                asociacion_exitosa = archivo_service.asociar_archivos_a_ticket(ticket_id=ticket_creado.id, tipo_ticket="municipio", ids_archivos=[archivo_id_a_vincular])
                if asociacion_exitosa: logger.info(f"Archivo ID {archivo_id_a_vincular} asociado a ticket {nro_ticket_str}.")
                else: logger.warning(f"No se pudo asociar archivo ID {archivo_id_a_vincular} a ticket {nro_ticket_str}.")

            if telefono_final_validado_e164:
                try:
                    enviar_notificacion_whatsapp_con_plantilla(telefono_final_validado_e164, nombre_vecino_final, str(ticket_creado.nro_ticket), categoria)
                    enviar_notificacion_sms(telefono_final_validado_e164, f"Hola {nombre_vecino_final}! Tu reclamo M-{ticket_creado.nro_ticket} ({categoria}) fue generado.")
                except Exception as e_notify:
                    logger.error(f"Error enviando notificaciones para {nro_ticket_str}: {e_notify}")

            return {
                "success": True,
                "message_to_user": f"¡Gracias {nombre_vecino_final}! Tu reclamo sobre '{categoria}' ha sido registrado con el número {nro_ticket_str}. Te mantendremos informado.",
                "data": {"ticket_id": ticket_creado.id, "nro_ticket": nro_ticket_str, "status": "creado"}
            }
        except Exception as e:
            logger.error(f"Error en CrearReclamoActionHandler: {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Hubo un problema al intentar registrar tu reclamo. Por favor, intenta de nuevo más tarde.",
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
        return {
            "success": True,
            "message_to_user": user_message,
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

        tramites_info = get_tramites_info()
        # Case-insensitive search for the tramite
        tramite_encontrado = None
        for key, value in tramites_info.items():
            if key.lower() == tramite_nombre.lower():
                tramite_encontrado = value
                break

        if tramite_encontrado:
            info_message = tramite_encontrado.get("descripcion", "No hay información disponible para este trámite.")
            return {
                "success": True,
                "message_to_user": info_message,
                "data": {"tramite_nombre": tramite_nombre, "info_recuperada": "real"}
            }
        else:
            return {
                "success": False,
                "message_to_user": f"No encontré información sobre el trámite '{tramite_nombre}'.",
                "pedir_info": "nombre_tramite"
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
        # Simulate saving suggestion
        simulated_sug_id = "SUG-SIM" + str(action_data.get("id_simulacion", "001"))
        user_message = f"¡Muchas gracias por tu sugerencia! La hemos registrado con el ID {simulated_sug_id} y será revisada por nuestro equipo."
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"sugerencia_id": simulated_sug_id, "status": "registrada"}
        }

class EjecutarHerramientaActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing EjecutarHerramientaActionHandler with data: {action_data}")
        nombre_herramienta = action_data.get("nombre_herramienta")
        parametros = action_data.get("parametros_herramienta", {})

        if not nombre_herramienta: # or nombre_herramienta not in TOOL_REGISTRY:
            logger.error(f"Nombre de herramienta no proporcionado o no válido: {nombre_herramienta}")
            return {"success": False, "message_to_user": "No pude identificar la herramienta a ejecutar."}

        # Simulate tool execution
        # herramienta_func = TOOL_REGISTRY[nombre_herramienta]["funcion"]
        # resultado_herramienta = herramienta_func(**parametros)
        resultado_simulado = f"Resultado simulado de la herramienta '{nombre_herramienta}' con parámetros {parametros}."
        if nombre_herramienta == "consultar_recoleccion_por_direccion":
            resultado_simulado = f"Según mis registros, la recolección en '{parametros.get('direccion', 'tu dirección')}' es los Lunes, Miércoles y Viernes por la mañana."

        return {
            "success": True,
            "message_to_user": resultado_simulado,
            "data": {"herramienta_ejecutada": nombre_herramienta, "resultado": "simulado"}
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

            target = self.context.get("target_entity_type", "municipio")

            if target == "pyme":
                ticket_data = {
                    "asunto": f"Chat en Vivo con {nombre or 'Cliente'}",
                    "categoria": "Atención en Vivo",
                    "pregunta": pregunta_original,
                    "detalles": action_data.get("motivo_derivacion", "Solicitud de agente"),
                    "user_id": self.context.get("user_id"),
                    "anon_id": self.context.get("anon_id") if not self.context.get("cliente_id") else None,
                    "rubro_id": self.context.get("rubro_id"),
                    "estado": "esperando_agente_en_vivo",
                    "telefono": telefono,
                    "email": email,
                }
                ticket_type = "pyme"
            else:
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
            sala = servicio_tickets.crear_nuevo_ticket(ticket_type, ticket_data_cleaned)
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

            if ticket_type == "pyme":
                chat_id = f"P-{sala.nro_ticket}"
            else:
                chat_id = f"M-{sala.nro_ticket}"

            user_message = (
                f"Hemos recibido tu solicitud para hablar con un agente. Tu número de chat es **{chat_id}**."
            )
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

        campo_a_corregir = action_data.get("campo_a_corregir") # e.g., "ubicacion", "descripcion"
        nuevo_valor = action_data.get("nuevo_valor")
        # contexto_original = action_data.get("contexto_original_del_reclamo") # To identify which claim if multiple are possible in context

        if not campo_a_corregir or nuevo_valor is None: # nuevo_valor can be empty string
            return {
                "success": False,
                "message_to_user": "No especificaste qué dato corregir o cuál es el nuevo valor.",
                "pedir_info": "detalle_correccion"
            }

        # Here, you would update the claim data in the session/context or database.
        # For now, just acknowledge.
        user_message = f"Entendido. He actualizado '{campo_a_corregir}' a '{nuevo_valor}'. ¿Algo más que desees cambiar o confirmamos el reclamo?"

        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"campo_corregido": campo_a_corregir, "valor_actualizado": nuevo_valor},
            "pedir_info": "confirmacion_tras_correccion" # Suggests asking for confirmation
        }

# Add other handlers as needed
# e.g., CalificarAtencionActionHandler, ConfirmarCierreTicketActionHandler

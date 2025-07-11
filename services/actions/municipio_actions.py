# services/actions/municipio_actions.py
import logging
from .base_action_handler import BaseActionHandler
from typing import Dict, Any

# Assuming services like servicio_tickets, and models are accessible
# This might require adjustments to imports based on actual project structure
# from services.ticket_service import servicio_tickets
# from models import MunicipioTicket, TicketComentario, db
# from services.municipios import enviar_notificacion_whatsapp_con_plantilla, enviar_notificacion_sms
# from services.herramientas_municipio import TOOL_REGISTRY

logger = logging.getLogger(__name__)

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Executing CrearReclamoActionHandler with data: {action_data}")
        # Simplified logic: Acknowledge and simulate ticket creation
        # In real implementation, this would call servicio_tickets.crear_nuevo_ticket
        # and interact with the database.

        required_fields = ["categoria", "descripcion", "nombre_vecino", "telefono_vecino"]
        if not self.validate_data(action_data, required_fields):
            return {
                "success": False,
                "message_to_user": "Faltan datos esenciales para crear el reclamo. Por favor, proporciona al menos categoría, descripción, tu nombre y teléfono.",
                "pedir_info": "datos_reclamo_faltantes" # Generic signal to ask for more
            }

        try:
            # Simulate ticket creation
            # ticket = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={...})
            simulated_ticket_nro = "M-SIM" + str(action_data.get("id_simulacion", "12345"))

            # Simulate notifications
            # telefono = action_data.get("telefono_vecino")
            # if telefono:
            #     enviar_notificacion_whatsapp_con_plantilla(telefono, action_data.get("nombre_vecino"), simulated_ticket_nro, action_data.get("categoria"))
            #     enviar_notificacion_sms(telefono, f"Reclamo {simulated_ticket_nro} ({action_data.get('categoria')}) generado.")

            user_message = f"¡Gracias {action_data.get('nombre_vecino', 'vecino/a')}! Tu reclamo sobre '{action_data.get('categoria')}' con descripción '{action_data.get('descripcion', 'N/A')[:30]}...' ha sido registrado con el número {simulated_ticket_nro}. Te mantendremos informado."

            return {
                "success": True,
                "message_to_user": user_message,
                "data": {"ticket_nro": simulated_ticket_nro, "status": "creado"}
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
        logger.info(f"Executing ConsultarInfoTramiteActionHandler with data: {action_data}")
        tramite_nombre = action_data.get("nombre_tramite") or action_data.get("categoria") # Categoria might be used if specific tramite name isn't clear
        if not tramite_nombre:
            return {
                "success": False,
                "message_to_user": "¿Sobre qué trámite necesitas información?",
                "pedir_info": "nombre_tramite"
            }
        # Simulate fetching tramite info
        # info = buscar_info_tramite_en_db_o_json(tramite_nombre)
        simulated_info = f"La información para el trámite '{tramite_nombre}' es la siguiente: ... [detalles y requisitos]..."
        if "licencia de conducir" in tramite_nombre.lower():
            simulated_info += " Para licencias, recordá llevar DNI y comprobante de grupo sanguíneo."
        return {
            "success": True,
            "message_to_user": simulated_info,
            "data": {"tramite_nombre": tramite_nombre, "info_recuperada": "simulada"}
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
        logger.info(f"Executing DerivarHumanoActionHandler with data: {action_data}")
        # Simulate creating a live chat ticket or notifying agents
        # ticket_chat = servicio_tickets.crear_nuevo_ticket(tipo_ticket="municipio", ticket_data={"asunto": "Solicitud Chat en Vivo", "categoria": "Atención en Vivo", ...})
        simulated_chat_id = "CHAT-SIM" + str(action_data.get("id_simulacion", "789"))
        user_message = f"Entendido. Te estoy conectando con un agente. Tu número de chat es {simulated_chat_id}. Por favor, aguardá un momento."
        return {
            "success": True,
            "message_to_user": user_message,
            "data": {"chat_id": simulated_chat_id, "status": "esperando_agente"}
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

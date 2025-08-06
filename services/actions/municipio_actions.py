import logging
from .base_action_handler import BaseActionHandler
from models import db

logger = logging.getLogger(__name__)

class GreetingHandler(BaseActionHandler):
    def execute(self, data: dict) -> dict:
        welcome_message = (
            "¡Hola! Soy JuniA, el asistente virtual de la Municipalidad de Junín.\n"
            "Estas son las cosas que puedo hacer por vos:"
        )
        return {
            "message_body": welcome_message,
            "options_list": [
                {"id": "reclamos", "texto": "RECLAMOS"},
                {"id": "licencia_conducir", "texto": "LICENCIA DE CONDUCIR"},
                {"id": "pago_tasas", "texto": "PAGO DE TASAS VIGENTES"},
                {"id": "defensa_consumidor", "texto": "DEFENSA DEL CONSUMIDOR"},
                {"id": "veterinaria_bromatologia", "texto": "VETERINARIA Y BROMATOLOGÍA"},
            ],
            "message_type": "interactive_list",
            "fuente": "greeting_handler_v7_junin"
        }

class ConsultarInfoTramiteActionHandler(BaseActionHandler):
    def execute(self, data: dict) -> dict:
        # This is a placeholder. The real implementation would fetch the info from a database or a file.
        return {
            "message_body": f"Información sobre el trámite: {data.get('nombre_tramite')}",
            "options_list": [],
            "message_type": "text",
            "fuente": "consultar_info_tramite_handler"
        }

class MenuPrincipalActionHandler(BaseActionHandler):
    def execute(self, data: dict) -> dict:
        # This is a placeholder. The real implementation would fetch the menu from a database or a file.
        return {
            "message_body": "Este es el menú principal.",
            "options_list": [
                {"id": "reclamos", "texto": "Reclamos"},
                {"id": "tramites", "texto": "Trámites"},
            ],
            "message_type": "interactive_list",
            "fuente": "menu_principal_handler"
        }

class ErrorActionHandler(BaseActionHandler):
    def execute(self, data: dict) -> dict:
        error_message = data.get("error_message", "Lo siento, no pude procesar tu solicitud.")
        return {
            "message_body": error_message,
            "options_list": [],
            "message_type": "text",
            "fuente": "error_handler"
        }

class CrearReclamoActionHandler(BaseActionHandler):
    def execute(self, data: dict) -> dict:
        # This is a placeholder. The real implementation would create a ticket.
        return {
            "success": True,
            "message_to_user": f"Se ha creado tu reclamo con la siguiente información: {data}",
            "data": {"ticket_id": "12345"}
        }

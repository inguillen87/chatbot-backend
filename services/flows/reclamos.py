from services.core.state_machine import StateMachine
from services.integrations import tickets
from services.geo import reverse as geo_reverse
import logging

logger = logging.getLogger(__name__)

def handle(msg, meta):
    """
    Handles the Reclamos (complaints) flow.
    `meta` contains the full context, including chat_db_context.
    """
    # This is a simplified state machine for now.
    # We would get the current state from the user's session context.
    # context = meta.get("chat_db_context", {}).get("contexto_municipio_v2", {})
    # current_state = context.get("estado_conversacion")

    # For now, let's simulate the flow without a real state machine.
    # This will be properly implemented when the state machine is fleshed out.

    # Placeholder for slot-locking logic
    # location = meta.get("location")
    # if location:
    #     geo_info = geo_reverse.reverse(location['latitude'], location['longitude'])
    #     # ... update context, skip to confirmation ...

    # For now, we just return a simple response indicating the flow was triggered.
    # In the next steps, I will build the full state machine logic here.

    # Let's create a placeholder ticket for now to test the integration.
    ticket_data = {
        "asunto": "Reclamo de prueba",
        "categoria": "Luminaria",
        "detalles": msg,
        "nombre_vecino": "Juan Perez (prueba)",
        "telefono_vecino": "+5491122334455",
        "email_vecino": "test@test.com",
        "municipio_id": meta.get("owner_user", {}).get("municipio_id", 1) # Default to 1 for testing
    }

    created_ticket = tickets.create(ticket_data)

    if created_ticket:
        return {
            "type": "reclamo",
            "title": "Reclamo Creado (Prueba)",
            "summary": "Este es un reclamo de prueba desde el nuevo flujo.",
            "ticket": created_ticket
        }
    else:
        return {
            "type": "error",
            "title": "Error en Flujo de Reclamo",
            "summary": "No se pudo crear el ticket de prueba."
        }

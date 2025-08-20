import logging
from flask import current_app

logger = logging.getLogger(__name__)

def _remove_redundant_urls_from_message(message_body: str, botones: list) -> str:
    """Si una URL de un botón ya está en el cuerpo del mensaje, la elimina del cuerpo."""
    if not botones:
        return message_body

    for option in botones:
        # Defensive checks for mock objects in tests
        url = option.get('url')
        if isinstance(option, dict) and isinstance(url, str):
            if isinstance(message_body, str) and url in message_body:
                # Elimina la URL del cuerpo del mensaje si ya está presente
                message_body = message_body.replace(url, "").strip()
                # Elimina saltos de línea dobles que puedan quedar
                message_body = message_body.replace("\n\n", "\n")

    return message_body


def formatear_ticket_respuesta(ticket, municipio_config, canal='web', viewer_user=None, extra_info=None, current_user=None, datos_llm=None, tipo='reclamo'):
    """
    Formatea una respuesta estándar para la creación de un ticket.
    Devuelve el texto de la respuesta y una lista de botones.
    """
    # Defensive check: If 'ticket' is not an object with an 'id', treat it as just the ticket number.
    if not hasattr(ticket, 'id'):
        nro_ticket_display = str(ticket)
        if tipo == 'sugerencia':
            respuesta = f"Tu sugerencia #{nro_ticket_display} ha sido registrada con éxito."
        else:
            respuesta = f"Tu reclamo #{nro_ticket_display} ha sido creado con éxito."
        botones = [{"texto": "Volver al menú principal", "action_id": "menu_principal"}]
        if canal == 'whatsapp':
             if tipo == 'sugerencia':
                 respuesta += "\n\n¡Gracias por tu aporte!"
             else:
                respuesta += "\n\nRecibirás actualizaciones por este medio. " \
                             "Puedes consultar el estado de tus reclamos escribiendo 'Mis reclamos'."
        return respuesta, botones

    base_url = current_app.config.get("FRONTEND_URL", "")
    nro_ticket_display = ticket.nro_ticket if hasattr(ticket, 'nro_ticket') and ticket.nro_ticket else ticket.id

    if tipo == 'sugerencia':
        respuesta = f"Tu sugerencia #{nro_ticket_display} ha sido registrada con éxito."
    else:
        respuesta = f"Tu reclamo #{nro_ticket_display} ha sido creado con éxito."

    botones = []

    if extra_info:
        if isinstance(extra_info, str):
            respuesta += f"\n\n{extra_info}"
        elif isinstance(extra_info, dict):
            for key, value in extra_info.items():
                respuesta += f"\n{key.replace('_', ' ').capitalize()}: {value}"

    # Generar URL de seguimiento
    url_seguimiento = None
    ticket_id_str = str(ticket.id)
    user_for_link = viewer_user or current_user
    if user_for_link and hasattr(user_for_link, 'id') and user_for_link.id:
        url_seguimiento = f"{base_url}/mis-tickets?ticket_id={ticket_id_str}"
    elif hasattr(ticket, 'anon_id') and ticket.anon_id:
        anon_id_str = str(ticket.anon_id)
        url_seguimiento = f"{base_url}/?anon_id={anon_id_str}&ticket_id={ticket_id_str}"

    if canal == 'whatsapp':
        if tipo == 'sugerencia':
            respuesta += "\n\n¡Gracias por tu aporte!"
        else:
            respuesta += "\n\nRecibirás actualizaciones por este medio. " \
                         "Puedes consultar el estado de tus reclamos escribiendo 'Mis reclamos'."
        if url_seguimiento and tipo != 'sugerencia':
             respuesta += f"\n\nO sigue su estado en la web: {url_seguimiento}"
    else:
        if url_seguimiento and tipo != 'sugerencia':
            botones.append({"texto": "Ver Estado del Reclamo", "type": "url", "url": url_seguimiento})
            respuesta += f"\n\nPuedes seguir su estado aquí: {url_seguimiento}"
        botones.append({"texto": "Volver al menú principal", "action_id": "menu_principal"})

    respuesta_limpia = _remove_redundant_urls_from_message(respuesta, botones)

    return respuesta_limpia, botones

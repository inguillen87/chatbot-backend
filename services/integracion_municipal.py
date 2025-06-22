import logging

logger = logging.getLogger(__name__)


def enviar_ticket_a_sigem(ticket) -> bool:
    """Envía la información de un ticket al sistema SIGEM.

    Actualmente es un stub que solo registra la operación.
    """
    if not ticket:
        return False
    logger.info("[SIGEM] Enviando ticket %s", getattr(ticket, "id", "?"))
    # Aquí iría la llamada real al web service de SIGEM
    return True
import logging

from flask import current_app, has_app_context

logger = logging.getLogger(__name__)


def enviar_ticket_a_sigem(ticket) -> bool:
    """Envía la información de un ticket al sistema SIGEM.

    El transporte autenticado todavía no está implementado. Nunca reportar
    éxito por registrar una línea de log: eso ocultaría reclamos no entregados.
    """
    if not ticket:
        return False
    enabled = bool(
        has_app_context() and current_app.config.get("SIGEM_LIVE_ENABLED", False)
    )
    if not enabled:
        logger.info(
            "[SIGEM] integration_skipped ticket_id=%s reason=not_configured",
            getattr(ticket, "id", None),
        )
        return False

    logger.error(
        "[SIGEM] integration_unavailable ticket_id=%s reason=transport_not_implemented",
        getattr(ticket, "id", None),
    )
    return False

"""Utilidades centralizadas para despachar notificaciones multicanal.

Se encapsulan los envíos de email, SMS y WhatsApp para que las rutas solo
tengan que invocar una función y registrar el resultado. Esto evita que los
errores de configuración (SMTP/Twilio) pasen desapercibidos y deja un punto
único para instrumentar métricas o trazas futuras.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from services.email_service import (
    enviar_email_ticket_novedad,
    enviar_sms_ticket_novedad,
    enviar_whatsapp_ticket_novedad,
)


logger = logging.getLogger(__name__)


def dispatch_ticket_update(
    ticket: Any,
    tipo: str,
    mensaje: str,
    *,
    comentario_reciente: Any = None,
    enable_whatsapp: bool = True,
) -> Dict[str, bool]:
    """Envía la notificación de novedad de ticket por los canales disponibles.

    Devuelve un diccionario con el estado de cada canal para facilitar el
    logging o la observabilidad desde las rutas.
    """

    resultados: Dict[str, bool] = {"email": False, "sms": False, "whatsapp": False}

    try:
        resultados["email"] = enviar_email_ticket_novedad(
            ticket,
            mensaje,
            comentario_reciente=comentario_reciente,
        )
    except Exception as exc:  # pragma: no cover - solo logging defensivo
        logger.error(
            "[NOTIFY] Error enviando email de novedad para ticket %s: %s",
            getattr(ticket, "id", "N/A"),
            exc,
            exc_info=True,
        )

    try:
        resultados["sms"] = enviar_sms_ticket_novedad(ticket, mensaje)
    except Exception as exc:  # pragma: no cover - solo logging defensivo
        logger.error(
            "[NOTIFY] Error enviando SMS de novedad para ticket %s: %s",
            getattr(ticket, "id", "N/A"),
            exc,
            exc_info=True,
        )

    if enable_whatsapp and tipo == "municipio":
        try:
            resultados["whatsapp"] = enviar_whatsapp_ticket_novedad(ticket, mensaje)
        except Exception as exc:  # pragma: no cover - solo logging defensivo
            logger.error(
                "[NOTIFY] Error enviando WhatsApp de novedad para ticket %s: %s",
                getattr(ticket, "id", "N/A"),
                exc,
                exc_info=True,
            )

    return resultados


def dispatch_ticket_state_change(
    ticket: Any,
    tipo: str,
    nuevo_estado: str,
    *,
    comentario_estado: Any = None,
) -> Dict[str, bool]:
    """Envoltura especializada para cambios de estado.

    Construye el mensaje estándar y delega en :func:`dispatch_ticket_update`.
    """

    mensaje = f"El estado de tu ticket #{getattr(ticket, 'nro_ticket', '')} ha sido actualizado a: '{nuevo_estado}'."
    return dispatch_ticket_update(
        ticket,
        tipo,
        mensaje,
        comentario_reciente=comentario_estado,
        enable_whatsapp=True,
    )


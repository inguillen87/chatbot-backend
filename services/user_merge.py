"""Utilities to merge anonymous activity into a registered user account."""

from __future__ import annotations

from typing import Dict, Optional, Union

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import (
    ChatSessionContext,
    MunicipioTicket,
    PymeTicket,
    PublicSurveyResponse,
    SugerenciaCiudadano,
    TicketComentario,
    User,
)
from services.ticket_service import servicio_tickets


def merge_anon_into_user(anon_id: Optional[str], user: Union[User, int, None]) -> Dict[str, int]:
    """Reassign records tracked by ``anon_id`` to the provided ``user``.

    The function migrates tickets, comments, surveys and conversation contexts so
    that any progress made while browsing anonymously is preserved once the user
    completes a Passkey registration or authentication flow.
    """

    stats: Dict[str, int] = {
        "tickets": 0,
        "municipio_tickets": 0,
        "pyme_tickets": 0,
        "ticket_comentarios": 0,
        "chat_contexts": 0,
        "sugerencias": 0,
        "encuestas": 0,
    }

    if not anon_id:
        current_app.logger.debug("[merge_anon_into_user] Skipped merge: missing anon_id")
        return stats

    anon_id = anon_id.strip()
    if not anon_id:
        current_app.logger.debug("[merge_anon_into_user] Skipped merge: anon_id empty after strip")
        return stats

    user_id: Optional[int]
    if isinstance(user, User):
        user_id = user.id
    elif isinstance(user, int):
        user_id = user
    else:
        user_id = None

    if not user_id:
        current_app.logger.debug(
            "[merge_anon_into_user] Skipped merge: user_id not provided for anon %s", anon_id
        )
        return stats

    try:
        stats["tickets"] = servicio_tickets.migrar_tickets_de_anonimo(anon_id, user_id)

        stats["municipio_tickets"] = (
            MunicipioTicket.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["pyme_tickets"] = (
            PymeTicket.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["ticket_comentarios"] = (
            TicketComentario.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["chat_contexts"] = (
            ChatSessionContext.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["sugerencias"] = (
            SugerenciaCiudadano.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["encuestas"] = (
            PublicSurveyResponse.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0

        db.session.commit()
        current_app.logger.info(
            "[merge_anon_into_user] anon %s merged into user %s: %s",
            anon_id,
            user_id,
            stats,
        )
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception(
            "[merge_anon_into_user] Database error while merging anon %s into user %s",
            anon_id,
            user_id,
        )
        raise

    return stats


__all__ = ["merge_anon_into_user"]

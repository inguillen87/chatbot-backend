"""Utilities to merge anonymous activity into a registered user account."""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Union

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import (
    ChatSessionContext,
    MarketCart,
    MarketOrder,
    MunicipioTicket,
    PymeTicket,
    PublicSurveyResponse,
    SugerenciaCiudadano,
    TicketComentario,
    User,
)
from services.ticket_service import servicio_tickets


def _clean_identity_values(values: Iterable[object]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def merge_anon_into_user(
    anon_id: Optional[str],
    user: Union[User, int, None],
    *,
    session_ids: Optional[Iterable[object]] = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, int]:
    """Reassign records tracked by ``anon_id`` to the provided ``user``.

    The function migrates tickets, comments, surveys and conversation contexts so
    that any progress made while browsing anonymously is preserved once the user
    completes a Passkey registration or authentication flow.  Widget carts and
    marketplace orders can be tracked by either anon id or chat session id, so
    callers may pass both identities.
    """

    stats: Dict[str, int] = {
        "tickets": 0,
        "municipio_tickets": 0,
        "pyme_tickets": 0,
        "ticket_comentarios": 0,
        "chat_contexts": 0,
        "sugerencias": 0,
        "encuestas": 0,
        "market_carts": 0,
        "market_orders": 0,
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
        identity_values = _clean_identity_values([anon_id, *(session_ids or [])])
        contact_keys = _clean_identity_values([f"session:{value}" for value in identity_values])

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
        chat_query = ChatSessionContext.query.filter(
            or_(
                ChatSessionContext.anon_id == anon_id,
                ChatSessionContext.chat_session_id.in_(identity_values),
            )
        )
        stats["chat_contexts"] = (
            chat_query.update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["sugerencias"] = (
            SugerenciaCiudadano.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["encuestas"] = (
            PublicSurveyResponse.query.filter_by(anon_id=anon_id)
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0

        cart_query = MarketCart.query.filter(
            or_(
                MarketCart.session_id.in_(identity_values),
                MarketCart.contact_key.in_(contact_keys),
            )
        )
        if tenant_id:
            cart_query = cart_query.filter(MarketCart.tenant_id == tenant_id)
        stats["market_carts"] = (
            cart_query.update({"user_id": user_id}, synchronize_session=False)
        ) or 0

        order_query = MarketOrder.query.filter(
            or_(
                MarketOrder.session_id.in_(identity_values),
                MarketOrder.contact_key.in_(contact_keys),
            )
        )
        if tenant_id:
            order_query = order_query.filter(MarketOrder.tenant_id == tenant_id)
        stats["market_orders"] = (
            order_query.update({"user_id": user_id}, synchronize_session=False)
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

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
    PublicSurvey,
    PublicSurveyResponse,
    SugerenciaCiudadano,
    TicketComentario,
    TenantFollower,
    TenantProfile,
    User,
)
from services.tenant_ticket_scope import (
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
)


def _clean_identity_values(values: Iterable[object]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _resolve_merge_tenant(
    user_obj: User | None,
    tenant_id: Optional[int],
) -> TenantProfile | None:
    if tenant_id is not None:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            return None
        if tenant is None or user_obj is None:
            return None
        if getattr(user_obj, "tenant_id", None) == tenant.id:
            return tenant
        if getattr(tenant, "municipio_id", None) == user_obj.id:
            return tenant
        if getattr(tenant, "pyme_id", None) == user_obj.id:
            return tenant
        if TenantFollower.query.filter_by(
            tenant_id=tenant.id,
            user_id=user_obj.id,
        ).one_or_none() is not None:
            return tenant
        return None
    explicit_tenant_id = getattr(user_obj, "tenant_id", None) if user_obj is not None else None
    if explicit_tenant_id:
        return db.session.get(TenantProfile, explicit_tenant_id)
    if user_obj is None:
        return None

    owner_ids = []
    for raw_owner_id in (
        getattr(user_obj, "municipio_id", None),
        getattr(user_obj, "pyme_id", None),
        getattr(user_obj, "empresa_id", None),
    ):
        try:
            owner_id = int(raw_owner_id)
        except (TypeError, ValueError):
            continue
        if owner_id > 0 and owner_id not in owner_ids:
            owner_ids.append(owner_id)

    tenant_ids = set()
    for owner_id in owner_ids:
        resolution = resolve_unique_tenant_for_owner(owner_id)
        if resolution.status != "unique" or resolution.tenant is None:
            return None
        tenant_ids.add(int(resolution.tenant.id))
    return db.session.get(TenantProfile, tenant_ids.pop()) if len(tenant_ids) == 1 else None


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
    user_obj = user if isinstance(user, User) else None
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

    if user_obj is None:
        user_obj = db.session.get(User, user_id)
    tenant = _resolve_merge_tenant(user_obj, tenant_id)
    if tenant is None:
        current_app.logger.warning(
            "[merge_anon_into_user] Skipped merge: tenant scope unavailable for user %s",
            user_id,
        )
        return stats

    try:
        identity_values = _clean_identity_values([anon_id, *(session_ids or [])])
        contact_keys = _clean_identity_values([f"session:{value}" for value in identity_values])

        municipio_ids = [
            row[0]
            for row in scoped_municipio_ticket_query(tenant)
            .filter(
                MunicipioTicket.anon_id == anon_id,
                or_(
                    MunicipioTicket.user_id.is_(None),
                    MunicipioTicket.user_id == user_id,
                ),
            )
            .with_entities(MunicipioTicket.id)
            .all()
        ]
        pyme_ids = [
            row[0]
            for row in PymeTicket.query.filter(
                PymeTicket.tenant_id == tenant.id,
                PymeTicket.anon_id == anon_id,
                or_(PymeTicket.user_id.is_(None), PymeTicket.user_id == user_id),
            )
            .with_entities(PymeTicket.id)
            .all()
        ]

        stats["municipio_tickets"] = (
            MunicipioTicket.query.filter(
                MunicipioTicket.id.in_(municipio_ids),
                MunicipioTicket.anon_id == anon_id,
                or_(
                    MunicipioTicket.user_id.is_(None),
                    MunicipioTicket.user_id == user_id,
                ),
            )
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["pyme_tickets"] = (
            PymeTicket.query.filter(
                PymeTicket.id.in_(pyme_ids),
                PymeTicket.anon_id == anon_id,
                or_(PymeTicket.user_id.is_(None), PymeTicket.user_id == user_id),
            )
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["tickets"] = stats["municipio_tickets"] + stats["pyme_tickets"]
        stats["ticket_comentarios"] = (
            TicketComentario.query.filter(
                TicketComentario.anon_id == anon_id,
                or_(
                    TicketComentario.user_id.is_(None),
                    TicketComentario.user_id == user_id,
                ),
                or_(
                    TicketComentario.municipio_ticket_id.in_(municipio_ids),
                    TicketComentario.pyme_ticket_id.in_(pyme_ids),
                ),
            )
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        chat_query = ChatSessionContext.query.filter(
            ChatSessionContext.anon_id == anon_id,
            ChatSessionContext.chat_session_id.in_(identity_values),
            ChatSessionContext.tenant_id == tenant.id,
            or_(
                ChatSessionContext.user_id.is_(None),
                ChatSessionContext.user_id == user_id,
            ),
        )
        stats["chat_contexts"] = (
            chat_query.update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0
        stats["sugerencias"] = 0
        if tenant.municipio_id is not None:
            stats["sugerencias"] = (
                SugerenciaCiudadano.query.filter_by(
                    anon_id=anon_id,
                    municipio_id=tenant.municipio_id,
                )
                .filter(
                    or_(
                        SugerenciaCiudadano.user_id.is_(None),
                        SugerenciaCiudadano.user_id == user_id,
                    )
                )
                .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
            ) or 0
        survey_ids = PublicSurvey.query.filter(
            PublicSurvey.tenant_id == tenant.id
        ).with_entities(PublicSurvey.id)
        stats["encuestas"] = (
            PublicSurveyResponse.query.filter(
                PublicSurveyResponse.anon_id == anon_id,
                PublicSurveyResponse.survey_id.in_(survey_ids),
                or_(
                    PublicSurveyResponse.user_id.is_(None),
                    PublicSurveyResponse.user_id == user_id,
                ),
            )
            .update({"user_id": user_id, "anon_id": None}, synchronize_session=False)
        ) or 0

        cart_query = MarketCart.query.filter(
            or_(
                MarketCart.session_id.in_(identity_values),
                MarketCart.contact_key.in_(contact_keys),
            )
        )
        cart_query = cart_query.filter(
            MarketCart.tenant_id == tenant.id,
            or_(MarketCart.user_id.is_(None), MarketCart.user_id == user_id),
        )
        stats["market_carts"] = (
            cart_query.update({"user_id": user_id}, synchronize_session=False)
        ) or 0

        order_query = MarketOrder.query.filter(
            or_(
                MarketOrder.session_id.in_(identity_values),
                MarketOrder.contact_key.in_(contact_keys),
            )
        )
        order_query = order_query.filter(
            MarketOrder.tenant_id == tenant.id,
            or_(MarketOrder.user_id.is_(None), MarketOrder.user_id == user_id),
        )
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

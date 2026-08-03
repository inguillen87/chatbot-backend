from __future__ import annotations

from flask import g, request
from werkzeug.exceptions import BadRequest

import pytest

from extensions import db
from models import PymePedido, PymeTicket, TenantProfile, User
from services.analytics.filters import AnalyticsFilters, parse_filters
from services.analytics.repository import pyme_pedido_query, pyme_ticket_query


def _user(user_id: int, *, tenant_id: int | None = None) -> User:
    user = User(
        id=user_id,
        name=f"Analytics user {user_id}",
        email=f"analytics-pyme-scope-{user_id}@example.com",
        rol="operador",
        tipo_chat="pyme",
        tenant_id=tenant_id,
    )
    user.set_password("test")
    db.session.add(user)
    db.session.flush()
    return user


def _tenant(
    profile_id: int,
    owner: User,
    *,
    suffix: str,
    scope: str = "pyme",
) -> TenantProfile:
    tenant = TenantProfile(
        id=profile_id,
        slug=f"analytics-{scope}-{suffix}",
        nombre=f"Analytics {scope} {suffix}",
        tipo=scope,
        pyme_id=owner.id if scope == "pyme" else None,
        municipio_id=owner.id if scope == "municipio" else None,
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def _ticket(
    ticket_id: int,
    *,
    tenant_id: int | None,
    owner_id: int,
) -> PymeTicket:
    ticket = PymeTicket(
        id=ticket_id,
        tenant_id=tenant_id,
        user_id=owner_id,
        pregunta=f"Ticket {ticket_id}",
        categoria="ventas",
        nro_ticket=800_000 + ticket_id,
    )
    db.session.add(ticket)
    return ticket


def _pedido(
    *,
    tenant_id: int | None,
    owner_id: int,
    asunto: str,
) -> PymePedido:
    pedido = PymePedido(
        pyme_id=owner_id,
        tenant_id=tenant_id,
        asunto=asunto,
        detalles="[]",
    )
    db.session.add(pedido)
    return pedido


def _filters(owner_id: int, *, profile_id: int | None = None) -> AnalyticsFilters:
    return AnalyticsFilters(
        tenant_id=str(owner_id),
        scope="pyme",
        date_from=None,
        date_to=None,
        canales=(),
        categorias=(),
        estados=(),
        agentes=(),
        zonas=(),
        etiquetas=(),
        rubros=(),
        bbox=None,
        pyme_ids=(),
        resolution=8,
        tenant_profile_id=profile_id,
    )


def test_viewer_tenant_id_is_profile_fk_even_when_it_collides_with_an_owner(app, client):
    selected_owner = _user(9_101)
    colliding_owner = _user(71)
    selected_tenant = _tenant(71, selected_owner, suffix="selected")
    colliding_tenant = _tenant(72, colliding_owner, suffix="colliding-owner")
    viewer = _user(9_102, tenant_id=selected_tenant.id)

    selected_ticket = _ticket(
        1,
        tenant_id=selected_tenant.id,
        owner_id=selected_owner.id,
    )
    _ticket(2, tenant_id=colliding_tenant.id, owner_id=colliding_owner.id)
    selected_order = _pedido(
        tenant_id=selected_tenant.id,
        owner_id=selected_owner.id,
        asunto="selected",
    )
    _pedido(
        tenant_id=colliding_tenant.id,
        owner_id=colliding_owner.id,
        asunto="colliding-owner",
    )
    db.session.commit()

    with app.test_request_context("/analytics/summary?scope=pyme"):
        g.viewer = viewer
        filters = parse_filters(request.args)
        ticket_ids = {ticket.id for ticket in pyme_ticket_query(filters).all()}
        order_ids = {pedido.id for pedido in pyme_pedido_query(filters).all()}

    assert filters.tenant_id == str(selected_owner.id)
    assert filters.tenant_profile_id == selected_tenant.id
    assert ticket_ids == {selected_ticket.id}
    assert order_ids == {selected_order.id}


def test_viewer_profile_with_wrong_scope_is_rejected(app, client):
    municipality_owner = _user(9_201)
    municipality = _tenant(
        81,
        municipality_owner,
        suffix="municipality",
        scope="municipio",
    )
    viewer = _user(9_202, tenant_id=municipality.id)
    db.session.commit()

    with app.test_request_context("/analytics/summary?scope=pyme"):
        g.viewer = viewer
        with pytest.raises(BadRequest, match="tenant context is invalid for scope"):
            parse_filters(request.args)


def test_exact_profile_keeps_legacy_rows_quarantined_for_ambiguous_owner(client):
    owner = _user(9_301)
    first = _tenant(91, owner, suffix="ambiguous-first")
    second = _tenant(92, owner, suffix="ambiguous-second")
    selected_ticket = _ticket(11, tenant_id=first.id, owner_id=owner.id)
    _ticket(12, tenant_id=second.id, owner_id=owner.id)
    _ticket(13, tenant_id=None, owner_id=owner.id)
    selected_order = _pedido(tenant_id=first.id, owner_id=owner.id, asunto="first")
    _pedido(tenant_id=second.id, owner_id=owner.id, asunto="second")
    _pedido(tenant_id=None, owner_id=owner.id, asunto="legacy")
    db.session.commit()

    exact_filters = _filters(owner.id, profile_id=first.id)

    assert {row.id for row in pyme_ticket_query(exact_filters).all()} == {
        selected_ticket.id
    }
    assert {row.id for row in pyme_pedido_query(exact_filters).all()} == {
        selected_order.id
    }


def test_owner_only_ambiguous_scope_and_exact_owner_mismatch_fail_closed(client):
    owner = _user(9_401)
    other_owner = _user(9_402)
    first = _tenant(101, owner, suffix="ambiguous-owner-first")
    _tenant(102, owner, suffix="ambiguous-owner-second")
    _ticket(21, tenant_id=first.id, owner_id=owner.id)
    _pedido(tenant_id=first.id, owner_id=owner.id, asunto="ambiguous")
    db.session.commit()

    ambiguous = _filters(owner.id)
    mismatched = _filters(other_owner.id, profile_id=first.id)

    assert pyme_ticket_query(ambiguous).all() == []
    assert pyme_pedido_query(ambiguous).all() == []
    assert pyme_ticket_query(mismatched).all() == []
    assert pyme_pedido_query(mismatched).all() == []


def test_unique_owner_supports_only_its_exact_and_legacy_rows(client):
    owner = _user(9_501)
    foreign_owner = _user(9_502)
    tenant = _tenant(111, owner, suffix="unique")
    foreign = _tenant(112, foreign_owner, suffix="foreign")
    explicit_ticket = _ticket(31, tenant_id=tenant.id, owner_id=owner.id)
    legacy_ticket = _ticket(32, tenant_id=None, owner_id=owner.id)
    _ticket(33, tenant_id=foreign.id, owner_id=foreign_owner.id)
    explicit_order = _pedido(tenant_id=tenant.id, owner_id=owner.id, asunto="explicit")
    legacy_order = _pedido(tenant_id=None, owner_id=owner.id, asunto="legacy")
    _pedido(tenant_id=foreign.id, owner_id=foreign_owner.id, asunto="foreign")
    db.session.commit()

    owner_filters = _filters(owner.id)

    assert {row.id for row in pyme_ticket_query(owner_filters).all()} == {
        explicit_ticket.id,
        legacy_ticket.id,
    }
    assert {row.id for row in pyme_pedido_query(owner_filters).all()} == {
        explicit_order.id,
        legacy_order.id,
    }

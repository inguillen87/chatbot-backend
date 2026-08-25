from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest

from models import MunicipioTicket, TenantProfile, TicketComentario, User, db
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    municipio_ticket_belongs_to_tenant,
    normalize_municipio_ticket_write_scope,
    resolve_municipio_ticket_access_tenant,
    scoped_municipio_ticket_query,
)
from services.ticket_service import ServicioTickets
from routes.archivos import _municipio_tenant_for_actor


def _owner(email: str) -> User:
    user = User(name=email.split("@", 1)[0], email=email, rol="admin", tipo_chat="municipio")
    user.set_password("scope-test-secret")
    db.session.add(user)
    db.session.flush()
    return user


def _tenant(owner: User, slug: str) -> TenantProfile:
    tenant = TenantProfile(
        slug=slug,
        nombre=slug,
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def _headers(app, owner: User, tenant: TenantProfile) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": owner.id,
            "rol": owner.rol,
            "tenant_slug": tenant.slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant.slug,
    }


def _legacy_ticket(owner: User, *, tenant_id=None, number: str) -> MunicipioTicket:
    ticket = MunicipioTicket(
        tenant_id=tenant_id,
        municipio_id=owner.id,
        nro_ticket=number,
        consulta_pin="654321",
        pregunta="Alumbrado sin servicio",
        asunto="Alumbrado",
        categoria="luminaria",
        estado="nuevo",
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket


def test_unique_legacy_owner_can_read_while_ambiguous_and_orphan_rows_are_quarantined(
    app,
    client,
):
    owner = _owner("legacy-owner@test.com")
    tenant_a = _tenant(owner, "legacy-a")
    owner.tenant_id = tenant_a.id
    owner.tenant_slug = tenant_a.slug
    db.session.commit()

    unique_ticket = _legacy_ticket(owner, number="M-SCOPE-UNIQUE")
    assert scoped_municipio_ticket_query(tenant_a).filter_by(id=unique_ticket.id).one() == unique_ticket
    assert municipio_ticket_belongs_to_tenant(unique_ticket, tenant_a)
    assert resolve_municipio_ticket_access_tenant(unique_ticket).id == tenant_a.id

    unique_read = client.get(
        f"/api/v2/inbox/omnichannel/{unique_ticket.id}?source_model=MunicipioTicket",
        headers=_headers(app, owner, tenant_a),
    )
    assert unique_read.status_code == 200

    tenant_b = _tenant(owner, "legacy-b")
    orphan_owner = _owner("legacy-orphan@test.com")
    orphan_ticket = _legacy_ticket(orphan_owner, number="M-SCOPE-ORPHAN")
    db.session.commit()

    assert scoped_municipio_ticket_query(tenant_a).filter_by(id=unique_ticket.id).first() is None
    assert scoped_municipio_ticket_query(tenant_b).filter_by(id=unique_ticket.id).first() is None
    assert not municipio_ticket_belongs_to_tenant(unique_ticket, tenant_a)
    assert scoped_municipio_ticket_query(tenant_a).filter_by(id=orphan_ticket.id).first() is None

    with pytest.raises(TicketTenantScopeError) as ambiguous:
        resolve_municipio_ticket_access_tenant(unique_ticket)
    assert ambiguous.value.code == "ticket_tenant_ambiguous"

    with pytest.raises(TicketTenantScopeError) as orphan:
        resolve_municipio_ticket_access_tenant(orphan_ticket)
    assert orphan.value.code == "ticket_tenant_not_found"

    hidden_read = client.get(
        f"/api/v2/inbox/omnichannel/{unique_ticket.id}?source_model=MunicipioTicket",
        headers=_headers(app, owner, tenant_a),
    )
    assert hidden_read.status_code == 404
    assert hidden_read.get_json()["reason_code"] == "ticket_not_found"


def test_explicit_tenant_is_authoritative_and_ambiguous_legacy_mutations_fail_closed(
    app,
    client,
):
    owner = _owner("shared-owner@test.com")
    tenant_a = _tenant(owner, "shared-a")
    tenant_b = _tenant(owner, "shared-b")
    owner.tenant_id = tenant_a.id
    owner.tenant_slug = tenant_a.slug
    legacy_ticket = _legacy_ticket(owner, number="M-SCOPE-MUTATE")
    explicit_b = _legacy_ticket(
        owner,
        tenant_id=tenant_b.id,
        number="M-SCOPE-EXPLICIT-B",
    )

    assert scoped_municipio_ticket_query(tenant_a).filter_by(id=explicit_b.id).first() is None
    assert scoped_municipio_ticket_query(tenant_b).filter_by(id=explicit_b.id).one() == explicit_b
    assert not municipio_ticket_belongs_to_tenant(explicit_b, tenant_a)
    assert municipio_ticket_belongs_to_tenant(explicit_b, tenant_b)

    response = client.post(
        "/api/v2/inbox/omnichannel/actions",
        json={
            "source_model": "MunicipioTicket",
            "legacy_id": legacy_ticket.id,
            "action": "close",
        },
        headers=_headers(app, owner, tenant_a),
    )
    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "ticket_not_found"
    db.session.refresh(legacy_ticket)
    assert legacy_ticket.estado == "nuevo"
    assert TicketComentario.query.filter_by(municipio_ticket_id=legacy_ticket.id).count() == 0

    legacy_response = client.put(
        f"/tickets/municipio/{legacy_ticket.id}/estado",
        json={"estado": "en_proceso"},
        headers=_headers(app, owner, tenant_a),
    )
    assert legacy_response.status_code == 404
    db.session.refresh(legacy_ticket)
    assert legacy_ticket.estado == "nuevo"


def test_municipal_ticket_creation_resolves_only_unique_owner_and_rejects_mismatch(
    app,
    client,
):
    del app, client
    owner = _owner("create-owner@test.com")
    tenant_a = _tenant(owner, "create-a")
    mismatch_owner = _owner("mismatch-owner@test.com")
    db.session.commit()
    service = ServicioTickets()

    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=False), patch.object(
        service,
        "_notificar_ticket_por_email",
    ):
        created = service.crear_nuevo_ticket(
            "municipio",
            {
                "municipio_id": owner.id,
                "pregunta": "Reclamo con propietario univoco",
                "categoria": "luminaria",
            },
            return_object=True,
        )

    assert created.tenant_id == tenant_a.id
    assert created.municipio_id == owner.id

    _tenant(owner, "create-b")
    db.session.commit()
    count_before = MunicipioTicket.query.count()

    with pytest.raises(TicketTenantScopeError) as ambiguous:
        service.crear_nuevo_ticket(
            "municipio",
            {
                "municipio_id": owner.id,
                "pregunta": "No debe persistirse",
            },
        )
    assert ambiguous.value.code == "ticket_tenant_ambiguous"
    assert MunicipioTicket.query.count() == count_before

    with pytest.raises(TicketTenantScopeError) as mismatch:
        service.crear_nuevo_ticket(
            "municipio",
            {
                "tenant_id": tenant_a.id,
                "municipio_id": mismatch_owner.id,
                "pregunta": "Tampoco debe persistirse",
            },
        )
    assert mismatch.value.code == "ticket_tenant_owner_mismatch"
    assert MunicipioTicket.query.count() == count_before


def test_attachment_actor_with_conflicting_legacy_owner_ids_fails_closed(app, client):
    del app, client
    owner_a = _owner("attachment-owner-a@test.com")
    owner_b = _owner("attachment-owner-b@test.com")
    _tenant(owner_a, "attachment-a")
    _tenant(owner_b, "attachment-b")
    employee = User(
        name="Empleado conflictivo",
        email="attachment-employee@test.com",
        rol="empleado",
        tipo_chat="municipio",
        municipio_id=owner_a.id,
        empresa_id=owner_b.id,
    )
    employee.set_password("scope-test-secret")
    db.session.add(employee)
    db.session.commit()

    assert _municipio_tenant_for_actor(employee) is None


def test_municipal_ticket_never_trusts_an_explicit_or_legacy_pyme_tenant(
    app,
    client,
):
    del app, client
    pyme_owner = User(
        name="PYME owner",
        email="municipal-ticket-pyme-owner@test.com",
        rol="admin",
        tipo_chat="pyme",
    )
    pyme_owner.set_password("scope-test-secret")
    db.session.add(pyme_owner)
    db.session.flush()
    pyme_tenant = TenantProfile(
        slug="municipal-ticket-cross-domain",
        nombre="Cross-domain PYME",
        tipo="pyme",
        pyme_id=pyme_owner.id,
        plan="full",
    )
    db.session.add(pyme_tenant)
    db.session.flush()

    explicit_wrong_scope = _legacy_ticket(
        pyme_owner,
        tenant_id=pyme_tenant.id,
        number="M-SCOPE-PYME-EXPLICIT",
    )
    legacy_wrong_scope = _legacy_ticket(
        pyme_owner,
        number="M-SCOPE-PYME-LEGACY",
    )

    assert (
        scoped_municipio_ticket_query(pyme_tenant)
        .filter_by(id=explicit_wrong_scope.id)
        .first()
        is None
    )
    assert not municipio_ticket_belongs_to_tenant(
        explicit_wrong_scope,
        pyme_tenant,
    )

    with pytest.raises(TicketTenantScopeError) as explicit_error:
        resolve_municipio_ticket_access_tenant(explicit_wrong_scope)
    assert explicit_error.value.code == "ticket_tenant_incompatible"

    with pytest.raises(TicketTenantScopeError) as legacy_error:
        resolve_municipio_ticket_access_tenant(legacy_wrong_scope)
    assert legacy_error.value.code == "ticket_tenant_incompatible"

    with pytest.raises(TicketTenantScopeError) as write_error:
        normalize_municipio_ticket_write_scope(
            {
                "tenant_id": pyme_tenant.id,
                "municipio_id": pyme_owner.id,
                "pregunta": "No debe adoptar un tenant PYME",
            }
        )
    assert write_error.value.code == "ticket_tenant_incompatible"

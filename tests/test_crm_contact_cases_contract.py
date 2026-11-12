from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

import services.crm_contact_cases as crm_contact_cases_service
from app import db
from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, User
from models_memory import Contact, InteractionEvent


def _user(*, email: str, role: str = "usuario", tenant_id: int | None = None, **kwargs) -> User:
    user = User(
        name=email.split("@", 1)[0],
        email=email,
        password_hash="test-hash",
        rol=role,
        tenant_id=tenant_id,
        **kwargs,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _tenant(*, slug: str, owner: User) -> TenantProfile:
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    return tenant


def _contact(*, tenant: TenantProfile, legacy_user_id=None) -> Contact:
    preferences = {}
    if legacy_user_id is not None:
        preferences["legacy_user_id"] = legacy_user_id
    contact = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name="Vecina CRM",
        phone="+5492613000999",
        type="neighbor",
        preferences=preferences,
    )
    db.session.add(contact)
    db.session.flush()
    return contact


def _auth(app, *, actor: User, tenant: TenantProfile) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": actor.id,
            "rol": actor.rol,
            "tenant_id": tenant.id,
            "tenant_slug": tenant.slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant": tenant.slug,
    }


def _history(client, app, *, actor: User, tenant: TenantProfile, contact: Contact):
    return client.get(
        f"/api/admin/tenants/{tenant.slug}/contacts/{contact.id}/history",
        headers=_auth(app, actor=actor, tenant=tenant),
    )


def _tenant_ticket(*, tenant: TenantProfile, user_id: int | None, category: str) -> TenantTicket:
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=user_id,
        categoria=category,
        descripcion=f"Solicitud {category}",
        estado="nuevo",
        origen="whatsapp",
        datos_extra={"title": f"Caso {category}"},
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket


def _municipio_ticket(*, tenant: TenantProfile, user_id: int, category: str) -> MunicipioTicket:
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        user_id=user_id,
        pregunta=f"Reclamo {category}",
        asunto=f"Caso municipal {category}",
        categoria=category,
        estado="nuevo",
        nro_ticket=f"crm-contact-{uuid4()}",
        canal_ingreso="whatsapp",
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket


def _pyme_ticket(*, tenant: TenantProfile, user_id: int, category: str) -> PymeTicket:
    ticket = PymeTicket(
        tenant_id=tenant.id,
        user_id=user_id,
        pregunta=f"Consulta {category}",
        asunto=f"Caso pyme {category}",
        categoria=category,
        estado="en_proceso",
        nro_ticket=100_000 + PymeTicket.query.count(),
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket


def _event(*, tenant: TenantProfile, contact: Contact, metadata: dict) -> InteractionEvent:
    event = InteractionEvent(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel="crm",
        direction="inbound",
        content_type="event",
        content="Vínculo de caso",
        metadata_payload=metadata,
    )
    db.session.add(event)
    return event


def test_contact_cases_contract_links_all_ticket_sources_by_exact_legacy_user(client, app):
    admin = _user(email="admin-contact-cases@test.com", role="admin")
    tenant = _tenant(slug="contact-cases-junin", owner=admin)
    citizen = _user(email="citizen-contact-cases@test.com", tenant_id=tenant.id)
    contact = _contact(tenant=tenant, legacy_user_id=citizen.id)

    tenant_ticket = _tenant_ticket(tenant=tenant, user_id=citizen.id, category="luminarias")
    municipio_ticket = _municipio_ticket(
        tenant=tenant,
        user_id=citizen.id,
        category="baches y calzada",
    )
    pyme_ticket = _pyme_ticket(tenant=tenant, user_id=citizen.id, category="consulta")
    _event(
        tenant=tenant,
        contact=contact,
        metadata={
            "source_model": "TenantTicket",
            "ticket_id": tenant_ticket.id,
        },
    )
    db.session.commit()

    response = _history(client, app, actor=admin, tenant=tenant, contact=contact)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["cases_contract_version"] == "crm.contact_cases.v1"
    assert payload["cases_total"] == 3
    assert payload["cases_truncated"] is False
    identities = {(item["source_model"], item["ticket_id"]) for item in payload["cases"]}
    assert identities == {
        ("TenantTicket", str(tenant_ticket.id)),
        ("MunicipioTicket", str(municipio_ticket.id)),
        ("PymeTicket", str(pyme_ticket.id)),
    }
    assert len(payload["cases"]) == len({item["case_key"] for item in payload["cases"]})
    for item in payload["cases"]:
        assert isinstance(item["ticket_id"], str)
        assert item["tenant_slug"] == tenant.slug
        assert f"source_model={item['source_model']}" in item["detail_href"]
        assert f"ticket_id={item['ticket_id']}" in item["detail_href"]
        assert "tenant_slug=contact-cases-junin" in item["detail_href"]


def test_contact_cases_contract_accepts_only_complete_consistent_event_identity(client, app):
    admin = _user(email="admin-event-cases@test.com", role="admin")
    tenant = _tenant(slug="event-cases-junin", owner=admin)
    other_admin = _user(email="other-admin-event-cases@test.com", role="admin")
    other_tenant = _tenant(slug="event-cases-other", owner=other_admin)
    contact = _contact(tenant=tenant)

    linked = _tenant_ticket(tenant=tenant, user_id=None, category="arbol caido")
    incomplete = _tenant_ticket(tenant=tenant, user_id=None, category="incompleto")
    conflicting = _tenant_ticket(tenant=tenant, user_id=None, category="conflicto")
    cross_tenant = _tenant_ticket(tenant=other_tenant, user_id=None, category="externo")
    _event(
        tenant=tenant,
        contact=contact,
        metadata={"source_model": "TenantTicket", "ticket_id": str(linked.id)},
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={"source_model": "TenantTicket", "ticket_id": linked.id},
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={"source_model": "TenantTicket"},
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={"ticket_id": incomplete.id},
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={
            "source_model": "TenantTicket",
            "ticket_id": conflicting.id,
            "legacy_model": "MunicipioTicket",
        },
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={
            "source_model": "TenantTicket",
            "ticket_id": conflicting.id,
            "ticketId": linked.id,
        },
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={"source_model": "TenantTicket", "ticket_id": cross_tenant.id},
    )
    db.session.commit()

    response = _history(client, app, actor=admin, tenant=tenant, contact=contact)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["cases_total"] == 1
    assert len(payload["cases"]) == 1
    assert payload["cases"][0]["source_model"] == "TenantTicket"
    assert payload["cases"][0]["ticket_id"] == str(linked.id)


def test_contact_cases_contract_applies_employee_category_scope_and_user_conflict(client, app):
    admin = _user(email="admin-scoped-cases@test.com", role="admin")
    tenant = _tenant(slug="scoped-cases-junin", owner=admin)
    employee = _user(
        email="employee-scoped-cases@test.com",
        role="empleado",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
        accesibilidad={"employee_scope": {"categorias": ["luminarias"]}},
    )
    citizen = _user(email="citizen-scoped-cases@test.com", tenant_id=tenant.id)
    other_citizen = _user(email="other-citizen-scoped-cases@test.com", tenant_id=tenant.id)
    contact = _contact(tenant=tenant, legacy_user_id=citizen.id)

    allowed = _tenant_ticket(tenant=tenant, user_id=citizen.id, category="luminarias")
    allowed_municipio = _municipio_ticket(
        tenant=tenant,
        user_id=citizen.id,
        category="luminarias",
    )
    allowed_pyme = _pyme_ticket(tenant=tenant, user_id=citizen.id, category="luminarias")
    _tenant_ticket(tenant=tenant, user_id=citizen.id, category="baches y calzada")
    _municipio_ticket(
        tenant=tenant,
        user_id=citizen.id,
        category="baches y calzada",
    )
    _pyme_ticket(tenant=tenant, user_id=citizen.id, category="baches y calzada")
    conflicting_user = _tenant_ticket(
        tenant=tenant,
        user_id=other_citizen.id,
        category="luminarias",
    )
    _event(
        tenant=tenant,
        contact=contact,
        metadata={
            "source_model": "TenantTicket",
            "ticket_id": conflicting_user.id,
        },
    )
    db.session.commit()

    response = _history(client, app, actor=employee, tenant=tenant, contact=contact)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["cases_total"] == 3
    assert {(item["source_model"], item["ticket_id"]) for item in payload["cases"]} == {
        ("TenantTicket", str(allowed.id)),
        ("MunicipioTicket", str(allowed_municipio.id)),
        ("PymeTicket", str(allowed_pyme.id)),
    }


def test_contact_cases_contract_never_uses_phone_or_email_as_ticket_relation(client, app):
    admin = _user(email="admin-no-fuzzy-cases@test.com", role="admin")
    tenant = _tenant(slug="no-fuzzy-cases-junin", owner=admin)
    citizen = _user(email="citizen-no-fuzzy-cases@test.com", tenant_id=tenant.id)
    citizen.telefono = "+5492613000999"
    contact = _contact(tenant=tenant)
    _tenant_ticket(tenant=tenant, user_id=citizen.id, category="luminarias")
    db.session.commit()

    response = _history(client, app, actor=admin, tenant=tenant, contact=contact)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["cases_contract_version"] == "crm.contact_cases.v1"
    assert payload["cases_total"] == 0
    assert payload["cases"] == []


def test_contact_cases_contract_counts_in_sql_and_caps_large_history(client, app):
    admin = _user(email="admin-large-cases@test.com", role="admin")
    tenant = _tenant(slug="large-cases-junin", owner=admin)
    citizen = _user(email="citizen-large-cases@test.com", tenant_id=tenant.id)
    contact = _contact(tenant=tenant, legacy_user_id=citizen.id)
    for index in range(crm_contact_cases_service.CRM_CONTACT_CASES_LIMIT + 5):
        _tenant_ticket(
            tenant=tenant,
            user_id=citizen.id,
            category=f"categoria-{index}",
        )
    db.session.commit()

    response = _history(client, app, actor=admin, tenant=tenant, contact=contact)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["cases_total"] == crm_contact_cases_service.CRM_CONTACT_CASES_LIMIT + 5
    assert payload["cases_total_is_exact"] is True
    assert payload["cases_truncated"] is True
    assert len(payload["cases"]) == crm_contact_cases_service.CRM_CONTACT_CASES_LIMIT


def test_contact_cases_contract_marks_bounded_event_identity_scan(client, app, monkeypatch):
    monkeypatch.setattr(crm_contact_cases_service, "CRM_CONTACT_CASE_EVENT_IDENTITY_LIMIT", 2)
    admin = _user(email="admin-bounded-events@test.com", role="admin")
    tenant = _tenant(slug="bounded-events-junin", owner=admin)
    contact = _contact(tenant=tenant)
    tickets = [
        _tenant_ticket(tenant=tenant, user_id=None, category=f"evento-{index}")
        for index in range(3)
    ]
    for ticket in tickets:
        _event(
            tenant=tenant,
            contact=contact,
            metadata={"source_model": "TenantTicket", "ticket_id": ticket.id},
        )
    db.session.commit()

    response = _history(client, app, actor=admin, tenant=tenant, contact=contact)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["cases_total"] == 2
    assert payload["cases_total_is_exact"] is False
    assert payload["cases_truncated"] is True
    assert len(payload["cases"]) == 2

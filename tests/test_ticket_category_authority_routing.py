"""Disposable SQL/HTTP regressions for municipal category read contracts.

Run after profile_acceptance_runtime.prepare_process: no customer DSN or
external network is used. Native fixture login is real; it is not nominal
customer acceptance.
"""

import secrets

import pytest
from sqlalchemy import event

from database import db
from models import CategoriaTicket, MunicipioTicket, TenantProfile, User
from services.employee_routing import tenant_open_ticket_snapshots
from services.ticket_category_authority import build_municipio_category_authorities


@pytest.fixture
def municipal_scope(client):
    password = secrets.token_urlsafe(24)
    owners, tenants = [], []
    for suffix in ("local", "foreign"):
        owner = User(
            name="Fixture administrator", email=f"category-{suffix}@example.invalid",
            rol="admin_municipio", tipo_chat="municipio", email_verified=True,
        )
        owner.set_password(password)
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=f"category-{suffix}", nombre="Fixture organization", tipo="municipio",
            municipio_id=owner.id, vertical="government", plan="full",
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        owners.append(owner)
        tenants.append(tenant)
    db.session.commit()
    login = client.post("/api/auth/admin/login", json={"email": owners[0].email, "password": password})
    assert login.status_code == 200
    token = login.get_json().get("token")
    assert isinstance(token, str) and token
    return owners, tenants, {"Authorization": "Bearer " + token, "X-Tenant-Slug": tenants[0].slug}


def ticket_for(owner, tenant, *, label="Sugerencia", category_id=None, legacy=False, number="CATEGORY-1"):
    ticket = MunicipioTicket(
        tenant_id=None if legacy else tenant.id, municipio_id=owner.id,
        categoria=label, categoria_id=category_id, pregunta="Fixture case",
        nro_ticket=number, estado="nuevo", canal_ingreso="web",
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket


def read_pair(client, tenant, ticket, headers):
    detail = client.get(f"/api/tickets/municipio/{ticket.id}", headers=headers)
    routing = client.get(f"/api/v2/tenants/{tenant.slug}/employee-routing", headers=headers)
    assert detail.status_code == 200
    assert routing.status_code == 200
    snapshot = next(row for row in routing.get_json()["queues"]["open"]
                    if row["source_model"] == "MunicipioTicket" and row["id"] == ticket.id)
    return detail.get_json(), routing.get_json(), snapshot


@pytest.mark.parametrize("label", ["Luminarias", "alumbrado público"])
@pytest.mark.parametrize("scope", ["foreign", "missing"])
def test_exact_alias_cannot_verify_a_ticket_outside_the_requested_tenant(client, municipal_scope, label, scope):
    owners, tenants, _ = municipal_scope
    ticket = ticket_for(owners[1], tenants[1], label=label)
    requested_tenant_id = tenants[0].id if scope == "foreign" else None
    authority = build_municipio_category_authorities([ticket], tenant_id=requested_tenant_id)[ticket.id]
    assert authority["verified"] is False
    assert authority["authoritative_category"] is None
    assert authority["reason_code"] == "ticket_tenant_mismatch"
    assert authority["conflict"] is False
    assert authority["source"] == "persisted_category_unverified"


def test_missing_catalog_reference_is_pending_in_detail_and_routing_without_writes(client, municipal_scope):
    owners, tenants, headers = municipal_scope
    ticket = ticket_for(owners[0], tenants[0])
    before = (CategoriaTicket.query.count(), MunicipioTicket.query.count(), ticket.asignado_a_id)
    detail, routing, snapshot = read_pair(client, tenants[0], ticket, headers)
    authority = detail["category_authority"]
    assert snapshot["category_authority"] == authority
    assert detail["authoritative_category"] is None
    assert snapshot["authoritative_category"] is None
    assert snapshot["category"] == "sugerencia"
    assert authority["persisted_category"] == "Sugerencia"
    assert authority["category_id"] is None
    assert authority["verified"] is False and authority["conflict"] is False
    assert authority["reason_code"] == "category_id_missing_and_alias_unverified"
    assert authority["message"] and authority["recovery_text"]
    assert authority["action_hint"] == "review_ticket_category"
    recommendation = next(row for row in routing["recommendations"] if row["ticket"]["id"] == ticket.id)
    assert recommendation["suggested_assignee"] is None
    assert recommendation["candidate_ids"] == []
    db.session.expire_all()
    assert before == (CategoriaTicket.query.count(), MunicipioTicket.query.count(), db.session.get(MunicipioTicket, ticket.id).asignado_a_id)


def test_verified_catalog_and_conflict_descriptor_match_without_foreign_recipient(client, municipal_scope):
    owners, tenants, headers = municipal_scope
    category = CategoriaTicket(tenant_id=tenants[0].id, nombre="Alumbrado público")
    db.session.add(category)
    db.session.flush()
    recipients = []
    for tenant in tenants:
        employee = User(
            name="Fixture employee", email=f"category-employee-{tenant.slug}@example.invalid",
            rol="empleado", es_empleado=True, tenant_id=tenant.id, tenant_slug=tenant.slug,
            municipio_id=tenant.municipio_id, tipo_chat="municipio",
            accesibilidad={"employee_scope": {"categorias": ["luminarias"]}},
        )
        employee.set_password(secrets.token_urlsafe(24))
        if tenant.id == tenants[0].id:
            employee.categorias_ticket.append(category)
        db.session.add(employee)
        recipients.append(employee)
    db.session.commit()
    ticket = ticket_for(owners[0], tenants[0], label="General", category_id=category.id)
    detail, routing, snapshot = read_pair(client, tenants[0], ticket, headers)
    authority = detail["category_authority"]
    assert snapshot["category_authority"] == authority
    assert authority["verified"] is True and authority["conflict"] is True
    assert authority["source"] == "tenant_category_catalog"
    assert authority["authoritative_category"] == "Alumbrado público"
    assert snapshot["authoritative_category"] == "alumbrado público"
    assert snapshot["category"] == "general"
    assert authority["message"] and authority["recovery_text"]
    recommendation = next(row for row in routing["recommendations"] if row["ticket"]["id"] == ticket.id)
    assert recommendation["candidate_ids"] == [recipients[0].id]
    assert recipients[1].id not in recommendation["candidate_ids"]
    assert ticket.asignado_a_id is None


def test_verified_exact_alias_keeps_its_original_policy_and_has_no_recovery_action(client, municipal_scope):
    owners, tenants, headers = municipal_scope
    ticket = ticket_for(owners[0], tenants[0], label="alumbrado público")
    detail, _, snapshot = read_pair(client, tenants[0], ticket, headers)
    authority = detail["category_authority"]
    assert snapshot["category_authority"] == authority
    assert authority["verified"] is True and authority["conflict"] is False
    assert authority["source"] == "persisted_category_exact_alias"
    assert authority["recovery_text"] is None and authority["action_hint"] is None


@pytest.mark.parametrize("invalid_reference", ["foreign", "stale"])
def test_unresolvable_reference_cannot_be_promoted_by_routing(client, municipal_scope, invalid_reference):
    owners, tenants, headers = municipal_scope
    foreign_category = CategoriaTicket(tenant_id=tenants[1].id, nombre="Private foreign catalog label")
    db.session.add(foreign_category)
    db.session.commit()
    category_id = foreign_category.id if invalid_reference == "foreign" else foreign_category.id + 1000
    ticket = ticket_for(owners[0], tenants[0], label="General", category_id=category_id)
    detail, _, snapshot = read_pair(client, tenants[0], ticket, headers)
    authority = detail["category_authority"]
    assert snapshot["category_authority"] == authority
    assert authority["verified"] is False and authority["authoritative_category"] is None
    assert snapshot["authoritative_category"] is None
    assert authority["reason_code"] == "category_not_found_in_tenant_catalog"
    assert foreign_category.nombre not in str(authority)


def test_unique_legacy_owner_remains_readable_without_faking_explicit_category_binding(client, municipal_scope):
    owners, tenants, headers = municipal_scope
    category = CategoriaTicket(tenant_id=tenants[0].id, nombre="Luminarias")
    db.session.add(category)
    db.session.commit()
    ticket = ticket_for(owners[0], tenants[0], label="General", category_id=category.id, legacy=True)
    detail, _, snapshot = read_pair(client, tenants[0], ticket, headers)
    assert snapshot["category_authority"] == detail["category_authority"]
    assert snapshot["authoritative_category"] is None
    assert snapshot["category_authority"]["reason_code"] == "ticket_tenant_mismatch"
    assert db.session.get(MunicipioTicket, ticket.id).tenant_id is None


def test_municipal_snapshot_batch_resolves_catalog_once(client, municipal_scope):
    owners, tenants, _ = municipal_scope
    category = CategoriaTicket(tenant_id=tenants[0].id, nombre="Sugerencia")
    db.session.add(category)
    db.session.commit()
    for number in range(3):
        ticket_for(owners[0], tenants[0], category_id=category.id, number=f"CATEGORY-BATCH-{number}")
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if "from categorias_ticket" in statement.lower():
            statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        snapshots = tenant_open_ticket_snapshots(tenants[0])
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert len(snapshots) == 3
    assert all(row["category_authority"]["verified"] for row in snapshots)

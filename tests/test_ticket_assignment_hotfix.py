"""Offline regressions for the production-baseline assignment backport."""
from copy import deepcopy
from types import SimpleNamespace

import jwt
import pytest

from app import db
from models import MunicipioTicket, PymeTicket, Rubro, TenantProfile, TenantTicket, TicketComentario, User
from services.ticket_assignment_policy import TicketAssignmentPolicyError, assignment_transition
from services.tenant_claim_receipts import TenantClaimValidationError, normalize_tenant_claim_payload


@pytest.fixture
def assignment_case(client, app):
    app.config.update(RATELIMIT_ENABLED=False, TWILIO_ALLOW_NETWORK_IN_TESTS=False)
    owner = User(name="Hotfix owner", email="hotfix-owner@test.local", password_hash="test-hash", rol="admin", tipo_chat="municipio")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="assignment-hotfix", nombre="Assignment Hotfix", tipo="municipio",
                           municipio_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
    operators = []
    for index in range(3):
        operator = User(name=f"Operator {index}", email=f"hotfix-operator-{index}@test.local", rol="empleado",
                        password_hash="test-hash",
                        es_empleado=True, tenant_id=tenant.id, tenant_slug=tenant.slug, tipo_chat="municipio",
                        municipio_id=owner.id, empresa_id=owner.id,
                        accesibilidad={"employee_scope": {"categorias": ["bacheo" if index < 2 else "arbolado"],
                                                          "permisos": [], "channels": ["web"]}})
        db.session.add(operator)
        operators.append(operator)
    citizen = User(name="Citizen", email="hotfix-citizen@test.local", rol="usuario", tenant_id=tenant.id,
                   password_hash="test-hash", tenant_slug=tenant.slug, tipo_chat="municipio")
    municipal = MunicipioTicket(municipio_id=owner.id, tenant_id=tenant.id, user_id=owner.id,
                               asunto="Bache", categoria="bacheo", pregunta="Bache en calle", estado="nuevo")
    modern = TenantTicket(tenant_id=tenant.id, user_id=owner.id, categoria="bacheo", descripcion="Bache en calle",
                          estado="nuevo", origen="web", datos_extra={"title": "Bache", "channel": "web"})
    business = PymeTicket(tenant_id=tenant.id, user_id=owner.id, categoria="bacheo", pregunta="Consulta",
                          estado="nuevo", nro_ticket=543210)
    db.session.add_all([citizen, municipal, modern, business])
    db.session.commit()

    def headers(user):
        token = jwt.encode({"user_id": user.id, "rol": user.rol, "tenant_slug": tenant.slug},
                           app.config["SECRET_KEY"], algorithm="HS256")
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant.slug, "X-Tenant": tenant.slug}

    return SimpleNamespace(client=client, owner=owner, tenant=tenant, operators=operators, citizen=citizen,
                           municipal=municipal, modern=modern, business=business, headers=headers)


def action(case, model, user, command="claim", **payload):
    ticket = {"MunicipioTicket": case.municipal, "TenantTicket": case.modern, "PymeTicket": case.business}[model]
    return case.client.post(f"/api/v2/inbox/omnichannel/{ticket.id}/actions", headers=case.headers(user),
                            json={"action": command, "source_model": model, "ticket_id": ticket.id, **payload})


def snapshot(case, model):
    db.session.expire_all()
    if model == "MunicipioTicket":
        return case.municipal.asignado_a_id, TicketComentario.query.filter_by(municipio_ticket_id=case.municipal.id).count()
    if model == "PymeTicket":
        return case.business.asignado_a_id, TicketComentario.query.filter_by(pyme_ticket_id=case.business.id).count()
    return case.modern.datos_extra.get("assignee_id"), len(case.modern.datos_extra.get("comments", []))


@pytest.mark.parametrize("model", ["MunicipioTicket", "TenantTicket", "PymeTicket"])
def test_claim_once_replay_and_competing_operator(assignment_case, model):
    case = assignment_case
    first = action(case, model, case.operators[0])
    assert first.status_code == 200, first.get_json()
    before = snapshot(case, model)
    replay = action(case, model, case.operators[0])
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["delivery"]["idempotent_replay"] is True
    assert snapshot(case, model) == before
    conflict = action(case, model, case.operators[1])
    assert conflict.status_code == 409, conflict.get_json()
    assert snapshot(case, model) == before
    assert before[0] == case.operators[0].id
    assert before[1] == 1


@pytest.mark.parametrize("model", ["MunicipioTicket", "TenantTicket", "PymeTicket"])
def test_assignment_supervision_expected_owner_replay_and_conflict(assignment_case, model):
    case = assignment_case
    employee = action(case, model, case.operators[0], "assign", assignee_id=case.operators[1].id, expected_assignee_id=None)
    assert employee.status_code == 403, employee.get_json()
    missing = action(case, model, case.owner, "assign", assignee_id=case.operators[0].id)
    assert missing.status_code == 400, missing.get_json()
    first = action(case, model, case.owner, "assign", assignee_id=case.operators[0].id, expected_assignee_id=None)
    assert first.status_code == 200, first.get_json()
    before = snapshot(case, model)
    assert action(case, model, case.owner, "assign", assignee_id=case.operators[0].id, expected_assignee_id=None).status_code == 200
    assert snapshot(case, model) == before
    assert action(case, model, case.owner, "assign", assignee_id=case.operators[1].id, expected_assignee_id=None).status_code == 409
    assert snapshot(case, model) == before
    final = action(case, model, case.owner, "assign", assignee_id=case.operators[1].id, expected_assignee_id=case.operators[0].id)
    assert final.status_code == 200, final.get_json()
    assert snapshot(case, model)[0] == case.operators[1].id


@pytest.mark.parametrize("model", ["MunicipioTicket", "TenantTicket"])
def test_claim_category_non_operational_and_handoff_cannot_steal(assignment_case, model):
    case = assignment_case
    assert action(case, model, case.operators[2]).status_code == 404
    assert action(case, model, case.owner).status_code == 403
    assert action(case, model, case.operators[0]).status_code == 200
    ticket = case.municipal if model == "MunicipioTicket" else case.modern
    extra = deepcopy(ticket.datos_extra or {})
    extra["handoff"] = {"state": "queued", "status": "queued", "requested_at": "2026-09-08T00:00:00Z"}
    ticket.datos_extra = extra
    db.session.commit()
    assert action(case, model, case.operators[1], "accept_handoff").status_code == 409
    assert snapshot(case, model)[0] == case.operators[0].id


@pytest.mark.parametrize("value", [True, False, 1.5, -1, 0, "1.0", "1e0", "１２", {}, []])
def test_assignment_ids_are_lossless(value):
    actor = SimpleNamespace(id=10, rol="admin", accesibilidad={})
    with pytest.raises(TicketAssignmentPolicyError):
        assignment_transition(actor=actor, payload={"expected_assignee_id": value}, current_assignee_id=None, target_assignee_id=12)


@pytest.mark.parametrize("payload", [{"source_model": "unknown"}, {"ticket_id": True}, {"ticket_id": 12345},
                                     {"legacy_model": "TenantTicket"}, {"type": "assign"}, {"id": 12345}])
def test_inbox_assignment_rejects_ambiguous_identity(assignment_case, payload):
    case = assignment_case
    response = action(case, "MunicipioTicket", case.operators[0], **payload)
    assert response.status_code == 400, response.get_json()
    assert snapshot(case, "MunicipioTicket")[0] is None
    assert snapshot(case, "TenantTicket")[0] is None


@pytest.mark.parametrize("method,path,payload", [
    ("post", "/api/admin/employees", {"email": "grant@test.local", "password": "not-a-secret"}),
    ("patch", "/api/admin/employees/{id}", {"role": "supervisor"}),
    ("put", "/api/admin/employees/{id}/scope", {"permisos": ["tickets.assign"]}),
    ("post", "/api/admin/employees/{id}/roles", {"role": "supervisor"}),
    ("post", "/api/admin/employees/{id}/categories", {"category_ids": []}),
])
def test_employee_cannot_self_grant_authority(assignment_case, method, path, payload):
    case = assignment_case
    employee = case.operators[0]
    response = getattr(case.client, method)(path.format(id=employee.id), headers=case.headers(employee), json=payload)
    assert response.status_code == 403, response.get_json()
    db.session.refresh(employee)
    assert employee.rol == "empleado"
    assert employee.accesibilidad["employee_scope"]["permisos"] == []


@pytest.mark.parametrize("container", ["extras", "metadata"])
@pytest.mark.parametrize("key", ["assignee_id", "assignee_email", "handoff", "assigned_user_id", "timeline", "comments", "handoff_history", "ASSIGNEE_ID"])
def test_public_intake_cannot_inject_assignment_state(container, key):
    with pytest.raises(TenantClaimValidationError) as raised:
        normalize_tenant_claim_payload({"descripcion": "Bache en la calle", "categoria": "bacheo", container: {key: 123}})
    assert raised.value.reason_code == "extras_reserved_key"


def test_v2_patch_and_create_cannot_bypass_assignment_authority(assignment_case):
    case = assignment_case
    assert action(case, "TenantTicket", case.operators[0]).status_code == 200
    response = case.client.patch(f"/api/v2/tickets/{case.modern.id}", headers=case.headers(case.operators[1]),
                                 json={"assignee_id": None, "expected_assignee_id": case.operators[0].id})
    assert response.status_code == 403, response.get_json()
    response = case.client.post("/api/v2/tickets", headers=case.headers(case.citizen),
                               json={"title": "Nuevo caso", "description": "Detalle", "category": "bacheo",
                                     "assignee_id": case.operators[0].id, "expected_assignee_id": None})
    assert response.status_code == 403, response.get_json()
    response = case.client.post("/api/v2/tickets", headers=case.headers(case.citizen),
                               json={"title": "Nuevo caso", "description": "Detalle", "category": "bacheo"})
    assert response.status_code == 201, response.get_json()


def test_routing_only_publishes_employee_authorized_categories(assignment_case):
    case = assignment_case
    response = case.client.get("/api/v2/employee-routing", headers=case.headers(case.operators[2]))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["queues"]["open"] == []
    assert [item["id"] for item in response.get_json()["employees"]] == [case.operators[2].id]


def test_supervisor_reads_and_assigns_without_becoming_the_assignee(assignment_case):
    case = assignment_case
    supervisor = case.operators[1]
    supervisor.rol = "supervisor"
    db.session.commit()
    for path in ("/api/v2/inbox/omnichannel", f"/api/v2/inbox/omnichannel/{case.municipal.id}?source_model=MunicipioTicket"):
        response = case.client.get(path, headers=case.headers(supervisor))
        assert response.status_code == 200, response.get_json()
    response = action(case, "MunicipioTicket", supervisor, "assign", assignee_id=case.operators[0].id, expected_assignee_id=None)
    assert response.status_code == 200, response.get_json()
    assert snapshot(case, "MunicipioTicket")[0] == case.operators[0].id


def test_legacy_assignment_obeys_supervision_cas_and_self_claim(assignment_case):
    case = assignment_case
    path = f"/tickets/municipio/{case.municipal.id}/asignar"
    first = case.client.post(path, headers=case.headers(case.operators[0]), json={})
    assert first.status_code == 200, first.get_json()
    before = snapshot(case, "MunicipioTicket")
    replay = case.client.post(path, headers=case.headers(case.operators[0]), json={})
    assert replay.status_code == 200, replay.get_json()
    assert snapshot(case, "MunicipioTicket") == before
    missing = case.client.post(path, headers=case.headers(case.owner), json={"user_id": case.operators[1].id})
    assert missing.status_code == 400, missing.get_json()
    stale = case.client.post(path, headers=case.headers(case.owner), json={"user_id": case.operators[1].id, "expected_assignee_id": None})
    assert stale.status_code == 409, stale.get_json()
    valid = case.client.post(path, headers=case.headers(case.owner), json={"user_id": case.operators[1].id, "expected_assignee_id": case.operators[0].id})
    assert valid.status_code == 200, valid.get_json()
    assert snapshot(case, "MunicipioTicket")[0] == case.operators[1].id


def test_serializers_and_advertised_assignment_contract_publish_identity(assignment_case):
    case = assignment_case
    response = case.client.get("/api/tickets", headers=case.headers(case.owner))
    assert response.status_code == 200, response.get_json()
    ticket = next(item for item in response.get_json()["tickets"] if item["id"] == case.municipal.id and item["tipo"] == "municipio")
    assert ticket["source_model"] == "MunicipioTicket"
    response = case.client.get(f"/tickets/municipio/{case.municipal.id}", headers=case.headers(case.owner))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["source_model"] == "MunicipioTicket"
    for model, source in (("MunicipioTicket", case.municipal), ("TenantTicket", case.modern)):
        response = case.client.get(f"/api/v2/inbox/omnichannel/{source.id}?source_model={model}", headers=case.headers(case.owner))
        assert response.status_code == 200, response.get_json()
        actions = {item["id"]: item for item in response.get_json()["ticket"]["allowed_actions"]}
        assert "claim" in actions
        assert "expected_assignee_id" in actions["assign"]["requires"]
        assert actions["assign"]["payload_defaults"]["source_model"] == model


def test_bulk_autoassign_requires_explicit_per_ticket_expected_owner(assignment_case):
    case = assignment_case
    path = "/api/v2/employee-routing/auto-assign"
    payload = {"dry_run": False, "tickets": [{"source_model": "TenantTicket", "id": case.modern.id}]}
    assert case.client.post(path, headers=case.headers(case.owner), json=payload).status_code == 400
    payload["tickets"][0]["expected_assignee_id"] = None
    response = case.client.post(path, headers=case.headers(case.owner), json=payload)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["applied_count"] == 1


def test_pyme_explicit_foreign_tenant_never_uses_same_rubro_fallback(assignment_case):
    from services.employee_routing import find_ticket_for_assignment, pyme_ticket_query_for_tenant
    case = assignment_case
    rubro = Rubro(nombre="Shared", clave="shared-hotfix")
    foreign_owner = User(name="Foreign", email="foreign-owner@test.local", password_hash="test-hash", rol="admin")
    db.session.add(foreign_owner)
    db.session.flush()
    foreign = TenantProfile(slug="foreign-hotfix", nombre="Foreign", tipo="pyme", pyme_id=foreign_owner.id)
    db.session.add_all([rubro, foreign])
    db.session.flush()
    case.owner.rubro_id = rubro.id
    case.business.rubro_id = rubro.id
    case.business.tenant_id = foreign.id
    db.session.commit()
    assert pyme_ticket_query_for_tenant(case.tenant).filter_by(id=case.business.id).first() is None
    assert find_ticket_for_assignment(case.tenant, "PymeTicket", case.business.id) is None
    response = action(case, "PymeTicket", case.operators[0])
    assert response.status_code == 404, response.get_json()


def test_comment_refreshes_stale_json_before_preserving_new_assignment(assignment_case, app):
    from sqlalchemy.orm import Session
    from services.v2.ticket_service import add_comment
    case = assignment_case
    stale = case.modern
    assert stale.datos_extra.get("assignee_id") is None
    # Independent ORM identity map emulates a committed writer while this request
    # still holds a stale object. SQLite does not prove PostgreSQL lock ordering.
    with Session(db.engine) as other:
        fresh = other.get(TenantTicket, stale.id)
        fresh.datos_extra = {**fresh.datos_extra, "assignee_id": case.operators[0].id}
        other.commit()
    with app.test_request_context():
        add_comment(tenant=case.tenant, actor_user=case.operators[0], ticket=stale, body="Nota interna", visibility="internal")
        db.session.commit()
    assert snapshot(case, "TenantTicket")[0] == case.operators[0].id


def test_public_intake_recursive_extras_cannot_smuggle_assignment():
    with pytest.raises(TenantClaimValidationError) as raised:
        normalize_tenant_claim_payload({"descripcion": "Bache en la calle", "categoria": "bacheo",
                                        "extras": {"nested": [{"ASSIGNEE_ID": 123}]}})
    assert raised.value.reason_code == "extras_reserved_key"


def test_explicit_get_model_never_falls_back_to_colliding_other_table(assignment_case):
    case = assignment_case
    # IDs collide in independent backing tables; an explicit identity is final.
    assert case.modern.id == case.municipal.id
    path = f"/api/v2/inbox/omnichannel/{case.municipal.id}"
    invalid = case.client.get(path + "?source_model=unknown", headers=case.headers(case.owner))
    assert invalid.status_code == 400, invalid.get_json()
    pyme = case.client.get(path + "?source_model=PymeTicket", headers=case.headers(case.owner))
    assert pyme.status_code == 200, pyme.get_json()
    assert pyme.get_json()["ticket"]["source_model"] == "PymeTicket"
    db.session.delete(case.modern)
    db.session.commit()
    missing = case.client.get(path + "?source_model=TenantTicket", headers=case.headers(case.owner))
    assert missing.status_code == 404, missing.get_json()
    legacy = case.client.get(path + "?source_model=MunicipioTicket", headers=case.headers(case.owner))
    assert legacy.status_code == 200, legacy.get_json()


def test_explicit_capability_assigns_but_cannot_grant_itself_more_scope(assignment_case):
    case = assignment_case
    actor = case.operators[0]
    actor.accesibilidad = {"employee_scope": {"categorias": ["bacheo"], "permisos": ["tickets.assign"]}}
    db.session.commit()
    response = action(case, "TenantTicket", actor, "assign", assignee_id=case.operators[1].id, expected_assignee_id=None)
    assert response.status_code == 200, response.get_json()
    assert snapshot(case, "TenantTicket")[0] == case.operators[1].id
    response = case.client.put(f"/api/admin/employees/{actor.id}/scope", headers=case.headers(actor),
                               json={"permisos": ["anything.admin"]})
    assert response.status_code == 403, response.get_json()


def test_legacy_assignment_foreign_employee_cannot_use_matching_owner_or_rubro(assignment_case):
    from services.ticket_service import ServicioTickets
    case = assignment_case
    foreign_owner = User(name="Foreign", email="foreign-legacy@test.local", password_hash="test-hash", rol="admin")
    db.session.add(foreign_owner)
    db.session.flush()
    foreign_tenant = TenantProfile(slug="foreign-legacy", nombre="Foreign", tipo="pyme", pyme_id=foreign_owner.id)
    db.session.add(foreign_tenant)
    db.session.flush()
    target = case.operators[1]
    target.tenant_id = foreign_tenant.id
    # The stale owner link remains deliberately inconsistent with explicit tenant.
    assert target.empresa_id == case.owner.id
    db.session.commit()
    response = case.client.post(f"/tickets/municipio/{case.municipal.id}/asignar", headers=case.headers(case.owner),
                               json={"user_id": target.id, "expected_assignee_id": None})
    assert response.status_code == 400, response.get_json()
    assert snapshot(case, "MunicipioTicket")[0] is None
    assert User.query.filter(ServicioTickets()._pyme_employee_scope_filter(case.business, case.owner.id),
                             User.id == target.id).first() is None


def test_legacy_assignment_rechecks_actor_category_after_lock_refresh(assignment_case, monkeypatch):
    import services.ticket_assignment_policy as policy
    case = assignment_case
    actor = case.operators[0]
    actor.accesibilidad = {"employee_scope": {"categorias": ["bacheo"], "permisos": ["tickets.assign"]}}
    case.operators[1].ticket_categorias = "bacheo,arbolado"
    db.session.commit()
    original_lock = policy.lock_assignment_ticket

    def category_changed_while_waiting(ticket):
        from sqlalchemy.orm import Session
        with Session(db.engine) as other:
            other.get(MunicipioTicket, ticket.id).categoria = "arbolado"
            other.commit()
        return original_lock(ticket)

    monkeypatch.setattr(policy, "lock_assignment_ticket", category_changed_while_waiting)
    response = case.client.post(f"/tickets/municipio/{case.municipal.id}/asignar", headers=case.headers(actor),
                               json={"user_id": case.operators[1].id, "expected_assignee_id": None})
    assert response.status_code == 404, response.get_json()
    assert snapshot(case, "MunicipioTicket")[0] is None


@pytest.mark.parametrize("age_hours,expected_status", [(90 * 24, "breached"), (1, "warning")])
def test_list_sla_is_consistent_and_does_not_rewrite_assignment_json(assignment_case, age_hours, expected_status):
    from datetime import datetime, timedelta
    case = assignment_case
    case.modern.created_at = datetime.utcnow() - timedelta(hours=age_hours)
    case.modern.datos_extra = {**case.modern.datos_extra, "priority": "urgent"}
    db.session.commit()
    before = deepcopy(case.modern.datos_extra)
    response = case.client.get("/api/v2/tickets", headers=case.headers(case.owner))
    assert response.status_code == 200, response.get_json()
    item = next(item for item in response.get_json()["items"] if item["id"] == case.modern.id)
    assert item["overdue"] is (expected_status == "breached")
    assert item["sla_status"] == item["sla_state"] == expected_status
    assert item["sla"]["resolution_due_at"]
    assert not db.session.dirty
    db.session.commit()
    db.session.refresh(case.modern)
    assert case.modern.datos_extra == before

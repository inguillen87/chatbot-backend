"""Authenticated employee-scope updates distinguish omission and explicit clearing."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from models import CategoriaTicket, TenantProfile, User, UserRole, db
from services.auth_session_lifecycle import issue_token
from services.ticket_assignment_policy import actor_can_assign_tickets

INITIAL = {
    "categorias": ["arbolado"],
    "zonas": ["centro"],
    "channels": ["whatsapp"],
    "permisos": ["tickets.assign"],
}


def _user(label, role="admin"):
    actor = User(name="Synthetic " + label, email=label + "@example.invalid",
                 rol=role, tipo_chat="municipio", es_empleado=role == "empleado")
    actor.set_password("synthetic-scope-revocation-only")
    db.session.add(actor)
    db.session.flush()
    return actor


def _headers(actor, tenant):
    claims = {"user_id": actor.id, "rol": actor.rol, "tipo_chat": actor.tipo_chat,
              "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    return {"Authorization": "Bearer " + issue_token(claims),
            "X-Tenant-Slug": tenant.slug}


@pytest.fixture
def context(client):
    assert str(db.engine.url) == "sqlite:///:memory:"
    owners = [_user("scope-owner-a"), _user("scope-owner-b")]
    tenants = []
    employees = []
    for suffix, owner in zip(("a", "b"), owners):
        tenant = TenantProfile(slug="scope-probe-" + suffix,
                               nombre="Synthetic scope " + suffix,
                               tipo="municipio", municipio_id=owner.id,
                               is_active=True, plan="enterprise")
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        employee = _user("scope-employee-" + suffix, "empleado")
        employee.tenant_id, employee.tenant_slug, employee.municipio_id = tenant.id, tenant.slug, owner.id
        employee.accesibilidad = {"employee_scope": deepcopy(INITIAL)}
        employee.categorias_lista = INITIAL["categorias"]
        category = CategoriaTicket(nombre="arbolado", tenant_id=tenant.id)
        db.session.add(category)
        db.session.flush()
        employee.categorias_ticket = [category]
        tenants.append(tenant)
        employees.append(employee)
    db.session.commit()
    for employee in employees:
        assert employee.rol == "empleado" and employee.es_empleado is True
        assert UserRole.query.filter_by(user_id=employee.id).count() == 0
        assert actor_can_assign_tickets(employee) is True
    return {"owners": owners, "tenants": tenants, "employees": employees}


def _state(employee):
    db.session.expire_all()
    refreshed = db.session.get(User, employee.id)
    return deepcopy((refreshed.accesibilidad or {}).get("employee_scope") or {}), refreshed




def _put(client, context, payload, *, target_index=0, actor=None):
    owner = actor or context["owners"][0]
    employee = context["employees"][target_index]
    return client.put("/api/admin/employees/" + str(employee.id),
                      json=payload, headers=_headers(owner, context["tenants"][0]))


def _readback(client, context, employee):
    response = client.get("/api/admin/employees",
                          headers=_headers(context["owners"][0], context["tenants"][0]))
    rows = response.get_json(silent=True)
    assert response.status_code == 200 and isinstance(rows, list)
    matches = [row for row in rows if row.get("id") == employee.id]
    return response.status_code, (matches[0].get("scope") if matches else None)


@pytest.mark.parametrize("field", ("permisos", "zonas", "channels"))
def test_explicit_empty_scope_dimension_is_persisted_as_revocation(client, context, field):
    employee = context["employees"][0]
    before, _ = _state(employee)
    desired = deepcopy(INITIAL)
    desired[field] = []
    # This is the current InternalUsers edit payload shape; canonical names,
    # no malformed input, and no privilege or organization switch.
    response = _put(client, context, {
        "name": "Synthetic edited employee",
        "roles": ["empleado"],
        "categorias": ["arbolado"],
        "scope": desired,
    })
    body = response.get_json(silent=True) or {}
    returned = ((body.get("employee") or {}).get("scope") or {})
    persisted, refreshed = _state(employee)
    readback_status, readback_scope = _readback(client, context, employee)
    assert response.status_code == 200
    assert returned[field] == [], returned
    assert persisted[field] == [], persisted
    assert readback_scope[field] == [], readback_scope
    for other in INITIAL:
        if other != field:
            assert persisted[other] == INITIAL[other]
    if field == "permisos":
        assert actor_can_assign_tickets(refreshed) is False


def test_omitted_scope_preserves_existing_scope(client, context):
    employee = context["employees"][0]
    response = _put(client, context, {"name": "Synthetic name-only edit"})
    persisted, _ = _state(employee)
    readback_status, readback_scope = _readback(client, context, employee)
    assert response.status_code == 200
    assert persisted == INITIAL
    assert readback_scope == INITIAL


def test_empty_canonical_permissions_never_silently_restores_nonempty_alias(client, context):
    employee = context["employees"][0]
    before, _ = _state(employee)
    response = _put(client, context, {"scope": {"permisos": [], "permissions": ["tickets.assign"]}})
    body = response.get_json(silent=True) or {}
    persisted, refreshed = _state(employee)
    readback_status, readback_scope = _readback(client, context, employee)
    # The contract may prefer canonical-by-presence or explicitly reject a
    # conflict. A successful response retaining the alias permission is unsafe.
    if response.status_code in (400, 409):
        assert body.get("error") or body.get("reason_code")
        assert persisted == before and readback_scope == before
    else:
        assert response.status_code == 200
        assert persisted["permisos"] == [] and readback_scope["permisos"] == []
        assert actor_can_assign_tickets(refreshed) is False


def test_nonempty_scope_replacement_control(client, context):
    employee = context["employees"][0]
    replacement = {"categorias": ["arbolado"], "zonas": ["norte"],
                   "channels": ["web"], "permisos": ["tickets.read"]}
    # Canonical nonempty values must take precedence over contradictory aliases.
    request_scope = {**replacement, "categories": ["otros"], "zones": ["sur"],
                     "canales": ["whatsapp"], "permissions": ["tickets.assign"]}
    response = _put(client, context, {"scope": request_scope})
    persisted, refreshed = _state(employee)
    readback_status, readback_scope = _readback(client, context, employee)
    assert response.status_code == 200
    assert persisted == replacement
    assert readback_scope == replacement
    assert actor_can_assign_tickets(refreshed) is False


def test_cross_tenant_target_is_not_mutated(client, context):
    employee = context["employees"][1]
    before, _ = _state(employee)
    response = _put(client, context, {"scope": {"permisos": []}}, target_index=1)
    persisted, _ = _state(employee)
    readback_status, readback_scope = _readback(client, context, employee)
    assert response.status_code == 404
    assert persisted == before
    assert readback_scope is None


@pytest.mark.parametrize("field,alias", (("permisos", "permissions"),
                                         ("zonas", "zones"),
                                         ("channels", "canales")))
def test_empty_alias_clears_only_the_present_dimension(client, context, field, alias):
    employee = context["employees"][0]
    response = _put(client, context, {"scope": {alias: []}})
    persisted, refreshed = _state(employee)
    _, readback = _readback(client, context, employee)
    expected = {**deepcopy(INITIAL), field: []}
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == expected
    assert persisted == expected and readback == expected
    if field == "permisos":
        assert actor_can_assign_tickets(refreshed) is False


@pytest.mark.parametrize("field", ("permisos", "zonas", "channels"))
def test_partial_patch_preserves_omitted_dimensions(client, context, field):
    employee = context["employees"][0]
    response = client.patch("/api/admin/employees/" + str(employee.id),
                            json={"scope": {field: []}},
                            headers=_headers(context["owners"][0], context["tenants"][0]))
    persisted, _ = _state(employee)
    _, readback = _readback(client, context, employee)
    expected = {**deepcopy(INITIAL), field: []}
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == expected
    assert persisted == expected and readback == expected


@pytest.mark.parametrize("field,alias", (("zonas", "zones"), ("channels", "canales")))
def test_empty_canonical_routing_dimension_wins_over_nonempty_alias(client, context, field, alias):
    employee = context["employees"][0]
    response = _put(client, context, {"scope": {field: [], alias: INITIAL[field]}})
    persisted, _ = _state(employee)
    _, readback = _readback(client, context, employee)
    expected = {**deepcopy(INITIAL), field: []}
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == expected
    assert persisted == expected and readback == expected


def test_empty_scope_object_preserves_all_dimensions(client, context):
    employee = context["employees"][0]
    response = _put(client, context, {"scope": {}})
    persisted, _ = _state(employee)
    _, readback = _readback(client, context, employee)
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == INITIAL
    assert persisted == INITIAL and readback == INITIAL


FIELDS = (("permisos", "permissions", ["tickets.read"]),
          ("zonas", "zones", ["norte"]),
          ("channels", "canales", ["web"]))


@pytest.mark.parametrize("field,alias,replacement", FIELDS)
@pytest.mark.parametrize("label,value", (("null", None), ("false", False),
    ("zero", 0), ("empty-string", ""), ("empty-object", {})))
def test_falsy_nonarray_canonical_preserves_historical_scope(client, context, field, alias, replacement, label, value):
    employee = context["employees"][0]
    response = _put(client, context, {"scope": {field: value}})
    persisted, refreshed = _state(employee)
    _, readback = _readback(client, context, employee)
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == INITIAL
    assert persisted == INITIAL and readback == INITIAL
    assert actor_can_assign_tickets(refreshed) is True


@pytest.mark.parametrize("field,alias,replacement", FIELDS)
def test_null_canonical_allows_nonempty_legacy_alias(client, context, field, alias, replacement):
    employee = context["employees"][0]
    response = _put(client, context, {"scope": {field: None, alias: replacement}})
    persisted, refreshed = _state(employee)
    _, readback = _readback(client, context, employee)
    expected = {**deepcopy(INITIAL), field: replacement}
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == expected
    assert persisted == expected and readback == expected
    if field == "permisos":
        assert actor_can_assign_tickets(refreshed) is False


@pytest.mark.parametrize("field,alias,csv,normalized", (
    ("permisos", "permissions", " tickets.read, tickets.comment, tickets.read ", ["tickets.read", "tickets.comment"]),
    ("zonas", "zones", " norte, sur, norte ", ["norte", "sur"]),
    ("channels", "canales", " web, email, web ", ["web", "email"]),
))
@pytest.mark.parametrize("use_alias", (False, True))
def test_nonempty_csv_scope_keeps_existing_normalization(client, context, field, alias, csv, normalized, use_alias):
    employee = context["employees"][0]
    request_scope = {field: None, alias: csv} if use_alias else {field: csv}
    response = _put(client, context, {"scope": request_scope})
    persisted, refreshed = _state(employee)
    _, readback = _readback(client, context, employee)
    expected = {**deepcopy(INITIAL), field: normalized}
    assert response.status_code == 200
    assert response.get_json()["employee"]["scope"] == expected
    assert persisted == expected and readback == expected
    if field == "permisos":
        assert actor_can_assign_tickets(refreshed) is False

"""Routing-matrix category replacement must revoke stale grants consistently.

Real Flask authorization, ORM persistence, private GET and category policy;
synthetic actors and disposable SQLite supplied by the isolated test runner.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from models import CategoriaTicket, TenantProfile, TenantTicket, User, db
from services.auth_session_lifecycle import issue_token
from services.employee_routing import employee_scope
from services.employee_ticket_access import (
    employee_ticket_category_values_allow,
    ticket_assignee_is_compatible,
)

OTHER_DIMENSIONS = {
    "zonas": ["centro"],
    "channels": ["whatsapp"],
    "permisos": ["tickets.read"],
}


def _headers(actor, tenant):
    token = issue_token({
        "user_id": actor.id, "rol": actor.rol, "tipo_chat": actor.tipo_chat,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    })
    return {"Authorization": "Bearer " + token, "X-Tenant-Slug": tenant.slug}


@pytest.fixture
def routing_context(client):
    # Reject a non-disposable database before any fixture write.
    assert str(db.engine.url) == "sqlite:///:memory:"
    records = []
    for suffix in ("a", "b"):
        owner = User(name="Synthetic owner " + suffix,
                     email="routing-category-owner-" + suffix + "@example.invalid",
                     rol="admin", tipo_chat="municipio")
        owner.set_password("synthetic-local-category-scope-only")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="routing-category-" + suffix,
                               nombre="Synthetic category routing " + suffix,
                               tipo="municipio", municipio_id=owner.id,
                               is_active=True, plan="enterprise")
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        old_category = CategoriaTicket(nombre="arbolado", tenant_id=tenant.id)
        new_category = CategoriaTicket(nombre="bacheo", tenant_id=tenant.id)
        db.session.add_all([old_category, new_category])
        db.session.flush()
        employee = User(name="Synthetic operator " + suffix,
                        email="routing-category-operator-" + suffix + "@example.invalid",
                        rol="empleado", tipo_chat="municipio", es_empleado=True,
                        tenant_id=tenant.id, tenant_slug=tenant.slug, municipio_id=owner.id)
        employee.set_password("synthetic-local-category-scope-only")
        employee.accesibilidad = {
            "employee_scope": {"categorias": ["arbolado"], **deepcopy(OTHER_DIMENSIONS)}
        }
        employee.categorias_lista = ["arbolado"]
        employee.categorias_ticket = [old_category]
        db.session.add(employee)
        db.session.flush()
        old_ticket = TenantTicket(tenant_id=tenant.id, user_id=owner.id,
                                  categoria="arbolado", descripcion="Synthetic old-category case",
                                  estado="nuevo", origen="web", datos_extra={})
        new_ticket = TenantTicket(tenant_id=tenant.id, user_id=owner.id,
                                  categoria="bacheo", descripcion="Synthetic new-category case",
                                  estado="nuevo", origen="web", datos_extra={})
        db.session.add_all([old_ticket, new_ticket])
        db.session.flush()
        records.append({"owner": owner, "tenant": tenant, "employee": employee,
                        "old_category": old_category, "new_category": new_category,
                        "old_ticket": old_ticket, "new_ticket": new_ticket})
    db.session.commit()
    return records


def _patch(client, record, categories, *, target=None):
    return client.patch(
        "/api/v2/employees/" + str((target or record["employee"]).id) + "/routing-scope",
        json={"categorias": categories, **deepcopy(OTHER_DIMENSIONS)},
        headers=_headers(record["owner"], record["tenant"]),
    )


def _observed(client, record, response):
    employee_id = record["employee"].id
    db.session.expire_all()
    employee = db.session.get(User, employee_id)
    persisted = deepcopy((employee.accesibilidad or {}).get("employee_scope") or {})
    old_status = client.get(
        "/api/v2/tickets/" + str(record["old_ticket"].id),
        headers=_headers(employee, record["tenant"]),
    ).status_code
    new_status = client.get(
        "/api/v2/tickets/" + str(record["new_ticket"].id),
        headers=_headers(employee, record["tenant"]),
    ).status_code
    return {
        "returned_categories": response.get_json()["employee"]["scope"]["categorias"],
        "persisted_categories": persisted.get("categorias"),
        "effective_categories": employee_scope(employee)["categorias"],
        "old_name_or_id_access": employee_ticket_category_values_allow(
            employee, category="arbolado", category_id=record["old_category"].id),
        "old_assignee_compatible": ticket_assignee_is_compatible(employee, record["old_ticket"]),
        "new_assignee_compatible": ticket_assignee_is_compatible(employee, record["new_ticket"]),
        "old_ticket_http": old_status,
        "new_ticket_http": new_status,
        "other_dimensions": {key: persisted.get(key) for key in OTHER_DIMENSIONS},
    }


def _assert_initial_access(client, record):
    employee = record["employee"]
    assert employee_ticket_category_values_allow(
        employee, category="arbolado", category_id=record["old_category"].id)
    response = client.get(
        "/api/v2/tickets/" + str(record["old_ticket"].id),
        headers=_headers(employee, record["tenant"]),
    )
    assert response.status_code == 200, response.get_json()


def test_matrix_clear_revokes_a_persisted_category_across_read_and_assignment(client, routing_context):
    record = routing_context[0]
    _assert_initial_access(client, record)
    response = _patch(client, record, [])
    assert response.status_code == 200, response.get_json()
    assert _observed(client, record, response) == {
        "returned_categories": [], "persisted_categories": [], "effective_categories": [],
        "old_name_or_id_access": False, "old_assignee_compatible": False,
        "new_assignee_compatible": False, "old_ticket_http": 404, "new_ticket_http": 404,
        "other_dimensions": OTHER_DIMENSIONS,
    }


def test_matrix_replacement_removes_old_category_without_losing_new_category(client, routing_context):
    record = routing_context[0]
    _assert_initial_access(client, record)
    response = _patch(client, record, ["bacheo"])
    assert response.status_code == 200, response.get_json()
    assert _observed(client, record, response) == {
        "returned_categories": ["bacheo"], "persisted_categories": ["bacheo"],
        "effective_categories": ["bacheo"], "old_name_or_id_access": False,
        "old_assignee_compatible": False, "new_assignee_compatible": True,
        "old_ticket_http": 404, "new_ticket_http": 200,
        "other_dimensions": OTHER_DIMENSIONS,
    }


def test_clear_without_legacy_relation_is_a_valid_control(client, routing_context):
    record = routing_context[0]
    record["employee"].categorias_ticket = []
    db.session.commit()
    _assert_initial_access(client, record)
    response = _patch(client, record, [])
    assert response.status_code == 200, response.get_json()
    observed = _observed(client, record, response)
    assert observed["returned_categories"] == []
    assert observed["persisted_categories"] == []
    assert observed["effective_categories"] == []
    assert observed["old_name_or_id_access"] is False
    assert observed["old_ticket_http"] == 404
    assert observed["other_dimensions"] == OTHER_DIMENSIONS


def test_foreign_employee_is_not_changed_by_current_tenant_scope_update(client, routing_context):
    current, foreign = routing_context
    target_id = foreign["employee"].id
    before_scope = deepcopy(foreign["employee"].accesibilidad)
    before_relations = [item.id for item in foreign["employee"].categorias_ticket]
    response = _patch(client, current, [], target=foreign["employee"])
    assert response.status_code == 404, response.get_json()
    db.session.expire_all()
    target = db.session.get(User, target_id)
    assert target.accesibilidad == before_scope
    assert [item.id for item in target.categorias_ticket] == before_relations



def test_replacement_preserves_owned_category_id_only_access(client, routing_context):
    record = routing_context[0]
    _assert_initial_access(client, record)
    response = _patch(client, record, ["bacheo"])
    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    employee = db.session.get(User, record["employee"].id)
    assert {category.id for category in employee.categorias_ticket} == {record["new_category"].id}
    assert employee_ticket_category_values_allow(
        employee, category=None, category_id=record["new_category"].id)
    assert not employee_ticket_category_values_allow(
        employee, category=None, category_id=record["old_category"].id)


def test_territorial_alias_matches_only_normalized_owned_catalog_rows(client, routing_context):
    record, foreign = routing_context
    owned_alias = CategoriaTicket(nombre=" LUMINARIA ", tenant_id=record["tenant"].id)
    foreign_alias = CategoriaTicket(nombre="luminarias", tenant_id=foreign["tenant"].id)
    ticket = TenantTicket(tenant_id=record["tenant"].id, user_id=record["owner"].id,
                          categoria="luminarias", descripcion="Synthetic alias case",
                          estado="nuevo", origen="web", datos_extra={})
    db.session.add_all([owned_alias, foreign_alias, ticket])
    db.session.commit()
    owned_id, foreign_id, ticket_id = owned_alias.id, foreign_alias.id, ticket.id
    response = _patch(client, record, [" ALUMBRADO PÚBLICO "])
    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    employee = db.session.get(User, record["employee"].id)
    assert employee.categorias_lista == ["alumbrado público"]
    assert {category.id for category in employee.categorias_ticket} == {owned_id}
    assert "luminarias" in employee_scope(employee)["categorias"]
    assert "arbolado" not in employee_scope(employee)["categorias"]
    assert employee_ticket_category_values_allow(employee, category=None, category_id=owned_id)
    assert not employee_ticket_category_values_allow(employee, category=None, category_id=foreign_id)
    assert client.get("/api/v2/tickets/" + str(ticket_id),
                      headers=_headers(employee, record["tenant"])).status_code == 200


def test_matching_foreign_catalog_ids_are_not_linked_or_granted(client, routing_context):
    record, foreign = routing_context
    foreign_category_id = foreign["new_category"].id
    foreign_employee_id = foreign["employee"].id
    foreign_scope = deepcopy(foreign["employee"].accesibilidad)
    response = _patch(client, record, ["bacheo"])
    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    employee = db.session.get(User, record["employee"].id)
    assert {category.id for category in employee.categorias_ticket} == {record["new_category"].id}
    assert not employee_ticket_category_values_allow(
        employee, category=None, category_id=foreign_category_id)
    assert client.get("/api/v2/tickets/" + str(foreign["new_ticket"].id),
                      headers=_headers(employee, record["tenant"])).status_code == 404
    assert db.session.get(CategoriaTicket, foreign_category_id).tenant_id == foreign["tenant"].id
    assert db.session.get(User, foreign_employee_id).accesibilidad == foreign_scope
    assert {category.id for category in CategoriaTicket.query.filter_by(
        tenant_id=record["tenant"].id).all()} == {record["old_category"].id, record["new_category"].id}


def test_nested_field_aliases_keep_other_dimensions_and_preferences(client, routing_context):
    record = routing_context[0]
    settings = deepcopy(record["employee"].accesibilidad)
    settings["ui_preferences"] = {"high_contrast": True}
    record["employee"].accesibilidad = settings
    db.session.commit()
    response = client.patch(
        "/api/v2/employees/" + str(record["employee"].id) + "/routing-scope",
        json={"employee_scope": {"categories": [" BACHEO "], "zones": [" NORTE "],
                                 "canales": [" WEB "], "permissions": [" tickets.read "]}},
        headers=_headers(record["owner"], record["tenant"]),
    )
    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    employee = db.session.get(User, record["employee"].id)
    assert employee.accesibilidad["employee_scope"] == {
        "categorias": ["bacheo"], "zonas": ["norte"], "channels": ["web"],
        "permisos": ["tickets.read"],
    }
    assert employee.accesibilidad["ui_preferences"] == {"high_contrast": True}
    assert {category.id for category in employee.categorias_ticket} == {record["new_category"].id}
    assert client.get("/api/v2/tickets/" + str(record["old_ticket"].id),
                      headers=_headers(employee, record["tenant"])).status_code == 404


def test_full_scope_replacement_keeps_omitted_categories_empty(client, routing_context):
    record = routing_context[0]
    _assert_initial_access(client, record)
    response = client.patch(
        "/api/v2/employees/" + str(record["employee"].id) + "/routing-scope",
        json=deepcopy(OTHER_DIMENSIONS),
        headers=_headers(record["owner"], record["tenant"]),
    )
    assert response.status_code == 200, response.get_json()
    observed = _observed(client, record, response)
    assert observed["returned_categories"] == []
    assert observed["persisted_categories"] == []
    assert observed["effective_categories"] == []
    assert observed["old_ticket_http"] == 404
    assert observed["old_name_or_id_access"] is False
    assert observed["other_dimensions"] == OTHER_DIMENSIONS

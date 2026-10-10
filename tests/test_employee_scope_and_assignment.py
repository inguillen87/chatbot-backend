import jwt

from app import db
from models import CategoriaTicket, TenantProfile, User
from services.employee_routing import filter_employee_category_labels


def _headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": "tenant-scope"}


def test_employee_category_filter_rejects_contact_noise_even_if_known():
    known = {"juancito", "luminaria", "arreglo de calle"}

    assert filter_employee_category_labels(["juancito", "luminaria", "arreglo de calle"], known_categories=known) == [
        "luminaria",
        "arreglo de calle",
    ]


def test_employee_scope_update_and_suggest_assignee(client, app):
    owner = User(email="owner-scope@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-scope", nombre="Tenant Scope", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    emp = User(email="emp-scope@test.com", name="Emp", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    emp.set_password("pass")
    db.session.add(emp)
    db.session.commit()

    scope_resp = client.put(
        f"/api/admin/employees/{emp.id}/scope",
        json={"categorias": ["luminaria"], "zonas": ["centro"], "permisos": ["tickets_update"]},
        headers=_headers(app, owner),
    )
    assert scope_resp.status_code == 200
    assert scope_resp.get_json()["scope"]["categorias"] == ["luminaria"]

    suggest = client.post(
        f"/api/admin/tenants/{tenant.slug}/employees/suggest-assignee",
        json={"categoria": "luminaria", "zona": "centro"},
        headers=_headers(app, owner),
    )
    assert suggest.status_code == 200
    payload = suggest.get_json()
    assert payload["ok"] is True
    assert payload["suggestions"][0]["employee_id"] == emp.id


def test_employee_cannot_grant_assignment_capability_or_mutate_employee_admin(client, app):
    owner = User(email="owner-scope-security@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-scope", nombre="Tenant Scope", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    actor = User(
        email="actor-scope-security@test.com",
        name="Actor",
        rol="empleado",
        es_empleado=True,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tipo_chat="pyme",
    )
    actor.set_password("pass")
    target = User(
        email="target-scope-security@test.com",
        name="Target",
        rol="empleado",
        es_empleado=True,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tipo_chat="pyme",
        accesibilidad={"employee_scope": {"categorias": ["luminaria"], "permisos": ["tickets.read"]}},
    )
    target.set_password("pass")
    db.session.add_all([actor, target])
    db.session.commit()

    attempts = (
        (
            "put",
            f"/api/admin/employees/{target.id}/scope",
            {"categorias": ["luminaria"], "permisos": ["tickets.assign"]},
        ),
        (
            "put",
            f"/api/admin/employees/{target.id}",
            {"scope": {"categorias": ["luminaria"], "permisos": ["tickets.assign"]}},
        ),
        (
            "post",
            f"/api/admin/employees/{target.id}/roles",
            {"role": "admin"},
        ),
    )
    for method, endpoint, payload in attempts:
        response = getattr(client, method)(endpoint, json=payload, headers=_headers(app, actor))
        assert response.status_code == 403
        assert response.get_json()["reason_code"] == "employee_administration_forbidden"

    refreshed = db.session.get(User, target.id)
    scope = (refreshed.accesibilidad or {}).get("employee_scope") or {}
    assert scope.get("permisos") == ["tickets.read"]
    assert refreshed.rol == "empleado"


def test_employee_categories_reject_cross_tenant_target_without_mutation(client, app):
    owner = User(email="owner-category-idor@test.com", name="Owner A", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    foreign_owner = User(
        email="foreign-owner-category-idor@test.com",
        name="Owner B",
        rol="admin",
        tipo_chat="pyme",
    )
    foreign_owner.set_password("pass")
    db.session.add_all([owner, foreign_owner])
    db.session.commit()

    tenant = TenantProfile(slug="tenant-scope", nombre="Tenant A", tipo="pyme", pyme_id=owner.id)
    foreign_tenant = TenantProfile(
        slug="tenant-foreign",
        nombre="Tenant B",
        tipo="pyme",
        pyme_id=foreign_owner.id,
    )
    db.session.add_all([tenant, foreign_tenant])
    db.session.commit()

    attacker_category = CategoriaTicket(nombre="attacker-category", tenant_id=tenant.id)
    foreign_category = CategoriaTicket(nombre="foreign-category", tenant_id=foreign_tenant.id)
    foreign_employee = User(
        email="foreign-employee-category-idor@test.com",
        name="Foreign employee",
        rol="empleado",
        es_empleado=True,
        tenant_id=foreign_tenant.id,
        tenant_slug=foreign_tenant.slug,
        tipo_chat="pyme",
        accesibilidad={
            "employee_scope": {
                "categorias": ["foreign-category"],
                "permisos": ["tickets.read"],
            }
        },
    )
    foreign_employee.set_password("pass")
    foreign_employee.categorias_ticket = [foreign_category]
    db.session.add_all([attacker_category, foreign_category, foreign_employee])
    db.session.commit()

    response = client.post(
        f"/api/admin/employees/{foreign_employee.id}/categories",
        json={"category_ids": [attacker_category.id]},
        headers=_headers(app, owner),
    )
    assert response.status_code == 404

    refreshed = db.session.get(User, foreign_employee.id)
    assert [category.id for category in refreshed.categorias_ticket] == [foreign_category.id]
    scope = (refreshed.accesibilidad or {}).get("employee_scope") or {}
    assert scope.get("categorias") == ["foreign-category"]


def test_employee_creation_returns_scope_and_coverage(client, app):
    owner = User(email="owner-scope-create@test.com", name="Owner Create", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-scope", nombre="Tenant Scope", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    create_resp = client.post(
        "/api/admin/employees",
        json={
            "email": "emp-create@test.com",
            "name": "Emp Create",
            "password": "pass",
            "scope": {
                "categorias": ["luminaria", "baches"],
                "zonas": ["centro"],
                "permisos": ["tickets_assign"],
            },
            "roles": ["empleado", "analista"],
        },
        headers=_headers(app, owner),
    )
    assert create_resp.status_code == 201
    create_body = create_resp.get_json()
    assert create_body["employee"]["scope"]["categorias"] == ["luminaria", "baches"]
    assert "analista" in create_body["employee"]["roles"]

    coverage_resp = client.get(
        f"/api/admin/tenants/{tenant.slug}/employees/coverage",
        headers=_headers(app, owner),
    )
    assert coverage_resp.status_code == 200
    coverage = coverage_resp.get_json()
    assert "luminaria" in coverage["coverage"]["categorias"]
    assert "centro" in coverage["coverage"]["zonas"]


def test_employee_admin_update_profile_roles_and_scope(client, app):
    owner = User(email="owner-scope-update@test.com", name="Owner Update", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-scope", nombre="Tenant Scope", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    emp = User(email="emp-update@test.com", name="Emp", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    emp.set_password("old-pass")
    db.session.add(emp)
    db.session.commit()

    update_resp = client.put(
        f"/api/admin/employees/{emp.id}",
        json={
            "name": "Mesa Alumbrado",
            "password": "new-pass",
            "roles": ["supervisor", "platform_admin"],
            "scope": {
                "categorias": ["luminaria"],
                "zonas": ["centro"],
                "channels": ["whatsapp"],
                "permisos": ["tickets_read", "tickets_assign"],
            },
        },
        headers=_headers(app, owner),
    )

    assert update_resp.status_code == 200
    payload = update_resp.get_json()
    assert payload["contract_version"] == "employee.admin_update.v1"
    assert payload["employee"]["name"] == "Mesa Alumbrado"
    assert payload["employee"]["scope"]["categorias"] == ["luminaria"]
    assert payload["employee"]["scope"]["channels"] == ["whatsapp"]
    assert payload["employee"]["roles"] == ["supervisor"]

    refreshed = db.session.get(User, emp.id)
    assert refreshed.check_password("new-pass")
    assert refreshed.rol == "supervisor"

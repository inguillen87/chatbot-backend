import jwt

from app import db
from models import TenantProfile, User


def _headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": "tenant-scope"}


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

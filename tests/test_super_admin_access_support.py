"""Support can open the door for a locked-out person without ever knowing their password."""

from app import db
from models import AdminAuditLog, User
from tests.auth_test_utils import clerk_superadmin_headers


def _platform(client, slug="soporte-org"):
    sa = User(name="Plataforma", email=f"sa-{slug}@chatboc.ar", rol="super_admin")
    sa.set_password("Synthetic123")
    db.session.add(sa)
    db.session.commit()
    headers = clerk_superadmin_headers(sa)
    created = client.post(
        "/api/admin/tenants",
        json={"slug": slug, "nombre": "Organización sintética", "tipo": "municipio", "email_admin": f"owner@{slug}.invalid"},
        headers=headers,
    )
    assert created.status_code == 201, created.json
    return sa, headers


def test_lists_who_signs_in_without_any_credential(client):
    _sa, headers = _platform(client)

    response = client.get("/api/admin/tenants/soporte-org/access-support/users", headers=headers)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.json
    assert body["link_minutes"] == 60
    assert [(user["email"], user["is_owner"]) for user in body["users"]] == [("owner@soporte-org.invalid", True)]
    assert not {"password", "password_hash", "token", "entity_token"} & set(body["users"][0])


def test_one_time_link_lets_the_person_choose_the_password(client):
    _sa, headers = _platform(client, "soporte-enlace")
    owner = User.query.filter_by(email="owner@soporte-enlace.invalid").one()

    issued = client.post(
        f"/api/admin/tenants/soporte-enlace/access-support/users/{owner.id}/reset-link", headers=headers
    )

    assert issued.status_code == 200, issued.json
    assert issued.json["single_use"] is True and issued.json["expires_in_minutes"] == 60
    token = issued.json["token"]
    audit = AdminAuditLog.query.filter_by(action="issue_access_reset_link").one()
    assert audit.details == {"target_user_id": owner.id}
    assert token not in str(audit.details)

    confirmed = client.post(
        "/auth/password/reset/confirm", json={"token": token, "new_password": "NuevaClave2026"}
    )
    assert confirmed.status_code == 200, confirmed.json
    db.session.refresh(owner)
    assert owner.check_password("NuevaClave2026")
    # The link works once.
    again = client.post("/auth/password/reset/confirm", json={"token": token, "new_password": "OtraClave2026"})
    assert again.status_code == 400


def test_refuses_other_organizations_and_platform_accounts(client):
    sa, headers = _platform(client, "soporte-limites")
    created = client.post(
        "/api/admin/tenants",
        json={"slug": "soporte-ajena", "nombre": "Otra sintética", "tipo": "municipio", "email_admin": "owner@soporte-ajena.invalid"},
        headers=headers,
    )
    assert created.status_code == 201, created.json
    foreign = User.query.filter_by(email="owner@soporte-ajena.invalid").one()

    wrong_tenant = client.post(
        f"/api/admin/tenants/soporte-limites/access-support/users/{foreign.id}/reset-link", headers=headers
    )
    assert wrong_tenant.status_code == 404
    platform = client.post(
        f"/api/admin/tenants/soporte-limites/access-support/users/{sa.id}/reset-link", headers=headers
    )
    assert platform.status_code == 404
    anonymous = client.application.test_client()
    assert anonymous.get("/api/admin/tenants/soporte-limites/access-support/users").status_code in (401, 403)
    assert anonymous.post(f"/api/admin/tenants/soporte-limites/access-support/users/{foreign.id}/reset-link").status_code in (401, 403)
    db.session.refresh(foreign)
    assert foreign.password_reset_selector is None

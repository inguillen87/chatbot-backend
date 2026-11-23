import pytest

from database import db
from models import TenantProfile, User


@pytest.fixture
def owner_org(client):
    owner = User(name="Titular Ejemplo", email="titular@example.test", rol="admin",
                 tipo_chat="pyme", password_hash="synthetic", telefono="+5492610000000")
    db.session.add(owner)
    db.session.flush()
    owner.token = "synthetic-owner-value"
    tenant = TenantProfile(slug="perfil-fixture", nombre="Comercio Público", tipo="pyme",
                           pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()
    return owner, tenant


def _widget_session(client, owner):
    response = client.post("/auth/widget-token", headers={"X-Entity-Token": owner.token})
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    data = response.get_json()
    return data.get("token") or data.get("access_token")


@pytest.mark.parametrize("path", ["/auth/me", "/auth/perfil", "/api/me", "/api/perfil", "/me", "/perfil"])
def test_widget_session_profile_is_the_organization_view(client, owner_org, path):
    owner, _tenant = owner_org
    token = _widget_session(client, owner)
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    data = response.get_json()
    assert data["session_kind"] == "widget"
    assert data["tipo_chat"] == "pyme"
    assert data["tenant_slug"] == "perfil-fixture"
    for private in ("email", "name", "telefono", "rol", "id", "plan", "permissions", "capabilities",
                    "organization_profile", "channel_activation", "integration_access"):
        assert private not in data
    assert owner.email not in body
    assert "+5492610000000" not in body
    assert "Titular Ejemplo" not in body


def test_widget_profile_read_is_read_only(client, owner_org):
    owner, tenant = owner_org
    token = _widget_session(client, owner)
    before = (owner.token, dict(tenant.configuracion or {}))
    for path in ("/auth/me", "/api/perfil"):
        assert client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    db.session.expire_all()
    owner_after = db.session.get(User, owner.id)
    tenant_after = db.session.get(TenantProfile, tenant.id)
    assert (owner_after.token, dict(tenant_after.configuracion or {})) == before


def test_panel_session_keeps_the_full_profile(client, owner_org):
    from utils.auth_helpers import generar_token

    owner, _tenant = owner_org
    token = generar_token(owner.id, owner.rol, owner.tipo_chat, owner.municipio_id, owner.pyme_id)
    data = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert data["email"] == owner.email
    assert "capabilities" in data



def test_installed_widget_keeps_its_identifier_without_owner_data(client, owner_org):
    owner, _tenant = owner_org
    data = client.get("/perfil", headers={"X-Entity-Token": owner.token}).get_json()
    assert data["entity_token"] == owner.token
    assert data["widget_embed_token"] is None
    assert owner.email not in str(data)

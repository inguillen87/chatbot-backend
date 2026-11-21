from copy import deepcopy
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from database import db
from models import TenantProfile, User
from tests import auth_test_utils
from utils.auth_helpers import generar_token


def session_headers(user):
    token = generar_token(user.id, user.rol, user.tipo_chat, user.municipio_id, user.pyme_id)
    return {"Authorization": f"Bearer {token}"}


def create_user(role, *, label=None, **fields):
    label = label or role
    user = User(
        name=label, email=f"{label}@example.test", rol=role,
        password_hash="synthetic", **fields,
    )
    db.session.add(user)
    db.session.flush()
    return user


@pytest.fixture
def organization(client):
    owner = create_user("admin", tipo_chat="municipio")
    owner.token = "synthetic-existing-widget"
    tenant = TenantProfile(
        slug="portal-fixture", nombre="Organización de prueba", tipo="municipio",
        municipio_id=owner.id, plan="full", configuracion={"widget_position": "left"},
    )
    db.session.add(tenant)
    db.session.commit()
    return tenant


def integration_path(tenant, alias="/api/v1/portal"):
    return f"{alias}/{tenant.slug}/integration"


@pytest.mark.parametrize("alias", ["/api/v1/portal", "/api/portal"])
def test_session_required_on_both_mounts(client, organization, alias):
    response = client.get(integration_path(organization, alias))
    assert response.status_code == 401
    assert "widgetScript" not in response.get_json()


@pytest.mark.parametrize("role", ["admin", "empleado", "super_admin"])
def test_operational_users_can_read_the_organization(client, organization, role, monkeypatch):
    if role == "super_admin":
        user = create_user(role)
        monkeypatch.setattr(auth_test_utils, "AUTHORIZED_SUPERADMIN_EMAIL", user.email)
        headers = auth_test_utils.clerk_superadmin_headers(user)
    else:
        user = organization.municipio if role == "admin" else create_user(
            role, tenant_id=organization.id, es_empleado=True
        )
        headers = session_headers(user)
    response = client.get(integration_path(organization), headers=headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["slug"] == organization.slug
    assert data["widget"]["embed_enabled"] is True
    assert response.cache_control.private and response.cache_control.no_store


@pytest.mark.parametrize("role", ["usuario", "empleado"])
def test_requires_an_operational_role_in_the_requested_organization(client, organization, role):
    if role == "empleado":
        owner = create_user("admin", label="other-owner", tipo_chat="pyme")
        other = TenantProfile(slug="portal-other", nombre="Otro", tipo="pyme", pyme_id=owner.id, plan="full")
        db.session.add(other)
        db.session.flush()
        user = create_user(role, tenant_id=other.id, es_empleado=True)
    else:
        user = create_user(role, tenant_id=organization.id)
    response = client.get(integration_path(organization), headers=session_headers(user))
    assert response.status_code == 403
    assert "widgetScript" not in response.get_json()


def test_locked_plan_omits_widget_embed(client, organization):
    organization.plan = "free"
    db.session.commit()
    response = client.get(integration_path(organization), headers=session_headers(organization.municipio))
    assert response.status_code == 200
    data = response.get_json()
    assert not data.get("widgetScript")
    assert not data.get("embed_snippet")
    assert data["widget"]["embed_enabled"] is False
    assert data["widget"]["access"]["enabled"] is False
    assert organization.municipio.token not in response.get_data(as_text=True)


@pytest.mark.parametrize("credential", ["bearer", "cookie"])
def test_repeated_reads_preserve_configuration_and_do_not_start_audio_jobs(
    client, organization, monkeypatch, credential
):
    organization.configuracion = {**organization.configuracion, "tts_fixed_menu_warmup_enabled": True}
    owner = organization.municipio
    if credential == "cookie":
        owner.set_password("synthetic-portal-password")
    db.session.commit()
    if credential == "cookie":
        login = client.post("/auth/login", json={"email": owner.email, "password": "synthetic-portal-password"})
        assert login.status_code == 200
        owner.entity_token = None
        db.session.commit()
        headers = {}
    else:
        headers = session_headers(owner)
    tenant_id = organization.id
    before = deepcopy(organization.configuracion)
    path = integration_path(organization)
    warm = Mock(return_value={})
    monkeypatch.setattr("services.tts_orchestrator.warm_tts_cache", warm)
    writes = []

    def collect_writes(connection, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(verb)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", collect_writes)
    try:
        for _ in range(2):
            assert client.get(path, headers=headers).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", collect_writes)
    assert not writes
    warm.assert_not_called()
    db.session.remove()
    assert db.session.get(TenantProfile, tenant_id).configuracion == before


def test_existing_widget_still_resolves_on_public_surface(client, organization):
    published = organization.municipio.token
    organization.configuracion = {"widget_tokens": [published]}
    db.session.commit()
    response = client.get("/api/public/widget-config", headers={"X-Widget-Token": published})
    assert response.status_code == 200
    data = response.get_json()
    assert data["tenant"]["slug"] == organization.slug
    assert data["widget"]["attributes"]["data-widget-token"] == published
    assert published in data["widget"]["embed_snippet"]

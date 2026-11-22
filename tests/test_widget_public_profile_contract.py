import pytest
from sqlalchemy import event

from database import db
from models import Rubro, TenantProfile, User, WidgetSettings
from utils.auth_helpers import _generate_widget_session_token


@pytest.fixture
def widget_organization(client):
    rubro = Rubro(clave="widget-sector", nombre="Sector de prueba")
    owner = User(name="Private fixture", email="owner@example.test", telefono="private-phone",
                 nombre_empresa="Private account label", rol="admin", rubro=rubro,
                 password_hash="synthetic", token="public-fixture-installation")
    tenant = TenantProfile(slug="widget-fixture", nombre="Public organization", tipo="municipio",
                           municipio=owner, logo_url="/published/logo.svg")
    tenant.widget_settings = WidgetSettings(
        welcome_title="Public greeting", welcome_subtitle="Public subtitle",
        theme_config={"light": {"primary": "#123456", "private_extra": "hidden"},
                      "dark": {"background": {"unpublished": "hidden"}}, "draft": {"secret": "hidden"}},
    )
    db.session.add(tenant)
    db.session.commit()
    return tenant


@pytest.mark.parametrize("credential", ["installation", "widget_session"])
def test_public_profile_routes_are_read_only_and_organization_scoped(client, widget_organization, credential):
    tenant = widget_organization
    owner = tenant.municipio
    token = owner.token if credential == "installation" else _generate_widget_session_token(owner)[0]
    headers = {"Authorization": f"Bearer {token}"}
    paths = ("/auth/me", "/auth/perfil", "/auth/profile", "/me", "/perfil", "/profile",
             "/api/me", "/api/perfil", "/auth/token-info")
    writes = []

    def collect_write(connection, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(verb)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", collect_write)
    try:
        for path in paths:
            # Repeated reads, including a caller-supplied organization hint, stay bound to the identity.
            for _ in range(2):
                response = client.get(path, headers=headers, query_string={"tenant_slug": "unrelated"})
                assert response.status_code == 200, path
                data = response.get_json()
                assert data["tipo_chat"] == "municipio"
                assert set(data) == {
                    "tenant_slug", "tenantSlug", "nombre_empresa", "tipo_chat", "rubro", "rubro_clave",
                    "logo_url", "theme_config", "welcome_title", "welcome_subtitle", "default_open",
                    "session_kind", "widget_session_active", "visitor_authenticated",
                    "session_expires_at", "session_renew_until",
                }, path
                assert data["tenant_slug"] == data["tenantSlug"] == tenant.slug
                assert data["nombre_empresa"] == tenant.nombre
                assert data["rubro"] == "Sector de prueba"
                assert data["rubro_clave"] == "widget-sector"
                assert data["logo_url"] == "/published/logo.svg"
                assert data["welcome_title"] == "Public greeting"
                assert data["welcome_subtitle"] == "Public subtitle"
                assert data["default_open"] is False
                assert data["theme_config"]["light"]["primary"] == "#123456"
                assert data["session_kind"] == "widget"
                assert data["widget_session_active"] is True
                assert data["visitor_authenticated"] is False
                assert data["session_expires_at"] and data["session_renew_until"]
                for private_value in (owner.name, owner.email, owner.telefono, owner.nombre_empresa, owner.token, "hidden"):
                    assert private_value not in response.get_data(as_text=True)
    finally:
        event.remove(engine, "before_cursor_execute", collect_write)
    assert writes == []

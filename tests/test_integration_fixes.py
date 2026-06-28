import pytest
from app import create_app
from config import TestingConfig
from models import db, TenantProfile, User

@pytest.fixture
def app():
    app = create_app(TestingConfig)
    app.config['TIENDANUBE_CLIENT_ID'] = None # Ensure it is missing
    app.config['ML_APP_ID'] = None # Ensure it is missing
    app.config['TWILIO_ACCOUNT_SID'] = None
    app.config['TWILIO_AUTH_TOKEN'] = None
    app.config['TWILIO_META_APP_ID'] = None
    app.config['TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID'] = None

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def _create_admin_tenant(email="admin@test.com"):
    user = User(email=email, name="Admin", rol="admin", tipo_chat="pyme")
    user.set_password("password")
    db.session.add(user)
    db.session.flush()

    tenant = TenantProfile(
        slug="test-tenant",
        nombre="Test Tenant",
        tipo="pyme",
        plan="full",
        pyme_id=user.id,
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    db.session.add(user)
    db.session.commit()

    from utils.auth_helpers import generar_token

    token = generar_token(user.id, user.rol, user.tipo_chat, user.municipio_id, user.pyme_id)
    return user, tenant, {"Authorization": f"Bearer {token}", "X-Tenant": tenant.slug}


def test_integration_missing_config_returns_frontend_safe_error(client, app):
    # Setup auth and tenant
    with app.app_context():
        _user, _tenant, headers = _create_admin_tenant()

    # Test MercadoLibre
    resp = client.get('/api/admin/tenants/test-tenant/integrations/mercadolibre/connect', headers=headers)
    assert resp.status_code == 200
    assert resp.json['error'] == 'platform_not_configured'

    # Test TiendaNube
    resp = client.get('/api/admin/tenants/test-tenant/integrations/tiendanube/connect', headers=headers)
    assert resp.status_code == 200
    assert resp.json['error'] == 'platform_not_configured'

def test_whatsapp_integration_returns_tech_provider_config_error(client, app):
    # Setup auth and tenant
    with app.app_context():
        _user, _tenant, headers = _create_admin_tenant()

    # Test WhatsApp
    resp = client.get('/api/admin/tenants/test-tenant/integrations/whatsapp/connect', headers=headers)
    assert resp.status_code == 409
    assert resp.json['error'] == 'platform_not_configured'
    assert resp.json['reason_code'] == 'missing_twilio_meta_platform_env'
    assert resp.json['provider'] == 'twilio_tech_provider'
    assert resp.json['frontend_contract']['render_as'] == 'twilio_tech_provider_onboarding'
    assert 'TWILIO_META_APP_ID' in resp.json['missing']

def test_widget_settings_includes_style(client, app):
    # Mock tenant resolution which might be complex in tests
    with app.app_context():
        _user, _tenant, headers = _create_admin_tenant()

    # Call widget settings endpoint
    # Note: widget settings endpoint is /widget-settings without tenant slug in path, it infers from user/headers
    resp = client.get('/widget-settings', headers=headers)

    assert resp.status_code == 200
    data = resp.json
    assert "embed_code" in data
    assert "<style>" in data["embed_code"]
    assert "transform: scale(1.05);" in data["embed_code"]

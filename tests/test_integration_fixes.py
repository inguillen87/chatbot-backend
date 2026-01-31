import pytest
from unittest.mock import patch, MagicMock
from flask import Flask, jsonify
from app import create_app
from models import db, TenantProfile, User
from routes.admin_tenant import admin_tenant_bp
from routes.widget_settings import widget_settings_bp

@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['TIENDANUBE_CLIENT_ID'] = None # Ensure it is missing
    app.config['ML_APP_ID'] = None # Ensure it is missing

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def test_integration_missing_config_returns_422(client, app):
    # Setup auth and tenant
    with app.app_context():
        # Create user and tenant
        user = User(email="admin@test.com", name="Admin", rol="admin")
        user.set_password("password")
        tenant = TenantProfile(slug="test-tenant", nombre="Test Tenant", tipo="pyme", plan="full")
        db.session.add(user)
        db.session.add(tenant)
        db.session.commit()

        # Link user to tenant
        user.tenant_id = tenant.id
        db.session.commit()

        # Get token
        from utils.auth_helpers import generar_token
        token = generar_token(user.id)

    headers = {"Authorization": f"Bearer {token}"}

    # Test MercadoLibre
    resp = client.get('/api/admin/tenants/test-tenant/integrations/mercadolibre/connect', headers=headers)
    assert resp.status_code == 422
    assert resp.json['error'] == 'platform_not_configured'

    # Test TiendaNube
    resp = client.get('/api/admin/tenants/test-tenant/integrations/tiendanube/connect', headers=headers)
    assert resp.status_code == 422
    assert resp.json['error'] == 'platform_not_configured'

def test_whatsapp_integration_returns_422_if_missing_config(client, app):
    # Setup auth and tenant
    with app.app_context():
        user = User(email="admin@test.com", name="Admin", rol="admin")
        user.set_password("password")
        tenant = TenantProfile(slug="test-tenant", nombre="Test Tenant", tipo="pyme", plan="full")
        db.session.add(user)
        db.session.add(tenant)
        db.session.commit()
        user.tenant_id = tenant.id
        db.session.commit()
        from utils.auth_helpers import generar_token
        token = generar_token(user.id)

    headers = {"Authorization": f"Bearer {token}"}

    # Test WhatsApp
    resp = client.get('/api/admin/tenants/test-tenant/integrations/whatsapp/connect', headers=headers)
    # Since we didn't set FACEBOOK_APP_ID, it should be 422
    assert resp.status_code == 422
    assert resp.json['error'] == 'platform_not_configured'

def test_widget_settings_includes_style(client, app):
    # Mock tenant resolution which might be complex in tests
    with app.app_context():
        # Setup tenant
        tenant = TenantProfile(slug="test-tenant", nombre="Test Tenant", tipo="pyme")
        db.session.add(tenant)
        db.session.commit()

        # Mock auth for widget settings (requires admin)
        user = User(email="admin@test.com", name="Admin", rol="admin")
        user.set_password("password")
        user.tenant_id = tenant.id
        db.session.add(user)
        db.session.commit()
        from utils.auth_helpers import generar_token
        token = generar_token(user.id)

    headers = {"Authorization": f"Bearer {token}"}

    # Call widget settings endpoint
    # Note: widget settings endpoint is /widget-settings without tenant slug in path, it infers from user/headers
    resp = client.get('/widget-settings', headers=headers)

    assert resp.status_code == 200
    data = resp.json
    assert "embed_code" in data
    assert "<style>" in data["embed_code"]
    assert "transform: scale(1.05);" in data["embed_code"]

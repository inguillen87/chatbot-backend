import pytest
import json
from app import create_app
from extensions import db, login_manager
from models import User, TenantProfile, TenantWidgetConfig
from config import Config
from utils.auth_helpers import generar_token

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SESSION_TYPE = 'filesystem'
    WTF_CSRF_ENABLED = False
    # Disable eventlet for tests
    FLASK_SKIP_GLOBAL_APP = True

@pytest.fixture
def app():
    app = create_app(TestConfig)

    # Register user_loader for Flask-Login to avoid crashes
    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def setup_data(app):
    # Create Admin User First (Owner)
    admin = User(
        email="admin@demo.com",
        name="Admin",
        rol="admin"
    )
    admin.set_password("password")
    db.session.add(admin)
    db.session.commit()

    # Create Tenant with Owner
    tenant = TenantProfile(
        slug="demo-saas",
        nombre="Demo SaaS",
        tipo="municipio",
        is_active=True,
        municipio_id=admin.id
    )
    db.session.add(tenant)
    db.session.commit()

    # Link user back to tenant
    admin.tenant_id = tenant.id
    db.session.add(admin)
    db.session.commit()

    return tenant, admin

def generate_admin_token(admin):
    """Helper to generate JWT matching the signature in auth_helpers."""
    return generar_token(
        user_id=admin.id,
        rol=admin.rol,
        tipo_chat=getattr(admin, 'tipo_chat', None),
        municipio_id=getattr(admin, 'municipio_id', None),
        pyme_id=getattr(admin, 'pyme_id', None)
    )

def test_public_config_default(client, setup_data):
    tenant, _ = setup_data

    # Test GET public config (should return defaults + tenant logo if any)
    resp = client.get(f'/api/public/tenants/{tenant.slug}/saas-config')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "appearance" in data
    # Default primaryColor in WidgetConfigService is #007aff
    assert data["appearance"]["primaryColor"] == "#007aff"

def test_admin_draft_flow(client, setup_data):
    tenant, admin = setup_data
    token = generate_admin_token(admin)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Get Admin Config (Init)
    resp = client.get(f'/api/admin/tenants/{tenant.slug}/widget-config', headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "draft" in data
    assert "live" in data
    assert data["draft"]["appearance"]["primaryColor"] == "#007aff"

    # 2. Update Draft
    new_draft = {
        "appearance": {
            "primaryColor": "#ff0000",
            "welcomeTitle": "New Title"
        }
    }
    resp = client.put(f'/api/admin/tenants/{tenant.slug}/widget-config',
                      headers=headers,
                      json=new_draft)
    assert resp.status_code == 200
    updated_draft = resp.get_json()["draft"]
    assert updated_draft["appearance"]["primaryColor"] == "#ff0000"

    # Verify Public is NOT changed yet
    resp = client.get(f'/api/public/tenants/{tenant.slug}/saas-config')
    public_data = resp.get_json()
    assert public_data["appearance"]["primaryColor"] == "#007aff" # Still default

    # 3. Publish
    resp = client.post(f'/api/admin/tenants/{tenant.slug}/widget-config/publish',
                       headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "published"

    # Verify Public IS changed
    resp = client.get(f'/api/public/tenants/{tenant.slug}/saas-config')
    public_data = resp.get_json()
    assert public_data["appearance"]["primaryColor"] == "#ff0000"
    assert public_data["appearance"]["welcomeTitle"] == "New Title"

def test_admin_preview(client, setup_data):
    tenant, admin = setup_data
    token = generate_admin_token(admin)
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "appearance": {
            "primaryColor": "#123456"
        },
        "domains": ["example.com"]
    }

    resp = client.post(f'/api/admin/tenants/{tenant.slug}/widget-config/preview',
                       headers=headers,
                       json=payload)
    assert resp.status_code == 200
    preview = resp.get_json()
    assert preview["appearance"]["primaryColor"] == "#123456"
    assert preview["domains"] == ["example.com"]

    # Ensure it didn't save to draft
    resp = client.get(f'/api/admin/tenants/{tenant.slug}/widget-config', headers=headers)
    draft = resp.get_json()["draft"]
    assert draft["appearance"]["primaryColor"] != "#123456"

def test_unauthorized_access(client, setup_data):
    tenant, admin = setup_data

    # Public endpoint is open
    resp = client.get(f'/api/public/tenants/{tenant.slug}/saas-config')
    assert resp.status_code == 200

    # Admin endpoint requires token
    resp = client.get(f'/api/admin/tenants/{tenant.slug}/widget-config')
    assert resp.status_code == 401

    # IDOR Check (Different tenant)
    # We need to create another user first to be the owner
    other_admin = User(email="other@demo.com", name="Other", rol="admin")
    other_admin.set_password("pass")
    db.session.add(other_admin)
    db.session.commit()

    other_tenant = TenantProfile(
        slug="other",
        nombre="Other",
        tipo="pyme",
        pyme_id=other_admin.id
    )
    db.session.add(other_tenant)
    db.session.commit()

    token = generate_admin_token(admin) # Admin for "demo-saas"
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get(f'/api/admin/tenants/other/widget-config', headers=headers)
    assert resp.status_code == 403

import pytest
from models import User, TenantProfile, Rubro, db

@pytest.fixture
def test_setup(client):
    """Setup test data"""
    # Create Rubro first
    rubro = Rubro(nombre="Bodega", clave="bodega", es_publico=True)
    db.session.add(rubro)
    db.session.commit()

    # Create User first (as owner)
    user = User(
        email="admin@test.com",
        name="Admin",
        rol="admin"
    )
    user.set_password("password")
    db.session.add(user)
    db.session.commit()

    # Create Tenant linked to User
    tenant = TenantProfile(
        nombre="Test Tenant",
        slug="test-tenant",
        tipo="pyme",
        plan="full",
        pyme_id=user.id  # Satisfy ck_tenant_profile_owner_present
    )
    db.session.add(tenant)
    db.session.commit()

    # Link User to Tenant
    user.tenant_id = tenant.id
    db.session.commit()

    return user, tenant

def test_get_and_update_dispatch_config(client, test_setup):
    user, tenant = test_setup

    # Login
    login_resp = client.post("/auth/login", json={
        "email": "admin@test.com",
        "password": "password"
    })
    assert login_resp.status_code == 200
    token = login_resp.json["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # GET initial config
    resp = client.get(f"/api/admin/tenants/{tenant.slug}/config", headers=headers)
    assert resp.status_code == 200
    data = resp.json
    # Expect None initially
    assert data["tenant"].get("dispatch_email") is None
    assert data["tenant"].get("dispatch_phone") is None

    # PUT update
    update_payload = {
        "tenant": {
            "dispatch_email": "warehouse@test.com",
            "dispatch_phone": "+1234567890"
        }
    }
    resp = client.put(f"/api/admin/tenants/{tenant.slug}/config", json=update_payload, headers=headers)
    assert resp.status_code == 200

    # GET verify
    resp = client.get(f"/api/admin/tenants/{tenant.slug}/config", headers=headers)
    assert resp.status_code == 200
    data = resp.json
    assert data["tenant"]["dispatch_email"] == "warehouse@test.com"
    assert data["tenant"]["dispatch_phone"] == "+1234567890"

    # Verify DB
    refreshed_tenant = db.session.get(TenantProfile, tenant.id)
    assert refreshed_tenant.dispatch_email == "warehouse@test.com"
    assert refreshed_tenant.dispatch_phone == "+1234567890"

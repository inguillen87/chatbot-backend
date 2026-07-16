from models import User, TenantProfile
from app import db
from tests.auth_test_utils import clerk_superadmin_headers

def test_super_admin_flow(client):
    # 1. Create Super Admin
    sa = User(name="Super Admin", email="sa@chatboc.ar", rol="super_admin")
    sa.set_password("admin123")
    db.session.add(sa)
    db.session.commit()

    # Superadmin access is issued exclusively by the verified Clerk exchange.
    headers = clerk_superadmin_headers(sa)

    # 2. Create Tenant
    payload = {
        "slug": "new-tenant",
        "nombre": "New Tenant",
        "tipo": "pyme",
        "email_admin": "admin@newtenant.com"
    }
    resp = client.post("/api/admin/tenants", json=payload, headers=headers)
    assert resp.status_code == 201
    assert resp.json['slug'] == "new-tenant"

    # 3. List Tenants
    resp = client.get("/api/admin/tenants", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json['tenants']) >= 1

    # 4. Impersonate
    resp = client.post("/api/admin/tenants/new-tenant/impersonate", headers=headers)
    assert resp.status_code == 200
    assert "token" in resp.json

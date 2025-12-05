import pytest
from models import User, TenantProfile
from app import db
from datetime import datetime, timezone

def test_super_admin_flow(client):
    # 1. Create Super Admin
    sa = User(name="Super Admin", email="sa@chatboc.ar", rol="superadmin")
    sa.set_password("admin123")
    db.session.add(sa)
    db.session.commit()

    # Login
    resp = client.post("/auth/admin/login", json={"email": "sa@chatboc.ar", "password": "admin123"})
    assert resp.status_code == 200
    token = resp.json['token']
    headers = {'Authorization': f'Bearer {token}'}

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

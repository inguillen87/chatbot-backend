
import pytest
from app import db
from models import User, TenantProfile
import json

def test_super_admin_flow(client):
    # 1. Create Super Admin
    super_admin = User(name="Super Admin", email="super@admin.com", rol="super_admin")
    super_admin.set_password("secret")
    db.session.add(super_admin)

    # 2. Create Standard Admin and Tenant
    owner = User(name="Owner", email="owner@tenant.com", rol="admin", plan="free")
    owner.set_password("secret")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-test", nombre="Tenant Test", tipo="municipio", municipio_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    # 3. Login as Super Admin
    resp = client.post("/auth/login", json={"email": "super@admin.com", "password": "secret"})
    assert resp.status_code == 200
    token = resp.json["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 4. List Tenants
    resp = client.get("/api/admin/tenants", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json["tenants"]) >= 1
    assert resp.json["tenants"][0]["slug"] == "tenant-test"
    assert resp.json["tenants"][0]["plan"] == "free"

    # 5. Update Tenant Plan
    resp = client.put("/api/admin/tenants/tenant-test/status", headers=headers, json={"plan": "pro"})
    assert resp.status_code == 200
    assert resp.json["plan"] == "pro"

    # Verify DB update
    db.session.refresh(owner)
    assert owner.plan == "pro"

def test_super_admin_access_denied(client):
    # Create normal user
    user = User(name="Normal", email="normal@user.com", rol="admin")
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()

    resp = client.post("/auth/login", json={"email": "normal@user.com", "password": "secret"})
    token = resp.json["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Try to access admin route
    resp = client.get("/api/admin/tenants", headers=headers)
    assert resp.status_code == 403

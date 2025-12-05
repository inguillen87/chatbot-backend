import pytest
from models import TenantProfile, User, MunicipioPost
from app import db
from datetime import datetime, timezone

def test_full_flow(client):
    # 1. Create Owner User
    owner = User(name="Owner", email="owner@demo.com", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    # 2. Create Tenant linked to Owner
    tenant = TenantProfile(
        slug="demo-flow",
        nombre="Demo Flow",
        tipo="municipio",
        municipio_id=owner.id
    )
    db.session.add(tenant)
    db.session.commit()

    # 3. Public API Check
    resp = client.get("/api/public/tenant?tenant_slug=demo-flow")
    print(f"DEBUG RESPONSE: {resp.data}")
    assert resp.status_code == 200
    assert resp.json['slug'] == "demo-flow"
    print("Step 3 Done")

    # 4. End User Registration
    reg_payload = {
        "email": "user@demo.com",
        "password": "password",
        "nombre": "User Demo",
        "tenant_slug": "demo-flow"
    }
    print("Step 4 Start")
    resp = client.post("/auth/register", json=reg_payload)
    print(f"Step 4 Resp: {resp.status_code} {resp.data}")
    assert resp.status_code == 201
    token = resp.json['token']

    # 5. Add Content (News) by Owner
    post = MunicipioPost(
        municipio_id=owner.id,
        titulo="Test News",
        descripcion="Body content",
        tipo_post="noticia",
        fecha_publicacion=datetime.now(timezone.utc)
    )
    db.session.add(post)
    db.session.commit()

    # 6. Portal API Check (Content)
    headers = {'Authorization': f'Bearer {token}'}
    resp = client.get(f"/api/v1/portal/demo-flow/content", headers=headers)
    assert resp.status_code == 200
    data = resp.json
    assert len(data['news']) > 0
    assert data['news'][0]['title'] == "Test News"
    assert "cover_url" in data['news'][0]

    # 7. Portal API Check (Orders - Empty)
    resp = client.get(f"/api/v1/portal/demo-flow/orders", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json, list)

    # 8. Create Order
    order_payload = {"items": [{"product_id": 1, "price": 100}], "total": 100}
    resp = client.post(f"/api/v1/portal/demo-flow/orders", headers=headers, json=order_payload)
    assert resp.status_code == 201

    # 9. Portal API Check (Orders - populated)
    resp = client.get(f"/api/v1/portal/demo-flow/orders", headers=headers)
    assert len(resp.json) == 1

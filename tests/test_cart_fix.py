import pytest
from flask import json
from models import CatalogoItem, TenantProfile, User, db

def test_add_to_cart_form_data(client):
    # Skip init_database fixture to avoid conflicts with app startup seeding
    cart_headers = {
        'X-Tenant': 'test-cart-fix',
        'X-Anon-Id': 'cart-fix-anon-session',
    }

    # Check if tenant exists or create one
    tenant = TenantProfile.query.filter_by(slug="test-cart-fix").first()
    if not tenant:
        # User ID 1 likely exists from seeding, or we create a dummy one
        user = User.query.get(1)
        if not user:
            # Create a minimal user if missing
            user = User(id=1, name="Test User", email="test@test.com", password_hash="hash")
            db.session.add(user)

        # Use pyme_id instead of user_id as TenantProfile doesn't have user_id
        # Also added 'tipo' field which is non-nullable
        tenant = TenantProfile(id=8888, slug="test-cart-fix", nombre="Test Tenant", tipo="pyme", pyme_id=1)
        db.session.add(tenant)

    # Create test product
    # CatalogoItem uses user_id, which corresponds to the owner (pyme_id or municipio_id)
    product = CatalogoItem(id=9999, tenant_id=tenant.id, nombre="Test Product", precio="10.00", user_id=1, disponible=True)
    db.session.merge(product) # Use merge to avoid ID conflicts
    db.session.commit()

    # Case 1: Standard JSON - should work
    resp = client.post('/carrito/agregar',
                       json={'catalogo_item_id': 9999},
                       headers=cart_headers)
    assert resp.status_code == 200, f"JSON add failed: {resp.data}"
    assert resp.json['items_count'] == 1

    # Case 2: Form Data (the fix) - should work
    resp = client.post('/carrito/agregar',
                       data={'catalogo_item_id': 9999, 'cantidad': 2},
                       headers=cart_headers)
    assert resp.status_code == 200, f"Form add failed: {resp.data}"
    assert resp.json['items_count'] == 3  # 1 + 2

    # Case 3: 'id' alias in JSON - should work
    resp = client.post('/carrito/agregar',
                       json={'id': 9999, 'cantidad': 1},
                       headers=cart_headers)
    assert resp.status_code == 200, f"Alias add failed: {resp.data}"
    assert resp.json['items_count'] == 4

    # Case 4: Modern marketplace route removes by path with DELETE
    resp = client.delete('/api/test-cart-fix/carrito/9999',
                         headers=cart_headers)
    assert resp.status_code == 200, f"DELETE item failed: {resp.data}"
    assert resp.json['items_count'] == 0

    # Case 5: Modern marketplace route clears the cart with DELETE
    resp = client.post('/carrito/agregar',
                       json={'catalogo_item_id': 9999, 'cantidad': 2},
                       headers=cart_headers)
    assert resp.status_code == 200, f"Re-add failed: {resp.data}"
    assert resp.json['items_count'] == 2

    resp = client.delete('/api/test-cart-fix/carrito',
                         headers=cart_headers)
    assert resp.status_code == 200, f"DELETE clear failed: {resp.data}"
    assert resp.json['items_count'] == 0

    # Case 6: Missing ID - should fail 400
    resp = client.post('/carrito/agregar',
                       json={'cantidad': 1},
                       headers=cart_headers)
    assert resp.status_code == 400

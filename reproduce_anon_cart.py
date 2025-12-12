
import json
from app import app
from flask import session

def test_anon_persistence():
    print("Testing Anonymous Cart Persistence on /carrito endpoints...")

    # Payload for adding an item
    # We need a valid item_id.
    # I'll try to find a tenant and an item first.

    with app.app_context():
        from models import TenantProfile, CatalogoItem
        tenant = TenantProfile.query.filter_by(slug='bodega').first()
        if not tenant:
            print("Tenant 'bodega' not found. Skipping.")
            return

        item = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
        if not item:
            print("No items found for tenant 'bodega'. Skipping.")
            return

        tenant_slug = tenant.slug
        item_id = item.id
        print(f"Using Tenant: {tenant_slug}, Item ID: {item_id}")

    client = app.test_client()

    headers = {
        'X-Anon-Id': 'test-uuid-12345',
        'X-Tenant': tenant_slug,
        'Content-Type': 'application/json'
    }

    # Step 1: Add item to cart
    print("\n--- Step 1: Add Item ---")
    resp = client.post('/carrito/agregar', headers=headers, json={
        'catalogo_item_id': item_id,
        'cantidad': 2
    })
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.get_json()}")

    if resp.status_code != 200:
        print("Failed to add item.")
        return

    # Step 2: Retrieve cart with SAME cookie (Standard Session)
    print("\n--- Step 2: Check with Cookie ---")
    # client.cookie_jar should handle cookies automatically
    resp2 = client.get('/carrito/resumen', headers=headers)
    data2 = resp2.get_json()
    count2 = data2.get('items_count', 0) if data2 else 0
    print(f"Items in cart (cookie): {count2}")

    # Step 3: Retrieve cart WITHOUT cookies but WITH X-Anon-Id
    print("\n--- Step 3: Check with X-Anon-Id ONLY (Simulate new session/incognito) ---")
    client_new = app.test_client() # New client, no cookies
    resp3 = client_new.get('/carrito/resumen', headers=headers)
    data3 = resp3.get_json()
    count3 = data3.get('items_count', 0) if data3 else 0
    print(f"Items in cart (X-Anon-Id only): {count3}")

    if count3 == 0 and count2 > 0:
        print("\n❌ FAILURE: Cart did not persist using X-Anon-Id.")
    elif count3 > 0:
        print("\n✅ SUCCESS: Cart persisted using X-Anon-Id.")
    else:
        print("\n❓ INCONCLUSIVE.")

if __name__ == "__main__":
    test_anon_persistence()

import sys
import os
sys.path.append(os.getcwd())
os.environ["FLASK_SKIP_GLOBAL_APP"] = "1"
os.environ["EVENTLET_NO_GREENDNS"] = "YES"

from app import create_app
app = create_app()

from database import db
from models import TenantProfile, User
from routes.market import _resolve_tenant, _get_or_create_cart_for_user

def test_alias_endpoint():
    with app.test_client() as client:
        # 1. Ensure bodega exists
        with app.app_context():
            slug = "bodega"
            tenant = _resolve_tenant(slug)

            # Need a product ID to add
            from models import CatalogoItem
            prod = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
            if not prod:
                print("No products found for bodega")
                sys.exit(1)
            prod_id = prod.id
            print(f"Product ID: {prod_id}")

        # 2. POST to /api/bodega/carrito (alias)
        # This mirrors the user log "POST /api/bodega/carrito?tenant_slug=bodega&tenant=bodega"

        url = f"/api/bodega/carrito?tenant_slug=bodega"

        # Test 1: Payload with 'catalogo_item_id' (Standard)
        payload1 = {"catalogo_item_id": prod_id, "cantidad": 1}
        resp = client.post(url, json=payload1, headers={"X-Anon-Id": "repro-test"})
        print(f"Standard Payload Response: {resp.status_code}")
        if resp.status_code != 200:
             print(resp.get_json())

        # Test 2: Payload with 'product_id' (Legacy/Frontend var)
        payload2 = {"product_id": prod_id, "cantidad": 1}
        resp = client.post(url, json=payload2, headers={"X-Anon-Id": "repro-test"})
        print(f"Legacy Payload Response: {resp.status_code}")
        if resp.status_code != 200:
             print(resp.get_json())

if __name__ == "__main__":
    test_alias_endpoint()

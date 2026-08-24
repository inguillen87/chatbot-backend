import sys
import os
sys.path.append(os.getcwd())
# Ensure env vars are set before importing app
os.environ["FLASK_SKIP_GLOBAL_APP"] = "1"

# Manually create app if 'from app import app' returns None due to FLASK_SKIP_GLOBAL_APP
from app import create_app
app = create_app()

from database import db
from models import TenantProfile, User
from routes.market import _resolve_tenant, _get_or_create_cart_for_user

def test_lazy_creation():
    with app.app_context():
        with app.test_request_context('/market/bodega/cart', headers={'X-Anon-Id': 'test-anon-123'}):
            slug = "bodega" # Should be a known demo slug

            print(f"Resolving tenant {slug}...")
            try:
                tenant = _resolve_tenant(slug)
            except Exception as e:
                print(f"Resolution failed: {e}")
                sys.exit(1)

            if not tenant:
                print("FAILED: Tenant not resolved")
                sys.exit(1)

            print(f"Tenant resolved: {tenant.id} - {tenant.slug}")

            if tenant.id is None:
                 print("FAILED: Tenant has no ID (not persisted)")
                 sys.exit(1)

            owner = tenant.pyme or tenant.municipio
            if not owner:
                 print("FAILED: Tenant has no owner")
                 sys.exit(1)

            print(f"Owner: {owner.email}")

            class MockUser:
                 id = None
                 telefono = "123456"
                 name = "Anon"

            print("Creating cart...")
            cart = _get_or_create_cart_for_user(tenant, MockUser(), create_if_missing=True)

            if not cart:
                print("FAILED: Cart not created")
                sys.exit(1)

            print(f"Cart created: ID {cart.id}, Session {cart.session_id}")
            print("SUCCESS")

if __name__ == "__main__":
    test_lazy_creation()

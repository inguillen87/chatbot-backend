import json
import unittest
from app import create_app, db
from config import TestConfig
from models import TenantProfile, User, CatalogoItem, MarketCart, MarketOrder

class TestLegacyCheckout(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Setup Tenant & Owner
        self.owner = User(name="Owner", email="owner@test.com", password_hash="x", rol="admin", tipo_chat="municipio")
        db.session.add(self.owner)
        db.session.flush() # get ID

        self.tenant = TenantProfile(
            slug="test-municipio",
            nombre="Municipio Test",
            tipo="municipio",
            municipio_id=self.owner.id
        )
        db.session.add(self.tenant)
        db.session.commit()

        # Setup Product
        self.product = CatalogoItem(
            user_id=self.owner.id,
            tenant_id=self.tenant.id,
            nombre="Producto Test",
            precio="100",
            precio_monetario=100.0,
            disponible=True
        )
        db.session.add(self.product)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_legacy_checkout_creates_order(self):
        # 1. Add item to cart
        resp = self.client.post(
            f"/api/municipio/carrito/items?tenant_slug={self.tenant.slug}",
            json={"catalogo_item_id": self.product.id, "cantidad": 2},
            headers={"X-Anon-Id": "anon-123"}
        )
        self.assertEqual(resp.status_code, 201)

        # Verify cart exists
        cart = MarketCart.query.filter_by(session_id="anon-123").first()
        self.assertIsNotNone(cart)
        self.assertEqual(cart.status, "open")
        self.assertEqual(cart.items.count(), 1)

        # 2. Checkout
        resp = self.client.post(
            f"/api/municipio/checkout?tenant_slug={self.tenant.slug}",
            json={"telefono": "123456789", "nombre": "Juan Perez"},
            headers={"X-Anon-Id": "anon-123"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("order_id", data)
        self.assertEqual(data["message"], "Pedido creado correctamente")

        # 3. Verify Order
        order_id = data["order_id"]
        order = MarketOrder.query.get(order_id)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.contact_name, "Juan Perez")
        self.assertEqual(order.total_monetary, 200.0) # 2 * 100

        # 4. Verify Cart is submitted/closed
        cart = MarketCart.query.get(cart.id)
        self.assertEqual(cart.status, "submitted")

        # 5. Verify subsequent cart request gets new cart
        resp = self.client.get(
            f"/api/municipio/carrito?tenant_slug={self.tenant.slug}",
            headers={"X-Anon-Id": "anon-123"}
        )
        new_data = resp.get_json()
        self.assertEqual(new_data["items_count"], 0)

    def test_checkout_empty_cart_error(self):
        resp = self.client.post(
            f"/api/municipio/checkout?tenant_slug={self.tenant.slug}",
            headers={"X-Anon-Id": "anon-empty"}
        )
        self.assertEqual(resp.status_code, 400) # Should fail if empty

if __name__ == "__main__":
    unittest.main()

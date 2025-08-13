import pytest
import unittest
from app import create_app, db
from services import cart as cart_service
from config import Config

@pytest.mark.legacy
class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class CartTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()
        self.pyme_id = 1  # Example pyme_id for testing

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_add_and_update(self):
        pyme_carts_data = {}
        cart_service.add_item_to_cart(pyme_carts_data, self.pyme_id, {"catalogo_item_id": 1, "nombre": "vino"}, 2)
        cart_service.add_item_to_cart(pyme_carts_data, self.pyme_id, {"catalogo_item_id": 1, "nombre": "vino"}, 1)
        summary = cart_service.get_cart_summary(pyme_carts_data, self.pyme_id)
        self.assertEqual(summary["items_detalle"][0]['nombre_producto'], 'vino')
        self.assertEqual(summary["items_detalle"][0]['cantidad'], 3)
        cart_service.update_item_quantity_in_cart(pyme_carts_data, self.pyme_id, 1, 5)
        summary = cart_service.get_cart_summary(pyme_carts_data, self.pyme_id)
        self.assertEqual(summary["items_detalle"][0]['cantidad'], 5)

    def test_remove(self):
        pyme_carts_data = {}
        cart_service.add_item_to_cart(pyme_carts_data, self.pyme_id, {"catalogo_item_id": 1, "nombre": "vino"}, 2)
        summary = cart_service.get_cart_summary(pyme_carts_data, self.pyme_id)
        cart_service.remove_item_from_cart(pyme_carts_data, self.pyme_id, 1)
        summary = cart_service.get_cart_summary(pyme_carts_data, self.pyme_id)
        self.assertEqual(summary["items_detalle"], [])


if __name__ == '__main__':
    unittest.main()

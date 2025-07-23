import unittest
from unittest.mock import patch
from app import create_app
from services import cart as cart_service
from config import TestConfig

class CartTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.pyme_id = 1
        cart_service.clear_cart(self.pyme_id)

    def tearDown(self):
        cart_service.clear_cart(self.pyme_id)
        self.app_context.pop()

    def test_add_and_update(self):
        cart_service.add_item_to_cart(self.pyme_id, {"nombre": "vino", "cantidad": 2}, {"precio": 10})
        cart = cart_service.get_cart(self.pyme_id)
        self.assertEqual(len(cart), 1)
        cart_service.add_item_to_cart(self.pyme_id, {"nombre": "vino", "cantidad": 3}, {"precio": 10})
        cart = cart_service.get_cart(self.pyme_id)
        self.assertEqual(cart[0]['cantidad'], 5)

    def test_remove(self):
        cart_service.add_item_to_cart(self.pyme_id, {"nombre": "vino", "cantidad": 2}, {"precio": 10})
        cart_service.remove_item_from_cart(self.pyme_id, {"nombre": "vino"})
        cart = cart_service.get_cart(self.pyme_id)
        self.assertEqual(len(cart), 0)

    def test_clear(self):
        cart_service.add_item_to_cart(self.pyme_id, {"nombre": "vino", "cantidad": 2}, {"precio": 10})
        cart_service.add_item_to_cart(self.pyme_id, {"nombre": "cerveza", "cantidad": 1}, {"precio": 5})
        cart_service.clear_cart(self.pyme_id)
        cart = cart_service.get_cart(self.pyme_id)
        self.assertEqual(len(cart), 0)

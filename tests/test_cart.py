import unittest
from flask import Flask
from services.cart import add_item, remove_item, update_item, get_summary, clear_cart


class CartTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.pyme_carts_data = {}
        clear_cart(self.pyme_carts_data)

    def tearDown(self):
        self.ctx.pop()

    def test_add_and_update(self):
        add_item(self.pyme_carts_data, 'vino', 2)
        add_item(self.pyme_carts_data, 'vino', 1)
        self.assertEqual(get_summary(self.pyme_carts_data), [{'nombre': 'vino', 'cantidad': 3}])
        update_item(self.pyme_carts_data, 'vino', 5)
        self.assertEqual(get_summary(self.pyme_carts_data)[0]['cantidad'], 5)

    def test_remove(self):
        add_item(self.pyme_carts_data, 'vino', 2)
        remove_item(self.pyme_carts_data, 'vino')
        self.assertEqual(get_summary(self.pyme_carts_data), [])


if __name__ == '__main__':
    unittest.main()

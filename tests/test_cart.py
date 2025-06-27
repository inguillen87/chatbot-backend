import unittest
from flask import Flask
from services.cart import add_item, remove_item, update_item, get_summary, clear_cart


class CartTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        clear_cart()

    def tearDown(self):
        self.ctx.pop()

    def test_add_and_update(self):
        add_item('vino', 2)
        add_item('vino', 1)
        self.assertEqual(get_summary(), [{'nombre': 'vino', 'cantidad': 3}])
        update_item('vino', 5)
        self.assertEqual(get_summary()[0]['cantidad'], 5)

    def test_remove(self):
        add_item('vino', 2)
        remove_item('vino')
        self.assertEqual(get_summary(), [])


if __name__ == '__main__':
    unittest.main()

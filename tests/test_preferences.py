import unittest
from flask import Flask
from services.preferences import add_preference, get_preferences, clear_preferences


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        clear_preferences({})

    def tearDown(self):
        self.ctx.pop()

    def test_add_and_get(self):
        context = {}
        add_preference(context, 'color', 'rojo')
        add_preference(context, 'color', 'azul')
        add_preference(context, 'vino', 'malbec')
        self.assertEqual(set(get_preferences(context, 'color')), {'rojo', 'azul'})
        self.assertEqual(get_preferences(context, 'vino'), ['malbec'])


if __name__ == '__main__':
    unittest.main()

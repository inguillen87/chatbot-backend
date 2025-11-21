import unittest

from flask import Flask

from routes.chat import chat_bp


class WidgetAttentionEndpointTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test")
        app.register_blueprint(chat_bp)
        self.client = app.test_client()

    def test_default_message(self):
        resp = self.client.get('/widget/attention')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('mensaje', resp.get_json())

    def test_random_from_choices(self):
        app = Flask(__name__)
        app.config.update(
            TESTING=True,
            SECRET_KEY="test",
            ATTENTION_BUBBLE_CHOICES=['hola', 'reclamo'],
        )
        app.register_blueprint(chat_bp)
        client = app.test_client()
        resp = client.get('/widget/attention')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.get_json()['mensaje'], ['hola', 'reclamo'])

if __name__ == '__main__':
    unittest.main()

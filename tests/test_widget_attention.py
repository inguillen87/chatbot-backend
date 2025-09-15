import unittest

try:
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class WidgetAttentionEndpointTests(unittest.TestCase):
    def setUp(self):
        from config import TestConfig
        app = create_app(TestConfig)
        self.client = app.test_client()

    def test_default_message(self):
        resp = self.client.get('/widget/attention')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('mensaje', resp.get_json())

    def test_random_from_choices(self):
        from config import TestConfig
        app = create_app(TestConfig)
        app.config['ATTENTION_BUBBLE_CHOICES'] = ['hola', 'reclamo']
        client = app.test_client()
        resp = client.get('/widget/attention')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.get_json()['mensaje'], ['hola', 'reclamo'])

if __name__ == '__main__':
    unittest.main()

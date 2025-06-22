import unittest
import sys

try:
    if 'flask' in sys.modules:
        del sys.modules['flask']
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class WidgetAttentionEndpointTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config['TESTING'] = True
        self.app = app
        self.client = app.test_client()

    def test_attention_endpoint_returns_greeting(self):
        custom_text = 'Prueba de atención'
        self.app.config['ATTENTION_BUBBLE_TEXT'] = custom_text
        res = self.client.get('/widget/attention')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get('mensaje'), custom_text)

if __name__ == '__main__':
    unittest.main()

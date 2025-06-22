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
        self.client = app.test_client()

    def test_attention_endpoint_returns_greeting(self):
        res = self.client.get('/widget/attention')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('mensaje', data)
        self.assertTrue(data['mensaje'])

if __name__ == '__main__':
    unittest.main()

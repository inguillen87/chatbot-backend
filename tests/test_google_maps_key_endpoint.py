import unittest
import os

try:
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class GoogleMapsKeyEndpointTest(unittest.TestCase):
    def setUp(self):
        os.environ['GOOGLE_MAPS_API_KEY'] = 'abc123'
        app = create_app()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_key_returned(self):
        resp = self.client.get('/google-maps-key')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get('api_key'), 'abc123')

if __name__ == '__main__':
    unittest.main()

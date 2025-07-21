import unittest
import os
from app import create_app
from config import TestConfig

class GoogleMapsKeyEndpointTest(unittest.TestCase):
    def setUp(self):
        os.environ['GOOGLE_MAPS_API_KEY'] = 'abc123'
        self.app = create_app(TestingConfig)
        self.client = self.app.test_client()

    def test_key_returned(self):
        with self.app.app_context():
            resp = self.client.get('/config/google-maps-key')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('google_maps_key'), 'abc123')

if __name__ == '__main__':
    unittest.main()

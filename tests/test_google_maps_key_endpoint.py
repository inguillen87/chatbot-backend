import unittest

from app import create_app
from config import TestConfig

class GoogleMapsKeyEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config['GOOGLE_MAPS_API_KEY'] = 'abc123'
        self.app.config['MAPTILER_API_KEY'] = 'maptiler-456'
        self.client = self.app.test_client()

    def test_key_returned(self):
        with self.app.app_context():
            resp = self.client.get('/config/google-maps-key')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('google_maps_key'), 'abc123')
            self.assertEqual(data.get('maptiler_key'), 'maptiler-456')
            self.assertEqual(data.get('provider'), 'google')

    def test_fallback_to_maptiler_when_google_missing(self):
        with self.app.app_context():
            self.app.config['GOOGLE_MAPS_API_KEY'] = ''
            self.app.config['MAPTILER_API_KEY'] = 'mt-key'
            resp = self.client.get('/config/google-maps-key')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('provider'), 'maptiler')
            self.assertEqual(data.get('maptiler_key'), 'mt-key')
            self.assertEqual(data.get('google_maps_key'), '')

    def test_returns_none_provider_when_no_keys_present(self):
        with self.app.app_context():
            self.app.config['GOOGLE_MAPS_API_KEY'] = ''
            self.app.config['MAPTILER_API_KEY'] = ''
            resp = self.client.get('/config/google-maps-key')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('provider'), 'none')
            self.assertEqual(data.get('google_maps_key'), '')
            self.assertEqual(data.get('maptiler_key'), '')

if __name__ == '__main__':
    unittest.main()

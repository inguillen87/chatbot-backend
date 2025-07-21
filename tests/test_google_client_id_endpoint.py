import unittest
import os
from app import create_app
from config import TestConfig

class GoogleClientIdEndpointTest(unittest.TestCase):
    def setUp(self):
        os.environ['GOOGLE_OAUTH_CLIENT_ID'] = 'id1,id2'
        self.app = create_app(TestingConfig)
        self.client = self.app.test_client()

    def test_first_client_id_returned(self):
        with self.app.app_context():
            resp = self.client.get('/google-client-id')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('client_id'), 'id1')

if __name__ == '__main__':
    unittest.main()

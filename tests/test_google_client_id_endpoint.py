import unittest
import os

try:
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class GoogleClientIdEndpointTest(unittest.TestCase):
    def setUp(self):
        os.environ['GOOGLE_OAUTH_CLIENT_ID'] = 'id1,id2'
        app = create_app()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_first_client_id_returned(self):
        resp = self.client.get('/auth/google-client-id')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get('client_id'), 'id1')

if __name__ == '__main__':
    unittest.main()

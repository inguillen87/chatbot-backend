import unittest
try:
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class CorsOptionsTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_historial_options(self):
        resp = self.client.options('/historial', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_notifications_options(self):
        resp = self.client.options('/notifications', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_ask_municipio_options(self):
        resp = self.client.options('/ask/municipio', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'POST'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_perfil_options(self):
        resp = self.client.options('/perfil', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'PUT'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

if __name__ == '__main__':
    unittest.main()

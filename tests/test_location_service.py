import unittest
import json
from app import create_app, db
from config import TestingConfig

class TestLocationService(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_get_google_maps_key(self):
        response = self.client.get("/config/google-maps-key")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("google_maps_key", data)

    def test_parse_location_data(self):
        from routes.chat import _parse_request
        with self.app.test_request_context(
            "/ask",
            method="POST",
            data=json.dumps({
                "pregunta": "test",
                "tipo_chat": "municipio",
                "location": {"lat": 12.34, "lon": 56.78}
            }),
            content_type="application/json"
        ):
            pregunta, contexto_previo, tipo_chat, rubro_id, rubro_clave, attachment_info, location, error_response = _parse_request()
            self.assertIsNone(error_response)
            self.assertEqual(location, {"lat": 12.34, "lon": 56.78})

if __name__ == "__main__":
    unittest.main()

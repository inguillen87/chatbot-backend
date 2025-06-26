import unittest

try:
    from app import create_app
except Exception:
    create_app = None


@unittest.skipIf(create_app is None, "Flask not available")
class ReaccionesEndpointTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()
        with app.app_context():
            from extensions import db
            from models import Conversacion

            db.create_all()
            conv = Conversacion(pregunta="p", respuesta="r", fuente="bot")
            db.session.add(conv)
            db.session.commit()
            self.conv_id = conv.id

    def test_options(self):
        resp = self.client.options(
            "/reacciones",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(resp.status_code, 200)

    def test_agregar_y_listar(self):
        self.client.post(
            "/reacciones",
            json={"conversacion_id": self.conv_id, "emoji": "👍"},
            headers={"Authorization": "Bearer tok"},
        )
        resp = self.client.get(
            f"/reacciones/{self.conv_id}", headers={"Authorization": "Bearer tok"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json.get("👍"), 1)


if __name__ == "__main__":
    unittest.main()

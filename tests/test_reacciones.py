import unittest
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_reacciones = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_reacciones not in sys.path:
    sys.path.insert(0, project_root_reacciones)

try:
    from app import create_app
    from models import Conversacion # Moved to top
    from extensions import db # Moved to top, though typically imported where used or in app context
except Exception:
    create_app = None
    Conversacion = None # Define for skipIf
    db = None # Define for skipIf

@unittest.skipIf(create_app is None or Conversacion is None or db is None, "Flask or Models not available")
class ReaccionesEndpointTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()
        with app.app_context():
            # db and Conversacion are now imported at the top
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

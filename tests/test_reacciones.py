import unittest
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_reacciones = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_reacciones not in sys.path:
    sys.path.insert(0, project_root_reacciones)

import jwt
from datetime import datetime, timedelta

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
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()
        from models import User
        # Create a user with the token 'tok'
        user = User(email="reacciones@test.com", name="Test User")
        user.set_password("password")
        db.session.add(user)
        db.session.commit() # Commit to get user.id

        conv = Conversacion(pregunta="p", respuesta="r", fuente="bot", user_id=user.id)
        db.session.add(conv)
        db.session.commit()
        self.conv_id = conv.id
        self.user_id = user.id

        jwt_payload = {'user_id': user.id, 'exp': datetime.utcnow() + timedelta(days=1)}
        self.jwt_token = jwt.encode(jwt_payload, self.app.config['SECRET_KEY'], algorithm="HS256")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

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
            headers={"Authorization": f"Bearer {self.jwt_token}"},
        )
        resp = self.client.get(
            f"/reacciones/{self.conv_id}", headers={"Authorization": f"Bearer {self.jwt_token}"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json.get("👍"), 1)


if __name__ == "__main__":
    unittest.main()

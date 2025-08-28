import unittest
from app import create_app, db
from services.actions.municipio_actions import CrearReclamoActionHandler
from config import TestConfig

class TestCrearReclamoActionHandler(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_execute(self):
        handler = CrearReclamoActionHandler(context={})
        action_data = {
            "categoria": "Bacheo",
            "descripcion": "Arreglar el bache que hay en mi cuadra",
            "ubicacion": "San Martín 15",
            "usuario": "Marcelo",
            "telefono": "2613168608",
            "email": "prueb@prueb.com",
            "pin": "654321",
            "dni": "33333333"
        }
        result = handler.execute(action_data)
        self.assertTrue(result["success"])
        self.assertIn("ticket_id", result["data"])
        self.assertIn("nro_ticket", result["data"])

if __name__ == "__main__":
    unittest.main()

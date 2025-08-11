import unittest
from app import create_app, db
from services.actions.municipio_actions import CrearReclamoActionHandler
from config import TestConfig
from models import User, MunicipioTicket

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

    def test_execute_with_full_data(self):
        handler = CrearReclamoActionHandler(context={})
        action_data = {
            "categoria": "Bacheo",
            "descripcion": "Arreglar el bache que hay en mi cuadra",
            "ubicacion": "San Martín 15",
            "distrito": "Ciudad",
            "usuario": "Marcelo",
            "telefono": "2613168608",
            "email": "prueb@prueb.com"
        }
        result = handler.execute(action_data)
        self.assertTrue(result["success"])
        self.assertIn("ticket_id", result["data"])
        self.assertIn("nro_ticket", result["data"])
        ticket = db.session.get(MunicipioTicket, result["data"]["ticket_id"])
        self.assertEqual(ticket.nombre_vecino, "Marcelo")
        self.assertEqual(ticket.distrito, "Ciudad")

    def test_execute_with_profile_name_and_district_normalization(self):
        # Create a mock user to be the viewer
        viewer_user = User(name="Test User", email="test@example.com", password_hash="somehash")
        db.session.add(viewer_user)
        db.session.commit()

        context = {
            "viewer_user_obj": viewer_user,
            "profile_name": "Marcelo Profile",
            "channel": "whatsapp",
        }
        handler = CrearReclamoActionHandler(context=context)

        action_data = {
            "categoria": "Arbolado",
            "descripcion": "Arboles tapando la calle",
            "ubicacion": "Don Bosco 55",
            "distrito": "junin centro", # Test normalization
            # No 'usuario' field from LLM
        }
        result = handler.execute(action_data)
        self.assertTrue(result["success"], f"Execution failed: {result.get('error_details')}")
        self.assertIn("ticket_id", result["data"])

        # Verify the created ticket in the database
        ticket = db.session.get(MunicipioTicket, result["data"]["ticket_id"])
        self.assertIsNotNone(ticket)
        # It should use the profile_name as a fallback
        self.assertEqual(ticket.nombre_vecino, "Marcelo Profile")
        # It should have normalized the district
        self.assertEqual(ticket.distrito, "Junín")

if __name__ == "__main__":
    unittest.main()

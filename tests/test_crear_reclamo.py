import unittest
from unittest.mock import patch
from app import create_app, db
from services.actions.municipio_actions import CrearReclamoActionHandler
from config import TestConfig
from models import MunicipioTicket, TenantProfile, User

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
        owner = User(
            name="Municipio Reclamo",
            email="municipio-reclamo@test.com",
            password_hash="test-hash",
            rol="admin",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="municipio-reclamo",
            nombre="Municipio Reclamo",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add(tenant)
        db.session.commit()
        handler = CrearReclamoActionHandler(
            context={
                "user_obj": owner,
                "tenant_profile": tenant,
                "tenant_id": tenant.id,
                "channel": "web",
                "anon_id": "crear-reclamo-test",
                "municipio_config_actual": {"tenant_slug": tenant.slug},
                "chat_db_context_data": {"processed_idempotency_keys": {}},
            }
        )
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
        with (
            patch(
                "services.herramientas_municipio.parse_direccion_completa",
                return_value={
                    "calle": "San Martín",
                    "numero": "15",
                    "localidad": "Centro",
                },
            ),
            patch(
                "services.location_service.geocode_address",
                return_value=(-32.89, -68.83),
            ),
        ):
            result = handler.execute(action_data)
        self.assertTrue(result["success"])
        self.assertIn("ticket_id", result["data"])
        self.assertIn("nro_ticket", result["data"])
        ticket = db.session.get(MunicipioTicket, result["data"]["ticket_id"])
        self.assertEqual(ticket.tenant_id, tenant.id)
        self.assertEqual(ticket.municipio_id, owner.id)

if __name__ == "__main__":
    unittest.main()

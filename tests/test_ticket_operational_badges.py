import unittest
from datetime import timedelta

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, User
from routes.ticket import serialize_ticket_to_json, _serialize_ticket_details
from utils.time_utils import get_local_now


class TicketOperationalBadgesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.admin = User(name="Admin", email="ops@example.com", rol="admin", tipo_chat="municipio")
        self.admin.set_password("pass")
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_unassigned_ticket_exposes_sla_badges(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="bache",
            nro_ticket="123456",
            estado="nuevo",
            fecha=get_local_now() - timedelta(hours=9),
            ultima_actividad=get_local_now() - timedelta(hours=9),
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio")

        self.assertEqual(payload["sla_status"], "por_vencer")
        self.assertIn("sin_asignar", payload["operational_badges"])
        self.assertIn("por_vencer", payload["operational_badges"])

    def test_detail_payload_marks_pending_response_for_assigned_ticket(self):
        agente = User(
            name="Agente",
            email="agente@example.com",
            rol="empleado",
            tipo_chat="municipio",
            municipio_id=self.admin.id,
        )
        agente.set_password("pass")
        db.session.add(agente)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="luminaria",
            nro_ticket="654321",
            estado="en_proceso",
            fecha=get_local_now() - timedelta(hours=12),
            ultima_actividad=get_local_now() - timedelta(hours=3),
            asignado_a_id=agente.id,
        )
        db.session.add(ticket)
        db.session.commit()

        payload = _serialize_ticket_details(ticket, "municipio")

        self.assertEqual(payload["sla_status"], "seguimiento")
        self.assertIn("respuesta_pendiente", payload["operational_badges"])
        self.assertGreaterEqual(payload["operational_metrics"]["inactivity_hours"], 3)


if __name__ == "__main__":
    unittest.main()

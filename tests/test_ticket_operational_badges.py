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

    def test_ticket_payload_exposes_only_consented_profile_avatar(self):
        vecino = User(
            name="Marcelo Vecino",
            email="vecino-avatar@example.com",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/vecino.webp",
                "avatar_source": "profile_upload",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            user_id=vecino.id,
            pregunta="arreglo de calle",
            nro_ticket="777001",
            estado="nuevo",
            nombre_vecino="Marcelo Vecino",
            telefono_vecino="+5492613168608",
        )
        db.session.add(ticket)
        db.session.commit()

        list_payload = serialize_ticket_to_json(ticket, "municipio", compact=True)
        detail_payload = _serialize_ticket_details(ticket, "municipio")

        self.assertEqual(list_payload["avatar_url"], "https://cdn.example.com/profile/vecino.webp")
        self.assertTrue(list_payload["avatar_consent"])
        self.assertEqual(list_payload["contact_identity"]["avatar_source"], "profile_upload")
        self.assertEqual(list_payload["contact"]["avatar_url"], "https://cdn.example.com/profile/vecino.webp")
        self.assertEqual(
            detail_payload["informacion_personal_vecino"]["avatar_url"],
            "https://cdn.example.com/profile/vecino.webp",
        )
        self.assertEqual(
            detail_payload["contact"]["identity"]["fallback"],
            "deterministic_identity_avatar",
        )

    def test_ticket_payload_hides_untrusted_whatsapp_profile_avatar(self):
        vecino = User(
            name="WhatsApp Vecino",
            email="vecino-whatsapp-avatar@example.com",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/wa-profile.webp",
                "avatar_source": "whatsapp_profile",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            user_id=vecino.id,
            pregunta="luminaria",
            nro_ticket="777002",
            estado="nuevo",
            nombre_vecino="WhatsApp Vecino",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertIsNone(payload["avatar_url"])
        self.assertFalse(payload["avatar_consent"])
        self.assertIsNone(payload["contact_identity"]["avatar_url"])
        self.assertEqual(payload["contact_identity"]["fallback"], "deterministic_identity_avatar")

    def test_ticket_payload_resolves_registered_profile_avatar_by_email_without_user_id(self):
        vecino = User(
            name="Vecino Registrado",
            email="vecino-registrado@example.com",
            telefono="+5492613168608",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/vecino-registrado.webp",
                "avatar_source": "social_login_google",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="arreglo de calle",
            nro_ticket="777003",
            estado="nuevo",
            nombre_vecino="Vecino Registrado",
            telefono_vecino="+54 9 261 316 8608",
            email_vecino="vecino-registrado@example.com",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(payload["avatar_url"], "https://cdn.example.com/profile/vecino-registrado.webp")
        self.assertTrue(payload["avatar_consent"])
        self.assertEqual(payload["contact_identity"]["user_id"], vecino.id)
        self.assertEqual(payload["contact_identity"]["avatar_source"], "social_login_google")
        self.assertEqual(payload["contact"]["avatar_url"], "https://cdn.example.com/profile/vecino-registrado.webp")

    def test_ticket_payload_resolves_registered_profile_avatar_by_phone_without_user_id(self):
        vecino = User(
            name="Vecino Telefono",
            email="vecino-telefono@example.com",
            telefono="+5492613000000",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/vecino-telefono.webp",
                "avatar_source": "profile_upload",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="luminaria",
            nro_ticket="777004",
            estado="nuevo",
            nombre_vecino="Vecino Telefono",
            telefono_vecino="5492613000000",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(payload["avatar_url"], "https://cdn.example.com/profile/vecino-telefono.webp")
        self.assertTrue(payload["avatar_consent"])
        self.assertEqual(payload["contact_identity"]["user_id"], vecino.id)
        self.assertEqual(payload["contact_identity"]["avatar_source"], "profile_upload")


if __name__ == "__main__":
    unittest.main()

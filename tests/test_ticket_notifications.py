import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, TenantProfile, TicketComentario, User
from services.ticket_service import servicio_tickets
from utils.auth_helpers import generar_token


class TicketNotificationFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Municipal admin user
        admin = User(
            name="Admin",
            email="admin@example.com",
            rol="admin",
            tipo_chat="municipio",
        )
        admin.set_password("secret")
        db.session.add(admin)
        db.session.flush()
        admin.municipio_id = admin.id

        tenant = TenantProfile(
            slug="ticket-notifications-municipio",
            nombre="Municipio Ticket Notifications",
            tipo="municipio",
            municipio_id=admin.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        admin.tenant_id = tenant.id
        admin.tenant_slug = tenant.slug
        db.session.commit()
        self.admin = admin
        self.tenant = tenant

        ticket = MunicipioTicket(
            nro_ticket="654321",
            municipio_id=admin.id,
            tenant_id=tenant.id,
            pregunta="¿Cuándo arreglan la luz?",
            canal_ingreso="web",
        )
        # Force an old last activity to verify it updates
        ticket.ultima_actividad = datetime(2023, 1, 1, tzinfo=timezone.utc)
        db.session.add(ticket)
        db.session.commit()
        self.ticket = ticket

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_crear_comentario_updates_activity_and_notifies(self):
        previous_activity = self.ticket.ultima_actividad
        with (
            patch("services.email_service.enviar_email_ticket_novedad") as mock_email,
            patch("services.email_service.enviar_sms_ticket_novedad") as mock_sms,
            patch("services.email_service.enviar_whatsapp_ticket_novedad") as mock_whatsapp,
        ):
            comentario = servicio_tickets.crear_comentario(
                ticket_id=self.ticket.id,
                tipo_ticket="municipio",
                comentario_data={
                    "comentario": "Avanzamos con la reparación",
                    "user_id": self.admin.id,
                    "es_admin": True,
                },
            )
            self.assertIsNotNone(comentario)
            db.session.commit()

        ticket_refreshed = db.session.get(MunicipioTicket, self.ticket.id)
        self.assertIsNotNone(ticket_refreshed.ultima_actividad)
        self.assertGreater(ticket_refreshed.ultima_actividad, previous_activity)
        mock_email.assert_called_once()
        mock_sms.assert_called_once()
        mock_whatsapp.assert_called_once()

    def test_cambiar_estado_ticket_dispatches_notifications(self):
        token = generar_token(
            self.admin.id,
            self.admin.rol,
            self.admin.tipo_chat,
            self.admin.municipio_id,
            self.admin.pyme_id,
        )
        headers = {"Authorization": f"Bearer {token}"}
        previous_activity = self.ticket.ultima_actividad

        with (
            patch("services.email_service.enviar_email_ticket_novedad") as mock_email,
            patch("services.email_service.enviar_sms_ticket_novedad") as mock_sms,
            patch("services.email_service.enviar_whatsapp_ticket_novedad") as mock_whatsapp,
            patch("routes.ticket.emit_ticket_update") as mock_emit,
        ):
            response = self.client.put(
                f"/tickets/municipio/{self.ticket.id}/estado",
                json={"estado": "en_proceso"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        updated_ticket = db.session.get(MunicipioTicket, self.ticket.id)
        self.assertEqual(updated_ticket.estado, "en_proceso")
        self.assertGreater(updated_ticket.ultima_actividad, previous_activity)

        comentarios = (
            TicketComentario.query
            .filter_by(municipio_ticket_id=self.ticket.id, estado_ticket="en_proceso")
            .all()
        )
        self.assertEqual(len(comentarios), 1)

        mock_email.assert_called_once()
        mock_sms.assert_called_once()
        mock_whatsapp.assert_called_once()
        mock_emit.assert_called_once()

        kwargs = mock_email.call_args.kwargs
        self.assertIn("comentario_reciente", kwargs)
        self.assertEqual(kwargs["comentario_reciente"].estado_ticket, "en_proceso")


if __name__ == "__main__":
    unittest.main()

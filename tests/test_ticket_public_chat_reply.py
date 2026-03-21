import unittest

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, TicketComentario, User


class TicketPublicChatReplyTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        admin = User(name="Admin", email="admin-chat@example.com", rol="admin", tipo_chat="municipio")
        admin.set_password("pass")
        db.session.add(admin)
        db.session.commit()
        self.admin = admin

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_pin_can_reply_to_ticket_chat(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="bache en la calle",
            estado="nuevo",
            nro_ticket="123456",
            consulta_pin="654321",
        )
        db.session.add(ticket)
        db.session.commit()

        response = self.client.post(
            f"/tickets/chat/{ticket.id}/responder_ciudadano?pin=654321",
            json={"comentario": "Necesito una actualización del reclamo"},
        )

        self.assertEqual(response.status_code, 201)
        comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).one()
        self.assertEqual(comentario.comentario, "Necesito una actualización del reclamo")
        self.assertFalse(comentario.es_admin)

    def test_public_pin_reply_rejects_invalid_pin(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="bache en la calle",
            estado="nuevo",
            nro_ticket="123456",
            consulta_pin="654321",
        )
        db.session.add(ticket)
        db.session.commit()

        response = self.client.post(
            f"/tickets/chat/{ticket.id}/responder_ciudadano?pin=000000",
            json={"comentario": "Necesito una actualización del reclamo"},
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()

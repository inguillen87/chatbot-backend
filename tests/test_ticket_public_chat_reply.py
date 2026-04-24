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

    def test_public_pin_can_reply_with_form_encoded_payload(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="luminaria apagada",
            estado="nuevo",
            nro_ticket="123457",
            consulta_pin="654321",
        )
        db.session.add(ticket)
        db.session.commit()

        response = self.client.post(
            f"/tickets/chat/{ticket.id}/responder_ciudadano?pin=654321",
            data={"comentario": "Mensaje enviado como form"},
            content_type="application/x-www-form-urlencoded",
        )

        self.assertEqual(response.status_code, 201)
        comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).order_by(TicketComentario.id.desc()).first()
        self.assertIsNotNone(comentario)
        self.assertEqual(comentario.comentario, "Mensaje enviado como form")

    def test_ticket_timeline_includes_unified_conversation_stream(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="bache en la calle",
            estado="nuevo",
            nro_ticket="123456",
            consulta_pin="654321",
        )
        db.session.add(ticket)
        db.session.commit()

        db.session.add(
            TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario="Seguimos esperando novedades",
                es_admin=False,
            )
        )
        db.session.commit()

        response = self.client.get(f"/tickets/municipio/{ticket.id}/timeline?pin=654321")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("unified_conversation_stream", payload)
        self.assertGreaterEqual(len(payload["unified_conversation_stream"]), 2)
        self.assertEqual(payload["unified_conversation_stream"][0]["source"], "timeline")
        self.assertIn("id", payload["unified_conversation_stream"][0])
        self.assertIn("actor_type", payload["unified_conversation_stream"][0])
        self.assertIn("preview_text", payload["unified_conversation_stream"][0])


if __name__ == "__main__":
    unittest.main()

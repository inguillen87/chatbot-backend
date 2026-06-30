import unittest

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, PymeTicket, TicketComentario, User


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
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tickets.public_chat_reply.v2")
        self.assertEqual(payload["legacy_contract_version"], "tickets.public_chat_reply.v1")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["ticket_id"], ticket.id)
        self.assertEqual(payload["tipo"], "municipio")
        self.assertEqual(payload["estado_chat"], "nuevo")
        self.assertEqual(payload["socket_room"], f"municipio_{self.admin.id}")
        self.assertEqual(payload["live_chat"]["socket_room"], f"municipio_{self.admin.id}")
        self.assertIn(payload["reply_status"], {"sent_to_live_chat", "queued_for_agent"})
        self.assertEqual(payload["delivery"]["channel"], "ticket_conversation")
        self.assertTrue(payload["polling"]["enabled"])
        self.assertEqual(payload["polling"]["interval_ms"], 10000)
        self.assertGreaterEqual(len(payload["ui_actions"]), 1)
        self.assertEqual(payload["comment"]["comentario"], payload["comentario"]["comentario"])
        self.assertEqual(payload["mensaje_id"], payload["comment"]["id"])
        self.assertIn(payload["mode"], {"live", "offline"})
        self.assertIn("live_chat", payload)
        self.assertIn("realtime_state", payload)
        self.assertEqual(payload["live_chat"]["contract_version"], "live_chat.schedule.v1")

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

    def test_public_pin_can_reply_to_pyme_ticket_chat(self):
        ticket = PymeTicket(
            rubro_id=77,
            pregunta="quiero confirmar un pedido",
            asunto="Pedido marketplace",
            categoria="Pedidos",
            estado="nuevo",
            nro_ticket=445566,
            consulta_pin="112233",
        )
        db.session.add(ticket)
        db.session.commit()

        response = self.client.post(
            f"/tickets/chat/pyme/{ticket.id}/responder_cliente?pin=112233",
            json={"comentario": "Hola, quiero hablar con ventas"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tickets.public_chat_reply.v2")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["ticket_id"], ticket.id)
        self.assertEqual(payload["tipo"], "pyme")
        self.assertEqual(payload["socket_room"], "pyme_77")
        self.assertEqual(payload["delivery"]["channel"], "ticket_conversation")
        self.assertTrue(payload["polling"]["enabled"])
        self.assertEqual(payload["comment"]["comentario"], "Hola, quiero hablar con ventas")

        comentario = TicketComentario.query.filter_by(pyme_ticket_id=ticket.id).one()
        self.assertEqual(comentario.comentario, "Hola, quiero hablar con ventas")
        self.assertFalse(comentario.es_admin)

    def test_public_pin_can_read_pyme_ticket_chat_messages(self):
        ticket = PymeTicket(
            rubro_id=77,
            pregunta="consulta de stock",
            asunto="Consulta de stock",
            categoria="Ventas",
            estado="nuevo",
            nro_ticket=445567,
            consulta_pin="112233",
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(
            TicketComentario(
                pyme_ticket_id=ticket.id,
                comentario="Me interesa comprar 20 unidades",
                es_admin=False,
            )
        )
        db.session.commit()

        response = self.client.get(f"/tickets/chat/pyme/{ticket.id}/mensajes?pin=112233")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["estado_chat"], "nuevo")
        self.assertEqual(len(payload["mensajes"]), 1)
        self.assertEqual(payload["mensajes"][0]["texto"], "Me interesa comprar 20 unidades")
        self.assertIn("realtime_state", payload)

    def test_public_pin_reply_rejects_invalid_pyme_pin(self):
        ticket = PymeTicket(
            rubro_id=77,
            pregunta="consulta de stock",
            asunto="Consulta de stock",
            categoria="Ventas",
            estado="nuevo",
            nro_ticket=445568,
            consulta_pin="112233",
        )
        db.session.add(ticket)
        db.session.commit()

        response = self.client.post(
            f"/tickets/chat/pyme/{ticket.id}/responder_cliente?pin=000000",
            json={"comentario": "Intento invalido"},
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()

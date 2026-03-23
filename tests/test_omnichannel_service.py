import unittest
from types import SimpleNamespace
from unittest.mock import patch

import services.omnichannel_service as omni


class DummySession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


db_stub = SimpleNamespace(session=DummySession())


class DummyTicket:
    def __init__(self, id=99):
        self.id = id
        self.estado = "nuevo"


class DummyComment:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class DummyUser(SimpleNamespace):
    id = 7
    anon_id = "anon-7"
    telefono = None


class DummyConversation(SimpleNamespace):
    id = "conv-123"


class OmnichannelServiceTest(unittest.TestCase):
    def test_rechaza_tipo_ticket_invalido(self):
        result = omni.registrar_interaccion_omnicanal({"tipo_ticket": "otro"})
        self.assertFalse(result["exito"])
        self.assertEqual(result["motivo"], "tipo_ticket_invalido")

    def test_crea_ticket_con_mensaje(self):
        payload = {
            "canal": "Messenger",
            "mensaje": "Hola omnicanal",
            "contacto": {"email": "test@example.com"},
            "tipo_ticket": "municipio",
            "tenant_id": 3,
        }

        with patch.object(omni, "db", db_stub), patch.object(
            omni, "TicketComentario", DummyComment
        ), patch.object(omni, "_deduplicate_contact", return_value=DummyUser()), patch.object(
            omni, "_buscar_ticket_abierto", return_value=None
        ), patch.object(
            omni, "resolve_or_create_conversation", return_value=DummyConversation()
        ), patch.object(
            omni, "append_conversation_message"
        ), patch.object(
            omni, "servicio_tickets", autospec=True
        ) as servicio_mock:
            servicio_mock.crear_nuevo_ticket.return_value = DummyTicket()

            result = omni.registrar_interaccion_omnicanal(payload)

        self.assertTrue(result["exito"])
        self.assertTrue(result["nuevo_ticket"])
        self.assertEqual(result["ticket_id"], 99)
        self.assertEqual(result["canal"], "messenger")
        self.assertEqual(result["conversation_id"], "conv-123")


if __name__ == "__main__":
    unittest.main()

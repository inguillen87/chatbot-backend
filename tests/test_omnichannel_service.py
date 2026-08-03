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

    def get(self, _model, object_id):
        return SimpleNamespace(id=object_id)


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
            omni,
            "normalize_municipio_ticket_write_scope",
            return_value={"tenant_id": 3, "municipio_id": 30},
        ), patch.object(
            omni, "servicio_tickets", autospec=True
        ) as servicio_mock:
            servicio_mock.crear_nuevo_ticket.return_value = DummyTicket()

            result = omni.registrar_interaccion_omnicanal(payload)

        self.assertTrue(result["exito"])
        self.assertTrue(result["nuevo_ticket"])
        self.assertEqual(result["ticket_id"], 99)
        self.assertEqual(result["canal"], "messenger")

    def test_source_event_is_recorded_once_per_ticket(self):
        ticket = SimpleNamespace(datos_extra={})
        payload = {
            "source": "signed_adapter:telegram",
            "provider_connection_id": 12,
            "source_event_id": "evt-1",
        }

        with patch.object(omni, "flag_modified") as flag:
            self.assertTrue(omni._record_ticket_event(ticket, payload))
            self.assertFalse(omni._record_ticket_event(ticket, payload))

        refs = ticket.datos_extra["omnichannel_source_events"]
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].startswith("omni-event-ref-v1:12:"))
        self.assertNotIn("evt-1", refs[0])
        flag.assert_called_once_with(ticket, "datos_extra")


if __name__ == "__main__":
    unittest.main()

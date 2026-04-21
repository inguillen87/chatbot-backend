import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask

from routes.ticket import get_public_ticket_status, ticket_bp


class _DummyTicket:
    nro_ticket = "12345"
    estado = "en_proceso"
    categoria = "alumbrado"
    subcategoria = "luminaria"
    canal_ingreso = "whatsapp"
    fecha = datetime(2026, 1, 1, 12, 0, 0)
    ultima_actividad = datetime(2026, 1, 2, 12, 0, 0)


class _DummyQuery:
    def __init__(self, ticket):
        self._ticket = ticket

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._ticket


class _DummyMunicipioTicket:
    query = _DummyQuery(_DummyTicket())


class _DummyMunicipioTicketMissing:
    query = _DummyQuery(None)


class TicketPublicStatusContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(ticket_bp)
        self.client = self.app.test_client()

    def test_public_status_requires_code_and_pin(self):
        response = self.client.get("/tickets/public/status")
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "tickets.public_status.v1")
        self.assertEqual(body["error"]["code"], 400)
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))

    def test_public_status_requires_pin(self):
        response = self.client.get("/tickets/public/status?code=M-12345")
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "tickets.public_status.v1")
        self.assertEqual(body["error"]["code"], 400)
        self.assertEqual(body["error"]["message"], "pin requerido.")
        self.assertTrue(body.get("request_id"))

    def test_public_status_contract(self):
        with patch("routes.ticket.MunicipioTicket", _DummyMunicipioTicket), patch(
            "routes.ticket.verify_recaptcha", return_value=True
        ):
            response = self.client.get("/tickets/public/status?code=M-12345&pin=9999")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "tickets.public_status.v1")
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))
        self.assertEqual(body["ticket"]["nro_ticket"], "M-12345")
        self.assertEqual(body["ticket"]["estado"], "en_proceso")

    def test_public_status_preserves_request_id_header(self):
        with patch("routes.ticket.MunicipioTicket", _DummyMunicipioTicket), patch(
            "routes.ticket.verify_recaptcha", return_value=True
        ):
            response = self.client.get(
                "/tickets/public/status?code=M-12345&pin=9999",
                headers={"X-Request-Id": "req-ticket-public"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["request_id"], "req-ticket-public")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-ticket-public")

    def test_public_status_not_found_contract(self):
        with patch("routes.ticket.MunicipioTicket", _DummyMunicipioTicketMissing):
            response = self.client.get("/tickets/public/status?code=M-99999&pin=1111")

        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "tickets.public_status.v1")
        self.assertEqual(body["error"]["code"], 404)
        self.assertEqual(body["error"]["message"], "Ticket no encontrado.")
        self.assertTrue(body.get("request_id"))


if __name__ == "__main__":
    unittest.main()

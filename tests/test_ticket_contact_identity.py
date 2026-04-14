import unittest

from flask import Flask, g

from routes.ticket import _request_anon_id, _request_contact_key


class TicketContactIdentityHelpersTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_request_anon_id_prefers_identity_context(self):
        with self.app.test_request_context(
            "/tickets/municipio/1",
            headers={"X-Anon-Id": "header-anon"},
        ):
            g.contact_identity = {"anon_id": "ctx-anon"}
            self.assertEqual(_request_anon_id(), "ctx-anon")

    def test_request_anon_id_fallbacks_to_headers(self):
        with self.app.test_request_context(
            "/tickets/municipio/1",
            headers={"Anon-Id": "header-anon"},
        ):
            g.contact_identity = {}
            self.assertEqual(_request_anon_id(), "header-anon")

    def test_request_contact_key_from_context(self):
        with self.app.test_request_context("/tickets/municipio/1"):
            g.contact_identity = {"contact_key": "contact:123"}
            self.assertEqual(_request_contact_key(), "contact:123")


if __name__ == "__main__":
    unittest.main()

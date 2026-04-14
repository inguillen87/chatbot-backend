import unittest

from flask import Flask, g

from routes.ticket import _request_active_session_id, _request_anon_id, _request_contact_key


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

    def test_request_active_session_id_prefers_chat_session_header(self):
        with self.app.test_request_context(
            "/tickets/municipio/1",
            headers={"X-Chat-Session-Id": "chat-session-1"},
        ):
            g.contact_identity = {"conversation_id": "conv-ctx"}
            self.assertEqual(_request_active_session_id(), "chat-session-1")

    def test_request_active_session_id_fallbacks_to_identity(self):
        with self.app.test_request_context("/tickets/municipio/1"):
            g.contact_identity = {"conversation_id": "conv-ctx"}
            self.assertEqual(_request_active_session_id(), "conv-ctx")


if __name__ == "__main__":
    unittest.main()

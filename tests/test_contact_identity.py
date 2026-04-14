import unittest

from flask import Flask, request

from utils.contact_identity import resolve_contact_identity_from_request


class ContactIdentityResolverTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_prefers_conversation_id_over_other_sources(self):
        with self.app.test_request_context(
            "/api/test",
            method="POST",
            headers={
                "X-Conversation-Id": "wa-conv-123",
                "X-Anon-Id": "anon-abc",
                "X-Contact-Phone": "+54 9 11 5555 1111",
            },
            json={"contact_key": ""},
        ):
            result = resolve_contact_identity_from_request(request)

        self.assertEqual(result["conversation_id"], "wa-conv-123")
        self.assertEqual(result["contact_key"], "wa-conv-123")
        self.assertEqual(result["source"], "conversation_id")

    def test_uses_explicit_contact_key_when_provided(self):
        with self.app.test_request_context(
            "/api/test",
            method="POST",
            headers={"X-Contact-Key": "crm:contact:999"},
            json={"conversation_id": "wa-conv-123"},
        ):
            result = resolve_contact_identity_from_request(request)

        self.assertEqual(result["contact_key"], "crm:contact:999")
        self.assertEqual(result["source"], "explicit")

    def test_falls_back_to_phone_then_anon(self):
        with self.app.test_request_context(
            "/api/test",
            method="POST",
            json={"telefono": "11 2345-6789"},
        ):
            phone_result = resolve_contact_identity_from_request(request)

        self.assertEqual(phone_result["phone_e164"], "+1123456789")
        self.assertEqual(phone_result["contact_key"], "+1123456789")
        self.assertEqual(phone_result["source"], "phone_e164")

        with self.app.test_request_context(
            "/api/test",
            method="GET",
            headers={"X-Anon-Id": "anon-only"},
        ):
            anon_result = resolve_contact_identity_from_request(request)

        self.assertEqual(anon_result["contact_key"], "anon-only")
        self.assertEqual(anon_result["source"], "anon_id")


if __name__ == "__main__":
    unittest.main()

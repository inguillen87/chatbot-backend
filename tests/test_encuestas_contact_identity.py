import unittest

from flask import Flask, g

from routes.encuestas_publicas import _metadata_with_contact_identity


class EncuestasContactIdentityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_metadata_enriched_with_identity(self):
        with self.app.test_request_context("/public/encuestas/demo/respuestas", method="POST"):
            g.contact_identity = {
                "contact_key": "ck-1",
                "conversation_id": "conv-1",
                "phone_e164": "+5491112345678",
                "source": "conversation_id",
            }
            result = _metadata_with_contact_identity(
                {"channel": "whatsapp"},
                payload={},
                anon_id="anon-1",
            )

        self.assertEqual(result["channel"], "whatsapp")
        self.assertEqual(result["contact_key"], "ck-1")
        self.assertEqual(result["conversation_id"], "conv-1")
        self.assertEqual(result["phone_e164"], "+5491112345678")
        self.assertEqual(result["anon_id"], "anon-1")
        self.assertEqual(result["identity_source"], "conversation_id")

    def test_payload_contact_overrides_when_identity_missing(self):
        with self.app.test_request_context("/public/encuestas/demo/respuestas", method="POST"):
            g.contact_identity = {}
            result = _metadata_with_contact_identity(
                {},
                payload={"contact_key": "payload-key", "conversationId": "payload-conv"},
                anon_id=None,
            )

        self.assertEqual(result["contact_key"], "payload-key")
        self.assertEqual(result["conversation_id"], "payload-conv")

    def test_null_metadata_identity_fields_are_replaced_with_resolved_identity(self):
        with self.app.test_request_context("/public/encuestas/demo/respuestas", method="POST"):
            g.contact_identity = {
                "contact_key": "resolved-ck",
                "conversation_id": "resolved-conv",
                "phone_e164": "+5491111111111",
                "source": "contact_key",
            }
            result = _metadata_with_contact_identity(
                {
                    "contact_key": None,
                    "conversation_id": None,
                    "phone_e164": None,
                    "identity_source": None,
                    "anon_id": None,
                },
                payload={},
                anon_id="anon-from-request",
            )

        self.assertEqual(result["contact_key"], "resolved-ck")
        self.assertEqual(result["conversation_id"], "resolved-conv")
        self.assertEqual(result["phone_e164"], "+5491111111111")
        self.assertEqual(result["identity_source"], "contact_key")
        self.assertEqual(result["anon_id"], "anon-from-request")

    def test_empty_string_metadata_identity_fields_are_replaced_with_resolved_identity(self):
        with self.app.test_request_context("/public/encuestas/demo/respuestas", method="POST"):
            g.contact_identity = {
                "contact_key": "resolved-empty-ck",
                "conversation_id": "resolved-empty-conv",
                "phone_e164": "+5491133333333",
                "source": "contact_key",
            }
            result = _metadata_with_contact_identity(
                {
                    "contact_key": " ",
                    "conversation_id": "",
                    "phone_e164": "   ",
                    "identity_source": "",
                },
                payload={},
                anon_id=None,
            )

        self.assertEqual(result["contact_key"], "resolved-empty-ck")
        self.assertEqual(result["conversation_id"], "resolved-empty-conv")
        self.assertEqual(result["phone_e164"], "+5491133333333")
        self.assertEqual(result["identity_source"], "contact_key")


if __name__ == "__main__":
    unittest.main()

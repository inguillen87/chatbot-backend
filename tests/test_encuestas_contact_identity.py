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


if __name__ == "__main__":
    unittest.main()

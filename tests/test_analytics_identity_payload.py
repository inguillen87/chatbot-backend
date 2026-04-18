import unittest

from flask import Flask, g

from routes.analytics import _build_event_payload_with_identity


class AnalyticsIdentityPayloadTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_enriches_payload_with_contact_identity(self):
        with self.app.test_request_context("/analytics/event", method="POST"):
            g.contact_identity = {
                "contact_key": "wa:contact:1",
                "conversation_id": "conv-1",
                "phone_e164": "+5491112345678",
                "source": "conversation_id",
            }
            result = _build_event_payload_with_identity({"payload": {"screen_name": "portal_home"}})

        self.assertEqual(result["contact_key"], "wa:contact:1")
        self.assertEqual(result["conversation_id"], "conv-1")
        self.assertEqual(result["phone_e164"], "+5491112345678")
        self.assertEqual(result["identity_source"], "conversation_id")
        self.assertEqual(result["screen_name"], "portal_home")

    def test_explicit_payload_values_override_identity_defaults(self):
        with self.app.test_request_context("/analytics/event", method="POST"):
            g.contact_identity = {
                "contact_key": "default-key",
                "conversation_id": "default-conv",
            }
            result = _build_event_payload_with_identity(
                {
                    "payload": {"screen_name": "checkout"},
                    "contact_key": "explicit-key",
                    "conversation_id": "explicit-conv",
                }
            )

        self.assertEqual(result["contact_key"], "explicit-key")
        self.assertEqual(result["conversation_id"], "explicit-conv")


if __name__ == "__main__":
    unittest.main()

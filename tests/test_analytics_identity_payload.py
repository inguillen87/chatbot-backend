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

    def test_null_payload_identity_fields_are_replaced_with_resolved_identity(self):
        with self.app.test_request_context("/analytics/event", method="POST"):
            g.contact_identity = {
                "contact_key": "resolved-key",
                "conversation_id": "resolved-conv",
                "phone_e164": "+5491110000000",
                "source": "resolver",
            }
            result = _build_event_payload_with_identity(
                {
                    "payload": {
                        "contact_key": None,
                        "conversation_id": None,
                        "phone_e164": None,
                        "identity_source": None,
                    }
                }
            )

        self.assertEqual(result["contact_key"], "resolved-key")
        self.assertEqual(result["conversation_id"], "resolved-conv")
        self.assertEqual(result["phone_e164"], "+5491110000000")
        self.assertEqual(result["identity_source"], "resolver")

    def test_empty_string_payload_identity_fields_are_replaced_with_resolved_identity(self):
        with self.app.test_request_context("/analytics/event", method="POST"):
            g.contact_identity = {
                "contact_key": "resolved-key-2",
                "conversation_id": "resolved-conv-2",
                "phone_e164": "+5491112222222",
                "source": "resolver",
            }
            result = _build_event_payload_with_identity(
                {
                    "payload": {
                        "contact_key": " ",
                        "conversation_id": "",
                        "phone_e164": "   ",
                        "identity_source": "",
                    }
                }
            )

        self.assertEqual(result["contact_key"], "resolved-key-2")
        self.assertEqual(result["conversation_id"], "resolved-conv-2")
        self.assertEqual(result["phone_e164"], "+5491112222222")
        self.assertEqual(result["identity_source"], "resolver")


if __name__ == "__main__":
    unittest.main()

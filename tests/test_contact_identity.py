import unittest
from unittest.mock import patch

from flask import Flask, request

from utils.contact_identity import (
    request_path_allows_contact_identity_body,
    resolve_contact_identity_from_request,
)


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

    def test_can_resolve_headers_without_materializing_domain_json(self):
        with self.app.test_request_context(
            "/api/v2/tenants/demo/whatsapp/workflow-studio/drafts",
            method="POST",
            headers={"X-Conversation-Id": "wa-header-only"},
            json={"conversation_id": "must-not-be-read"},
        ):
            with patch(
                "flask.wrappers.Request.get_json",
                side_effect=AssertionError("domain_json_must_remain_lazy"),
            ):
                result = resolve_contact_identity_from_request(
                    request,
                    include_body=False,
                )

        self.assertEqual(result["conversation_id"], "wa-header-only")
        self.assertEqual(result["contact_key"], "wa-header-only")

    def test_skips_oversized_identity_json(self):
        with self.app.test_request_context(
            "/ask",
            method="POST",
            json={"conversation_id": "wa-body"},
        ):
            result = resolve_contact_identity_from_request(
                request,
                max_body_bytes=1,
            )

        self.assertIsNone(result["conversation_id"])
        self.assertIsNone(result["contact_key"])

    def test_body_identity_path_allowlist_is_fail_closed(self):
        self.assertTrue(request_path_allows_contact_identity_body("/api/ask/municipio"))
        self.assertTrue(
            request_path_allows_contact_identity_body(
                "/public/encuestas/participacion/respuestas"
            )
        )
        self.assertFalse(
            request_path_allows_contact_identity_body(
                "/api/v2/tenants/demo/whatsapp/workflow-studio/drafts"
            )
        )
        self.assertFalse(
            request_path_allows_contact_identity_body("/api/admin/market/catalog")
        )
        self.assertFalse(
            request_path_allows_contact_identity_body("/api/market/demo/cart/add")
        )
        self.assertFalse(
            request_path_allows_contact_identity_body(
                "/api/tickets/municipio/1/asignar"
            )
        )
        self.assertFalse(
            request_path_allows_contact_identity_body("/api/pwa/app/tickets")
        )

    def test_invalid_or_unreasonable_body_limits_fail_closed(self):
        invalid_limits = (0, -1, 1024 * 1024 + 1, "invalid")
        for invalid_limit in invalid_limits:
            with self.subTest(max_body_bytes=invalid_limit):
                with self.app.test_request_context(
                    "/ask",
                    method="POST",
                    json={"conversation_id": "must-not-be-read"},
                ):
                    with patch(
                        "flask.wrappers.Request.get_json",
                        side_effect=AssertionError("invalid_limit_must_fail_closed"),
                    ):
                        result = resolve_contact_identity_from_request(
                            request,
                            max_body_bytes=invalid_limit,
                        )

                self.assertIsNone(result["contact_key"])


if __name__ == "__main__":
    unittest.main()

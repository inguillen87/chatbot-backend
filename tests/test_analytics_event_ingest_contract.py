import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g
from werkzeug.exceptions import Forbidden

from routes.analytics import ANALYTICS_EVENT_INGEST_CONTRACT_VERSION, analytics_bp


class AnalyticsEventIngestContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(analytics_bp)
        self.client = self.app.test_client()
        self.tenant_resolution = {
            "tenant_profile_id": 77,
            "owner_tenant_id": 7,
            "tenant_slug": "contract-tenant",
            "tenant_type": "municipio",
            "scope": "municipio",
            "resolution_sources": ["tenant_id_owner"],
        }
        self.tenant_resolver = patch(
            "routes.analytics._resolve_tenant_id_from_event_payload",
            return_value=(77, self.tenant_resolution),
        )
        self.tenant_resolver.start()
        self.addCleanup(self.tenant_resolver.stop)

    def test_event_ingest_response_contract(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), \
             patch("routes.analytics.require_access", return_value=None), \
             patch("routes.analytics.analytics_ingestor.track", return_value=None):
            response = self.client.post(
                "/analytics/event",
                json={
                    "tenant_id": 7,
                    "event_name": "portal_opened",
                    "payload": {"screen_name": "portal_home"},
                    "contact_key": "ck-123",
                    "conversation_id": "conv-123",
                },
            )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["success"], True)
        self.assertEqual(body["accepted"], True)
        self.assertEqual(body["ignored"], False)
        self.assertEqual(body["contract_version"], ANALYTICS_EVENT_INGEST_CONTRACT_VERSION)
        self.assertEqual(body["tenant_id"], 7)
        self.assertEqual(body["tenant_profile_id"], 77)
        self.assertEqual(body["event_name"], "portal_opened")
        self.assertEqual(body["contact_key"], "ck-123")
        self.assertEqual(body["conversation_id"], "conv-123")
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))

    def test_event_ingest_uses_identity_source_when_present(self):
        @self.app.before_request
        def _attach_identity_for_test():
            g.contact_identity = {
                "contact_key": "id-1",
                "conversation_id": "conv-id-1",
                "source": "conversation_id",
            }

        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), \
             patch("routes.analytics.require_access", return_value=None), \
             patch("routes.analytics.analytics_ingestor.track", return_value=None):
            response = self.client.post(
                "/analytics/event",
                json={
                    "tenant_id": 9,
                    "event_name": "widget_opened",
                    "payload": {},
                },
            )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["contract_version"], ANALYTICS_EVENT_INGEST_CONTRACT_VERSION)
        self.assertEqual(body["identity_source"], "conversation_id")

    def test_event_ingest_preserves_request_id_header(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), \
             patch("routes.analytics.require_access", return_value=None), \
             patch("routes.analytics.analytics_ingestor.track", return_value=None):
            response = self.client.post(
                "/analytics/event",
                headers={"X-Request-Id": "req-analytics-ingest-1"},
                json={"tenant_id": 10, "event_name": "portal_opened", "payload": {}},
            )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["request_id"], "req-analytics-ingest-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-analytics-ingest-1")

    def test_event_ingest_ignored_without_tenant_preserves_contract(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics._resolve_tenant_id_from_event_payload",
            return_value=(None, {"code": "tenant_unresolved", "status": 400}),
        ):
            response = self.client.post(
                "/analytics/event",
                headers={"X-Request-Id": "req-ignored-tenant"},
                json={"event_name": "survey_vote_attempt", "payload": {}},
            )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["success"], True)
        self.assertEqual(body["accepted"], False)
        self.assertEqual(body["ignored"], True)
        self.assertEqual(body["reason"], "tenant_unresolved")
        self.assertEqual(body["contract_version"], ANALYTICS_EVENT_INGEST_CONTRACT_VERSION)
        self.assertEqual(body["event_name"], "survey_vote_attempt")
        self.assertEqual(body["request_id"], "req-ignored-tenant")

    def test_event_ingest_ignored_access_denied_preserves_contract(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), \
             patch("routes.analytics.require_access", side_effect=Forbidden()):
            response = self.client.post(
                "/analytics/event",
                json={"tenant_id": 7, "event_name": "survey_vote_attempt", "payload": {}},
            )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["success"], True)
        self.assertEqual(body["accepted"], False)
        self.assertEqual(body["ignored"], True)
        self.assertEqual(body["reason"], "access_denied")
        self.assertEqual(body["contract_version"], ANALYTICS_EVENT_INGEST_CONTRACT_VERSION)
        self.assertEqual(body["tenant_id"], 7)


if __name__ == "__main__":
    unittest.main()

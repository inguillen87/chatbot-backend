import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g

from routes.analytics import ANALYTICS_EVENT_INGEST_CONTRACT_VERSION, analytics_bp


class AnalyticsEventIngestContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(analytics_bp)
        self.client = self.app.test_client()

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
        self.assertEqual(body["contract_version"], ANALYTICS_EVENT_INGEST_CONTRACT_VERSION)
        self.assertEqual(body["tenant_id"], 7)
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


if __name__ == "__main__":
    unittest.main()

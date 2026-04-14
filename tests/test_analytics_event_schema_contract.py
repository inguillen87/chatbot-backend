import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.analytics import (
    ANALYTICS_EVENT_SCHEMA_CONTRACT_VERSION,
    ANALYTICS_CANONICAL_EVENT_NAMES,
    analytics_bp,
)


class AnalyticsEventSchemaContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(analytics_bp)
        self.client = self.app.test_client()

    def test_schema_endpoint_returns_contract(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), \
             patch("routes.analytics.require_access", return_value=None) as mock_require_access:
            response = self.client.get("/analytics/event/schema?tenant_id=12")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], ANALYTICS_EVENT_SCHEMA_CONTRACT_VERSION)
        self.assertEqual(body["tenant_id"], 12)
        self.assertEqual(body["canonical_events"], ANALYTICS_CANONICAL_EVENT_NAMES)
        self.assertIn("required_dimensions", body)
        mock_require_access.assert_called_once_with(
            "12",
            "visor",
            required_capability="analytics.read",
        )

    def test_schema_endpoint_requires_numeric_tenant_id(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)):
            response = self.client.get("/analytics/event/schema?tenant_id=abc")

        self.assertEqual(response.status_code, 400)
        self.assertIn("tenant_id", response.get_json().get("error", ""))


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.admin_analytics import admin_analytics_bp


class AdminAnalyticsCapabilityContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(admin_analytics_bp)
        self.client = self.app.test_client()
        self.filters = SimpleNamespace(tenant_id="10", scope="municipio")

    def test_overview_requires_analytics_admin_capability(self):
        with patch("routes.admin_analytics.parse_filters", return_value=self.filters), patch(
            "routes.admin_analytics.require_access", return_value=None
        ) as mock_require_access, patch(
            "routes.admin_analytics.get_summary", return_value={"totals": {"tickets": 0}}
        ):
            response = self.client.get("/admin/analytics/overview")

        self.assertEqual(response.status_code, 200)
        mock_require_access.assert_called_once_with(
            "10", "operador", required_capability="analytics.admin"
        )

    def test_dashboard_requires_analytics_admin_capability(self):
        with patch("routes.admin_analytics.parse_filters", return_value=self.filters), patch(
            "routes.admin_analytics.require_access", return_value=None
        ) as mock_require_access, patch(
            "routes.admin_analytics._dashboard_response",
            return_value=self.app.response_class("{}", status=200, mimetype="application/json"),
        ):
            response = self.client.get("/admin/analytics/dashboard")

        self.assertEqual(response.status_code, 200)
        mock_require_access.assert_called_once_with(
            "10", "operador", required_capability="analytics.admin"
        )

    def test_whatsapp_funnel_requires_analytics_admin_capability(self):
        with patch("routes.admin_analytics.parse_filters", return_value=self.filters), patch(
            "routes.admin_analytics.require_access", return_value=None
        ) as mock_require_access, patch(
            "routes.admin_analytics._build_whatsapp_funnel_payload",
            return_value={"contract_version": "admin.analytics.whatsapp_funnel.v1", "stages": []},
        ):
            response = self.client.get("/admin/analytics/whatsapp-funnel")

        self.assertEqual(response.status_code, 200)
        mock_require_access.assert_called_once_with(
            "10", "operador", required_capability="analytics.admin"
        )


if __name__ == "__main__":
    unittest.main()

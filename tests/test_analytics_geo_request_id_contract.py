import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.analytics import analytics_bp


class AnalyticsGeoRequestIdContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(analytics_bp)
        self.client = self.app.test_client()
        self.filters = SimpleNamespace(tenant_id="10", scope="municipio")

    def test_geo_heatmap_preserves_forwarded_request_id(self):
        with patch("routes.analytics.parse_filters", return_value=self.filters), patch(
            "routes.analytics.require_access", return_value=None
        ), patch(
            "routes.analytics.get_geo_heatmap",
            return_value={"cells": [], "render_contract": {"state": "ok"}},
        ):
            response = self.client.get(
                "/analytics/geo/heatmap?tenant_id=10",
                headers={"X-Request-Id": "req-geo-heatmap-1"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["request_id"], "req-geo-heatmap-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-geo-heatmap-1")

    def test_geo_points_replaces_blank_request_id_header(self):
        with patch("routes.analytics.parse_filters", return_value=self.filters), patch(
            "routes.analytics.require_access", return_value=None
        ), patch(
            "routes.analytics.get_geo_points",
            return_value={"points": [], "render_contract": {"state": "ok"}},
        ):
            response = self.client.get(
                "/analytics/geo/points?tenant_id=10&limit=5",
                headers={"X-Request-Id": "   "},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["request_id"])
        self.assertNotEqual(body["request_id"], "   ")
        self.assertEqual(body["request_id"], response.headers.get("X-Request-Id"))


if __name__ == "__main__":
    unittest.main()

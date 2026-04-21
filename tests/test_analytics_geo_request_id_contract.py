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

    def test_geo_heatmap_includes_openstreet_category_layer_contract(self):
        with patch("routes.analytics.parse_filters", return_value=self.filters), patch(
            "routes.analytics.require_access", return_value=None
        ), patch(
            "routes.analytics.get_geo_heatmap",
            return_value={
                "cells": [{"categories": {"bache": 2}}],
                "meta": {"map": {"bounds": {"south": -34.0, "west": -58.0, "north": -33.0, "east": -57.0}}},
                "render_contract": {"state": "ok"},
            },
        ):
            response = self.client.get("/analytics/geo/heatmap?tenant_id=10")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["map_layers"]["contract_version"], "analytics.geo_layers.v1")
        self.assertEqual(body["map_layers"]["provider"]["name"], "openstreetmap")
        self.assertEqual(body["map_layers"]["category_heatmap"]["top_categories"][0]["category"], "bache")

    def test_geo_heatmap_filters_by_category_query(self):
        with patch("routes.analytics.parse_filters", return_value=self.filters), patch(
            "routes.analytics.require_access", return_value=None
        ), patch(
            "routes.analytics.get_geo_heatmap",
            return_value={
                "cells": [
                    {"count": 5, "categories": {"bache": 3, "luz": 2}},
                    {"count": 2, "categories": {"luz": 2}},
                ],
                "render_contract": {"state": "ok"},
            },
        ):
            response = self.client.get("/analytics/geo/heatmap?tenant_id=10&category=luz")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["map_layers"]["category_heatmap"]["applied_categories"], ["luz"])
        self.assertEqual(len(body["cells"]), 2)
        self.assertTrue(all("luz" in (cell.get("categories") or {}) for cell in body["cells"]))

    def test_geo_heatmap_reports_missing_requested_categories(self):
        with patch("routes.analytics.parse_filters", return_value=self.filters), patch(
            "routes.analytics.require_access", return_value=None
        ), patch(
            "routes.analytics.get_geo_heatmap",
            return_value={
                "cells": [{"count": 3, "categories": {"bache": 3}}],
                "render_contract": {"state": "ok"},
            },
        ):
            response = self.client.get("/analytics/geo/heatmap?tenant_id=10&category=alumbrado")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        category_layer = body["map_layers"]["category_heatmap"]
        self.assertEqual(category_layer["applied_categories"], ["alumbrado"])
        self.assertEqual(category_layer["missing_categories"], ["alumbrado"])
        self.assertEqual(category_layer["warning"], "requested_categories_without_data")


if __name__ == "__main__":
    unittest.main()

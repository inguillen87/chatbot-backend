import unittest

from flask import Flask

from routes.geo_routes import geo_bp


class GeoPolygonTruthBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["TESTING"] = True
        self.app.register_blueprint(geo_bp)
        self.client = self.app.test_client()

    def test_polygon_endpoint_is_retired_without_synthetic_features(self):
        first = self.client.get("/api/geo/polygons?tenant_id=1")
        second = self.client.get("/api/geo/polygons?tenant_id=999")

        self.assertEqual(first.status_code, 410)
        self.assertEqual(second.status_code, 410)
        payload = first.get_json()
        self.assertEqual(payload, second.get_json())
        self.assertEqual(payload.get("contract_version"), "geo.polygons.truth_boundary.v1")
        self.assertEqual(payload.get("reason_code"), "official_geo_boundaries_unavailable")
        self.assertEqual(payload.get("source_status"), "not_configured")
        self.assertEqual(payload.get("truth_boundary"), "no_synthetic_boundaries")
        self.assertFalse(payload.get("retryable"))
        self.assertNotIn("type", payload)
        self.assertNotIn("features", payload)

        serialized = first.get_data(as_text=True)
        for forbidden in ("Polygon", "coordinates", "density", "value", "fill", "Zona"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()

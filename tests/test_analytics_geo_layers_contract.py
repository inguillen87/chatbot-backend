import unittest

from routes.analytics import (
    ANALYTICS_GEO_LAYERS_CONTRACT_VERSION,
    _apply_geo_category_filter,
    _augment_geo_payload_for_frontend,
)


class AnalyticsGeoLayersContractTestCase(unittest.TestCase):
    def test_heatmap_augments_category_layers_and_osm_provider(self):
        payload = {
            "cells": [
                {"categories": {"bache": 3, "luz": 1}},
                {"categories": {"bache": 2}},
            ],
            "meta": {"map": {"bounds": {"south": -34.9, "west": -58.5, "north": -34.5, "east": -58.2}}},
        }

        enriched = _augment_geo_payload_for_frontend(payload, module="heatmap")

        layers = enriched["map_layers"]
        self.assertEqual(layers["contract_version"], ANALYTICS_GEO_LAYERS_CONTRACT_VERSION)
        self.assertEqual(layers["provider"]["name"], "openstreetmap")
        self.assertEqual(layers["category_heatmap"]["source_module"], "heatmap")
        self.assertEqual(layers["category_heatmap"]["top_categories"][0]["category"], "bache")
        self.assertEqual(layers["category_heatmap"]["top_categories"][0]["count"], 5)
        self.assertIn("bache", layers["category_heatmap"]["available_categories"])
        self.assertEqual(layers["visual_system"]["style"], "premium_operational_map")
        self.assertTrue(layers["visual_system"]["animations"]["radar_sweep"])
        self.assertEqual(layers["intensity"]["total_cases"], 6)
        self.assertEqual(layers["operator_metrics"]["top_category"], "bache")
        self.assertEqual(layers["hotspots"]["focus"]["category"], "bache")
        self.assertEqual(layers["hotspots"]["focus"]["risk"]["level"], "critical")
        self.assertEqual(enriched["cells"][0]["dominant_category"], "bache")
        self.assertEqual(enriched["cells"][0]["visual"]["label_mode"], "always")
        self.assertEqual(enriched["render_contract"]["recommended_component"], "PremiumTerritoryMap")

    def test_points_uses_categoria_field_for_top_categories(self):
        payload = {"points": [{"categoria": "recoleccion", "estado": "nuevo"}, {"categoria": "recoleccion"}, {"categoria": "alumbrado"}]}

        enriched = _augment_geo_payload_for_frontend(payload, module="points")

        top = enriched["map_layers"]["category_heatmap"]["top_categories"]
        self.assertEqual(top[0]["category"], "recoleccion")
        self.assertEqual(top[0]["count"], 2)
        self.assertEqual(enriched["points"][0]["risk"]["level"], "high")
        self.assertEqual(enriched["points"][0]["visual"]["marker"], "pulse")
        self.assertTrue(enriched["map_layers"]["visual_system"]["animations"]["live_beacon"])

    def test_heatmap_category_filter_recomputes_cell_count_and_intensity(self):
        payload = {
            "cells": [
                {"count": 6, "intensity": 1.0, "categories": {"bache": 4, "luz": 2}},
                {"count": 2, "intensity": 0.4, "categories": {"luz": 2}},
            ]
        }

        filtered = _apply_geo_category_filter(payload, module="heatmap", categories=["luz"])

        self.assertEqual(len(filtered["cells"]), 2)
        self.assertEqual(filtered["cells"][0]["count"], 2)
        self.assertEqual(filtered["cells"][0]["categories"], {"luz": 2})
        self.assertEqual(filtered["cells"][0]["intensity"], 1.0)

    def test_points_category_filter_keeps_only_selected_categoria(self):
        payload = {"points": [{"categoria": "recoleccion"}, {"categoria": "alumbrado"}, {"categoria": "recoleccion"}]}

        filtered = _apply_geo_category_filter(payload, module="points", categories=["recoleccion"])

        self.assertEqual(len(filtered["points"]), 2)
        self.assertTrue(all(point.get("categoria") == "recoleccion" for point in filtered["points"]))


if __name__ == "__main__":
    unittest.main()

"""Offline contract tests. Temporary polygons are synthetic test fixtures only."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import operational_jurisdiction as territory


class OperationalJurisdictionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"DATA_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tenant = SimpleNamespace(slug="junin", municipio_id=987654, tipo="municipio")
        self.bundled = Path(territory._REPOSITORY_ROOT) / "data/municipios/junin"

    def config(self):
        return json.loads((self.bundled / "geo.json").read_text(encoding="utf-8"))

    def write_config(self, config, *, path="municipios/junin/geo.json", boundary=None):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if boundary is not None:
            raw = json.dumps(boundary).encode("utf-8")
            (target.parent / "custom.geojson").write_bytes(raw)
            config["boundary"]["file"] = "custom.geojson"
            config["boundary"]["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
        target.write_text(json.dumps(config), encoding="utf-8")
        return target

    def square(self, *, geometry=None):
        return {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"departamen": "JUNIN", "codigo_dep": "09",
                "globalid": "{FEA13AA1-46F3-4570-BAEE-188FF11AFF94}"},
            "geometry": geometry or {"type": "Polygon", "coordinates": [
                [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]},
        }]}

    def test_official_snapshot_hash_identity_and_public_provenance(self):
        raw = (self.bundled / "official_department_boundary.geojson").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
            "3dbfc3bb3c98601d6bf1897d737c1e739f39173f1c10c440aef35e516661a731")
        self.assertEqual(self.config()["boundary"]["snapshot_sha256"], hashlib.sha256(raw).hexdigest())
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        self.assertTrue(contract["containment_verified"])
        self.assertEqual(contract["containment_method"], "point_in_polygon")
        authority = contract["boundary_authority"]
        self.assertEqual(authority["department_code"], "09")
        self.assertEqual(authority["publisher"], "Infraestructura de Datos Espaciales de Mendoza")
        self.assertTrue(authority["source_ref"].startswith("https://ide.mendoza.gov.ar/server/rest/"))
        layer = contract["boundary_feature_collection"]
        self.assertEqual(authority, layer["metadata"]["provenance"])
        self.assertEqual(layer["features"][0]["properties"]["departamen"], "JUNIN")
        self.assertFalse(layer["metadata"]["synthetic"])

    def test_official_containment_does_not_accept_operational_envelope(self):
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        for lat, lng, expected in [(-33.136, -68.49, "within"),
                (-33.0808, -68.4895, "outside"), (-33.0567, -68.4954, "outside"),
                (-34.6037, -58.3816, "outside")]:
            with self.subTest(lat=lat, lng=lng):
                self.assertEqual(territory.coordinate_jurisdiction_status(lat, lng, contract), expected)

    def test_unrelated_tenant_never_inherits_junin(self):
        self.tenant.slug = "another-municipality"
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        self.assertEqual(contract["state"], "unconfigured")
        points, review = territory.scope_heatmap_points([{"lat": -33.136, "lng": -68.49}], self.tenant, contract)
        self.assertEqual(points, [])
        self.assertEqual(review["unverified_jurisdiction_count"], 1)
        self.assertEqual(review["outside_jurisdiction_count"], 0)

    def test_non_government_without_boundary_keeps_valid_points_unverified(self):
        self.tenant.slug, self.tenant.tipo = "shop", "pyme"
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        points, _ = territory.scope_heatmap_points([{"lat": -33.136, "lng": -68.49}], self.tenant, contract)
        self.assertEqual(len(points), 1)
        self.assertFalse(points[0]["containment_verified"])

    def test_safe_segments_do_not_read_another_tenant(self):
        self.tenant.slug, self.tenant.municipio_id = "../junin", "../junin"
        self.assertEqual(territory.resolve_tenant_jurisdiction(self.tenant)["state"], "unconfigured")

    def test_official_custom_has_priority_and_does_not_require_envelope(self):
        config = self.config()
        config.pop("bounds")
        self.write_config(config, boundary=self.square())
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        self.assertTrue(contract["containment_verified"])
        self.assertEqual(contract["boundary_source"]["storage"], "persistent_data")
        self.assertEqual(territory.coordinate_jurisdiction_status(2, 2, contract), "within")
        self.assertEqual(territory.coordinate_jurisdiction_status(-33.136, -68.49, contract), "outside")

    def test_legacy_matching_place_can_borrow_bundled_boundary(self):
        config = self.config()
        config.pop("boundary")
        self.write_config(config)
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        self.assertTrue(contract["containment_verified"])
        self.assertEqual(contract["source"]["storage"], "persistent_data")
        self.assertEqual(contract["boundary_source"]["storage"], "bundled_data")

    def test_custom_incidental_metadata_is_not_published(self):
        config = self.config()
        config["boundary"]["authority"]["private_note"] = "private fixture metadata"
        boundary = self.square()
        boundary["features"][0]["properties"]["private_note"] = "private fixture property"
        self.write_config(config, boundary=boundary)
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        self.assertTrue(contract["containment_verified"])
        self.assertNotIn("private_note", json.dumps(contract["boundary_feature_collection"]))
        contract["private_review"] = "private fixture review"
        self.assertNotIn("private_review", territory.public_jurisdiction(contract))

    def test_legacy_other_place_or_missing_identity_does_not_borrow_boundary(self):
        for field, value in [("city", "San Martin"), ("state", "Buenos Aires"),
                             ("country", "UY"), ("state", None)]:
            with self.subTest(field=field, value=value):
                config = self.config()
                config.pop("boundary")
                config[field] = value
                self.write_config(config)
                self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])

    def test_missing_declared_boundary_never_falls_back(self):
        self.write_config(self.config())
        self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])

    def test_corrupt_hash_never_falls_back(self):
        config = self.config()
        path = self.write_config(config, boundary=self.square())
        config["boundary"]["snapshot_sha256"] = "0" * 64
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])

    def test_malformed_override_never_falls_back(self):
        path = self.write_config(self.config())
        path.write_text("{ invalid json", encoding="utf-8")
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        self.assertEqual(contract["state"], "invalid_config")
        self.assertFalse(contract["containment_verified"])

    def test_null_boundary_is_explicit_blocker(self):
        config = self.config()
        config["boundary"] = None
        self.write_config(config)
        self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])

    def test_wrong_identity_or_coordinate_system_is_rejected_even_with_matching_hash(self):
        for field, value in [("departamen", "OTHER"), ("codigo_dep", "99"), ("globalid", "foreign")]:
            with self.subTest(field=field):
                collection = self.square()
                collection["features"][0]["properties"][field] = value
                self.write_config(self.config(), boundary=collection)
                self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])
        config = self.config()
        config["boundary"]["out_sr"] = 3857
        self.write_config(config, boundary=self.square())
        self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])

    def test_polygon_holes_disjoint_parts_and_edges(self):
        geometry = {"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
             [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]],
            [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
        ]}
        self.write_config(self.config(), boundary=self.square(geometry=geometry))
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        for lat, lng, status in [(0.5, 0.5, "within"), (2, 2, "outside"),
                (0, 2, "within"), (2, 1, "outside"), (10.5, 10.5, "within"), (5, 5, "outside")]:
            with self.subTest(lat=lat, lng=lng):
                self.assertEqual(territory.coordinate_jurisdiction_status(lat, lng, contract), status)

    def test_malformed_geometry_is_not_authoritative(self):
        for coordinates in [[], [[[0, 0], [1, 1], [2, 2], [0, 0]]],
                            [[[0, 0], [1, 0], [1, 1], [0, 1]]],
                            [[[0, 0], [1, 0], [1, 91], [0, 0]]]]:
            with self.subTest(coordinates=coordinates):
                self.write_config(self.config(), boundary=self.square(
                    geometry={"type": "Polygon", "coordinates": coordinates}))
                self.assertFalse(territory.resolve_tenant_jurisdiction(self.tenant)["containment_verified"])

    def test_evidence_is_bound_to_snapshot_and_does_not_mutate_locations(self):
        contract = territory.resolve_tenant_jurisdiction(self.tenant)
        source = [{"id": "inside", "lat": -33.136, "lng": -68.49},
                  {"id": "outside", "lat": -34.6037, "lng": -58.3816},
                  {"id": "invalid", "lat": float("nan"), "lng": -68.49}]
        original = copy.deepcopy(source)
        points, review = territory.scope_heatmap_points(source, self.tenant, contract)
        self.assertEqual(source, original)
        self.assertEqual([point["id"] for point in points], ["inside"])
        evidence = points[0]["jurisdiction_evidence"]
        self.assertTrue(evidence["containment_verified"])
        self.assertEqual(evidence["snapshot_sha256"], contract["boundary_authority"]["snapshot_sha256"])
        self.assertEqual(evidence["source_ref"], contract["boundary_authority"]["source_ref"])
        self.assertEqual(review["outside_jurisdiction_count"], 1)
        self.assertEqual(review["invalid_coordinate_count"], 1)
        self.assertNotIn("candidates", review)
        self.assertNotIn("lat", review)
        self.assertFalse(review["writes_performed"])


if __name__ == "__main__":
    unittest.main()

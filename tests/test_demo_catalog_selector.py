import os
import unittest
from unittest.mock import patch


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config


class DemoCatalogSelectorTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


def _field_names(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _field_names(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _field_names(nested)


class DemoCatalogSelectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(DemoCatalogSelectorTestConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_default_profile_preserves_full_catalog_contract(self):
        response = self.client.get(
            "/api/v2/demo/catalog",
            headers={"X-Request-Id": "catalog-full-contract"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.catalog.v2")
        self.assertNotIn("response_profile", payload)
        self.assertEqual(payload.get("request_id"), "catalog-full-contract")
        self.assertEqual(len(payload.get("rubros") or []), 18)

        first_rubro = (payload.get("rubros") or [])[0]
        for field in (
            "sales_story",
            "consulting_playbook",
            "wow_flows",
            "live_modules",
            "openai_runtime",
            "survey_voting",
        ):
            self.assertIn(field, first_rubro)

        for group in payload.get("sector_groups") or []:
            self.assertIn("rubros", group)
            self.assertNotIn("rubro_slugs", group)

    def test_selector_contract_is_complete_small_and_excludes_heavy_fields(self):
        response = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={"X-Request-Id": "catalog-selector-contract"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Request-Id"), "catalog-selector-contract")
        self.assertEqual(response.headers.get("Cache-Control"), "public, max-age=300")
        self.assertIn("Origin", response.headers.get("Vary") or "")
        self.assertLess(len(response.data), 50_000)

        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.catalog.v2")
        self.assertEqual(payload.get("response_profile"), "selector")
        self.assertEqual(payload.get("sectors"), ["gobierno", "empresas", "educacion"])
        self.assertTrue(payload.get("pillars"))
        self.assertTrue(payload.get("resources"))

        rubros = payload.get("rubros") or []
        self.assertEqual(len(rubros), 18)
        required_rubro_fields = {
            "slug",
            "key",
            "label",
            "tipo_chat",
            "tenant_slug",
            "sector",
            "pillar",
            "resources",
            "sample_prompts",
        }
        self.assertTrue(all(required_rubro_fields.issubset(rubro) for rubro in rubros))

        municipio = next(rubro for rubro in rubros if rubro.get("slug") == "municipio")
        self.assertEqual(municipio.get("tenant_slug"), "municipio")
        self.assertEqual(municipio.get("sector"), "gobierno")
        self.assertTrue(municipio.get("resources"))
        self.assertTrue(municipio.get("sample_prompts"))

        expected_tenants = {
            "gobierno": "municipio",
            "empresas": "bodega",
            "educacion": "colegio-demo",
        }
        grouped_slugs = set()
        for group in payload.get("sector_groups") or []:
            self.assertEqual(group.get("tenant_slug"), expected_tenants[group.get("key")])
            self.assertNotIn("rubros", group)
            self.assertTrue(group.get("rubro_slugs"))
            grouped_slugs.update(group.get("rubro_slugs") or [])
        self.assertEqual(grouped_slugs, {rubro.get("slug") for rubro in rubros})

        forbidden_fields = {
            "sales_story",
            "consulting_playbook",
            "wow_flows",
            "live_modules",
            "openai_runtime",
            "survey_voting",
            "request_id",
            "server_time",
        }
        self.assertTrue(forbidden_fields.isdisjoint(set(_field_names(payload))))

    def test_selector_does_not_build_commercial_bundle(self):
        with patch("routes.v2.demo._commercial_demo_bundle") as commercial_bundle:
            response = self.client.get("/api/v2/demo/catalog?response_profile=selector")

        self.assertEqual(response.status_code, 200)
        commercial_bundle.assert_not_called()

    def test_selector_etag_is_stable_and_supports_conditional_get(self):
        first = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={"X-Request-Id": "selector-etag-one"},
        )
        second = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={"X-Request-Id": "selector-etag-two"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.headers.get("ETag"))
        self.assertEqual(first.headers.get("ETag"), second.headers.get("ETag"))
        self.assertEqual(first.data, second.data)
        self.assertNotEqual(first.headers.get("X-Request-Id"), second.headers.get("X-Request-Id"))

        conditional = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector",
            headers={
                "If-None-Match": first.headers["ETag"],
                "X-Request-Id": "selector-etag-conditional",
            },
        )

        self.assertEqual(conditional.status_code, 304)
        self.assertEqual(conditional.data, b"")
        self.assertEqual(conditional.headers.get("ETag"), first.headers.get("ETag"))
        self.assertEqual(conditional.headers.get("Cache-Control"), "public, max-age=300")
        self.assertEqual(conditional.headers.get("X-Request-Id"), "selector-etag-conditional")


if __name__ == "__main__":
    unittest.main()

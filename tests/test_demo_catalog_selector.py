import os
import time
import unittest
from unittest.mock import patch


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from extensions import limiter
from routes.v2 import demo as demo_routes


class DemoCatalogSelectorTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    DEMO_CATALOG_FULL_RATE_LIMIT = "2 per second"


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

    def setUp(self):
        limiter.reset()

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

    def test_selector_and_options_do_not_consume_full_catalog_budget(self):
        for _ in range(4):
            selector = self.client.get(
                "/api/v2/demo/catalog?response_profile=selector"
            )
            self.assertEqual(selector.status_code, 200)
        for _ in range(4):
            preflight = self.client.options(
                "/api/v2/demo/catalog",
                headers={"Origin": "https://chatboc.ar"},
            )
            self.assertEqual(preflight.status_code, 200)

        with (
            patch.object(
                demo_routes,
                "legacy_demo_catalog",
                wraps=demo_routes.legacy_demo_catalog,
            ) as legacy_catalog,
            patch.object(
                demo_routes,
                "_commercial_demo_bundle",
                wraps=demo_routes._commercial_demo_bundle,
            ) as commercial_bundle,
        ):
            first_full = self.client.get("/api/v2/demo/catalog")
            second_full = self.client.head(
                "/api/v2/demo/catalog?response_profile=full"
            )
            legacy_calls_before_limit = legacy_catalog.call_count
            bundle_calls_before_limit = commercial_bundle.call_count
            limited_full = self.client.get("/api/v2/demo/catalog")

            self.assertGreater(legacy_calls_before_limit, 0)
            self.assertGreater(bundle_calls_before_limit, 0)
            self.assertEqual(legacy_catalog.call_count, legacy_calls_before_limit)
            self.assertEqual(commercial_bundle.call_count, bundle_calls_before_limit)

        selector_after_limit = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector"
        )
        options_after_limit = self.client.options(
            "/api/v2/demo/catalog",
            headers={"Origin": "https://chatboc.ar"},
        )

        self.assertEqual(first_full.status_code, 200)
        self.assertEqual(second_full.status_code, 200)
        self.assertEqual(second_full.data, b"")
        self.assertEqual(limited_full.status_code, 429)
        self.assertEqual(selector_after_limit.status_code, 200)
        self.assertEqual(
            selector_after_limit.headers.get("Cache-Control"),
            "public, max-age=300",
        )
        self.assertTrue(selector_after_limit.headers.get("ETag"))
        self.assertEqual(options_after_limit.status_code, 200)
        self.assertEqual(
            options_after_limit.headers.get("Access-Control-Allow-Origin"),
            "https://chatboc.ar",
        )

    def test_full_catalog_limit_is_proxy_safe_json_cors_and_recovers(self):
        request_headers = {
            "Origin": "https://chatboc.ar",
            "X-Forwarded-For": "198.51.100.10",
        }
        first = self.client.get(
            "/api/v2/demo/catalog",
            headers={**request_headers, "X-Request-Id": "full-limit-one"},
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        second = self.client.get(
            "/api/v2/demo/catalog?response_profile=full",
            headers={
                **request_headers,
                "X-Forwarded-For": "198.51.100.11",
                "X-Request-Id": "full-limit-two",
            },
            environ_base={"REMOTE_ADDR": "203.0.113.11"},
        )
        limited = self.client.get(
            "/api/v2/demo/catalog?response_profile=selector%00",
            headers={
                **request_headers,
                "X-Forwarded-For": "198.51.100.12",
                "X-Request-Id": "full-limit-blocked",
            },
            environ_base={"REMOTE_ADDR": "203.0.113.12"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json().get("request_id"), "full-limit-one")
        self.assertEqual(second.get_json().get("request_id"), "full-limit-two")
        self.assertEqual(limited.status_code, 429)
        self.assertLess(len(limited.data), 2_000)
        limited_payload = limited.get_json()
        self.assertEqual(
            limited_payload.get("reason_code"),
            "demo_catalog_full_rate_limited",
        )
        self.assertEqual(limited_payload.get("request_id"), "full-limit-blocked")
        self.assertNotIn("server_time", limited_payload)
        self.assertEqual(limited.headers.get("X-Request-Id"), "full-limit-blocked")
        self.assertEqual(limited.headers.get("Cache-Control"), "no-store")
        self.assertGreaterEqual(int(limited.headers.get("Retry-After") or 0), 1)
        self.assertEqual(
            limited.headers.get("Access-Control-Allow-Origin"),
            "https://chatboc.ar",
        )
        self.assertIn(
            "retry-after",
            (limited.headers.get("Access-Control-Expose-Headers") or "").lower(),
        )
        self.assertIn("Origin", limited.headers.get("Vary") or "")

        time.sleep(1.1)
        recovered = self.client.get(
            "/api/v2/demo/catalog",
            headers={"X-Request-Id": "full-limit-recovered"},
        )
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(
            recovered.get_json().get("request_id"),
            "full-limit-recovered",
        )


if __name__ == "__main__":
    unittest.main()
